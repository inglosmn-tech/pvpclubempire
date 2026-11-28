"""Persistent bot approval, raid verification and temporary-role management."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("pvpclub-empire.security")

LOCKED_PERMISSIONS = (
    "view_channel", "send_messages", "send_messages_in_threads", "add_reactions",
    "connect", "speak", "stream", "use_voice_activation", "attach_files",
    "embed_links", "create_public_threads", "create_private_threads",
    "use_application_commands", "use_external_apps", "send_voice_messages",
    "send_polls", "create_instant_invite", "mention_everyone", "use_soundboard",
    "use_embedded_activities", "send_tts_messages",
)
MAX_TEMP_SECONDS = 30 * 86400
VERIFY_ATTEMPTS = 5
VERIFY_MINUTES = 15


def _is_owner(guild, user) -> bool:
    return bool(guild and user and user.id == guild.owner_id)


def _parse_duration(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,4})\s*([mhd])\s*", value or "", re.I)
    if not match:
        return None
    amount = int(match.group(1))
    seconds = amount * {"m": 60, "h": 3600, "d": 86400}[match.group(2).casefold()]
    return seconds if amount > 0 and seconds <= MAX_TEMP_SECONDS else None


def _utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _challenge() -> tuple[str, str]:
    op = random.choice(("+", "-", "×"))
    if op == "+":
        a, b = secrets.randbelow(39) + 11, secrets.randbelow(39) + 11
        answer = a + b
    elif op == "-":
        a, b = secrets.randbelow(60) + 20, secrets.randbelow(19) + 2
        answer = a - b
    else:
        a, b = secrets.randbelow(8) + 3, secrets.randbelow(8) + 2
        answer = a * b
    return f"{a} {op} {b}", str(answer)


def _answer_hash(salt: str, answer: str) -> str:
    return hashlib.sha256((salt + ":" + answer.strip()).encode("utf-8")).hexdigest()


class BotApprovalView(discord.ui.View):
    def __init__(self, cog, guild_id: int, bot_id: int, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.bot_id = bot_id
        approve = discord.ui.Button(
            label="Approve Bot", style=discord.ButtonStyle.success,
            custom_id=f"pce:bot:approve:{guild_id}:{bot_id}", disabled=disabled,
        )
        reject = discord.ui.Button(
            label="Reject Bot", style=discord.ButtonStyle.danger,
            custom_id=f"pce:bot:reject:{guild_id}:{bot_id}", disabled=disabled,
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    async def _decide(self, interaction, approved: bool):
        if not interaction.guild or interaction.guild.id != self.guild_id or not _is_owner(interaction.guild, interaction.user):
            await interaction.response.send_message("Only the actual server owner can approve or reject bots.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        review_message_id = interaction.message.id if interaction.message else None
        ok, message = await self.cog.decide_bot(
            interaction.guild, self.bot_id, approved, interaction.user, review_message_id
        )
        if ok and interaction.message:
            try:
                await interaction.message.edit(view=BotApprovalView(self.cog, self.guild_id, self.bot_id, disabled=True))
            except discord.HTTPException:
                log.exception("Could not disable bot review buttons for bot %s", self.bot_id)
        await interaction.followup.send(message, ephemeral=True)

    async def _approve(self, interaction):
        await self._decide(interaction, True)

    async def _reject(self, interaction):
        await self._decide(interaction, False)


class VerifyHumanModal(discord.ui.Modal, title="Human verification"):
    answer = discord.ui.TextInput(label="Answer", placeholder="Enter the number", min_length=1, max_length=5)

    def __init__(self, cog, guild_id: int, member_id: int, question: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.member_id = member_id
        self.answer.label = f"Solve: {question}"

    async def on_submit(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.member_id:
            await interaction.response.send_message("This challenge belongs to another member.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, response = await self.cog.submit_verification(interaction.guild, interaction.user, str(self.answer))
        await interaction.followup.send(response, ephemeral=True)


class VerifyHumanView(discord.ui.View):
    def __init__(self, cog, guild_id: int, member_id: int, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.member_id = member_id
        button = discord.ui.Button(
            label="Verify I'm Human", style=discord.ButtonStyle.success,
            custom_id=f"pce:verify:{guild_id}:{member_id}", disabled=disabled,
        )
        button.callback = self.verify
        self.add_item(button)

    async def verify(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.member_id:
            await interaction.response.send_message("This verification panel belongs to another member.", ephemeral=True)
            return
        question, response = await self.cog.get_verification_question(interaction.guild, interaction.user)
        if question is None:
            await interaction.response.send_message(response, ephemeral=True)
            return
        await interaction.response.send_modal(VerifyHumanModal(self.cog, self.guild_id, self.member_id, question))


class SecurityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.join_windows: dict[int, deque[tuple[float, int]]] = defaultdict(deque)
        self.locks: dict[tuple[int, int, str], asyncio.Lock] = {}

    def pool(self):
        return getattr(self.bot.store, "pool", None)

    async def cog_load(self):
        pool = self.pool()
        if pool is None:
            log.error("Persistent Security features are disabled because PostgreSQL is unavailable.")
            return
        statements = [
            """CREATE TABLE IF NOT EXISTS pvpclub_bot_approvals (
                guild_id BIGINT NOT NULL, bot_id BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                review_channel_id BIGINT, review_message_id BIGINT, invited_by BIGINT,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), approved_by BIGINT, decided_at TIMESTAMPTZ,
                PRIMARY KEY (guild_id,bot_id))""",
            """CREATE TABLE IF NOT EXISTS pvpclub_quarantine_overwrites (
                guild_id BIGINT NOT NULL, member_id BIGINT NOT NULL, scope TEXT NOT NULL,
                channel_id BIGINT NOT NULL, permissions JSONB NOT NULL,
                PRIMARY KEY (guild_id,member_id,scope,channel_id))""",
            """CREATE TABLE IF NOT EXISTS pvpclub_active_quarantines (
                guild_id BIGINT NOT NULL, member_id BIGINT NOT NULL, scope TEXT NOT NULL,
                bypass_channel_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id,member_id,scope))""",
            """CREATE TABLE IF NOT EXISTS pvpclub_raid_verifications (
                guild_id BIGINT NOT NULL, member_id BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                question TEXT NOT NULL, answer_salt TEXT NOT NULL, answer_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, panel_message_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL,
                expired_at TIMESTAMPTZ, verified_at TIMESTAMPTZ,
                PRIMARY KEY (guild_id,member_id))""",
            """CREATE TABLE IF NOT EXISTS pvpclub_temporary_roles (
                id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, member_id BIGINT NOT NULL,
                role_id BIGINT NOT NULL, assigned_by BIGINT NOT NULL,
                assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_extended_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ NOT NULL, reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active', attempts INTEGER NOT NULL DEFAULT 0,
                retry_after TIMESTAMPTZ, last_error TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
            """CREATE UNIQUE INDEX IF NOT EXISTS pvpclub_temporary_roles_active_or_granting_uq
                ON pvpclub_temporary_roles (guild_id,member_id,role_id) WHERE status IN ('active','granting')""",
            """CREATE INDEX IF NOT EXISTS pvpclub_temporary_roles_expiry_idx
                ON pvpclub_temporary_roles (expires_at) WHERE status='active'""",
        ]
        for statement in statements:
            await pool.execute(statement)
        # Re-register persistent interactive controls after a Render restart.
        try:
            rows = await pool.fetch("SELECT guild_id,bot_id,review_message_id FROM pvpclub_bot_approvals WHERE status='pending' AND review_message_id IS NOT NULL")
            for row in rows:
                self.bot.add_view(BotApprovalView(self, int(row["guild_id"]), int(row["bot_id"])), message_id=int(row["review_message_id"]))
            rows = await pool.fetch("SELECT guild_id,member_id,panel_message_id FROM pvpclub_raid_verifications WHERE status IN ('pending','expired') AND panel_message_id IS NOT NULL")
            for row in rows:
                self.bot.add_view(VerifyHumanView(self, int(row["guild_id"]), int(row["member_id"])), message_id=int(row["panel_message_id"]))
        except Exception:
            log.exception("Could not restore persistent security views.")
        self.temporary_role_worker.start()
        self.raid_worker.start()

    def cog_unload(self):
        self.temporary_role_worker.cancel()
        self.raid_worker.cancel()

    def config(self, key: str) -> dict:
        result = self.bot.store.data.setdefault(key, {})
        if not isinstance(result, dict):
            self.bot.store.data[key] = {}
            result = self.bot.store.data[key]
        return result

    async def owner_alert(self, guild: discord.Guild, title: str, message: str):
        owner = guild.get_member(guild.owner_id)
        if owner is None:
            try:
                owner = await guild.fetch_member(guild.owner_id)
            except discord.HTTPException:
                owner = None
        if owner:
            try:
                await owner.send(f"⚠️ {title} — {guild.name}\n{message[:1700]}")
                return
            except discord.HTTPException:
                log.warning("Unable to DM server owner %s for security alert", guild.owner_id)
        channel_id = self.bot.store.data.get("bot_approval_channels", {}).get(str(guild.id))
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(f"⚠️ **{title}**\n{message[:1700]}", allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("Could not send owner security alert in guild %s", guild.id)

    async def modlog(self, guild, action, target, target_id, actor, actor_id, reason, details=None, color=0xFF647C):
        channel_id = (
            self.bot.store.data.get("modlog_channels", {}).get(str(guild.id))
            or self.bot.store.data.get("logs_channels", {}).get(str(guild.id))
        )
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        e = discord.Embed(title=f"🛡️ {action}", color=color)
        e.add_field(name="Target", value=f"{target} ({target_id})", inline=False)
        e.add_field(name="Actor", value=f"{actor} ({actor_id if actor_id else 'system'})", inline=True)
        e.add_field(name="Reason", value=(reason or "Not specified")[:1024], inline=False)
        if details:
            e.add_field(name="Details", value=details[:1024], inline=False)
        e.timestamp = discord.utils.utcnow()
        try:
            await channel.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not send security modlog to guild %s", guild.id)

    async def apply_channel_quarantine(self, guild, member, scope, channel, bypass_channel_id):
        pool = self.pool()
        if pool is None:
            raise RuntimeError("PostgreSQL unavailable; refusing non-persistent quarantine.")
        overwrite = channel.overwrites_for(member)
        original = {name: getattr(overwrite, name) for name in LOCKED_PERMISSIONS if hasattr(overwrite, name)}
        await pool.execute(
            """INSERT INTO pvpclub_quarantine_overwrites
               (guild_id,member_id,scope,channel_id,permissions)
               VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING""",
            guild.id, member.id, scope, channel.id, json.dumps(original),
        )
        for name in LOCKED_PERMISSIONS:
            if hasattr(overwrite, name):
                setattr(overwrite, name, False)
        if channel.id == bypass_channel_id:
            overwrite.view_channel = True
            if hasattr(overwrite, "read_message_history"):
                overwrite.read_message_history = True
            overwrite.send_messages = False
        else:
            overwrite.view_channel = False
        await channel.set_permissions(member, overwrite=overwrite, reason=f"PVPClub Empire {scope} quarantine")

    async def quarantine(self, guild, member, scope, bypass_channel_id) -> bool:
        if not guild.me or not guild.me.guild_permissions.manage_roles:
            return False
        if member.guild_permissions.administrator:
            return False
        lock = self.locks.setdefault((guild.id, member.id, scope), asyncio.Lock())
        async with lock:
            pool = self.pool()
            if pool is None:
                return False
            await pool.execute(
                """INSERT INTO pvpclub_active_quarantines (guild_id,member_id,scope,bypass_channel_id)
                   VALUES ($1,$2,$3,$4) ON CONFLICT (guild_id,member_id,scope)
                   DO UPDATE SET bypass_channel_id=EXCLUDED.bypass_channel_id""",
                guild.id, member.id, scope, bypass_channel_id,
            )
            try:
                for channel in list(guild.channels):
                    await self.apply_channel_quarantine(guild, member, scope, channel, bypass_channel_id)
                return True
            except Exception:
                log.exception("Partial quarantine failure for member %s in guild %s", member.id, guild.id)
                return False

    async def restore_quarantine(self, guild, member, scope) -> bool:
        pool = self.pool()
        if pool is None:
            return False
        rows = await pool.fetch(
            "SELECT channel_id,permissions FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope=$3",
            guild.id, member.id, scope,
        )
        try:
            for row in rows:
                channel = guild.get_channel(int(row["channel_id"]))
                if channel is None:
                    continue
                saved = row["permissions"]
                if isinstance(saved, str):
                    saved = json.loads(saved)
                overwrite = channel.overwrites_for(member)
                for name, value in saved.items():
                    if hasattr(overwrite, name):
                        setattr(overwrite, name, value)
                if any(value is not None for value in saved.values()):
                    await channel.set_permissions(member, overwrite=overwrite, reason=f"PVPClub Empire {scope} quarantine lifted")
                else:
                    await channel.set_permissions(member, overwrite=None, reason=f"PVPClub Empire {scope} quarantine lifted")
            await pool.execute(
                "DELETE FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope=$3",
                guild.id, member.id, scope,
            )
            await pool.execute(
                "DELETE FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope=$3",
                guild.id, member.id, scope,
            )
            return True
        except Exception:
            log.exception("Failed to restore quarantine for %s in guild %s", member.id, guild.id)
            return False

    async def on_new_channel(self, channel: discord.abc.GuildChannel):
        pool = self.pool()
        if pool is None:
            return
        try:
            rows = await pool.fetch(
                "SELECT member_id,scope,bypass_channel_id FROM pvpclub_active_quarantines WHERE guild_id=$1",
                channel.guild.id,
            )
            for row in rows:
                member = channel.guild.get_member(int(row["member_id"]))
                if member:
                    await self.apply_channel_quarantine(
                        channel.guild, member, str(row["scope"]), channel,
                        int(row["bypass_channel_id"]) if row["bypass_channel_id"] else None,
                    )
        except Exception:
            log.exception("Could not apply quarantine permissions to newly created channel %s", channel.id)

    async def bot_inviter(self, guild, member):
        if not guild.me or not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            await asyncio.sleep(1)
            async for entry in guild.audit_logs(limit=20, action=discord.AuditLogAction.bot_add):
                if getattr(entry.target, "id", None) == member.id:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            log.info("Bot add audit log unavailable for bot %s in guild %s", member.id, guild.id)
        return None

    @commands.command(name="botapprovalsetup")
    @commands.guild_only()
    async def botapprovalsetup(self, ctx, channel: discord.TextChannel):
        if not _is_owner(ctx.guild, ctx.author):
            await ctx.reply("Only the actual server owner can configure bot approval.", mention_author=False)
            return
        if self.pool() is None:
            await ctx.reply("Bot approval requires PostgreSQL. Check DATABASE_URL.", mention_author=False)
            return
        bot_perms = ctx.guild.me.guild_permissions
        perms = channel.permissions_for(ctx.guild.me)
        if not bot_perms.manage_roles or not perms.view_channel or not perms.send_messages or not perms.embed_links:
            await ctx.reply("I need Manage Roles and View Channel, Send Messages and Embed Links in that channel.", mention_author=False)
            return

        status_message = await ctx.reply("Scanning existing members so their current bots can be grandfathered in...", mention_author=False)
        existing_bots = []
        try:
            async for existing in ctx.guild.fetch_members(limit=None):
                if existing.bot:
                    existing_bots.append(existing.id)
        except (discord.Forbidden, discord.HTTPException):
            await status_message.edit(
                content="I couldn't scan every existing member. Enable the Server Members Intent and check my access, then run this setup again. Bot approval was not enabled."
            )
            return

        self.config("bot_approval_channels")[str(ctx.guild.id)] = channel.id
        await self.bot.store.save()
        for bot_id in existing_bots:
            await self.pool().execute(
                """INSERT INTO pvpclub_bot_approvals (guild_id,bot_id,status,approved_by,decided_at)
                   VALUES ($1,$2,'approved',$3,NOW()) ON CONFLICT (guild_id,bot_id) DO NOTHING""",
                ctx.guild.id, bot_id, ctx.guild.owner_id,
            )
        await status_message.edit(
            content=f"✅ Bot approval is configured in {channel.mention}. {len(existing_bots)} existing bots were grandfathered in."
        )

    async def handle_bot_join(self, member: discord.Member):
        guild = member.guild
        # Bot approval is opt-in. Do not affect existing/new bots until the
        # actual owner has configured the dedicated review channel.
        configured_channel = self.bot.store.data.get("bot_approval_channels", {}).get(str(guild.id))
        if not configured_channel:
            return
        pool = self.pool()
        if pool is None:
            await self.owner_alert(guild, "Bot approval unavailable", f"{member} ({member.id}) joined, but PostgreSQL is unavailable.")
            try:
                await member.kick(reason="PVPClub Empire cannot persist bot approval state")
            except discord.HTTPException:
                log.exception("Fail-closed bot kick failed for %s", member.id)
            return

        row = await pool.fetchrow(
            "SELECT status,review_message_id FROM pvpclub_bot_approvals WHERE guild_id=$1 AND bot_id=$2",
            guild.id, member.id,
        )
        if row and row["status"] == "approved":
            # If a previous process stopped after recording approval but before
            # restoring the saved overwrites, finish that restoration on rejoin.
            active = await pool.fetchval(
                "SELECT 1 FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
                guild.id, member.id,
            )
            if active and not await self.restore_quarantine(guild, member, "bot"):
                await self.owner_alert(guild, "Approved bot is still quarantined", f"Could not restore all saved permissions for {member} ({member.id}).")
            return
        if row and row["status"] == "rejected":
            try:
                await member.kick(reason="Previously rejected by the server owner")
            except discord.HTTPException:
                log.exception("Previously rejected bot %s could not be kicked", member.id)
                await self.owner_alert(guild, "Rejected bot rejoined", f"Could not kick {member} ({member.id}).")
            return

        channel_id = self.bot.store.data.get("bot_approval_channels", {}).get(str(guild.id))
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await self.owner_alert(guild, "Bot approval is not configured", f"Unapproved bot {member} ({member.id}) joined. Run ?botapprovalsetup #channel.")
            try:
                await member.kick(reason="Bot approval is not configured")
            except discord.HTTPException:
                log.exception("Could not kick bot %s while approval was unconfigured", member.id)
            return

        # Admin bypasses all channel overwrites; do not pretend it is quarantined.
        if member.guild_permissions.administrator or not await self.quarantine(guild, member, "bot", channel.id):
            await self.owner_alert(guild, "BOT QUARANTINE FAILED", f"Could not safely quarantine {member} ({member.id}). Attempting fail-closed kick.")
            try:
                await member.kick(reason="PVPClub Empire could not safely quarantine this unapproved bot")
            except discord.HTTPException:
                log.exception("Could not kick unquarantinable bot %s", member.id)
            return

        inviter = await self.bot_inviter(guild, member)
        await pool.execute(
            """INSERT INTO pvpclub_bot_approvals (guild_id,bot_id,status,review_channel_id,invited_by,joined_at)
               VALUES ($1,$2,'pending',$3,$4,NOW())
               ON CONFLICT (guild_id,bot_id) DO UPDATE SET
               status='pending',review_channel_id=EXCLUDED.review_channel_id,
               invited_by=EXCLUDED.invited_by,joined_at=NOW()
               WHERE pvpclub_bot_approvals.status <> 'approved'""",
            guild.id, member.id, channel.id, inviter.id if inviter else None,
        )
        if row and row["status"] == "pending" and row["review_message_id"]:
            try:
                old = await channel.fetch_message(int(row["review_message_id"]))
                self.bot.add_view(BotApprovalView(self, guild.id, member.id), message_id=old.id)
                return
            except discord.HTTPException:
                log.info("Previous review message is unavailable; posting a replacement.")
        e = discord.Embed(
            title="🤖 Bot approval required",
            description=f"{member.mention} is quarantined until the server owner decides.",
            color=0xFFB347,
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="Bot", value=f"{member} ({member.id})", inline=False)
        e.add_field(name="Account created", value=member.created_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        e.add_field(name="Bot account", value="Yes", inline=True)
        e.add_field(name="Invited by", value=f"{inviter} ({inviter.id})" if inviter else "Unavailable (audit log permission or entry not available)", inline=False)
        e.set_footer(text="Only the actual server owner can approve or reject.")
        try:
            # Persist the exact review-message ID before enabling its buttons so
            # stale panels from a previous join can never decide a new request.
            message = await channel.send(embed=e)
            await pool.execute(
                "UPDATE pvpclub_bot_approvals SET review_message_id=$3 WHERE guild_id=$1 AND bot_id=$2 AND status='pending'",
                guild.id, member.id, message.id,
            )
            await message.edit(view=BotApprovalView(self, guild.id, member.id))
        except discord.HTTPException:
            log.exception("Could not send approval panel for bot %s", member.id)
            await self.owner_alert(guild, "Bot approval panel failed", f"{member} ({member.id}) remains quarantined, but I could not post the review message in {channel.mention}.")

    async def decide_bot(self, guild, bot_id: int, approved: bool, actor, review_message_id: int | None):
        pool = self.pool()
        row = await pool.fetchrow(
            "SELECT status,review_message_id FROM pvpclub_bot_approvals WHERE guild_id=$1 AND bot_id=$2",
            guild.id, bot_id,
        )
        if not row or row["status"] != "pending":
            return False, "This bot has no pending approval request."
        if not review_message_id or int(row["review_message_id"] or 0) != int(review_message_id):
            return False, "This is an outdated approval panel. Use the latest bot approval message."
        member = guild.get_member(bot_id)
        if member is None:
            try:
                member = await guild.fetch_member(bot_id)
            except discord.NotFound:
                await pool.execute(
                    "UPDATE pvpclub_bot_approvals SET status='left',decided_at=NOW() WHERE guild_id=$1 AND bot_id=$2 AND status='pending'",
                    guild.id, bot_id,
                )
                return False, "That bot has already left the server."
            except discord.HTTPException:
                return False, "I could not fetch that bot. Please try again."

        if approved:
            result = await pool.execute(
                """UPDATE pvpclub_bot_approvals SET status='approved',approved_by=$3,decided_at=NOW()
                   WHERE guild_id=$1 AND bot_id=$2 AND status='pending'""",
                guild.id, bot_id, actor.id,
            )
            if result.endswith(" 0"):
                return False, "The approval state changed before this decision could be saved."
            if not await self.restore_quarantine(guild, member, "bot"):
                await pool.execute(
                    """UPDATE pvpclub_bot_approvals SET status='pending',approved_by=NULL,decided_at=NULL
                       WHERE guild_id=$1 AND bot_id=$2 AND status='approved' AND approved_by=$3""",
                    guild.id, bot_id, actor.id,
                )
                approval_id = self.bot.store.data.get("bot_approval_channels", {}).get(str(guild.id))
                await self.quarantine(guild, member, "bot", int(approval_id) if approval_id else None)
                await self.owner_alert(guild, "Bot approval could not finish", f"{member} ({bot_id}) remains quarantined because permission restoration failed.")
                return False, "I could not restore every saved permission. The bot remains quarantined."
            await self.modlog(
                guild, "Bot approved", str(member), bot_id, str(actor), actor.id,
                "Approved by the actual server owner. No additional permissions were granted.",
                color=0x57D69A,
            )
            return True, f"✅ {member} approved. Its previous channel permissions were restored; no new permissions were granted."

        result = await pool.execute(
            """UPDATE pvpclub_bot_approvals SET status='rejected',approved_by=$3,decided_at=NOW()
               WHERE guild_id=$1 AND bot_id=$2 AND status='pending'""",
            guild.id, bot_id, actor.id,
        )
        if result.endswith(" 0"):
            return False, "The approval state changed before this decision could be saved."
        try:
            await member.kick(reason=f"Rejected by server owner {actor} ({actor.id})")
        except discord.HTTPException:
            log.exception("Could not kick rejected bot %s", bot_id)
            await pool.execute(
                """UPDATE pvpclub_bot_approvals SET status='pending',approved_by=NULL,decided_at=NULL
                   WHERE guild_id=$1 AND bot_id=$2 AND status='rejected' AND approved_by=$3""",
                guild.id, bot_id, actor.id,
            )
            return False, "I could not kick that bot. Check Kick Members and role hierarchy; it remains pending."
        await pool.execute(
            "DELETE FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
            guild.id, bot_id,
        )
        await pool.execute(
            "DELETE FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
            guild.id, bot_id,
        )
        await self.modlog(
            guild, "Bot rejected and kicked", str(member), bot_id, str(actor), actor.id,
            "Rejected by the actual server owner.",
        )
        return True, f"✅ {member} was rejected and kicked."

    def raid_config(self, guild_id: int) -> dict:
        configs = self.config("raid_config")
        config = configs.setdefault(str(guild_id), {})
        config.setdefault("enabled", False)
        config.setdefault("threshold", 10)
        config.setdefault("window_seconds", 10)
        config.setdefault("protection_minutes", 10)
        config.setdefault("kick_after_minutes", 0)
        return config

    async def is_verified(self, guild_id: int, member_id: int) -> bool:
        row = await self.pool().fetchrow(
            "SELECT status FROM pvpclub_raid_verifications WHERE guild_id=$1 AND member_id=$2",
            guild_id, member_id,
        )
        return bool(row and row["status"] == "verified")

    def trusted_member(self, member: discord.Member) -> bool:
        p = member.guild_permissions
        return bool(
            member.id == member.guild.owner_id or p.administrator or p.manage_guild
            or p.manage_roles or p.manage_channels or p.manage_messages
        )

    async def create_challenge(self, guild, member, channel):
        pool = self.pool()
        existing = await pool.fetchrow(
            "SELECT status,attempts,expires_at,panel_message_id FROM pvpclub_raid_verifications WHERE guild_id=$1 AND member_id=$2",
            guild.id, member.id,
        )
        if existing and existing["status"] == "verified":
            return
        if existing and existing["status"] == "failed":
            return
        if existing and existing["status"] == "pending":
            expiry = _utc(existing["expires_at"])
            if expiry and expiry > discord.utils.utcnow() and existing["panel_message_id"]:
                try:
                    panel = await channel.fetch_message(int(existing["panel_message_id"]))
                    self.bot.add_view(VerifyHumanView(self, guild.id, member.id), message_id=panel.id)
                    return
                except discord.HTTPException:
                    pass
        question, answer = _challenge()
        salt = secrets.token_hex(16)
        expiry = discord.utils.utcnow() + timedelta(minutes=VERIFY_MINUTES)
        attempts = int(existing["attempts"]) if existing else 0
        if attempts >= VERIFY_ATTEMPTS:
            return
        await pool.execute(
            """INSERT INTO pvpclub_raid_verifications
               (guild_id,member_id,status,question,answer_salt,answer_hash,attempts,created_at,expires_at,expired_at,verified_at)
               VALUES ($1,$2,'pending',$3,$4,$5,$6,NOW(),$7,NULL,NULL)
               ON CONFLICT (guild_id,member_id) DO UPDATE SET status='pending',
               question=EXCLUDED.question,answer_salt=EXCLUDED.answer_salt,
               answer_hash=EXCLUDED.answer_hash,created_at=NOW(),expires_at=EXCLUDED.expires_at,expired_at=NULL
               WHERE pvpclub_raid_verifications.status <> 'verified'
                 AND pvpclub_raid_verifications.status <> 'failed'""",
            guild.id, member.id, question, salt, _answer_hash(salt, answer), attempts, expiry,
        )
        e = discord.Embed(
            title="🛡️ Verify you're human",
            description=f"{member.mention}, raid protection is active. Press **Verify I'm Human** and solve the arithmetic question to restore access.\n\nThe challenge expires in {VERIFY_MINUTES} minutes. You have {VERIFY_ATTEMPTS} attempts.",
            color=0x55DDFB,
        )
        e.set_footer(text="This challenge is personal to your Discord account.")
        panel = await channel.send(embed=e, view=VerifyHumanView(self, guild.id, member.id), allowed_mentions=discord.AllowedMentions(users=[member]))
        await pool.execute(
            "UPDATE pvpclub_raid_verifications SET panel_message_id=$3 WHERE guild_id=$1 AND member_id=$2",
            guild.id, member.id, panel.id,
        )

    async def handle_member_join(self, member: discord.Member) -> bool:
        if member.bot:
            await self.handle_bot_join(member)
            return False
        guild = member.guild
        config = self.raid_config(guild.id)
        if not config.get("enabled") or self.trusted_member(member) or self.pool() is None:
            return False
        if await self.is_verified(guild.id, member.id):
            return False
        channel_id = config.get("verification_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return False
        now = time.monotonic()
        bucket = self.join_windows[guild.id]
        window = int(config.get("window_seconds", 10))
        while bucket and now - bucket[0][0] > window:
            bucket.popleft()
        bucket.append((now, member.id))
        ends = _utc(config.get("ends_at"))
        active = bool(config.get("active")) and ends and ends > discord.utils.utcnow()
        triggered = not active and len(bucket) >= int(config.get("threshold", 10))
        if triggered:
            config["active"] = True
            config["started_at"] = discord.utils.utcnow().isoformat()
            config["ends_at"] = (discord.utils.utcnow() + timedelta(minutes=int(config["protection_minutes"]))).isoformat()
            await self.bot.store.save()
            await self.modlog(
                guild, "Raid protection activated", "New member join wave", guild.id,
                "PVPClub Empire", None,
                f"{len(bucket)} humans joined within {window} seconds (trigger: {config['threshold']}).",
                details=f"Verification channel: {channel.mention}", color=0xFFB347,
            )
            await self.owner_alert(guild, "Raid Protection activated", f"{len(bucket)} members joined in {window} seconds. New human arrivals are being verified in {channel.mention}.")
        elif active:
            config["ends_at"] = max(ends, discord.utils.utcnow() + timedelta(minutes=int(config["protection_minutes"]))).isoformat()
            await self.bot.store.save()
        if not triggered and not active:
            return False
        candidates = [uid for _, uid in bucket] if triggered else [member.id]
        for user_id in dict.fromkeys(candidates):
            candidate = guild.get_member(user_id)
            if candidate is None or candidate.bot or self.trusted_member(candidate):
                continue
            if await self.is_verified(guild.id, candidate.id):
                continue
            ok = await self.quarantine(guild, candidate, "raid", channel.id)
            if not ok:
                await self.owner_alert(guild, "RAID QUARANTINE FAILED", f"Could not fully restrict {candidate} ({candidate.id}). I am attempting to remove the member so they cannot bypass verification. Check Manage Roles and channel overwrites.")
                try:
                    await candidate.kick(reason="PVPClub Empire could not safely enforce raid quarantine")
                    await self.modlog(guild, "Member removed after quarantine failure", str(candidate), candidate.id, "PVPClub Empire", None, "Could not safely apply verification restrictions.")
                except discord.HTTPException:
                    log.exception("Could not fail-closed remove unquarantinable member %s", candidate.id)
                continue
            try:
                await self.create_challenge(guild, candidate, channel)
            except Exception:
                log.exception("Verification panel creation failed for %s", candidate.id)
                await self.owner_alert(guild, "Verification panel failed", f"{candidate} ({candidate.id}) is quarantined but the panel could not be sent in {channel.mention}.")
        return True

    async def question_for(self, guild, user):
        if guild is None or self.pool() is None:
            return None, "Verification is temporarily unavailable. Contact a moderator."
        row = await self.pool().fetchrow(
            "SELECT question,status,attempts,expires_at FROM pvpclub_raid_verifications WHERE guild_id=$1 AND member_id=$2",
            guild.id, user.id,
        )
        if not row or row["status"] == "verified":
            return None, "You are already verified or do not have an active challenge."
        if row["status"] == "failed" or int(row["attempts"]) >= VERIFY_ATTEMPTS:
            return None, "Your attempts are exhausted. Contact a moderator."
        expiry = _utc(row["expires_at"])
        if row["status"] == "expired" or not expiry or expiry <= discord.utils.utcnow():
            member = guild.get_member(user.id)
            config = self.raid_config(guild.id)
            channel_id = config.get("verification_channel_id")
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if not member or not isinstance(channel, discord.TextChannel):
                return None, "I could not refresh your challenge. Contact a moderator."
            await self.create_challenge(guild, member, channel)
            row = await self.pool().fetchrow(
                "SELECT question,status FROM pvpclub_raid_verifications WHERE guild_id=$1 AND member_id=$2",
                guild.id, user.id,
            )
        if not row or row["status"] != "pending":
            return None, "Your challenge is no longer active."
        return str(row["question"]), "Challenge ready."

    async def submit_verification(self, guild, user, answer):
        if guild is None or self.pool() is None:
            return False, "Verification is temporarily unavailable."
        pool = self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status,answer_salt,answer_hash,attempts,expires_at FROM pvpclub_raid_verifications WHERE guild_id=$1 AND member_id=$2 FOR UPDATE",
                    guild.id, user.id,
                )
                if not row or row["status"] != "pending":
                    return False, "This challenge is not active. Use the verification button again."
                expiry = _utc(row["expires_at"])
                if not expiry or expiry <= discord.utils.utcnow():
                    await conn.execute(
                        "UPDATE pvpclub_raid_verifications SET status='expired',expired_at=COALESCE(expired_at,NOW()) WHERE guild_id=$1 AND member_id=$2",
                        guild.id, user.id,
                    )
                    return False, "That challenge expired. Press Verify I'm Human for a new question."
                correct = hmac.compare_digest(_answer_hash(str(row["answer_salt"]), answer), str(row["answer_hash"]))
                if not correct:
                    attempts = int(row["attempts"]) + 1
                    status = "failed" if attempts >= VERIFY_ATTEMPTS else "pending"
                    await conn.execute(
                        "UPDATE pvpclub_raid_verifications SET attempts=$3,status=$4,expired_at=CASE WHEN $4='failed' THEN NOW() ELSE expired_at END WHERE guild_id=$1 AND member_id=$2",
                        guild.id, user.id, attempts, status,
                    )
                else:
                    status = "correct"
                    await conn.execute(
                        "UPDATE pvpclub_raid_verifications SET status='verified',verified_at=NOW() WHERE guild_id=$1 AND member_id=$2 AND status='pending'",
                        guild.id, user.id,
                    )
        if status != "correct":
            if status == "failed":
                await self.modlog(guild, "Human verification failed", str(user), user.id, "PVPClub Empire", None, "All verification attempts were used.")
                await self.owner_alert(guild, "Verification attempts exhausted", f"{user} ({user.id}) remains quarantined after {VERIFY_ATTEMPTS} incorrect answers.")
                await self.update_verification_panel(guild, user.id, True, "Verification attempts exhausted")
                return False, "You used all attempts. You remain quarantined; contact a moderator."
            return False, f"Incorrect answer. Attempts remaining: {VERIFY_ATTEMPTS - attempts}."
        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except discord.HTTPException:
                return False, "I could not fetch your member record. Your verification is saved; contact a moderator if access is not restored."
        if not await self.restore_quarantine(guild, member, "raid"):
            await pool.execute(
                "UPDATE pvpclub_raid_verifications SET status='pending',verified_at=NULL WHERE guild_id=$1 AND member_id=$2 AND status='verified'",
                guild.id, user.id,
            )
            await self.quarantine(
                guild, member, "raid",
                int(self.raid_config(guild.id).get("verification_channel_id", 0)) or None,
            )
            await self.owner_alert(guild, "Verification permission restore failed", f"{member} ({member.id}) remains under quarantine.")
            return False, "I could not restore all permissions. You remain quarantined; try again shortly."
        await self.modlog(guild, "Member verified", str(member), member.id, "PVPClub Empire", None, "Completed the arithmetic verification.", color=0x57D69A)
        await self.update_verification_panel(guild, user.id, True, "✅ Verified")
        return True, "✅ Verification successful. Your normal server permissions have been restored."

    async def update_verification_panel(self, guild, member_id, disabled, title):
        row = await self.pool().fetchrow(
            "SELECT panel_message_id FROM pvpclub_raid_verifications WHERE guild_id=$1 AND member_id=$2",
            guild.id, member_id,
        )
        config = self.raid_config(guild.id)
        channel_id = config.get("verification_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel) or not row or not row["panel_message_id"]:
            return
        try:
            panel = await channel.fetch_message(int(row["panel_message_id"]))
            e = discord.Embed(title=title, description=f"Verification status for member {member_id}.", color=0x57D69A)
            await panel.edit(embed=e, view=VerifyHumanView(self, guild.id, member_id, disabled=disabled))
        except discord.HTTPException:
            log.info("Could not update verification panel for %s", member_id)

    async def handle_member_leave(self, member: discord.Member):
        """Restore saved member overwrites so a rejoin starts from a clean permission state."""
        pool = self.pool()
        if pool is None:
            return
        for scope in ("bot", "raid"):
            try:
                await self.restore_quarantine(member.guild, member, scope)
            except Exception:
                log.exception("Could not restore quarantine overwrites for departing member %s", member.id)
        if member.bot:
            try:
                review = await pool.fetchrow(
                    """SELECT review_channel_id,review_message_id FROM pvpclub_bot_approvals
                       WHERE guild_id=$1 AND bot_id=$2 AND status='pending'""",
                    member.guild.id, member.id,
                )
                if review and review["review_channel_id"] and review["review_message_id"]:
                    review_channel = member.guild.get_channel(int(review["review_channel_id"]))
                    if isinstance(review_channel, discord.TextChannel):
                        try:
                            old_message = await review_channel.fetch_message(int(review["review_message_id"]))
                            await old_message.edit(view=BotApprovalView(self, member.guild.id, member.id, disabled=True))
                        except discord.HTTPException:
                            log.info("Could not disable departed bot's review panel for %s", member.id)
            except Exception:
                log.exception("Could not close the review panel for departing bot %s", member.id)
        try:
            await pool.execute(
                """UPDATE pvpclub_bot_approvals SET status='left'
                   WHERE guild_id=$1 AND bot_id=$2 AND status='pending'""",
                member.guild.id, member.id,
            )
            await pool.execute(
                """UPDATE pvpclub_raid_verifications SET status='left'
                   WHERE guild_id=$1 AND member_id=$2 AND status IN ('pending','expired')""",
                member.guild.id, member.id,
            )
        except Exception:
            log.exception("Could not persist departure state for security records of member %s", member.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self.on_new_channel(channel)

    @tasks.loop(seconds=30)
    async def raid_worker(self):
        pool = self.pool()
        if pool is None:
            return
        now = discord.utils.utcnow()
        try:
            interrupted = await pool.fetch(
                """SELECT q.guild_id,q.member_id FROM pvpclub_active_quarantines q
                   JOIN pvpclub_raid_verifications v ON v.guild_id=q.guild_id AND v.member_id=q.member_id
                   WHERE q.scope='raid' AND v.status='verified' LIMIT 100"""
            )
            for item in interrupted:
                guild = self.bot.get_guild(int(item["guild_id"]))
                member = guild.get_member(int(item["member_id"])) if guild else None
                if guild and member:
                    await self.restore_quarantine(guild, member, "raid")
        except Exception:
            log.exception("Could not recover completed verifications after restart.")
        # Reconcile bot approval state after a restart. An approved bot with
        # leftover overwrites is restored; pending bots are kept restricted.
        try:
            bot_rows = await pool.fetch(
                """SELECT q.guild_id,q.member_id,a.status,q.bypass_channel_id
                   FROM pvpclub_active_quarantines q
                   LEFT JOIN pvpclub_bot_approvals a
                     ON a.guild_id=q.guild_id AND a.bot_id=q.member_id
                   WHERE q.scope='bot' LIMIT 100"""
            )
            for item in bot_rows:
                guild = self.bot.get_guild(int(item["guild_id"]))
                if guild is None:
                    continue
                member = guild.get_member(int(item["member_id"]))
                if member is None:
                    try:
                        member = await guild.fetch_member(int(item["member_id"]))
                    except discord.NotFound:
                        await pool.execute(
                            "DELETE FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
                            guild.id, int(item["member_id"]),
                        )
                        await pool.execute(
                            "DELETE FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
                            guild.id, int(item["member_id"]),
                        )
                        continue
                    except discord.HTTPException:
                        continue
                status = str(item["status"] or "pending")
                if status == "approved":
                    await self.restore_quarantine(guild, member, "bot")
                elif status == "rejected":
                    try:
                        await member.kick(reason="PVPClub Empire recovered a rejected bot after restart")
                        await pool.execute(
                            "DELETE FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
                            guild.id, member.id,
                        )
                        await pool.execute(
                            "DELETE FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope='bot'",
                            guild.id, member.id,
                        )
                    except discord.HTTPException:
                        log.exception("Could not finish rejecting bot %s after restart", member.id)
                        await self.owner_alert(guild, "Rejected bot still present", f"Could not finish kicking {member} ({member.id}) after restart.")
                else:
                    approval_id = self.bot.store.data.get("bot_approval_channels", {}).get(str(guild.id))
                    if not await self.quarantine(guild, member, "bot", int(approval_id) if approval_id else None):
                        await self.owner_alert(guild, "Pending bot quarantine recovery failed", f"Could not re-apply quarantine to {member} ({member.id}).")
        except Exception:
            log.exception("Could not reconcile pending bot approval quarantines.")

        # Verification panels whose database record exists but panel send was
        # interrupted are recreated after a restart.
        try:
            missing_panels = await pool.fetch(
                """SELECT guild_id,member_id FROM pvpclub_raid_verifications
                   WHERE status='pending' AND panel_message_id IS NULL LIMIT 100"""
            )
            for item in missing_panels:
                guild = self.bot.get_guild(int(item["guild_id"]))
                member = guild.get_member(int(item["member_id"])) if guild else None
                config = self.raid_config(guild.id) if guild else {}
                channel_id = config.get("verification_channel_id")
                channel = guild.get_channel(int(channel_id)) if guild and channel_id else None
                if guild and member and isinstance(channel, discord.TextChannel):
                    try:
                        await self.create_challenge(guild, member, channel)
                    except Exception:
                        log.exception("Could not recover verification panel for member %s", member.id)

        except Exception:
            log.exception("Could not recover verification panels.")

        for guild in self.bot.guilds:
            config = self.raid_config(guild.id)
            ends = _utc(config.get("ends_at"))
            if config.get("active") and ends and ends <= now:
                config["active"] = False
                await self.bot.store.save()
                await self.modlog(guild, "Raid protection ended", "Protection window", guild.id, "PVPClub Empire", None, "Configured protection window elapsed. Unverified members remain quarantined.", color=0x57D69A)
        rows = await self.pool().fetch(
            """SELECT guild_id,member_id,status,expires_at,expired_at FROM pvpclub_raid_verifications
               WHERE status IN ('pending','expired','failed') ORDER BY expires_at LIMIT 100"""
        )
        for row in rows:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue
            member_id = int(row["member_id"])
            status = str(row["status"])
            expiry = _utc(row["expires_at"])
            expired_at = _utc(row["expired_at"])
            if status == "pending" and expiry and expiry <= now:
                await self.pool().execute(
                    "UPDATE pvpclub_raid_verifications SET status='expired',expired_at=COALESCE(expired_at,NOW()) WHERE guild_id=$1 AND member_id=$2 AND status='pending'",
                    guild.id, member_id,
                )
                expired_at = now
                await self.modlog(guild, "Verification expired", f"Member {member_id}", member_id, "PVPClub Empire", None, "Challenge expired; member remains quarantined.")
            config = self.raid_config(guild.id)
            kick_after = int(config.get("kick_after_minutes", 0) or 0)
            if kick_after and expired_at and now >= expired_at + timedelta(minutes=kick_after):
                member = guild.get_member(member_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.NotFound:
                        await self.pool().execute(
                            "UPDATE pvpclub_raid_verifications SET status='left' WHERE guild_id=$1 AND member_id=$2 AND status IN ('pending','expired','failed')",
                            guild.id, member_id,
                        )
                        await self.pool().execute(
                            "DELETE FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope='raid'",
                            guild.id, member_id,
                        )
                        await self.pool().execute(
                            "DELETE FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope='raid'",
                            guild.id, member_id,
                        )
                        continue
                    except discord.HTTPException:
                        continue
                try:
                    await member.kick(reason="PVPClub Empire raid verification expired")
                    await self.pool().execute(
                        "UPDATE pvpclub_raid_verifications SET status='kicked' WHERE guild_id=$1 AND member_id=$2 AND status IN ('pending','expired','failed')",
                        guild.id, member_id,
                    )
                    await self.pool().execute(
                        "DELETE FROM pvpclub_active_quarantines WHERE guild_id=$1 AND member_id=$2 AND scope='raid'",
                        guild.id, member_id,
                    )
                    await self.pool().execute(
                        "DELETE FROM pvpclub_quarantine_overwrites WHERE guild_id=$1 AND member_id=$2 AND scope='raid'",
                        guild.id, member_id,
                    )
                    await self.modlog(guild, "Unverified member kicked", str(member), member.id, "PVPClub Empire", None, f"Challenge expired; kick delay was {kick_after} minutes.")
                except discord.HTTPException:
                    log.exception("Could not kick expired unverified member %s", member_id)

    @raid_worker.before_loop
    async def before_raid_worker(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def temporary_role_worker(self):
        pool = self.pool()
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(
                        """SELECT * FROM pvpclub_temporary_roles
                           WHERE (status='granting' OR (status='active' AND expires_at<=NOW()))
                             AND (retry_after IS NULL OR retry_after<=NOW())
                           ORDER BY expires_at LIMIT 25 FOR UPDATE SKIP LOCKED"""
                    )
                    for row in rows:
                        guild = self.bot.get_guild(int(row["guild_id"]))
                        if guild is None:
                            continue
                        member_id, role_id = int(row["member_id"]), int(row["role_id"])
                        member = guild.get_member(member_id)
                        if member is None:
                            try:
                                member = await guild.fetch_member(member_id)
                            except discord.NotFound:
                                await conn.execute("UPDATE pvpclub_temporary_roles SET status='member_left',updated_at=NOW() WHERE id=$1", int(row["id"]))
                                continue
                            except discord.HTTPException:
                                continue
                        role = guild.get_role(role_id)
                        if role is None:
                            await conn.execute("UPDATE pvpclub_temporary_roles SET status='role_deleted',updated_at=NOW() WHERE id=$1", int(row["id"]))
                            continue
                        expiry = _utc(row["expires_at"])
                        try:
                            # A grant can be interrupted by a Render restart between
                            # the database intent and the Discord role assignment.
                            if row["status"] == "granting" and expiry and expiry > discord.utils.utcnow():
                                if role not in member.roles:
                                    await member.add_roles(role, reason="PVPClub Empire recovered temporary role grant")
                                await conn.execute(
                                    """UPDATE pvpclub_temporary_roles SET status='active',retry_after=NULL,
                                       last_error=NULL,updated_at=NOW() WHERE id=$1 AND status='granting'""",
                                    int(row["id"]),
                                )
                                await self.modlog(
                                    guild, "Temporary role grant recovered", str(member), member.id,
                                    "PVPClub Empire", self.bot.user.id if self.bot.user else None,
                                    str(row["reason"] or "Recovered after restart"),
                                    details=f"Role: {role.name} ({role.id})\nExpires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}",
                                    color=0x57D69A,
                                )
                                continue

                            if role in member.roles:
                                await member.remove_roles(role, reason="PVPClub Empire temporary role expired")
                            await conn.execute(
                                "UPDATE pvpclub_temporary_roles SET status='completed',updated_at=NOW(),last_error=NULL WHERE id=$1",
                                int(row["id"]),
                            )
                            await self.modlog(
                                guild, "Temporary role expired", str(member), member.id, "PVPClub Empire",
                                self.bot.user.id if self.bot.user else None, str(row["reason"] or "Scheduled expiry"),
                                details=f"Removed role: {role.name} ({role.id}). Expired: {expiry.strftime('%Y-%m-%d %H:%M UTC') if expiry else 'unknown'}.",
                                color=0x57D69A,
                            )
                        except discord.HTTPException as exc:
                            log.exception("Could not process temporary role %s for member %s", role_id, member_id)
                            await conn.execute(
                                """UPDATE pvpclub_temporary_roles SET attempts=attempts+1,
                                   retry_after=NOW()+INTERVAL '1 minute',last_error=$2,updated_at=NOW() WHERE id=$1""",
                                int(row["id"]), str(exc)[:500],
                            )
        except Exception:
            log.exception("Temporary role expiry worker failed.")

    @temporary_role_worker.before_loop
    async def before_temporary_role_worker(self):
        await self.bot.wait_until_ready()

    def role_error(self, interaction, member, role):
        guild = interaction.guild
        actor = interaction.user
        if not guild or not isinstance(actor, discord.Member):
            return "Use this command inside a server."
        if actor.id != guild.owner_id and not actor.guild_permissions.manage_roles:
            return "Only the server owner or members with Manage Roles can manage temporary roles."
        bot_member = guild.me
        if not bot_member or not bot_member.guild_permissions.manage_roles:
            return "I need Manage Roles."
        if role.is_default() or role.managed or role >= bot_member.top_role:
            return "That role is @everyone, managed, or at/above my highest role."
        if member.id == guild.owner_id:
            return "Temporary roles cannot be assigned to the server owner."
        if member.top_role >= bot_member.top_role:
            return "My highest role must be above the target member's highest role."
        if actor.id != guild.owner_id:
            if member.id == guild.owner_id or member.top_role >= actor.top_role or role >= actor.top_role:
                return "You must have a higher role than the target and the role you are assigning."
        if member.id == bot_member.id:
            return "I cannot assign roles to myself."
        return None

    async def grant_role(self, interaction, member, role, duration, reason):
        seconds = _parse_duration(duration)
        if seconds is None:
            await interaction.followup.send("Use 10m, 2h, 1d, 7d or 30d. Maximum duration is 30 days.", ephemeral=True)
            return
        error = self.role_error(interaction, member, role)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        pool = self.pool()
        if pool is None:
            await interaction.followup.send("Temporary roles require PostgreSQL. Check DATABASE_URL.", ephemeral=True)
            return

        now = discord.utils.utcnow()
        async with pool.acquire() as conn:
            async with conn.transaction():
                lock_key = int(hashlib.sha256(
                    f"{interaction.guild_id}:{member.id}:{role.id}".encode("ascii")
                ).hexdigest()[:15], 16)
                await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", lock_key)
                existing = await conn.fetchrow(
                    """SELECT id,expires_at FROM pvpclub_temporary_roles
                       WHERE guild_id=$1 AND member_id=$2 AND role_id=$3
                         AND status IN ('active','granting') FOR UPDATE""",
                    interaction.guild_id, member.id, role.id,
                )
                if existing:
                    expiry = min(
                        max(_utc(existing["expires_at"]) or now, now) + timedelta(seconds=seconds),
                        now + timedelta(seconds=MAX_TEMP_SECONDS),
                    )
                    record_id = int(existing["id"])
                    action = "Temporary role extended"
                    await conn.execute(
                        """UPDATE pvpclub_temporary_roles SET status='granting',expires_at=$2,
                           assigned_by=$3,last_extended_at=NOW(),reason=$4,retry_after=NULL,
                           last_error=NULL,updated_at=NOW() WHERE id=$1""",
                        record_id, expiry, interaction.user.id, reason[:1000],
                    )
                else:
                    if role in member.roles:
                        await interaction.followup.send(
                            "The member already has this role permanently or from another system. I won't schedule its removal.",
                            ephemeral=True,
                        )
                        return
                    expiry = now + timedelta(seconds=seconds)
                    rec = await conn.fetchrow(
                        """INSERT INTO pvpclub_temporary_roles
                           (guild_id,member_id,role_id,assigned_by,expires_at,reason,status)
                           VALUES ($1,$2,$3,$4,$5,$6,'granting') RETURNING id""",
                        interaction.guild_id, member.id, role.id, interaction.user.id, expiry, reason[:1000],
                    )
                    record_id = int(rec["id"])
                    action = "Temporary role granted"

        try:
            if role not in member.roles:
                await member.add_roles(role, reason=f"Temporary role: {reason[:400]}")
        except discord.HTTPException:
            await pool.execute(
                """UPDATE pvpclub_temporary_roles SET status='failed',
                   last_error='Discord role assignment failed',updated_at=NOW()
                   WHERE id=$1 AND status='granting'""",
                record_id,
            )
            log.exception("Could not grant temporary role %s to %s", role.id, member.id)
            await interaction.followup.send("Discord refused the role assignment. No active temporary assignment was left.", ephemeral=True)
            return

        await pool.execute(
            """UPDATE pvpclub_temporary_roles SET status='active',retry_after=NULL,
               last_error=NULL,updated_at=NOW() WHERE id=$1 AND status='granting'""",
            record_id,
        )
        await self.modlog(
            interaction.guild, action, str(member), member.id, str(interaction.user), interaction.user.id,
            reason, details=f"Role: {role.name} ({role.id})\nExpires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}",
            color=0x57D69A,
        )
        await interaction.followup.send(
            f"✅ {role.mention} {'extended for' if existing else 'granted to'} {member.mention} until {expiry.strftime('%Y-%m-%d %H:%M UTC')}.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )



temporaryrole_group = app_commands.Group(name="temporaryrole", description="Manage roles with automatic expiry")
raid_group = app_commands.Group(name="raid", description="Configure raid protection and human verification")


@temporaryrole_group.command(name="grant", description="Give a member a temporary role.")
@app_commands.guild_only()
async def temporaryrole_grant(interaction, member: discord.Member, role: discord.Role, duration: str, reason: app_commands.Range[str, 1, 500]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None:
        await interaction.followup.send("Security system unavailable.", ephemeral=True)
        return
    await cog.grant_role(interaction, member, role, duration, reason)


@temporaryrole_group.command(name="remove", description="Remove a member's active temporary role.")
@app_commands.guild_only()
async def temporaryrole_remove(interaction, member: discord.Member, role: discord.Role, reason: str = "Removed by moderator"):
    await interaction.response.defer(ephemeral=True, thinking=True)
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None or cog.pool() is None:
        await interaction.followup.send("Security database unavailable.", ephemeral=True)
        return
    error = cog.role_error(interaction, member, role)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return
    row = await cog.pool().fetchrow(
        "SELECT id FROM pvpclub_temporary_roles WHERE guild_id=$1 AND member_id=$2 AND role_id=$3 AND status IN ('active','granting')",
        interaction.guild_id, member.id, role.id,
    )
    if not row:
        await interaction.followup.send("No active temporary assignment was found.", ephemeral=True)
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason=f"Temporary role manually removed: {reason[:400]}")
        await cog.pool().execute(
            "UPDATE pvpclub_temporary_roles SET status='manual_removed',updated_at=NOW(),last_error=NULL WHERE id=$1 AND status IN ('active','granting')",
            int(row["id"]),
        )
    except discord.HTTPException:
        log.exception("Manual temporary-role removal failed.")
        await interaction.followup.send("Discord refused to remove the role. The record remains active.", ephemeral=True)
        return
    await cog.modlog(interaction.guild, "Temporary role manually removed", str(member), member.id, str(interaction.user), interaction.user.id, reason, details=f"Role: {role.name} ({role.id})")
    await interaction.followup.send(f"✅ Removed {role.mention} from {member.mention}.", ephemeral=True)


@temporaryrole_group.command(name="list", description="List active temporary role assignments.")
@app_commands.guild_only()
async def temporaryrole_list(interaction, member: discord.Member | None = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    if interaction.user.id != interaction.guild.owner_id and not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Manage Roles is required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None or cog.pool() is None:
        await interaction.followup.send("Security database unavailable.", ephemeral=True)
        return
    if member:
        rows = await cog.pool().fetch(
            """SELECT member_id,role_id,expires_at,reason FROM pvpclub_temporary_roles
               WHERE guild_id=$1 AND member_id=$2 AND status IN ('active','granting') ORDER BY expires_at LIMIT 25""",
            interaction.guild_id, member.id,
        )
    else:
        rows = await cog.pool().fetch(
            """SELECT member_id,role_id,expires_at,reason FROM pvpclub_temporary_roles
               WHERE guild_id=$1 AND status IN ('active','granting') ORDER BY expires_at LIMIT 25""",
            interaction.guild_id,
        )
    if not rows:
        await interaction.followup.send("No active temporary roles found.", ephemeral=True)
        return
    lines = []
    for row in rows:
        target = interaction.guild.get_member(int(row["member_id"]))
        role = interaction.guild.get_role(int(row["role_id"]))
        expiry = _utc(row["expires_at"])
        target_text = target.mention if target else str(row["member_id"])
        role_text = role.mention if role else "Deleted role " + str(row["role_id"])
        expiry_text = expiry.strftime("%Y-%m-%d %H:%M UTC") if expiry else "unknown"
        lines.append(f"{target_text} — {role_text}\nExpires: {expiry_text}\nReason: {(row['reason'] or 'Not specified')[:100]}")
    await interaction.followup.send(embed=discord.Embed(title="⏳ Active temporary roles", description="\n\n".join(lines)[:4000], color=0x55DDFB), ephemeral=True)


@raid_group.command(name="setup", description="Configure the verification channel and join threshold.")
@app_commands.guild_only()
async def raid_setup(interaction, verification_channel: discord.TextChannel, threshold: app_commands.Range[int, 2, 50] = 10, window_seconds: app_commands.Range[int, 3, 60] = 10, protection_minutes: app_commands.Range[int, 1, 120] = 10, kick_after_minutes: app_commands.Range[int, 0, 1440] = 0):
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the actual server owner can configure Raid Protection.", ephemeral=True)
        return
    perms = verification_channel.permissions_for(interaction.guild.me)
    if not interaction.guild.me.guild_permissions.manage_roles or not perms.view_channel or not perms.send_messages or not perms.embed_links:
        await interaction.response.send_message("I need Manage Roles and View Channel, Send Messages and Embed Links in the verification channel.", ephemeral=True)
        return
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None:
        await interaction.response.send_message("Security system unavailable.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    config = cog.raid_config(interaction.guild.id)
    config.update({
        "enabled": True, "verification_channel_id": verification_channel.id,
        "threshold": int(threshold), "window_seconds": int(window_seconds),
        "protection_minutes": int(protection_minutes), "kick_after_minutes": int(kick_after_minutes),
    })
    await cog.bot.store.save()
    await interaction.followup.send(
        f"✅ Raid Protection configured and enabled.\nTrigger: {threshold} human joins / {window_seconds}s\n"
        f"Protection window: {protection_minutes}m\nVerification: {verification_channel.mention}\n"
        f"Kick after expiry: {str(kick_after_minutes)+'m' if kick_after_minutes else 'Disabled'}",
        ephemeral=True,
    )


@raid_group.command(name="enable", description="Enable automatic raid detection.")
@app_commands.guild_only()
async def raid_enable(interaction):
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the actual server owner can manage Raid Protection.", ephemeral=True)
        return
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None:
        await interaction.response.send_message("Security system unavailable.", ephemeral=True)
        return
    config = cog.raid_config(interaction.guild.id)
    channel_id = config.get("verification_channel_id")
    if not channel_id or not isinstance(interaction.guild.get_channel(int(channel_id)), discord.TextChannel):
        await interaction.response.send_message("Run /raid setup first and choose a verification channel.", ephemeral=True)
        return
    config["enabled"] = True
    await cog.bot.store.save()
    await interaction.response.send_message("✅ Raid Protection enabled.", ephemeral=True)


@raid_group.command(name="disable", description="Disable raid detection for future joins.")
@app_commands.guild_only()
async def raid_disable(interaction):
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the actual server owner can manage Raid Protection.", ephemeral=True)
        return
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None:
        await interaction.response.send_message("Security system unavailable.", ephemeral=True)
        return
    config = cog.raid_config(interaction.guild.id)
    config["enabled"] = False
    config["active"] = False
    config["ends_at"] = None
    await cog.bot.store.save()
    await interaction.response.send_message("Raid Protection disabled for new joins. Existing quarantined members remain quarantined until verified.", ephemeral=True)


@raid_group.command(name="status", description="Inspect Raid Protection status.")
@app_commands.guild_only()
async def raid_status(interaction):
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the actual server owner can inspect Raid Protection.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    cog = interaction.client.get_cog("SecurityCog")
    if cog is None:
        await interaction.followup.send("Security system unavailable.", ephemeral=True)
        return
    config = cog.raid_config(interaction.guild.id)
    pending = 0
    failed = 0
    if cog.pool():
        pending = int(await cog.pool().fetchval("SELECT COUNT(*) FROM pvpclub_raid_verifications WHERE guild_id=$1 AND status IN ('pending','expired')", interaction.guild.id) or 0)
        failed = int(await cog.pool().fetchval("SELECT COUNT(*) FROM pvpclub_raid_verifications WHERE guild_id=$1 AND status='failed'", interaction.guild.id) or 0)
    ends = _utc(config.get("ends_at"))
    channel = interaction.guild.get_channel(int(config["verification_channel_id"])) if config.get("verification_channel_id") else None
    e = discord.Embed(title="🛡️ Raid Protection", color=0x55DDFB)
    e.add_field(name="Detection", value="Enabled" if config.get("enabled") else "Disabled", inline=True)
    e.add_field(name="Currently active", value="Yes" if config.get("active") and ends and ends > discord.utils.utcnow() else "No", inline=True)
    e.add_field(name="Join threshold", value=f"{config.get('threshold',10)} / {config.get('window_seconds',10)}s", inline=True)
    e.add_field(name="Verification channel", value=channel.mention if channel else "Not set", inline=True)
    e.add_field(name="Pending / expired", value=str(pending), inline=True)
    e.add_field(name="Attempts exhausted", value=str(failed), inline=True)
    e.add_field(name="Kick delay", value=f"{config.get('kick_after_minutes')}m" if config.get("kick_after_minutes") else "Disabled", inline=True)
    if ends and config.get("active"):
        e.add_field(name="Ends", value=discord.utils.format_dt(ends, style="R"), inline=True)
    await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot):
    cog = SecurityCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(temporaryrole_group)
    bot.tree.add_command(raid_group)

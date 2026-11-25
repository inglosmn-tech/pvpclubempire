"""XP and voice leveling system backed by normalized PostgreSQL rows."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("pvpclub-empire.leveling")
_bot = None

ROLE_MILESTONES = [
    (1, "Level 1", 0xA0A0A0),
    (5, "Level 5", 0xE67E22),
    (10, "Level 10", 0x42C7D7),
    (20, "Level 20", 0x77D6A2),
    (35, "Level 35", 0xE5B83B),
    (50, "Level 50", 0x8E44AD),
]
LEGACY_LEVEL_ROLE_NAMES = {
    "🧶 Stray Kitten", "🐾 Curious Cat", "🐟 Fish Hunter",
    "🌿 Catnip Addict", "🦁 Alley Lion", "👑 Purr Royalty",
}
LEGACY_LEVEL_CHANNEL_NAMES = {"🐾-level-ups", "meow-levels"}


def xp_required(level: int) -> int:
    return 5 * (max(0, int(level)) ** 2) + (50 * max(0, int(level))) + 100


def level_from_total_xp(total_xp: int) -> tuple[int, int, int]:
    remaining = max(0, int(total_xp))
    level = 0
    while remaining >= xp_required(level):
        remaining -= xp_required(level)
        level += 1
        if level > 10000:
            break
    return level, remaining, xp_required(level)


def level_embed(title: str, description: str = "", color: int = 0x5865F2) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="PVPClub Empire • Leveling")
    return e


def progress_bar(current: int, needed: int, width: int = 10) -> str:
    filled = min(width, int((current / max(1, needed)) * width))
    return "█" * filled + "░" * (width - filled)


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LevelingCog", guild_id: int, page: int = 0):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.page = page
        self._refresh_buttons()

    def _refresh_buttons(self):
        for child in self.children:
            if child.custom_id == "levelboard:previous":
                child.disabled = self.page <= 0

    async def _render(self, interaction: discord.Interaction):
        rows = await self.cog.bot.store.get_level_leaderboard(self.guild_id, 10, self.page * 10)
        if not rows and self.page > 0:
            self.page -= 1
            rows = await self.cog.bot.store.get_level_leaderboard(self.guild_id, 10, self.page * 10)
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for index, row in enumerate(rows, start=self.page * 10 + 1):
            marker = medals[index - 1] if index <= 3 else f"`#{index}`"
            lines.append(f"{marker} <@{row['user_id']}> — **Level {row['level']}** · {row['total_xp']:,} XP")
        e = level_embed("🏆 Level Leaderboard", "\n".join(lines) or "No XP earned yet. Send messages or be active in voice!", 0xFF9CEE)
        e.set_footer(text=f"Page {self.page + 1} • PVPClub Empire")
        self._refresh_buttons()
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="levelboard:previous")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This leaderboard belongs to another server.", ephemeral=True)
            return
        self.page = max(0, self.page - 1)
        await self._render(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, custom_id="levelboard:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This leaderboard belongs to another server.", ephemeral=True)
            return
        self.page += 1
        await self._render(interaction)


class LevelResetView(discord.ui.View):
    def __init__(self, cog: "LevelingCog", guild: discord.Guild, actor_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild = guild
        self.actor_id = actor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Only the administrator who opened this reset can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Reset server XP", style=discord.ButtonStyle.danger)
    async def reset_xp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.bot.store.delete_guild_levels(self.guild.id)
        await interaction.response.edit_message(content="✅ Server XP has been reset. Your channels and level roles were kept.", embed=None, view=None)

    @discord.ui.button(label="Delete setup roles/channel", style=discord.ButtonStyle.secondary)
    async def delete_assets(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = self.cog.bot.store.data.get("level_config", {}).pop(str(self.guild.id), {})
        bot_member = self.guild.me
        deleted_roles, deleted_channel = 0, False
        if isinstance(config, dict):
            channel_id = config.get("channel_id") if config.get("generated_channel") else None
            channel = self.guild.get_channel(int(channel_id)) if channel_id else None
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.delete(reason="PVPClub Empire meow level reset")
                    deleted_channel = True
                except discord.HTTPException:
                    pass
            for role_id in config.get("generated_role_ids", []):
                role = self.guild.get_role(int(role_id)) if str(role_id).isdigit() else None
                if role and bot_member and role < bot_member.top_role:
                    try:
                        await role.delete(reason="PVPClub Empire meow level reset")
                        deleted_roles += 1
                    except discord.HTTPException:
                        pass
        await self.cog.bot.store.save()
        await interaction.response.edit_message(
            content=f"✅ Level setup configuration removed. Deleted {deleted_roles} roles and {'the' if deleted_channel else 'no'} level channel. XP data was kept.",
            embed=None, view=None,
        )




class DeadchatRewardView(discord.ui.View):
    """Persistent one-claim reward button for an active deadchat event."""
    def __init__(self, cog: "LevelingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim 5 XP", emoji="🎁", style=discord.ButtonStyle.success,
                       custom_id="deadchat:claim-xp")
    async def claim_xp(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This reward is only available in the server.", ephemeral=True)
            return
        config = self.cog._config(interaction.guild.id)
        event = config.get("deadchat_event") or {}
        now = time.time()
        if int(event.get("message_id", 0)) != interaction.message.id or float(event.get("active_until", 0)) <= now:
            await interaction.response.send_message("This deadchat XP event has ended.", ephemeral=True)
            return
        claimed = {int(x) for x in event.get("claimed", []) if str(x).isdigit()}
        if interaction.user.id in claimed:
            await interaction.response.send_message("You already claimed your 5 XP for this event!", ephemeral=True)
            return
        claimed.add(interaction.user.id)
        event["claimed"] = sorted(claimed)
        await self.cog.bot.store.save()
        await self.cog.award_xp(interaction.user, 5, voice=False)
        await interaction.response.send_message("You claimed **5 XP**! Keep the chat active for 3× XP.", ephemeral=True)


class LevelingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.text_cooldowns: dict[tuple[int, int], float] = {}
        self.deadchat_view = DeadchatRewardView(self)
        self.bot.add_view(self.deadchat_view)
        self.cat_invasion_locks: dict[int, asyncio.Lock] = {}
        self.voice_xp_loop.start()
        self.deadchat_watcher.start()
        self.cat_invasion_watcher.start()

    def cog_unload(self):
        self.voice_xp_loop.cancel()
        self.deadchat_watcher.cancel()
        self.cat_invasion_watcher.cancel()

    def _config(self, guild_id: int) -> dict:
        return self.bot.store.data.setdefault("level_config", {}).setdefault(str(guild_id), {})

    def _next_cat_invasion_at(self, *, initial: bool = False) -> float:
        """Schedule surprises during active hours in the Netherlands."""
        now = datetime.now(ZoneInfo("Europe/Amsterdam"))
        delay = timedelta(minutes=random.randint(15, 45)) if initial else timedelta(hours=random.uniform(3, 6))
        candidate = now + delay
        if candidate.hour < 10:
            candidate = candidate.replace(hour=10, minute=0, second=0, microsecond=0)
            candidate += timedelta(minutes=random.randint(0, 180))
        elif candidate.hour >= 22:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0
            ) + timedelta(minutes=random.randint(0, 180))
        return candidate.timestamp()

    def _xp_multiplier(self, member: discord.Member, config: dict) -> float:
        multipliers = config.get("multipliers", {})
        values = []
        for role in member.roles:
            try:
                value = float(multipliers.get(str(role.id), 1.0))
                if 0.1 <= value <= 5:
                    values.append(value)
            except (TypeError, ValueError):
                continue
        return max(values, default=1.0)

    async def process_message(self, message: discord.Message):
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        config = self._config(message.guild.id)
        await self.process_cat_invasion_message(message, config)
        if message.channel.id in {int(x) for x in config.get("ignored_channels", []) if str(x).isdigit()}:
            return
        ignored_names = {"afk", "bot-commands"}
        if message.channel.name.casefold() in ignored_names:
            return
        ignored_roles = {int(x) for x in config.get("ignored_roles", []) if str(x).isdigit()}
        if any(role.id in ignored_roles for role in message.author.roles):
            return
        deadchat = config.get("deadchat") or {}
        if int(deadchat.get("channel_id", 0) or 0) == message.channel.id:
            deadchat["last_activity_at"] = time.time()
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.text_cooldowns.get(key, 0.0) < 60:
            return
        self.text_cooldowns[key] = now
        # Bound the in-memory cooldown cache: stale users can be removed safely
        # because the cooldown is only 60 seconds long.
        if len(self.text_cooldowns) > 2000:
            cutoff = now - 60.0
            self.text_cooldowns = {k: stamp for k, stamp in self.text_cooldowns.items() if stamp >= cutoff}
        event = config.get("deadchat_event") or {}
        deadchat_boost = 3 if float(event.get("active_until", 0)) > time.time() else 1
        # award_xp applies configured role multipliers once; do not apply them here too.
        amount = max(1, random.randint(15, 25) * deadchat_boost)
        await self.award_xp(message.author, amount, voice=False)

    async def award_xp(self, member: discord.Member, amount: int, *, voice: bool, voice_seconds: int = 0):
        before = await self.bot.store.get_level(member.guild.id, member.id)
        config = self._config(member.guild.id)
        adjusted = max(1, int(amount * self._xp_multiplier(member, config)))
        after = await self.bot.store.add_xp(member.guild.id, member.id, adjusted, voice=voice, voice_seconds=voice_seconds)
        if after["level"] > before["level"]:
            await self._assign_milestone_roles(member, after["level"], config)
            channel_id = config.get("channel_id")
            channel = member.guild.get_channel(int(channel_id)) if channel_id else None
            if isinstance(channel, discord.TextChannel):
                e = level_embed("🎉 Level Up!", f"{member.mention} reached **Level {after['level']}**!\nYou have earned **{after['total_xp'] - before['total_xp']} XP**. Keep collecting catnip! 🐾", 0x57D69A)
                e.set_thumbnail(url=member.display_avatar.url)
                try:
                    await channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException:
                    log.exception("Could not send level-up notice in guild %s", member.guild.id)
        elif after["level"] > 0:
            await self._assign_milestone_roles(member, after["level"], config)

    async def _assign_milestone_roles(self, member: discord.Member, level: int, config: dict):
        role_ids = config.get("roles", {})
        bot_member = member.guild.me
        if not bot_member or not bot_member.guild_permissions.manage_roles:
            return
        reached = []
        for milestone_text, role_id in role_ids.items():
            try:
                milestone = int(milestone_text)
                role = member.guild.get_role(int(role_id))
            except (TypeError, ValueError):
                continue
            if milestone <= level and role and role < bot_member.top_role:
                reached.append((milestone, role))
        reached.sort(key=lambda item: item[0])
        managed = {member.guild.get_role(int(value)) for value in role_ids.values() if str(value).isdigit()}
        managed.discard(None)
        mode = config.get("role_mode", "replace")
        desired = {role for _, role in reached} if mode == "stack" else ({reached[-1][1]} if reached else set())
        to_add = desired - set(member.roles)
        to_remove = (managed & set(member.roles)) - desired
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="PVPClub Empire level role update")
            if to_add:
                await member.add_roles(*to_add, reason="PVPClub Empire level milestone")
        except discord.HTTPException:
            log.warning("Could not update level roles for %s in guild %s", member.id, member.guild.id)

    @app_commands.command(name="meow-setup", description="Automatically set up the meow leveling system.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meow_setup(self, interaction: discord.Interaction):
        guild = interaction.guild
        bot_member = guild.me
        if not bot_member or not bot_member.guild_permissions.manage_channels or not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message("I need Manage Channels and Manage Roles. Enable both and try again.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = self._config(guild.id)
        channel = guild.get_channel(int(config.get("channel_id", 0) or 0)) if config.get("channel_id") else None
        channel_created = bool(config.get("generated_channel", False))
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(guild.text_channels, name="🐾-level-ups") or discord.utils.get(guild.text_channels, name="meow-levels")
            if channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False, add_reactions=True),
                    bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_messages=True),
                }
                channel = await guild.create_text_channel("🐾-level-ups", overwrites=overwrites, reason="PVPClub Empire meow leveling setup")
                channel_created = True
        config["generated_channel"] = bool(channel_created)
        config["channel_id"] = channel.id
        roles_created, roles_existing = [], []
        generated_role_ids = set(int(x) for x in config.get("generated_role_ids", []) if str(x).isdigit())
        role_map = config.setdefault("roles", {})
        for milestone, name, color in ROLE_MILESTONES:
            role = guild.get_role(int(role_map.get(str(milestone), 0))) if role_map.get(str(milestone)) else None
            role = role or discord.utils.get(guild.roles, name=name)
            if role is None:
                role = await guild.create_role(name=name, colour=discord.Colour(color), mentionable=False,
                                               reason="PVPClub Empire level milestone role")
                roles_created.append(name)
                generated_role_ids.add(role.id)
            else:
                roles_existing.append(name)
            role_map[str(milestone)] = role.id
        config.setdefault("role_mode", "replace")
        config.setdefault("text_cooldown", 60)
        config.setdefault("voice_enabled", True)
        ignored = set(int(x) for x in config.get("ignored_channels", []) if str(x).isdigit())
        ignored.update(ch.id for ch in guild.text_channels if ch.name.casefold() in {"afk", "bot-commands"})
        if guild.afk_channel:
            ignored.add(guild.afk_channel.id)
        config["ignored_channels"] = sorted(ignored)
        config.setdefault("ignored_roles", [])
        config.setdefault("multipliers", {})
        config["generated_role_ids"] = sorted(generated_role_ids)
        await self.bot.store.save()
        note = "\n".join([
            f"**Level-up channel:** {channel.mention} {'(created)' if channel_created else '(existing)'}",
            f"**Created roles:** {', '.join(roles_created) if roles_created else 'None'}",
            f"**Existing roles:** {', '.join(roles_existing) if roles_existing else 'None'}",
            "**Defaults:** 15–25 XP/message · 60s text cooldown · voice XP every 60s · role mode: replace",
            "**Ignored:** channels named `afk` and `bot-commands`, plus the server AFK channel",
            "**Role hierarchy:** move my highest role above the six generated cat roles so I can assign them.",
        ])
        await interaction.followup.send(embed=level_embed("🐾 Meow leveling setup complete", note, 0x57D69A), ephemeral=True)

    @app_commands.command(name="meow-reset", description="Reset server leveling data or remove generated setup assets.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meow_reset(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=level_embed("⚠️ Reset meow leveling?", "Choose an action below. Resetting XP cannot be undone."),
            view=LevelResetView(self, interaction.guild, interaction.user.id), ephemeral=True,
        )

    @app_commands.command(name="rank", description="View your or another member's meow level and XP.")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        data = await self.bot.store.get_level(interaction.guild_id, target.id)
        level, in_level, required = level_from_total_xp(data["total_xp"])
        rank_num = await self.bot.store.get_level_rank(interaction.guild_id, target.id)
        config = self._config(interaction.guild_id)
        role_ids = config.get("roles", {})
        current_role = "None"
        reached = []
        for milestone_text, role_id in role_ids.items():
            try:
                milestone = int(milestone_text)
                role = interaction.guild.get_role(int(role_id))
            except (TypeError, ValueError):
                continue
            if milestone <= level and role:
                reached.append((milestone, role))
        if reached:
            milestone, role = max(reached, key=lambda item: item[0])
            current_role = role.mention
        voice_seconds = data["voice_seconds"]
        voice_time = f"{voice_seconds // 3600}h {(voice_seconds % 3600) // 60}m"
        e = level_embed(f"🐱 {target.display_name} • Cat Rank", color=0xFF9CEE)
        e.set_thumbnail(url=target.display_avatar.url)
        e.add_field(name="Level", value=f"**{level}**", inline=True)
        e.add_field(name="Server rank", value=f"**#{rank_num}**" if rank_num else "Unranked", inline=True)
        e.add_field(name="Cat role", value=current_role, inline=True)
        e.add_field(name="XP progress", value=f"{progress_bar(in_level, required)}\n**{in_level:,} / {required:,} XP** to next level", inline=False)
        e.add_field(name="Lifetime XP", value=f"{data['total_xp']:,}", inline=True)
        e.add_field(name="Voice time / XP", value=f"{voice_time} / {data['voice_xp']:,} XP", inline=True)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="leaderboard", description="View the server's top meow levels.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.store.get_level_leaderboard(interaction.guild_id, 10, 0)
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for index, row in enumerate(rows, 1):
            marker = medals[index - 1] if index <= 3 else f"`#{index}`"
            lines.append(f"{marker} <@{row['user_id']}> — **Level {row['level']}** · {row['total_xp']:,} XP")
        e = level_embed("🏆 Empire Catnip Leaderboard", "\n".join(lines) or "No XP yet. Be the first kitten! 🐾")
        await interaction.response.send_message(embed=e, view=LeaderboardView(self, interaction.guild_id))


    @commands.command(name="deadchat")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def deadchat(self, ctx: commands.Context, role: discord.Role):
        """Configure daily deadchat pings in the channel where this command is used."""
        if role.is_default() or role.managed:
            await ctx.reply("Choose a normal, mentionable server role.")
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("Use this command in a text channel.")
            return
        config = self._config(ctx.guild.id)
        config["deadchat"] = {
            "channel_id": ctx.channel.id,
            "role_id": role.id,
            "timezone": "Europe/Amsterdam",
            "enabled": True,
            "last_activity_at": time.time(),
            "schedule_date": None,
            "schedule_hours": [],
            "sent_today": [],
        }
        await self.bot.store.save()
        await ctx.reply(
            f"✅ Deadchat is set up in {ctx.channel.mention} for {role.mention}. "
            "I'll check between 15:00 and 23:00 (Amsterdam time), up to twice per day, "
            "and only ping after at least 1 hour without a message. Each ping starts 30 minutes of 3× message XP."
        )

    @commands.command(name="deadchatstop")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def deadchatstop(self, ctx: commands.Context):
        config = self._config(ctx.guild.id)
        deadchat = config.get("deadchat") or {}
        deadchat["enabled"] = False
        await self.bot.store.save()
        await ctx.reply("🛑 Deadchat pings are disabled.")

    def _deadchat_schedule(self, deadchat: dict, today):
        """Choose one or two daily target times; persisted so restarts don't reroll them."""
        if deadchat.get("schedule_date") == today.isoformat():
            return
        count = random.choice((1, 2))
        hours = sorted(random.sample(range(15, 23), count))
        if len(hours) == 2 and hours[1] - hours[0] < 2:
            hours[1] = min(22, hours[0] + 2)
        deadchat["schedule_date"] = today.isoformat()
        deadchat["schedule_hours"] = sorted(set(hours))
        deadchat["sent_today"] = []

    @tasks.loop(seconds=60)
    async def deadchat_watcher(self):
        """Check the configured channel for one-hour quiet periods during 15:00–23:00."""
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Europe/Amsterdam")
        now_dt = datetime.now(zone)
        now_ts = time.time()
        for guild in list(self.bot.guilds):
            config = self._config(guild.id)
            deadchat = config.get("deadchat") or {}
            if not deadchat.get("enabled"):
                continue
            self._deadchat_schedule(deadchat, now_dt.date())
            channel = guild.get_channel(int(deadchat.get("channel_id", 0) or 0))
            role = guild.get_role(int(deadchat.get("role_id", 0) or 0))
            if not isinstance(channel, discord.TextChannel) or role is None:
                continue
            # Expire the previous event and disable its button.
            event = config.get("deadchat_event") or {}
            if event and float(event.get("active_until", 0)) <= now_ts and not event.get("expired"):
                event["expired"] = True
                try:
                    message = await channel.fetch_message(int(event.get("message_id", 0)))
                    await message.edit(view=None)
                except (discord.HTTPException, discord.NotFound, ValueError):
                    pass
                await self.bot.store.save()

            if not (15 <= now_dt.hour <= 22):
                continue
            sent = {int(x) for x in deadchat.get("sent_today", []) if str(x).isdigit()}
            due_hours = [int(x) for x in deadchat.get("schedule_hours", []) if int(x) not in sent and now_dt.hour >= int(x)]
            if not due_hours or now_dt.hour >= 23:
                continue
            # Read Discord's latest message directly so activity during a bot restart
            # is never mistaken for an hour of silence.
            last_activity = 0.0
            try:
                async for latest_message in channel.history(limit=1):
                    last_activity = latest_message.created_at.timestamp()
                    break
            except discord.HTTPException:
                continue
            if last_activity <= 0 or now_ts - last_activity < 3600:
                continue
            previous_event = config.get("deadchat_event") or {}
            if float(previous_event.get("started_at", 0) or 0) and now_ts - float(previous_event.get("started_at", 0)) < 2 * 3600:
                continue
            # Record the daily slot before sending to prevent duplicate pings on retry/restart.
            slot = due_hours[0]
            sent.add(slot)
            deadchat["sent_today"] = sorted(sent)
            await self.bot.store.save()
            active_until = now_ts + 30 * 60
            event_id = f"{now_dt.date().isoformat()}-{slot}-{int(now_ts)}"
            embed = level_embed(
                "💀 DEAD CHAT DETECTED",
                f"{role.mention} the chat has been silent for over an hour!\n\n"
                "**3× XP is now active for 30 minutes.** Start talking, keep the chat alive, "
                "and press the button below to claim **5 bonus XP** once.",
                0xFF9CEE,
            )
            embed.set_footer(text="PVPClub Empire • Deadchat event")
            try:
                sent_message = await channel.send(
                    content=role.mention, embed=embed, view=self.deadchat_view,
                    allowed_mentions=discord.AllowedMentions(roles=[role]),
                )
            except discord.HTTPException:
                log.exception("Could not send deadchat ping in guild %s", guild.id)
                continue
            config["deadchat_event"] = {
                "event_id": event_id,
                "channel_id": channel.id,
                "message_id": sent_message.id,
                "started_at": now_ts,
                "active_until": active_until,
                "claimed": [],
                "expired": False,
            }
            await self.bot.store.save()

    @deadchat_watcher.before_loop
    async def before_deadchat_watcher(self):
        await self.bot.wait_until_ready()


    async def process_cat_invasion_message(self, message: discord.Message, config: dict | None = None):
        """Award the first member who answers an active invasion with exactly MEOW."""
        if not message.guild or not isinstance(message.author, discord.Member):
            return
        if (message.content or "").strip().casefold() != "meow":
            return
        config = config if config is not None else self._config(message.guild.id)
        invasion = config.get("cat_invasion") or {}
        if not invasion.get("enabled") or int(invasion.get("channel_id", 0) or 0) != message.channel.id:
            return
        lock = self.cat_invasion_locks.setdefault(message.guild.id, asyncio.Lock())
        won = False
        async with lock:
            invasion = config.get("cat_invasion") or {}
            event = invasion.get("active_event") or {}
            now = time.time()
            if event and float(event.get("expires_at", 0)) > now and not event.get("winner_id"):
                event["winner_id"] = message.author.id
                event["won_at"] = now
                invasion["active_event"] = None
                invasion["next_at"] = self._next_cat_invasion_at()
                await self.bot.store.save()
                won = True
        if won:
            try:
                await self.award_xp(message.author, 25, voice=False)
                await message.reply(
                    f"🐱 **CAT INVASION!** {message.author.mention} was the first to MEOW and wins **25 bonus XP**!",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions(users=[message.author]),
                )
            except Exception:
                log.exception("Could not award Cat Invasion XP in guild %s", message.guild.id)

    @commands.command(name="catinvasion")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def catinvasion(self, ctx: commands.Context):
        """Enable random Cat Invasion events in the channel where the command is used."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("Use this command in a text channel.")
            return
        config = self._config(ctx.guild.id)
        config["cat_invasion"] = {
            "enabled": True,
            "channel_id": ctx.channel.id,
            "next_at": self._next_cat_invasion_at(initial=True),
            "active_event": None,
        }
        await self.bot.store.save()
        await ctx.reply(
            f"🐱 **Cat Invasion enabled** in {ctx.channel.mention}! "
            "A cat will randomly appear every 3–6 hours (between 10:00 and 22:00, Amsterdam time). "
            "The first person to type exactly MEOW within 60 seconds wins **25 bonus XP**."
        )

    @commands.command(name="catinvasionstop")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def catinvasionstop(self, ctx: commands.Context):
        config = self._config(ctx.guild.id)
        invasion = config.get("cat_invasion") or {}
        invasion["enabled"] = False
        invasion["active_event"] = None
        await self.bot.store.save()
        await ctx.reply("🛑 Cat Invasion has been disabled.")

    @tasks.loop(seconds=30)
    async def cat_invasion_watcher(self):
        """Spawn scheduled cat events while keeping the timing randomized and persistent."""
        now = time.time()
        local_now = datetime.now(ZoneInfo("Europe/Amsterdam"))
        for guild in list(self.bot.guilds):
            config = self._config(guild.id)
            invasion = config.get("cat_invasion") or {}
            if not invasion.get("enabled"):
                continue
            channel = guild.get_channel(int(invasion.get("channel_id", 0) or 0))
            if not isinstance(channel, discord.TextChannel):
                continue
            event = invasion.get("active_event") or {}
            if event:
                if float(event.get("expires_at", 0)) > now:
                    continue
                invasion["active_event"] = None
                invasion["next_at"] = self._next_cat_invasion_at()
                await self.bot.store.save()
                continue
            if float(invasion.get("next_at", 0) or 0) > now:
                continue
            if not (10 <= local_now.hour < 22):
                invasion["next_at"] = self._next_cat_invasion_at()
                await self.bot.store.save()
                continue
            embed = level_embed(
                "🐈 CAT INVASION!",
                "A wild cat has appeared in the chat!\n\n"
                "Type MEOW within 60 seconds.\n"
                "The first person to answer wins 25 bonus XP. No spam needed — one clean MEOW!",
                0xFF9CEE,
            )
            try:
                sent = await channel.send(embed=embed)
            except discord.HTTPException:
                log.exception("Could not start Cat Invasion in guild %s", guild.id)
                invasion["next_at"] = now + 300
                await self.bot.store.save()
                continue
            invasion["active_event"] = {
                "event_id": f"{guild.id}-{sent.id}",
                "message_id": sent.id,
                "started_at": now,
                "expires_at": now + 60,
                "winner_id": None,
            }
            await self.bot.store.save()

    @cat_invasion_watcher.before_loop
    async def before_cat_invasion_watcher(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def voice_xp_loop(self):
        for guild in list(self.bot.guilds):
            config = self._config(guild.id)
            if not config.get("voice_enabled", True):
                continue
            ignored_channels = {int(x) for x in config.get("ignored_channels", []) if str(x).isdigit()}
            for channel in guild.voice_channels:
                if guild.afk_channel and channel.id == guild.afk_channel.id:
                    continue
                if channel.id in ignored_channels:
                    continue
                eligible = [member for member in channel.members
                            if not member.bot
                            and member.voice is not None
                            and not member.voice.self_mute
                            and not member.voice.mute
                            and not member.voice.self_deaf
                            and not member.voice.deaf]
                # Require at least two actively unmuted humans in the channel.
                if len(eligible) < 2:
                    continue
                for member in eligible:
                    try:
                        await self.award_xp(member, random.randint(5, 10), voice=True, voice_seconds=60)
                    except Exception:
                        log.exception("Voice XP award failed for %s", member.id)

    @voice_xp_loop.before_loop
    async def before_voice_xp_loop(self):
        await self.bot.wait_until_ready()


level_admin = app_commands.Group(name="level-admin", description="Admin controls for server leveling")


def _admin_check(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild)


@level_admin.command(name="set-level", description="Set a member's level (progress resets to the start of that level).")
async def level_set_level(interaction: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, 10000]):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    total = sum(xp_required(x) for x in range(level))
    result = await _bot.store.set_level_total(interaction.guild_id, member.id, total)
    cog = _bot.get_cog("LevelingCog")
    if cog:
        await cog._assign_milestone_roles(member, result["level"], cog._config(interaction.guild_id))
    await interaction.response.send_message(f"Set {member.mention} to **Level {result['level']}**.", ephemeral=True)


@level_admin.command(name="set-xp", description="Set a member's lifetime total XP.")
async def level_set_xp(interaction: discord.Interaction, member: discord.Member, total_xp: app_commands.Range[int, 0, 1000000000]):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    result = await _bot.store.set_level_total(interaction.guild_id, member.id, total_xp)
    cog = _bot.get_cog("LevelingCog")
    if cog:
        await cog._assign_milestone_roles(member, result["level"], cog._config(interaction.guild_id))
    await interaction.response.send_message(f"Set {member.mention} to **{result['total_xp']:,} XP (Level {result['level']})**.", ephemeral=True)


@level_admin.command(name="add-role", description="Link a Discord role to a level milestone.")
async def level_add_role(interaction: discord.Interaction, level: app_commands.Range[int, 1, 10000], role: discord.Role):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    if role.is_default() or role.managed:
        await interaction.response.send_message("That role cannot be used as a level reward.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("level_config", {}).setdefault(str(interaction.guild_id), {})
    config.setdefault("roles", {})[str(level)] = role.id
    await _bot.store.save()
    await interaction.response.send_message(f"Linked {role.mention} to level {level}.", ephemeral=True)


@level_admin.command(name="remove-role", description="Remove a level-to-role reward mapping.")
async def level_remove_role(interaction: discord.Interaction, level: app_commands.Range[int, 1, 10000]):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("level_config", {}).setdefault(str(interaction.guild_id), {})
    removed = config.setdefault("roles", {}).pop(str(level), None)
    await _bot.store.save()
    await interaction.response.send_message("Removed the role mapping." if removed else "No role was mapped to that level.", ephemeral=True)


@level_admin.command(name="role-mode", description="Choose whether milestone roles stack or replace each other.")
@app_commands.choices(mode=[app_commands.Choice(name="Stack", value="stack"), app_commands.Choice(name="Replace", value="replace")])
async def level_role_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("level_config", {}).setdefault(str(interaction.guild_id), {})
    config["role_mode"] = mode.value
    await _bot.store.save()
    await interaction.response.send_message(f"Level role mode is now **{mode.value}**.", ephemeral=True)


@level_admin.command(name="ignore", description="Add or remove a channel or role from XP exclusions.")
@app_commands.choices(target_type=[app_commands.Choice(name="Channel", value="channel"), app_commands.Choice(name="Role", value="role")],
                      action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
async def level_ignore(interaction: discord.Interaction, target_type: app_commands.Choice[str], action: app_commands.Choice[str], channel: discord.TextChannel | None = None, role: discord.Role | None = None):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    target = channel if target_type.value == "channel" else role
    if target is None:
        await interaction.response.send_message("Select the matching channel or role option.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("level_config", {}).setdefault(str(interaction.guild_id), {})
    key = "ignored_channels" if target_type.value == "channel" else "ignored_roles"
    values = set(int(x) for x in config.setdefault(key, []) if str(x).isdigit())
    if action.value == "add": values.add(target.id)
    else: values.discard(target.id)
    config[key] = sorted(values)
    await _bot.store.save()
    await interaction.response.send_message(f"Updated XP exclusion for {getattr(target, 'mention', target.name)}.", ephemeral=True)


@level_admin.command(name="multiplier", description="Set an XP multiplier for a role (0.1–5.0).")
async def level_multiplier(interaction: discord.Interaction, role: discord.Role, amount: app_commands.Range[float, 0.1, 5.0]):
    if not _admin_check(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("level_config", {}).setdefault(str(interaction.guild_id), {})
    config.setdefault("multipliers", {})[str(role.id)] = float(amount)
    await _bot.store.save()
    await interaction.response.send_message(f"{role.mention} now has an XP multiplier of **{amount:.2f}x**.", ephemeral=True)


async def setup(bot):
    global _bot
    _bot = bot
    await bot.add_cog(LevelingCog(bot))
    bot.tree.add_command(level_admin)

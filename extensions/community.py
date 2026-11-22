"""Community utilities: meows, birthdays, suggestions, welcome, autoroles and logs."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("pvpclub-empire.community")

MEOW_ROLE_SPECS = {
    "meow": ("Meow", 0xFF9CEE),
    "rookie": ("Meow Rookie", 0xA0A0A0),
    "enthusiast": ("Meow Enthusiast", 0xE67E22),
    "master": ("Meow Master", 0x42C7D7),
    "legend": ("Meow Legend", 0xE5B83B),
    "king": ("meowking", 0x8E44AD),
}
MEOW_THRESHOLDS = [(250, "legend"), (100, "master"), (50, "enthusiast"), (10, "rookie")]


def make_embed(title: str, description: str = "", color: int = 0xFF9CEE) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="PVPClub Empire • meow :3")
    return e


class SuggestionReviewView(discord.ui.View):
    def __init__(self, *, disabled: bool = False):
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                child.disabled = True

    async def _decide(self, interaction: discord.Interaction, status: str, color: int):
        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.manage_messages
            or interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message("Only moderators can review suggestions.", ephemeral=True)
            return
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("This suggestion message is unavailable.", ephemeral=True)
            return
        old = interaction.message.embeds[0]
        new = discord.Embed.from_dict(old.to_dict())
        new.color = color
        new.add_field(name="Review status", value=f"**{status}** • reviewed by {interaction.user.mention}", inline=False)
        new.timestamp = discord.utils.utcnow()
        # Replace only this message's view; never mutate the shared persistent view.
        await interaction.response.edit_message(embed=new, view=SuggestionReviewView(disabled=True))

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="suggestion:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, "Approved", 0x57D69A)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="suggestion:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, "Rejected", 0xFF647C)


class CommunityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.meow_response_last: dict[int, float] = {}

    async def cog_load(self):
        # Re-register persistent suggestion review buttons after bot restarts.
        self.bot.add_view(SuggestionReviewView())
        self.birthday_watcher.start()
        self.storage_watcher.start()

    def cog_unload(self):
        self.birthday_watcher.cancel()
        self.storage_watcher.cancel()

    async def process_meow_message(self, message: discord.Message):
        """Count substring hits, but award the base role/streak only on exact 'meow'."""
        if not message.guild or message.author.bot:
            return
        content = (message.content or "").strip()
        contains_meow = "meow" in content.casefold()
        exact_meow = content.casefold() == "meow"
        if not contains_meow:
            return

        try:
            total = await self.bot.store.increment_meows(message.author.id, 1)
        except Exception:
            log.exception("Could not persist meow count for %s", message.author.id)
            return

        if exact_meow:
            try:
                await self.bot.store.record_meow_day(message.guild.id, message.author.id)
            except Exception:
                log.exception("Could not persist meow streak for %s", message.author.id)

            role_config = self.bot.store.data.get("meow_role_config", {}).get(str(message.guild.id), {})
            role_ids = role_config.get("roles", {}) if isinstance(role_config, dict) else {}
            meow_role_id = role_ids.get("meow")
            role = message.guild.get_role(int(meow_role_id)) if meow_role_id else discord.utils.get(message.guild.roles, name="Meow")
            if role and role not in message.author.roles:
                if message.guild.me and role < message.guild.me.top_role:
                    try:
                        await message.author.add_roles(role, reason="Member sent exactly 'meow'")
                    except discord.HTTPException:
                        log.warning("Could not assign the Meow role in guild %s", message.guild.id)

        await self._sync_meow_milestone_roles(message.author, total)
        await self._sync_meowking(message.guild, candidate_id=message.author.id, candidate_total=total)

        if exact_meow:
            now = time.monotonic()
            last = self.meow_response_last.get(message.channel.id, 0.0)
            if now - last >= 3.0:
                self.meow_response_last[message.channel.id] = now
                try:
                    await message.reply("meow", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass

    async def _sync_meow_milestone_roles(self, member: discord.Member, total: int):
        config = self.bot.store.data.get("meow_role_config", {}).get(str(member.guild.id), {})
        role_ids = config.get("roles", {}) if isinstance(config, dict) else {}
        if not role_ids:
            return
        bot_member = member.guild.me
        if not bot_member or not bot_member.guild_permissions.manage_roles:
            return
        desired_key = next((key for threshold, key in MEOW_THRESHOLDS if total >= threshold), None)
        managed_keys = {"rookie", "enthusiast", "master", "legend"}
        desired_role = member.guild.get_role(int(role_ids[desired_key])) if desired_key and role_ids.get(desired_key) else None
        for key in managed_keys:
            role_id = role_ids.get(key)
            role = member.guild.get_role(int(role_id)) if role_id else None
            if not role or role >= bot_member.top_role:
                continue
            try:
                if role == desired_role and role not in member.roles:
                    await member.add_roles(role, reason="Meow milestone reached")
                elif role != desired_role and role in member.roles:
                    await member.remove_roles(role, reason="Meow milestone role updated")
            except discord.HTTPException:
                log.warning("Could not update meow milestone role %s in guild %s", key, member.guild.id)

    async def _sync_meowking(self, guild: discord.Guild, *, candidate_id: int | None = None,
                             candidate_total: int | None = None):
        config = self.bot.store.data.get("meow_role_config", {}).get(str(guild.id), {})
        role_ids = config.get("roles", {}) if isinstance(config, dict) else {}
        king_role = guild.get_role(int(role_ids.get("king", 0))) if role_ids.get("king") else None
        bot_member = guild.me
        if not king_role or not bot_member or king_role >= bot_member.top_role:
            return
        old_king_id = config.get("king_id")
        # Most meows do not change the #1 spot. Keep the known leader count in
        # memory and avoid a leaderboard SELECT unless a member can overtake it.
        if candidate_id is not None and candidate_total is not None and old_king_id is not None:
            known_king_count = int(config.get("king_count", 0) or 0)
            if int(candidate_id) == int(old_king_id):
                config["king_count"] = int(candidate_total)
                return
            if int(candidate_total) <= known_king_count:
                return
        leaders = await self.bot.store.meow_leaderboard(1)
        new_king_id = leaders[0][0] if leaders else None
        new_king_count = leaders[0][1] if leaders else 0
        old_member = guild.get_member(int(old_king_id)) if old_king_id else None
        new_member = guild.get_member(int(new_king_id)) if new_king_id else None
        if old_member is None and old_king_id:
            try:
                old_member = await guild.fetch_member(int(old_king_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                old_member = None
        if new_member is None and new_king_id:
            try:
                new_member = await guild.fetch_member(int(new_king_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                new_member = None
        if old_king_id == new_king_id and (new_king_id is None or (new_member and king_role in new_member.roles)):
            return
        try:
            if old_member and king_role in old_member.roles:
                await old_member.remove_roles(king_role, reason="Meow leaderboard leader changed")
            if new_member and king_role not in new_member.roles:
                await new_member.add_roles(king_role, reason="Current #1 on the meow leaderboard")
            config["king_id"] = new_king_id
            config["king_count"] = int(new_king_count)
            await self.bot.store.save()
        except discord.HTTPException:
            log.warning("Could not update meowking role in guild %s", guild.id)

    @commands.command(name="meowrolessetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def meowrolessetup(self, ctx: commands.Context):
        guild = ctx.guild
        config = self.bot.store.data.setdefault("meow_role_config", {}).setdefault(str(guild.id), {"roles": {}})
        role_map = config.setdefault("roles", {})
        created, existing = [], []
        for key, (name, color) in MEOW_ROLE_SPECS.items():
            role = guild.get_role(int(role_map.get(key, 0))) if role_map.get(key) else None
            role = role or discord.utils.get(guild.roles, name=name)
            if role is None:
                try:
                    role = await guild.create_role(name=name, colour=discord.Colour(color), mentionable=False,
                                                   reason="PVPClub Empire meow role setup")
                    created.append(name)
                except discord.HTTPException:
                    log.exception("Could not create meow role %s", name)
                    continue
            else:
                existing.append(name)
            role_map[key] = role.id
        await self.bot.store.save()
        await self._sync_meowking(guild)
        detail = []
        if created:
            detail.append("Created: " + ", ".join(f"`{x}`" for x in created))
        if existing:
            detail.append("Already existed: " + ", ".join(f"`{x}`" for x in existing))
        detail.append("The `Meow` role is assigned only when a member sends exactly `meow`.")
        detail.append("`meowking` is assigned to the current #1 lifetime meower in this bot.")
        detail.append("Move the bot's highest role above these roles if it cannot assign them.")
        await ctx.reply("🐾 **Meow roles configured**\n" + "\n".join(detail), mention_author=False)

    @meowrolessetup.error
    async def meowrolessetup_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You need Manage Roles to run this setup.", mention_author=False)
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.reply("I need Manage Roles. Move my bot role above the roles I should manage.", mention_author=False)
        else:
            log.exception("?meowrolessetup failed", exc_info=error)
            await ctx.reply("I couldn't finish the meow role setup.", mention_author=False)

    @commands.command(name="suggestsetup")
    @commands.guild_only()
    @commands.check_any(
        commands.has_guild_permissions(manage_messages=True),
        commands.has_guild_permissions(manage_guild=True),
    )
    async def suggestsetup(self, ctx: commands.Context, channel: discord.TextChannel):
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            await ctx.reply("I need Send Messages and Embed Links in that channel.", mention_author=False)
            return
        self.bot.store.data.setdefault("suggestion_channels", {})[str(ctx.guild.id)] = channel.id
        await self.bot.store.save()
        await ctx.reply(f"✅ Members can now use `/suggest` to post suggestions in {channel.mention}. Moderators can approve or reject them.", mention_author=False)

    @commands.command(name="setwelcome")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setwelcome(self, ctx: commands.Context, channel: discord.TextChannel):
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            await ctx.reply("I need Send Messages and Embed Links in that channel.", mention_author=False)
            return
        self.bot.store.data.setdefault("welcome_channels", {})[str(ctx.guild.id)] = channel.id
        await self.bot.store.save()
        await ctx.reply(f"🐾 Meowing welcome messages will be sent in {channel.mention}.", mention_author=False)

    @commands.command(name="autorole")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def autorole(self, ctx: commands.Context, role: discord.Role):
        bot_member = ctx.guild.me
        if role.is_default() or role.managed or not bot_member or role >= bot_member.top_role:
            await ctx.reply("I can't assign that role. Check that it is not managed and is below my highest role.", mention_author=False)
            return
        self.bot.store.data.setdefault("autoroles", {})[str(ctx.guild.id)] = role.id
        await self.bot.store.save()
        await ctx.reply(f"✅ New members will receive {role.mention}.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="logssetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def logssetup(self, ctx: commands.Context, channel: discord.TextChannel):
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.view_channel or not perms.send_messages or not perms.embed_links:
            await ctx.reply("I need View Channel, Send Messages and Embed Links in that channel.", mention_author=False)
            return
        self.bot.store.data.setdefault("logs_channels", {})[str(ctx.guild.id)] = channel.id
        await self.bot.store.save()
        await ctx.reply(f"✅ General server logs will be sent to {channel.mention}.", mention_author=False)

    @commands.command(name="setmodlog")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setmodlog(self, ctx: commands.Context, channel: discord.TextChannel):
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.view_channel or not perms.send_messages or not perms.embed_links:
            await ctx.reply("I need View Channel, Send Messages and Embed Links in that channel.", mention_author=False)
            return
        self.bot.store.data.setdefault("modlog_channels", {})[str(ctx.guild.id)] = channel.id
        await self.bot.store.save()
        await ctx.reply(f"🛡️ Moderation logs will now be sent to {channel.mention}.", mention_author=False)

    @commands.command(name="storage")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def storage(self, ctx: commands.Context):
        """Show real database usage; quota is configured from the user's Neon plan."""
        try:
            stats = await self.bot.store.storage_stats()
        except Exception:
            log.exception("Could not read database storage stats")
            await ctx.reply("I couldn't read database storage right now. Check the database connection.", mention_author=False)
            return
        size_bytes = int(stats.get("bytes", 0))
        size_mb = size_bytes / (1024 * 1024)
        limit_mb = stats.get("limit_mb")
        usage = stats.get("usage_percent")
        lines = [
            f"**Backend:** `{stats.get('backend', 'unknown')}`",
            f"**Database size:** `{size_mb:.2f} MiB`",
            f"**Persistent:** `{'Yes' if stats.get('persistent') else 'No'}`",
            f"**Connection healthy:** `{'Yes' if stats.get('healthy') else 'No'}`",
        ]
        if limit_mb:
            lines.append(f"**Configured quota:** `{limit_mb:,} MiB` ({usage:.1f}% used)")
            if usage >= 95:
                lines.append("⚠️ **Critical:** database is near the configured quota. Review Neon usage and retention.")
            elif usage >= 80:
                lines.append("⚠️ Database usage has passed 80% of the configured quota.")
        else:
            lines.append("**Configured quota:** Not set. Add `DATABASE_STORAGE_LIMIT_MB` using the limit shown in your database plan to enable usage percentages.")
        lines.append("Counts and XP use compact per-user rows; individual message contents are not stored for meow/XP tracking.")
        await ctx.reply(embed=make_embed("🗄️ PVPClub Empire storage", "\n".join(lines), 0x55DDFB), mention_author=False)

    @commands.command(name="birthdaysetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def birthdaysetup(self, ctx: commands.Context, channel: discord.TextChannel):
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            await ctx.reply("I need Send Messages and Embed Links in that channel.", mention_author=False)
            return
        self.bot.store.data.setdefault("birthday_channels", {})[str(ctx.guild.id)] = channel.id
        await self.bot.store.save()
        await ctx.reply(f"🎂 Birthday announcements will be posted in {channel.mention}.", mention_author=False)

    @app_commands.command(name="suggest", description="Submit a suggestion to the server's suggestion channel.")
    @app_commands.guild_only()
    async def suggest(self, interaction: discord.Interaction, suggestion: app_commands.Range[str, 10, 1500]):
        channel_id = self.bot.store.data.get("suggestion_channels", {}).get(str(interaction.guild_id))
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Suggestions aren't configured yet. Ask a moderator to run `?suggestsetup #channel`.", ephemeral=True)
            return
        e = make_embed("💡 New community suggestion", suggestion.strip(), 0x55DDFB)
        e.add_field(name="Suggested by", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        e.set_footer(text="PVPClub Empire • moderators can approve or reject below")
        try:
            message = await channel.send(embed=e, view=SuggestionReviewView(), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't post the suggestion. Check my channel permissions.", ephemeral=True)
            return
        await interaction.response.send_message(f"Your suggestion was submitted in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="meowstreak", description="View your current and best daily meow streak.")
    @app_commands.guild_only()
    async def meowstreak(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        value = await self.bot.store.get_meow_streak(interaction.guild_id, target.id)
        e = make_embed("🔥 Meow streak", f"**Member:** {target.mention}\n**Current streak:** {value['current']} days\n**Best streak:** {value['best']} days\n\nA streak advances once per UTC day when the member sends exactly `meow`.")
        e.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="birthday", description="Set or view your server birthday (year is not stored).")
    @app_commands.guild_only()
    async def birthday(self, interaction: discord.Interaction,
                       month: app_commands.Range[int, 1, 12] | None = None,
                       day: app_commands.Range[int, 1, 31] | None = None):
        key = f"{interaction.guild_id}:{interaction.user.id}"
        if month is None and day is None:
            value = await self.bot.store.get_birthday(interaction.guild_id, interaction.user.id)
            if not value:
                await interaction.response.send_message("You haven't set a birthday yet. Use `/birthday month day`.", ephemeral=True)
                return
            await interaction.response.send_message(f"🎂 Your saved birthday is **{int(value['day']):02d}-{int(value['month']):02d}**. Your birth year is not stored.", ephemeral=True)
            return
        if month is None or day is None:
            await interaction.response.send_message("Provide both month and day, for example `/birthday 5 23`.", ephemeral=True)
            return
        try:
            datetime(2000, month, day)
        except ValueError:
            await interaction.response.send_message("That date doesn't exist. Check the month and day.", ephemeral=True)
            return
        await self.bot.store.set_birthday(interaction.guild_id, interaction.user.id, month, day)
        await interaction.response.send_message(f"🎂 Saved your birthday as **{day:02d}-{month:02d}**. I won't store your birth year.", ephemeral=True)

    @app_commands.command(name="birthdayremove", description="Remove your saved server birthday.")
    @app_commands.guild_only()
    async def birthdayremove(self, interaction: discord.Interaction):
        await self.bot.store.remove_birthday(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Your saved birthday was removed.", ephemeral=True)

    @tasks.loop(minutes=30)
    async def birthday_watcher(self):
        now = datetime.now(timezone.utc)
        for guild_id, user_id in await self.bot.store.due_birthdays(now.month, now.day, now.year):
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            channel_id = self.bot.store.data.get("birthday_channels", {}).get(str(guild_id))
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                continue
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    # Don't mark it announced; the member might become fetchable later today.
                    continue
            try:
                e = make_embed("🎂 Happy birthday!", f"Everyone wish {member.mention} a very happy birthday! May your day be full of cat treats, good vibes and wins. 🐾")
                e.set_thumbnail(url=member.display_avatar.url)
                await channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=[member]))
                await self.bot.store.mark_birthday_announced(guild_id, user_id, now.year)
            except discord.HTTPException:
                log.exception("Could not announce birthday for %s in guild %s", user_id, guild_id)

    @birthday_watcher.before_loop
    async def before_birthday_watcher(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def storage_watcher(self):
        """Warn staff before a configured database quota is reached.

        Set DATABASE_STORAGE_LIMIT_MB to the real limit shown by the database plan.
        No plan limit is guessed or hardcoded.
        """
        try:
            stats = await self.bot.store.storage_stats()
        except Exception:
            log.exception("Could not check database storage quota")
            return
        usage = stats.get("usage_percent")
        if usage is None:
            return  # No configured quota: /storage still reports absolute usage.
        bucket = 95 if usage >= 95 else 80 if usage >= 80 else 0
        meta = self.bot.store.data.setdefault("storage_meta", {})
        previous = int(meta.get("alert_bucket", 0) or 0)
        if bucket == 0:
            if previous != 0:
                meta["alert_bucket"] = 0
                await self.bot.store.save()
            return
        if bucket <= previous:
            return
        meta["alert_bucket"] = bucket
        try:
            await self.bot.store.save()
        except Exception:
            # Still try to deliver the warning even if the database is already close to full.
            log.exception("Could not persist the storage alert state")
        size_mb = stats["bytes"] / (1024 * 1024)
        level = "critical" if bucket == 95 else "warning"
        message = (
            f"⚠️ **Database storage {level}: {usage:.1f}% used**\n"
            f"Current database size: **{size_mb:.2f} MiB** of configured **{stats['limit_mb']:,} MiB**.\n"
            "Review the database provider's usage panel and remove data only after backing it up."
        )
        for guild in self.bot.guilds:
            channel_id = (self.bot.store.data.get("logs_channels", {}).get(str(guild.id))
                          or self.bot.store.data.get("modlog_channels", {}).get(str(guild.id)))
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning("Could not post storage alert in guild %s", guild.id)

    @storage_watcher.before_loop
    async def before_storage_watcher(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        role_id = self.bot.store.data.get("autoroles", {}).get(str(member.guild.id))
        if role_id and member.guild.me and member.guild.me.guild_permissions.manage_roles:
            role = member.guild.get_role(int(role_id))
            if role and role < member.guild.me.top_role:
                try:
                    await member.add_roles(role, reason="PVPClub Empire automatic join role")
                except discord.HTTPException:
                    log.warning("Could not assign autorole in guild %s", member.guild.id)
        channel_id = self.bot.store.data.get("welcome_channels", {}).get(str(member.guild.id))
        channel = member.guild.get_channel(int(channel_id)) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            e = make_embed("🐾 A new kitten has arrived!", f"Welcome {member.mention} to **{member.guild.name}**!\n\nSay `meow` to join the meow leaderboard and earn your Meow role. :3")
            e.set_thumbnail(url=member.display_avatar.url)
            try:
                await channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                log.exception("Could not send welcome message in guild %s", member.guild.id)
        await self._sync_meowking(member.guild)
        await self._log(member.guild, "Member joined", f"{member.mention} (`{member.id}`) joined the server.", member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._log(member.guild, "Member left", f"{member} (`{member.id}`) left the server.", member)
        await self._sync_meowking(member.guild)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild and not message.author.bot:
            content = (message.content or "[no text content]").replace("|", "¦")[:1500]
            await self._log(message.guild, "Message deleted", f"**Author:** {message.author} (`{message.author.id}`)\n**Channel:** {message.channel.mention}\n**Content:** ||{content}||")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild and not before.author.bot and before.content != after.content:
            old = (before.content or "[empty]").replace("|", "¦")[:700]
            new = (after.content or "[empty]").replace("|", "¦")[:700]
            await self._log(before.guild, "Message edited", f"**Author:** {before.author} (`{before.author.id}`)\n**Channel:** {before.channel.mention}\n**Before:** ||{old}||\n**After:** ||{new}||")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            await self._log(after.guild, "Nickname updated", f"{after.mention}\n**Before:** {before.nick or before.name}\n**After:** {after.nick or after.name}", after)
        before_roles = {r.id: r for r in before.roles}
        after_roles = {r.id: r for r in after.roles}
        added = [r.mention for rid, r in after_roles.items() if rid not in before_roles and not r.is_default()]
        removed = [r.name for rid, r in before_roles.items() if rid not in after_roles and not r.is_default()]
        if added or removed:
            text = f"{after.mention}\n**Added:** {', '.join(added) or 'None'}\n**Removed:** {', '.join(removed) or 'None'}"
            await self._log(after.guild, "Member roles updated", text, after)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self._log(guild, "Member banned", f"{user} (`{user.id}`) was banned.")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self._log(guild, "Member unbanned", f"{user} (`{user.id}`) was unbanned.")

    async def _log(self, guild: discord.Guild, title: str, description: str, member: discord.Member | None = None):
        channel_id = self.bot.store.data.get("logs_channels", {}).get(str(guild.id))
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        e = make_embed(f"📋 {title}", description[:3800], 0x5865F2)
        if member:
            e.set_thumbnail(url=member.display_avatar.url)
        e.timestamp = discord.utils.utcnow()
        try:
            await channel.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not send logs channel entry in guild %s", guild.id)


async def setup(bot):
    await bot.add_cog(CommunityCog(bot))
    bot.add_view(SuggestionReviewView())

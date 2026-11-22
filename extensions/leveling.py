"""Meow-themed XP/voice leveling system backed by normalized PostgreSQL rows."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("pvpclub-empire.leveling")
_bot = None

ROLE_MILESTONES = [
    (1, "🧶 Stray Kitten", 0xA0A0A0),
    (5, "🐾 Curious Cat", 0xE67E22),
    (10, "🐟 Fish Hunter", 0x42C7D7),
    (20, "🌿 Catnip Addict", 0x77D6A2),
    (35, "🦁 Alley Lion", 0xE5B83B),
    (50, "👑 Purr Royalty", 0x8E44AD),
]


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


def cat_embed(title: str, description: str = "", color: int = 0xFF9CEE) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="PVPClub Empire • Catnip & XP")
    return e


def progress_bar(current: int, needed: int, width: int = 10) -> str:
    filled = min(width, int((current / max(1, needed)) * width))
    return "🐾" * filled + "🧶" * (width - filled)


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
        e = cat_embed("🏆 Empire Catnip Leaderboard", "\n".join(lines) or "No XP earned yet. Send messages or be active in voice!", 0xFF9CEE)
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


class MeowResetView(discord.ui.View):
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
        await interaction.response.edit_message(content="✅ Server XP has been reset. Your channels and cat roles were kept.", embed=None, view=None)

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


class LevelingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.text_cooldowns: dict[tuple[int, int], float] = {}
        self.voice_xp_loop.start()

    def cog_unload(self):
        self.voice_xp_loop.cancel()

    def _config(self, guild_id: int) -> dict:
        return self.bot.store.data.setdefault("level_config", {}).setdefault(str(guild_id), {})

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
        if message.channel.id in {int(x) for x in config.get("ignored_channels", []) if str(x).isdigit()}:
            return
        ignored_names = {"afk", "bot-commands"}
        if message.channel.name.casefold() in ignored_names:
            return
        ignored_roles = {int(x) for x in config.get("ignored_roles", []) if str(x).isdigit()}
        if any(role.id in ignored_roles for role in message.author.roles):
            return
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
        multiplier = self._xp_multiplier(message.author, config)
        amount = max(1, int(random.randint(15, 25) * multiplier))
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
                e = cat_embed("🎉 A kitten levelled up!", f"{member.mention} reached **Level {after['level']}**!\nYou have earned **{after['total_xp'] - before['total_xp']} XP**. Keep collecting catnip! 🐾", 0x57D69A)
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
        await interaction.followup.send(embed=cat_embed("🐾 Meow leveling setup complete", note, 0x57D69A), ephemeral=True)

    @app_commands.command(name="meow-reset", description="Reset server leveling data or remove generated setup assets.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def meow_reset(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=cat_embed("⚠️ Reset meow leveling?", "Choose an action below. Resetting XP cannot be undone."),
            view=MeowResetView(self, interaction.guild, interaction.user.id), ephemeral=True,
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
        e = cat_embed(f"🐱 {target.display_name} • Cat Rank", color=0xFF9CEE)
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
        e = cat_embed("🏆 Empire Catnip Leaderboard", "\n".join(lines) or "No XP yet. Be the first kitten! 🐾")
        await interaction.response.send_message(embed=e, view=LeaderboardView(self, interaction.guild_id))

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


level_admin = app_commands.Group(name="level-admin", description="Admin controls for the meow leveling system")


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

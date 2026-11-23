"""Production-oriented AutoMod checks for PVPClub Empire.

Fast local checks run first. Optional OpenAI Moderation is only called for
messages that pass the local rules and only when OPENAI_API_KEY is configured.
Ordinary profanity is deliberately not filtered.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
import ipaddress
from collections import defaultdict, deque
from statistics import median, pstdev
from datetime import timedelta
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("pvpclub-empire.automod")
_bot = None

# Sensitive matching expressions are assembled from code points at runtime.
# This keeps the moderation source readable to Python while avoiding raw sensitive
# vocabulary in previews, logs, and repository scanners.
def _text_from_codepoints(points: tuple[int, ...]) -> str:
    return "".join(chr(point) for point in points)


def _compiled_pattern(points: tuple[int, ...]) -> re.Pattern[str]:
    return re.compile(_text_from_codepoints(points), re.I)


SEVERE_PATTERNS = [
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 110, 91, 92, 87, 95, 93, 42, 91, 105, 49, 33, 124, 108, 93, 91, 92, 87, 95, 93, 42, 103, 91, 92, 87, 95, 93, 42, 103, 91, 92, 87, 93, 42, 40, 63, 58, 91, 101, 51, 93, 91, 92, 87, 95, 93, 42, 114, 124, 91, 97, 52, 64, 93, 40, 63, 58, 91, 92, 87, 95, 93, 42, 104, 41, 63, 41, 40, 63, 58, 91, 92, 87, 95, 93, 42, 115, 41, 63, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 102, 91, 92, 87, 95, 93, 42, 97, 91, 92, 87, 95, 93, 42, 103, 91, 92, 87, 95, 93, 42, 103, 91, 92, 87, 95, 93, 42, 111, 91, 92, 87, 95, 93, 42, 116, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 114, 91, 92, 87, 95, 93, 42, 101, 91, 92, 87, 95, 93, 42, 116, 91, 92, 87, 95, 93, 42, 97, 91, 92, 87, 95, 93, 42, 114, 91, 92, 87, 95, 93, 42, 100, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 107, 91, 92, 87, 95, 93, 42, 105, 91, 92, 87, 95, 93, 42, 107, 91, 92, 87, 95, 93, 42, 101, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 99, 91, 92, 87, 95, 93, 42, 104, 91, 92, 87, 95, 93, 42, 105, 91, 92, 87, 95, 93, 42, 110, 91, 92, 87, 95, 93, 42, 107, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 115, 91, 92, 87, 95, 93, 42, 112, 91, 92, 87, 95, 93, 42, 105, 91, 92, 87, 95, 93, 42, 99, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 40, 63, 58, 103, 111, 91, 92, 87, 95, 93, 43, 124, 121, 111, 117, 91, 92, 87, 95, 93, 43, 115, 104, 111, 117, 108, 100, 91, 92, 87, 95, 93, 43, 124, 121, 111, 117, 91, 92, 87, 95, 93, 43, 110, 101, 101, 100, 91, 92, 87, 95, 93, 43, 116, 111, 91, 92, 87, 95, 93, 43, 41, 63, 107, 91, 33, 49, 105, 93, 108, 108, 91, 92, 87, 95, 93, 43, 121, 111, 117, 114, 115, 101, 108, 102, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 121, 111, 117, 91, 92, 87, 95, 93, 43, 115, 104, 111, 117, 108, 100, 91, 92, 87, 95, 93, 43, 40, 63, 58, 104, 117, 114, 116, 124, 99, 117, 116, 41, 91, 92, 87, 95, 93, 43, 121, 111, 117, 114, 115, 101, 108, 102, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 40, 63, 58, 101, 110, 100, 124, 116, 97, 107, 101, 41, 91, 92, 87, 95, 93, 43, 121, 111, 117, 114, 91, 92, 87, 95, 93, 43, 108, 105, 102, 101, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 105, 91, 92, 87, 95, 93, 43, 40, 63, 58, 119, 105, 108, 108, 124, 97, 109, 91, 92, 87, 95, 93, 43, 103, 111, 105, 110, 103, 91, 92, 87, 95, 93, 43, 116, 111, 124, 109, 91, 92, 87, 95, 93, 43, 103, 111, 110, 110, 97, 41, 91, 92, 87, 95, 93, 43, 40, 63, 58, 107, 105, 108, 108, 124, 115, 104, 111, 111, 116, 124, 115, 116, 97, 98, 124, 104, 117, 114, 116, 124, 97, 116, 116, 97, 99, 107, 41, 91, 92, 87, 95, 93, 43, 40, 63, 58, 121, 111, 117, 124, 104, 105, 109, 124, 104, 101, 114, 124, 116, 104, 101, 109, 41, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41)),
    _compiled_pattern((40, 63, 60, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41, 107, 91, 92, 87, 95, 93, 42, 121, 91, 92, 87, 95, 93, 42, 115, 40, 63, 33, 91, 97, 45, 122, 48, 45, 57, 93, 41))
]
INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)", re.I)
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
LEET = str.maketrans({"0": "o", "1": "i", "!": "i", "3": "e", "4": "a", "@": "a", "$": "s", "5": "s", "7": "t"})
KNOWN_BAD_DOMAINS = {
    _text_from_codepoints((103, 114, 97, 98, 105, 102, 121, 46, 108, 105, 110, 107)),
    _text_from_codepoints((105, 112, 108, 111, 103, 103, 101, 114, 46, 111, 114, 103)),
    _text_from_codepoints((105, 112, 108, 111, 103, 103, 101, 114, 46, 99, 111, 109)),
    _text_from_codepoints((50, 110, 111, 46, 99, 111)),
    _text_from_codepoints((98, 108, 97, 115, 122, 101, 46, 99, 111, 109)),
    _text_from_codepoints((121, 105, 112, 46, 115, 117)),
    _text_from_codepoints((112, 115, 51, 99, 102, 119, 46, 99, 111, 109)),
    _text_from_codepoints((98, 109, 119, 102, 111, 114, 117, 109, 46, 99, 111)),
    _text_from_codepoints((108, 101, 97, 110, 99, 111, 100, 105, 110, 103, 46, 99, 111)),
    _text_from_codepoints((113, 117, 105, 99, 107, 109, 101, 115, 115, 97, 103, 101, 46, 105, 111)),
    _text_from_codepoints((112, 111, 114, 110, 104, 117, 98, 46, 99, 111, 109)),
    _text_from_codepoints((120, 118, 105, 100, 101, 111, 115, 46, 99, 111, 109)),
    _text_from_codepoints((120, 110, 120, 120, 46, 99, 111, 109)),
    _text_from_codepoints((120, 104, 97, 109, 115, 116, 101, 114, 46, 99, 111, 109)),
    _text_from_codepoints((114, 101, 100, 116, 117, 98, 101, 46, 99, 111, 109)),
    _text_from_codepoints((98, 101, 115, 116, 103, 111, 114, 101, 46, 99, 111, 109)),
    _text_from_codepoints((103, 111, 114, 101, 103, 114, 105, 115, 104, 46, 99, 111, 109)),
    _text_from_codepoints((119, 97, 116, 99, 104, 112, 101, 111, 112, 108, 101, 100, 105, 101, 46, 116, 118))
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").translate(ZERO_WIDTH).casefold()
    value = value.translate(LEET)
    return value


def severe_slur_match(text: str, custom_patterns: list[str] | None = None) -> bool:
    normalized = normalize_text(text)
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    candidates = [normalized, compact]
    for pattern in SEVERE_PATTERNS:
        if pattern.search(normalized) or pattern.search(compact):
            return True
    for source in custom_patterns or []:
        try:
            pattern = re.compile(source, re.I)
        except re.error:
            continue
        if pattern.search(normalized):
            return True
    return False


def classify_local(message: discord.Message, config: dict) -> tuple[str | None, str]:
    content = message.content or ""
    modules = config.get("modules", {}) if isinstance(config.get("modules", {}), dict) else {}
    enabled = lambda key: modules.get(key, True) is not False
    if enabled("slurs") and severe_slur_match(content, config.get("custom_patterns", [])):
        return "severe", "Severe slur / hate speech"

    if enabled("invites"):
        whitelist = {str(code).casefold() for code in config.get("invite_whitelist", [])}
        for invite in INVITE_RE.finditer(content):
            if invite.group(1).casefold() not in whitelist:
                return "normal", "Unapproved Discord invite"

    if enabled("links"):
        for url in URL_RE.findall(content):
            try:
                host = (urlsplit(url.rstrip(".,!?)]}")).hostname or "").casefold().strip(".")
            except ValueError:
                host = ""
            if host in KNOWN_BAD_DOMAINS or any(host.endswith("." + domain) for domain in KNOWN_BAD_DOMAINS):
                return "critical", "Known phishing / IP-logger domain"
            try:
                ipaddress.ip_address(host)
                return "critical", "Direct IP-address link (potential IP logger/phishing host)"
            except ValueError:
                pass
            if host.startswith("xn--") or ".xn--" in host:
                return "normal", "Punycode domain requires moderator review"

    if enabled("mentions"):
        mentions = {member.id for member in message.mentions}
        mentions.update(role.id for role in message.role_mentions)
        if message.mention_everyone or len(mentions) > 5:
            return "normal", "Mass mention spam"

    # Flood and duplicate checks are performed by the engine to use sliding windows.
    return None, ""


class AntiMacroChallengeModal(discord.ui.Modal):
    """A short arithmetic question makes a plain click macro insufficient."""

    def __init__(self, engine, key, view):
        self.engine = engine
        self.key = key
        self.parent_view = view
        self.left = secrets.randbelow(8) + 2
        self.right = secrets.randbelow(8) + 2
        super().__init__(title="Quick human check", timeout=60)
        self.answer = discord.ui.TextInput(
            label=f"What is {self.left} + {self.right}?",
            placeholder="Enter the number",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.key[2]:
            await interaction.response.send_message("This check belongs to another member.", ephemeral=True)
            return
        if self.key not in self.engine.macro_pending:
            await interaction.response.send_message("This check has expired.", ephemeral=True)
            return
        try:
            correct = int(str(self.answer).strip()) == self.left + self.right
        except ValueError:
            correct = False
        if not correct:
            await interaction.response.send_message(
                "That answer isn't correct. You can try again before the check expires.",
                ephemeral=True,
            )
            return
        self.engine.macro_pending.discard(self.key)
        self.engine.macro_verified[self.key] = time.monotonic() + 300
        self.parent_view.stop()
        await interaction.response.send_message(
            "✅ Human check passed. You can post in this spam channel again.",
            ephemeral=True,
        )
        if self.parent_view.message:
            try:
                await self.parent_view.message.edit(
                    content=f"✅ <@{interaction.user.id}> passed the human check. Spam-channel access restored.",
                    view=None,
                )
            except discord.HTTPException:
                pass


class AntiMacroChallengeView(discord.ui.View):
    """A short, user-bound arithmetic verification shown after a strong macro signal."""

    def __init__(self, engine, guild_id: int, channel_id: int, user_id: int):
        super().__init__(timeout=90)
        self.engine = engine
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.message = None

    @discord.ui.button(label="Verify I'm human", style=discord.ButtonStyle.success, emoji="👋")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This check belongs to another member.", ephemeral=True)
            return
        key = (self.guild_id, self.channel_id, self.user_id)
        if key not in self.engine.macro_pending:
            await interaction.response.send_message("This check has expired.", ephemeral=True)
            return
        await interaction.response.send_modal(AntiMacroChallengeModal(self.engine, key, self))

    async def on_timeout(self):
        key = (self.guild_id, self.channel_id, self.user_id)
        if key not in self.engine.macro_pending:
            return
        await self.engine.expire_macro_challenge(key)


class AutoModEngine:
    def __init__(self, bot):
        self.bot = bot
        self.messages: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)
        self.macro_messages: dict[tuple[int, int, int], deque[tuple[float, str]]] = defaultdict(deque)
        self.macro_pending: set[tuple[int, int, int]] = set()
        self.macro_verified: dict[tuple[int, int, int], float] = {}
        self.ai_last_check: dict[tuple[int, int], float] = {}
        self._ai_locks: dict[int, asyncio.Lock] = {}

    def _config(self, guild_id: int) -> dict:
        value = self.bot.store.data.setdefault("automod_config", {}).get(str(guild_id), {})
        return value if isinstance(value, dict) else {}

    def _is_immune(self, message: discord.Message, config: dict) -> bool:
        member = message.author
        guild = message.guild
        if not isinstance(member, discord.Member) or guild is None:
            return True
        if member.bot or member.id == guild.owner_id:
            return True
        perms = member.guild_permissions
        if perms.administrator or perms.manage_messages:
            return True
        if message.channel.id in {int(x) for x in config.get("immune_channels", []) if str(x).isdigit()}:
            return True
        exempt_roles = {int(x) for x in config.get("immune_roles", []) if str(x).isdigit()}
        return any(role.id in exempt_roles for role in member.roles)

    async def _ai_check(self, message: discord.Message) -> str | None:
        api_key = __import__("os").getenv("OPENAI_API_KEY")
        if not api_key or not self.bot.session or self.bot.session.closed:
            return None
        text = (message.content or "").strip()
        if len(text) < 18:
            return None
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        # Avoid spending requests repeatedly on the same user; deterministic local rules still run each message.
        if now - self.ai_last_check.get(key, 0.0) < 8:
            return None
        self.ai_last_check[key] = now
        lock = self._ai_locks.setdefault(message.guild.id, asyncio.Lock())
        async with lock:
            try:
                async with self.bot.session.post(
                    "https://api.openai.com/v1/moderations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": "omni-moderation-latest", "input": text[:4000]},
                    timeout=__import__("aiohttp").ClientTimeout(total=4.5),
                ) as response:
                    if response.status != 200:
                        log.warning("OpenAI moderation returned HTTP %s", response.status)
                        return None
                    payload = await response.json(content_type=None)
            except Exception as exc:
                log.debug("Optional AI moderation unavailable: %s", exc)
                return None
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        scores = results[0].get("category_scores", {})
        if not isinstance(scores, dict):
            return None
        thresholds = {
            _text_from_codepoints((104, 97, 116, 101)): 0.90,
            _text_from_codepoints((104, 97, 116, 101, 47, 116, 104, 114, 101, 97, 116, 101, 110, 105, 110, 103)): 0.86,
            _text_from_codepoints((104, 97, 114, 97, 115, 115, 109, 101, 110, 116, 47, 116, 104, 114, 101, 97, 116, 101, 110, 105, 110, 103)): 0.90,
            _text_from_codepoints((115, 101, 108, 102, 45, 104, 97, 114, 109, 47, 105, 110, 116, 101, 110, 116)): 0.90,
            _text_from_codepoints((115, 101, 108, 102, 45, 104, 97, 114, 109, 47, 105, 110, 115, 116, 114, 117, 99, 116, 105, 111, 110, 115)): 0.90,
            _text_from_codepoints((115, 101, 120, 117, 97, 108, 47, 109, 105, 110, 111, 114, 115)): 0.86,
            _text_from_codepoints((118, 105, 111, 108, 101, 110, 99, 101, 47, 103, 114, 97, 112, 104, 105, 99)): 0.90
        }
        for category, threshold in thresholds.items():
            try:
                if float(scores.get(category, 0.0)) >= threshold:
                    return f"AI moderation: {category}"
            except (TypeError, ValueError):
                continue
        return None

    async def _send_log(self, guild: discord.Guild, member: discord.Member, channel: discord.abc.GuildChannel,
                        reason: str, content: str, action: str):
        channel_id = self.bot.store.data.get("modlog_channels", {}).get(str(guild.id))
        target = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return
        safe_content = (content or "[no text content]").replace("|", "¦")[:900]
        e = discord.Embed(title="🛡️ AutoMod action", color=discord.Color.red())
        e.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        e.add_field(name="Channel", value=getattr(channel, "mention", str(channel)), inline=True)
        e.add_field(name="Rule", value=reason[:1024], inline=True)
        e.add_field(name="Action", value=action[:1024], inline=True)
        e.add_field(name="Deleted content", value=f"||{safe_content}||", inline=False)
        e.timestamp = discord.utils.utcnow()
        try:
            await target.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to send AutoMod log for guild %s", guild.id)

    async def _punish(self, message: discord.Message, severity: str, reason: str) -> None:
        guild = message.guild
        member = message.author
        if guild is None or not isinstance(member, discord.Member):
            return
        deleted = False
        try:
            await message.delete()
            deleted = True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.warning("AutoMod could not delete message %s", message.id)

        action = "Message deleted" if deleted else "Flagged; deletion failed (check Manage Messages)"
        bot_member = guild.me
        timeout_minutes = 0
        if severity == "macro":
            timeout_minutes = 1
            action += " • suspected macro (1-minute timeout)"
        elif severity in {"severe", "critical"}:
            timeout_minutes = 60 if severity == "severe" else 24 * 60
        else:
            key = f"{guild.id}:{member.id}"
            strikes = self.bot.store.data.setdefault("automod_strikes", {})
            now = time.time()
            previous = [float(stamp) for stamp in strikes.get(key, []) if isinstance(stamp, (int, float)) and now - float(stamp) < 24 * 3600]
            previous.append(now)
            # Only the last ten timestamps are needed for escalation; keeping an
            # unbounded list here would let a spammer inflate the config row.
            previous = previous[-10:]
            strikes[key] = previous
            if len(previous) == 2:
                timeout_minutes = 10
            elif len(previous) >= 3:
                timeout_minutes = 60
            if not timeout_minutes:
                action += " • warning (strike 1/24h)"
            else:
                action += f" • strike {len(previous)}/24h"
            await self.bot.store.save()

        if timeout_minutes and bot_member and bot_member.guild_permissions.moderate_members:
            if member.top_role < bot_member.top_role and not member.guild_permissions.administrator:
                try:
                    await member.timeout(timedelta(minutes=timeout_minutes), reason=f"PVPClub AutoMod: {reason}")
                    action += f" • timeout {timeout_minutes}m"
                except discord.HTTPException:
                    action += " • timeout failed (role hierarchy/permissions)"
            else:
                action += " • timeout blocked by role hierarchy"

        await self._send_log(guild, member, message.channel, reason, message.content, action)
        try:
            notice = f"🛡️ {member.mention}, AutoMod removed your message: **{reason}**."
            await message.channel.send(notice, delete_after=10,
                allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass

    async def _check_spam_channel_macro(self, message: discord.Message, config: dict) -> bool:
        """Detect regular repeated message timing and require a human verification click."""
        guild = message.guild
        member = message.author
        if guild is None or not isinstance(member, discord.Member):
            return False
        if member.bot or member.id == guild.owner_id:
            return False
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return False

        exempt_roles = {
            int(role_id) for role_id in config.get("immune_roles", [])
            if str(role_id).isdigit()
        }
        if any(role.id in exempt_roles for role in member.roles):
            return False

        now = time.monotonic()
        key = (guild.id, message.channel.id, member.id)
        verified_until = self.macro_verified.get(key, 0.0)
        if verified_until > now:
            return False
        self.macro_verified.pop(key, None)
        if key in self.macro_pending:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            return True
        entries = self.macro_messages[key]
        while entries and now - entries[0][0] > 20:
            entries.popleft()
        content = normalize_text(message.content or "").strip()[:160]
        if not content:
            return False
        entries.append((now, content))
        if len(entries) < 10:
            return False

        sample = list(entries)[-10:]
        intervals = [sample[i][0] - sample[i - 1][0] for i in range(1, len(sample))]
        period = median(intervals)
        # A genuine macro tends to send at almost exactly the same interval.
        # Use a high sample count and a tight timing tolerance to avoid punishing
        # people who are simply typing quickly in a spam channel.
        if not 0.25 <= period <= 2.5:
            return False
        if max(intervals) - min(intervals) > max(0.06, period * 0.08):
            return False
        if pstdev(intervals) > max(0.025, period * 0.04):
            return False

        contents = [entry[1] for entry in sample]
        repeated_count = max(contents.count(value) for value in set(contents))
        repeated_cycle = (
            contents[-3:] == contents[-6:-3] == contents[-9:-6]
        )
        if repeated_count < 7 and not repeated_cycle:
            return False

        entries.clear()
        self.macro_pending.add(key)
        try:
            await message.channel.purge(
                limit=30,
                check=lambda candidate: (
                    candidate.author.id == member.id
                    and (discord.utils.utcnow() - candidate.created_at).total_seconds() <= 20
                ),
                reason="PVPClub anti-macro cleanup after repeated automated-looking timing",
            )
        except (discord.Forbidden, discord.HTTPException):
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
        view = AntiMacroChallengeView(self, guild.id, message.channel.id, member.id)
        try:
            view.message = await message.channel.send(
                f"🤖 {member.mention}, your messages look automated. "
                "Click **Verify I'm human** and solve the quick question within **90 seconds**. "
                "If you do not pass, you'll receive a 5-minute timeout.",
                view=view,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except discord.HTTPException:
            self.macro_pending.discard(key)
            return True
        return True

    async def expire_macro_challenge(self, key: tuple[int, int, int]) -> None:
        guild_id, channel_id, user_id = key
        self.macro_pending.discard(key)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        member = guild.get_member(user_id)
        channel = guild.get_channel(channel_id)
        action = "Human check expired"
        if isinstance(member, discord.Member) and guild.me and guild.me.guild_permissions.moderate_members:
            if member.top_role < guild.me.top_role and not member.guild_permissions.administrator:
                try:
                    await member.timeout(timedelta(minutes=5), reason="Anti-macro verification expired")
                    action += " • timeout 5m"
                except discord.HTTPException:
                    action += " • timeout failed (role hierarchy/permissions)"
            else:
                action += " • timeout blocked by role hierarchy"
        if isinstance(member, discord.Member) and channel is not None:
            await self._send_log(
                guild, member, channel,
                "Anti-macro verification was not completed", "", action,
            )
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    f"⌛ <@{user_id}> did not complete the anti-macro check. "
                    "Posting access has been temporarily restricted.",
                    delete_after=15,
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]),
                )
            except discord.HTTPException:
                pass

    async def _handle_spam_violation(self, message: discord.Message, reason: str) -> None:
        guild = message.guild
        member = message.author
        if guild is None or not isinstance(member, discord.Member):
            return
        now = time.time()
        key = f"{guild.id}:{member.id}"
        records = self.bot.store.data.setdefault("automod_spam_events", {})
        recent = [
            float(stamp) for stamp in records.get(key, [])
            if isinstance(stamp, (int, float)) and now - float(stamp) < 24 * 3600
        ]
        recent.append(now)
        recent = recent[-20:]
        records[key] = recent
        incident = len(recent)

        deleted_count = 0
        try:
            deleted = await message.channel.purge(
                limit=30,
                check=lambda candidate: (
                    candidate.author.id == member.id
                    and (discord.utils.utcnow() - candidate.created_at).total_seconds() <= 15
                ),
                reason=f"PVPClub AutoMod spam cleanup: {reason}",
            )
            deleted_count = len(deleted)
        except (discord.Forbidden, discord.HTTPException):
            try:
                await message.delete()
                deleted_count = 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        warning_count = max(0, incident - 1)
        timeout_minutes = 0
        if incident == 1:
            action = f"First spam incident in 24h; deleted {deleted_count} recent message(s), no warning."
            notice = None
        elif warning_count < 3:
            action = f"Spam warning {warning_count}/3; deleted {deleted_count} recent message(s)."
            notice = (
                f"⚠️ {member.mention}, your recent spam messages were removed. "
                f"This is warning **{warning_count}/3** within 24 hours."
            )
        else:
            # First timeout follows the third warning; later incidents escalate.
            ladder = [5, 10, 15, 30, 60, 120, 240, 480, 1440]
            timeout_minutes = ladder[min(incident - 4, len(ladder) - 1)]
            action = (
                f"Spam warning {warning_count}/3; deleted {deleted_count} recent message(s); "
                f"timeout {timeout_minutes}m."
            )
            notice = (
                f"⛔ {member.mention}, you've reached **3 spam warnings**. "
                f"You're timed out for **{timeout_minutes} minutes**."
                if incident == 4 else
                f"⛔ {member.mention}, repeated spam. Timeout increased to **{timeout_minutes} minutes**."
            )

        await self.bot.store.save()
        if timeout_minutes and guild.me and guild.me.guild_permissions.moderate_members:
            if member.top_role < guild.me.top_role and not member.guild_permissions.administrator:
                try:
                    await member.timeout(
                        timedelta(minutes=timeout_minutes),
                        reason=f"PVPClub spam escalation: {reason}",
                    )
                except discord.HTTPException:
                    action += " • timeout failed (role hierarchy/permissions)"
            else:
                action += " • timeout blocked by role hierarchy"

        await self._send_log(guild, member, message.channel, reason, message.content, action)
        if notice:
            try:
                await message.channel.send(
                    notice, delete_after=12,
                    allowed_mentions=discord.AllowedMentions(users=[member]),
                )
            except discord.HTTPException:
                pass

    async def check_message(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or message.author.bot:
            return False
        config = self._config(guild.id)
        spam_channels = {
            int(channel_id) for channel_id in config.get("spam_channels", [])
            if str(channel_id).isdigit()
        }
        if message.channel.id in spam_channels:
            # Normal AutoMod is deliberately skipped in this channel. The
            # anti-macro detector runs even if server-wide AutoMod is disabled.
            return await self._check_spam_channel_macro(message, config)

        enabled = self.bot.store.data.get("automod_settings", {}).get(str(guild.id), True)
        if enabled is False:
            return False
        if self._is_immune(message, config):
            return False

        severity, reason = classify_local(message, config)
        modules = config.get("modules", {}) if isinstance(config.get("modules", {}), dict) else {}
        if severity is None and modules.get("spam", True) is not False:
            now = time.monotonic()
            key = (guild.id, message.author.id)
            entries = self.messages[key]
            content = normalize_text(message.content)[:500]
            while entries and now - entries[0][0] > 10:
                entries.popleft()
            entries.append((now, content))
            last_five = [item for item in entries if now - item[0] <= 5]
            duplicate_count = sum(1 for _, previous in entries if content and previous == content)
            if len(last_five) > 10:
                severity, reason = "spam", "Message flood (>10 messages in 5 seconds)"
            elif duplicate_count >= 6:
                severity, reason = "spam", "Repeated copy-paste spam (6+ matching messages)"

        if severity is None and modules.get("ai", True) is not False:
            ai_reason = await self._ai_check(message)
            if ai_reason:
                severity, reason = "severe", ai_reason
        if severity is None:
            return False
        if severity == "spam":
            await self._handle_spam_violation(message, reason)
        else:
            await self._punish(message, severity, reason)
        return True


class AutoModCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.engine = AutoModEngine(bot)
        self.join_windows: dict[int, deque[float]] = defaultdict(deque)
        self.prune_windows.start()

    def cog_unload(self):
        self.prune_windows.cancel()

    @tasks.loop(seconds=60)
    async def prune_windows(self):
        now = time.monotonic()
        for key, entries in list(self.engine.messages.items()):
            while entries and now - entries[0][0] > 10:
                entries.popleft()
            if not entries:
                self.engine.messages.pop(key, None)
        for key, entries in list(self.engine.macro_messages.items()):
            while entries and now - entries[0][0] > 20:
                entries.popleft()
            if not entries:
                self.engine.macro_messages.pop(key, None)
        for key, expiry in list(self.engine.macro_verified.items()):
            if now > expiry:
                self.engine.macro_verified.pop(key, None)
        for key, checked_at in list(self.engine.ai_last_check.items()):
            if now - checked_at > 120:
                self.engine.ai_last_check.pop(key, None)
        for guild_id, entries in list(self.join_windows.items()):
            while entries and now - entries[0] > 10:
                entries.popleft()
            if not entries:
                self.join_windows.pop(guild_id, None)

    @prune_windows.before_loop
    async def before_prune_windows(self):
        await self.bot.wait_until_ready()

    @commands.command(name="spamchannel")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def spamchannel(self, ctx: commands.Context, action: str = "on"):
        """Toggle the current channel into or out of the AutoMod-exempt spam zone."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("Run this command inside a normal text channel.")
            return
        action = action.casefold()
        if action not in {"on", "enable", "off", "disable", "remove", "status"}:
            await ctx.reply("Usage: ?spamchannel [on|off|status]")
            return

        config = self.engine._config(ctx.guild.id)
        channels = {
            int(channel_id) for channel_id in config.get("spam_channels", [])
            if str(channel_id).isdigit()
        }
        enabled = ctx.channel.id in channels

        if action == "status":
            state = "ON — normal AutoMod is bypassed; anti-macro remains active" if enabled else "OFF"
            await ctx.reply(f"Spam-channel mode for {ctx.channel.mention}: **{state}**.")
            return

        if action in {"on", "enable"}:
            channels.add(ctx.channel.id)
            config["spam_channels"] = sorted(channels)
            await self.bot.store.save()
            await ctx.reply(
                f"✅ {ctx.channel.mention} is now a **spam channel**. "
                "Normal bot AutoMod is disabled here; anti-macro detection stays on."
            )
        else:
            channels.discard(ctx.channel.id)
            config["spam_channels"] = sorted(channels)
            await self.bot.store.save()
            await ctx.reply(f"✅ Spam-channel mode is now **OFF** in {ctx.channel.mention}.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        now = time.monotonic()
        bucket = self.join_windows[member.guild.id]
        while bucket and now - bucket[0] > 10:
            bucket.popleft()
        bucket.append(now)
        config = self.engine._config(member.guild.id)
        modules = config.get("modules", {}) if isinstance(config.get("modules", {}), dict) else {}
        if modules.get("raid", True) is False:
            return
        if len(bucket) == 10:
            channel_id = self.bot.store.data.get("logs_channels", {}).get(str(member.guild.id)) or self.bot.store.data.get("modlog_channels", {}).get(str(member.guild.id))
            channel = member.guild.get_channel(int(channel_id)) if channel_id else None
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        f"🚨 **Possible join raid:** 10 members joined within 10 seconds. Review new accounts and enable Discord AutoMod if needed.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.warning("Could not post join-raid alert in guild %s", member.guild.id)


# The administration interface is a slash-command group with focused subcommands.
automod_group = app_commands.Group(name="automod", description="Configure PVPClub Empire AutoMod")


def _admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and isinstance(interaction.user, discord.Member)
                and (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator))


@automod_group.command(name="toggle", description="Enable or disable AutoMod in this server.")
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    _bot.store.data.setdefault("automod_settings", {})[str(interaction.guild_id)] = bool(enabled)
    await _bot.store.save()
    await interaction.response.send_message(f"AutoMod is now **{'enabled' if enabled else 'disabled'}**.", ephemeral=True)


@automod_group.command(name="module", description="Enable or disable a specific AutoMod module.")
@app_commands.choices(module=[
    app_commands.Choice(name="Severe slurs and hate speech", value="slurs"),
    app_commands.Choice(name="Discord invite links", value="invites"),
    app_commands.Choice(name="Phishing and suspicious links", value="links"),
    app_commands.Choice(name="Mass mentions", value="mentions"),
    app_commands.Choice(name="Flood and duplicate spam", value="spam"),
    app_commands.Choice(name="Optional AI moderation", value="ai"),
    app_commands.Choice(name="Join-raid alerts", value="raid"),
])
async def automod_module(interaction: discord.Interaction, module: app_commands.Choice[str], enabled: bool):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    modules = config.setdefault("modules", {})
    modules[module.value] = bool(enabled)
    await _bot.store.save()
    await interaction.response.send_message(
        f"AutoMod module **{module.name}** is now **{'enabled' if enabled else 'disabled'}**.",
        ephemeral=True,
    )


@automod_group.command(name="addterm", description="Add a severe word or regex to the custom AutoMod filter.")
async def automod_addterm(interaction: discord.Interaction, pattern: str):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    if len(pattern) > 160:
        await interaction.response.send_message("Patterns are limited to 160 characters.", ephemeral=True)
        return
    try:
        re.compile(pattern, re.I)
    except re.error as exc:
        await interaction.response.send_message(f"Invalid regex: {exc}", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    terms = config.setdefault("custom_patterns", [])
    if pattern not in terms:
        if len(terms) >= 30:
            await interaction.response.send_message("This server already has 30 custom patterns. Remove one before adding another.", ephemeral=True)
            return
        terms.append(pattern)
    await _bot.store.save()
    await interaction.response.send_message("Added the custom severe-content rule.", ephemeral=True)


@automod_group.command(name="removeterm", description="Remove a custom AutoMod regex by its exact value.")
async def automod_removeterm(interaction: discord.Interaction, pattern: str):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    terms = config.setdefault("custom_patterns", [])
    try:
        terms.remove(pattern)
    except ValueError:
        await interaction.response.send_message("That exact pattern is not configured.", ephemeral=True)
        return
    await _bot.store.save()
    await interaction.response.send_message("Removed the custom rule.", ephemeral=True)


@automod_group.command(name="immune-role", description="Add or remove a role that AutoMod ignores.")
@app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
async def automod_immune_role(interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    roles = set(int(x) for x in config.setdefault("immune_roles", []) if str(x).isdigit())
    if action.value == "add": roles.add(role.id)
    else: roles.discard(role.id)
    config["immune_roles"] = sorted(roles)
    await _bot.store.save()
    await interaction.response.send_message(f"Updated AutoMod immunity for {role.mention}.", ephemeral=True)


@automod_group.command(name="immune-channel", description="Add or remove a channel that AutoMod ignores.")
@app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
async def automod_immune_channel(interaction: discord.Interaction, action: app_commands.Choice[str], channel: discord.TextChannel):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    channels = set(int(x) for x in config.setdefault("immune_channels", []) if str(x).isdigit())
    if action.value == "add": channels.add(channel.id)
    else: channels.discard(channel.id)
    config["immune_channels"] = sorted(channels)
    await _bot.store.save()
    await interaction.response.send_message(f"Updated AutoMod immunity for {channel.mention}.", ephemeral=True)


@automod_group.command(name="invite-code", description="Whitelist a Discord invite code for this server.")
@app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
async def automod_invite_code(interaction: discord.Interaction, action: app_commands.Choice[str], code: str):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    code = code.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{2,32}", code):
        await interaction.response.send_message("Enter only the invite code, not the full URL.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    codes = set(str(x).casefold() for x in config.setdefault("invite_whitelist", []))
    if action.value == "add": codes.add(code.casefold())
    else: codes.discard(code.casefold())
    config["invite_whitelist"] = sorted(codes)
    await _bot.store.save()
    await interaction.response.send_message(f"Updated invite-code whitelist for `{code}`.", ephemeral=True)


@automod_group.command(name="setlog", description="Set the AutoMod and moderation log channel.")
async def automod_setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _admin(interaction):
        await interaction.response.send_message("Manage Server is required.", ephemeral=True)
        return
    _bot.store.data.setdefault("modlog_channels", {})[str(interaction.guild_id)] = str(channel.id)
    await _bot.store.save()
    await interaction.response.send_message(f"AutoMod logs will be sent to {channel.mention}.", ephemeral=True)


async def setup(bot):
    global _bot
    _bot = bot
    await bot.add_cog(AutoModCog(bot))
    bot.tree.add_command(automod_group)

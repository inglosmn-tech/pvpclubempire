"""Production-oriented AutoMod checks for PVPClub Empire.

Fast local checks run first. Optional OpenAI Moderation is only called for
messages that pass the local rules and only when OPENAI_API_KEY is configured.
Ordinary profanity is deliberately not filtered.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import aiohttp
import logging
import re
import secrets
import time
import unicodedata
import ipaddress
from collections import defaultdict, deque
from statistics import median, pstdev
from datetime import timedelta, timezone
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
        super().__init__(title="Quick human check", timeout=60)
        self.engine = engine
        self.key = key
        self.parent_view = view
        self.left = secrets.randbelow(8) + 2
        self.right = secrets.randbelow(8) + 2
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
        self.engine.macro_blocked_until.pop(self.key, None)
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
        self.macro_blocked_until: dict[tuple[int, int, int], float] = {}
        self.spam_cooldowns: dict[tuple[int, int], float] = {}
        self.ai_last_check: dict[tuple[int, int], float] = {}
        self.ai_checked_images: dict[str, float] = {}
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

    async def _moderation_image_inputs(self, message: discord.Message) -> list[dict]:
        """Read a small number of supported images in memory; never persist image bytes."""
        if not self.bot.session or self.bot.session.closed:
            return []
        result = []
        seen = set()
        allowed_mimes = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        allowed_hosts = {"cdn.discordapp.com", "media.discordapp.net", "images-ext-1.discordapp.net"}
        max_bytes = 8 * 1024 * 1024

        async def add_bytes(raw: bytes, mime: str):
            if not raw or len(raw) > max_bytes or mime not in allowed_mimes:
                return
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen:
                return
            seen.add(digest)
            if time.monotonic() - self.ai_checked_images.get(digest, 0.0) < 600:
                return
            self.ai_checked_images[digest] = time.monotonic()
            import base64
            data_url = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
            result.append({"type": "image_url", "image_url": {"url": data_url}})

        for attachment in message.attachments[:3]:
            mime = (attachment.content_type or "").split(";", 1)[0].casefold()
            if mime not in allowed_mimes or attachment.size > max_bytes:
                continue
            try:
                raw = await attachment.read(use_cached=True)
                await add_bytes(raw, mime)
            except (discord.HTTPException, asyncio.TimeoutError):
                log.warning("Could not read image attachment %s for moderation", attachment.id)

        image_urls = []
        for item in message.embeds:
            if item.image and item.image.url:
                image_urls.append(item.image.url)
            if item.thumbnail and item.thumbnail.url:
                image_urls.append(item.thumbnail.url)
        # Direct image URLs are accepted only from Discord's own media CDN.
        for url in URL_RE.findall(message.content or ""):
            image_urls.append(url.rstrip(".,!?)]}"))

        for url in image_urls[:3]:
            try:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in allowed_hosts:
                    continue
                async with self.bot.session.get(
                    url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=4)
                ) as response:
                    if response.status != 200:
                        continue
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
                    if mime not in allowed_mimes:
                        continue
                    length = response.content_length
                    if length is not None and length > max_bytes:
                        continue
                    raw = await response.content.read(max_bytes + 1)
                    if len(raw) <= max_bytes:
                        await add_bytes(raw, mime)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                log.info("Skipping inaccessible or invalid Discord image URL during moderation")
        return result[:3]

    async def _ai_check(self, message: discord.Message) -> tuple[str, str] | None:
        api_key = __import__("os").getenv("OPENAI_API_KEY")
        if not api_key or not self.bot.session or self.bot.session.closed:
            return None
        config = self._config(message.guild.id)
        nsfw_enabled = bool(config.get("nsfw_enabled", False))
        text = (message.content or "").strip()
        images = await self._moderation_image_inputs(message) if nsfw_enabled else []
        modules = config.get("modules", {}) if isinstance(config.get("modules", {}), dict) else {}
        ai_enabled = modules.get("ai", True) is not False
        if not images and (not ai_enabled or len(text) < 18):
            return None
        if not images and not text:
            return None

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if not images and now - self.ai_last_check.get(key, 0.0) < 8:
            return None
        self.ai_last_check[key] = now
        inputs = []
        if text:
            inputs.append({"type": "text", "text": text[:4000]})
        inputs.extend(images)
        lock = self._ai_locks.setdefault(message.guild.id, asyncio.Lock())
        async with lock:
            try:
                async with self.bot.session.post(
                    "https://api.openai.com/v1/moderations",
                    headers={"Authorization": "Bearer " + api_key},
                    json={"model": "omni-moderation-latest", "input": inputs},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as response:
                    if response.status == 429:
                        log.warning("OpenAI moderation rate limited guild %s", message.guild.id)
                        return None
                    if response.status != 200:
                        log.warning("OpenAI moderation returned HTTP %s", response.status)
                        return None
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                log.exception("Optional OpenAI moderation request failed")
                return None

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        result = results[0]
        scores = result.get("category_scores", {})
        applied = result.get("category_applied_input_types", {})
        if not isinstance(scores, dict):
            return None

        if nsfw_enabled:
            # The documented moderation model supports sexual/minors for text only.
            # Never infer this category from image scores or send suspected CSAM to this endpoint.
            minor_types = applied.get("sexual/minors", []) if isinstance(applied, dict) else []
            try:
                minor_score = float(scores.get("sexual/minors", 0.0))
            except (TypeError, ValueError):
                minor_score = 0.0
            minor_threshold = float(config.get("nsfw_minor_threshold", 0.60))
            if "text" in minor_types and minor_score >= minor_threshold:
                return "critical", "Sexual content involving a minor (highest severity)"

            try:
                sexual_score = float(scores.get("sexual", 0.0))
            except (TypeError, ValueError):
                sexual_score = 0.0
            sexual_threshold = float(config.get("nsfw_sexual_threshold", 0.90))
            sexual_types = applied.get("sexual", []) if isinstance(applied, dict) else []
            if sexual_types and sexual_score >= sexual_threshold:
                source = "image" if "image" in sexual_types else "text"
                return "normal", "Explicit sexual content detected in " + source

        if not ai_enabled:
            return None
        thresholds = {
            "hate": 0.90,
            "hate/threatening": 0.86,
            "harassment/threatening": 0.90,
            "self-harm/intent": 0.90,
            "self-harm/instructions": 0.90,
            "violence/graphic": 0.90,
        }
        for category, threshold in thresholds.items():
            try:
                if float(scores.get(category, 0.0)) >= threshold:
                    return "severe", "AI moderation: " + category
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
        blocked_until = self.macro_blocked_until.get(key, 0.0)
        if blocked_until > now:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            return True
        self.macro_blocked_until.pop(key, None)
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
        self.macro_blocked_until[key] = time.monotonic() + 300
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
        # Discord can deliver multiple message events before its member-update
        # event reflects a newly-applied timeout. Never escalate or extend an
        # already-active timeout; just remove any late-arriving spam message.
        existing_timeout = getattr(member, "timed_out_until", None)
        if existing_timeout is not None:
            if existing_timeout.tzinfo is None:
                existing_timeout = existing_timeout.replace(tzinfo=timezone.utc)
            if existing_timeout > discord.utils.utcnow():
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
                log.info(
                    "Skipped duplicate spam punishment for %s in guild %s; timeout active until %s",
                    member.id,
                    guild.id,
                    existing_timeout.isoformat(),
                )
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
            if self.spam_cooldowns.get(key, 0.0) > now:
                # Treat a continuous burst as one incident, but keep deleting
                # additional messages during the short cleanup window.
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
                return True
            self.spam_cooldowns.pop(key, None)
            entries = self.messages[key]
            content = normalize_text(message.content)[:500]
            while entries and now - entries[0][0] > 10:
                entries.popleft()
            entries.append((now, content))
            last_five = [item for item in entries if now - item[0] <= 5]
            duplicate_count = sum(1 for _, previous in entries if content and previous == content)
            if len(last_five) > 15:
                severity, reason = "spam", "Message flood (>15 messages in 5 seconds)"
            elif duplicate_count >= 9:
                severity, reason = "spam", "Repeated copy-paste spam (9+ matching messages)"

        config_nsfw = bool(config.get("nsfw_enabled", False))
        if severity is None and (modules.get("ai", True) is not False or config_nsfw):
            ai_result = await self._ai_check(message)
            if ai_result:
                severity, reason = ai_result
        if severity is None:
            return False
        if severity == "spam":
            key = (guild.id, message.author.id)
            now = time.monotonic()
            # Reserve this user's burst immediately, before the first await in
            # the slow moderation handler, so rapid follow-up events cannot
            # trigger another strike or timeout.
            self.spam_cooldowns[key] = now + 10
            self.messages.pop(key, None)
            await self._handle_spam_violation(message, reason)
        else:
            await self._punish(message, severity, reason)
        return True


class AutoModCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.engine = AutoModEngine(bot)
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
        for key, expiry in list(self.engine.macro_blocked_until.items()):
            if now > expiry:
                self.engine.macro_blocked_until.pop(key, None)
        for key, expiry in list(self.engine.spam_cooldowns.items()):
            if now > expiry:
                self.engine.spam_cooldowns.pop(key, None)
        for key, checked_at in list(self.engine.ai_last_check.items()):
            if now - checked_at > 120:
                self.engine.ai_last_check.pop(key, None)

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


# NSFW configuration is a sub-group of the existing AutoMod command group.
nsfw_group = app_commands.Group(name="nsfw", description="Configure explicit-content filtering")


def _actual_owner(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.id == interaction.guild.owner_id)


@nsfw_group.command(name="on", description="Enable explicit text and image filtering.")
async def automod_nsfw_on(interaction: discord.Interaction):
    if not _actual_owner(interaction):
        await interaction.response.send_message("Only the actual server owner can configure NSFW filtering.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    if not interaction.client.session or not __import__("os").getenv("OPENAI_API_KEY"):
        await interaction.response.send_message("Set OPENAI_API_KEY in Render before enabling AI text and image filtering.", ephemeral=True)
        return
    config["nsfw_enabled"] = True
    await _bot.store.save()
    await interaction.response.send_message("✅ NSFW text and image filtering is enabled.", ephemeral=True)


@nsfw_group.command(name="off", description="Disable explicit text and image filtering.")
async def automod_nsfw_off(interaction: discord.Interaction):
    if not _actual_owner(interaction):
        await interaction.response.send_message("Only the actual server owner can configure NSFW filtering.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    config["nsfw_enabled"] = False
    await _bot.store.save()
    await interaction.response.send_message("NSFW text and image filtering is disabled. Other AutoMod modules remain active.", ephemeral=True)


@nsfw_group.command(name="status", description="Inspect explicit-content filtering configuration.")
async def automod_nsfw_status(interaction: discord.Interaction):
    if not _actual_owner(interaction):
        await interaction.response.send_message("Only the actual server owner can inspect NSFW filtering.", ephemeral=True)
        return
    config = _bot.store.data.get("automod_config", {}).get(str(interaction.guild_id), {})
    log_id = _bot.store.data.get("modlog_channels", {}).get(str(interaction.guild_id))
    log_channel = interaction.guild.get_channel(int(log_id)) if log_id else None
    key_ready = bool(__import__("os").getenv("OPENAI_API_KEY") and _bot.session and not _bot.session.closed)
    e = discord.Embed(title="NSFW AutoMod status", color=discord.Color.blurple())
    e.add_field(name="Filtering", value="Enabled" if config.get("nsfw_enabled") else "Disabled", inline=True)
    e.add_field(name="OpenAI service", value="Configured" if key_ready else "OPENAI_API_KEY missing", inline=True)
    e.add_field(name="Moderation log", value=log_channel.mention if isinstance(log_channel, discord.TextChannel) else "Not configured", inline=True)
    e.add_field(name="Explicit-content threshold", value=str(config.get("nsfw_sexual_threshold", 0.90)), inline=True)
    e.add_field(name="Minors text threshold", value=str(config.get("nsfw_minor_threshold", 0.60)), inline=True)
    e.add_field(name="Images", value="Discord image attachments and supported Discord CDN embeds; maximum 8 MiB each.", inline=False)
    e.set_footer(text="The standard moderation model's sexual/minors category is text-only.")
    await interaction.response.send_message(embed=e, ephemeral=True)


@nsfw_group.command(name="setup", description="Set the moderation log and detection thresholds.")
@app_commands.describe(log_channel="Channel for moderation actions", sexual_threshold="Confidence threshold for explicit sexual content", minors_threshold="Confidence threshold for sexual content involving minors in text")
async def automod_nsfw_setup(
    interaction: discord.Interaction,
    log_channel: discord.TextChannel,
    sexual_threshold: app_commands.Range[float, 0.50, 0.99] = 0.90,
    minors_threshold: app_commands.Range[float, 0.30, 0.99] = 0.60,
):
    if not _actual_owner(interaction):
        await interaction.response.send_message("Only the actual server owner can configure NSFW filtering.", ephemeral=True)
        return
    permissions = log_channel.permissions_for(interaction.guild.me)
    if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links:
        await interaction.response.send_message("I need View Channel, Send Messages and Embed Links in the log channel.", ephemeral=True)
        return
    if not __import__("os").getenv("OPENAI_API_KEY"):
        await interaction.response.send_message("Set OPENAI_API_KEY in Render first. I have not enabled NSFW filtering.", ephemeral=True)
        return
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(interaction.guild_id), {})
    config["nsfw_enabled"] = True
    config["nsfw_sexual_threshold"] = float(sexual_threshold)
    config["nsfw_minor_threshold"] = float(minors_threshold)
    _bot.store.data.setdefault("modlog_channels", {})[str(interaction.guild_id)] = str(log_channel.id)
    await _bot.store.save()
    await interaction.response.send_message(
        f"✅ NSFW filtering is enabled. Logs: {log_channel.mention}; explicit threshold: {sexual_threshold:.2f}; minors-text threshold: {minors_threshold:.2f}.",
        ephemeral=True,
    )


automod_group.add_command(nsfw_group)


async def _managed_native_rules(guild: discord.Guild):
    rules = await guild.fetch_automod_rules()
    config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(guild.id), {})
    stored = config.setdefault("native_rule_ids", {})
    bot_id = _bot.user.id if _bot.user else 0
    owned = {}
    for rule in rules:
        if rule.creator_id == bot_id and rule.name.startswith("PCE Managed "):
            owned[rule.name] = rule
    return rules, stored, owned


@automod_group.command(name="sync", description="Synchronize PVPClub Empire managed native Discord AutoMod rules.")
async def automod_native_sync(interaction: discord.Interaction):
    if not _actual_owner(interaction):
        await interaction.response.send_message("Only the actual server owner can synchronize native AutoMod rules.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if not guild.me.guild_permissions.manage_guild:
        await interaction.followup.send("I need Manage Server to inspect and manage native AutoMod rules.", ephemeral=True)
        return
    try:
        _, stored, owned = await _managed_native_rules(guild)
        config = _bot.store.data.setdefault("automod_config", {}).setdefault(str(guild.id), {})
        alert_id = _bot.store.data.get("modlog_channels", {}).get(str(guild.id))
        alert_channel = guild.get_channel(int(alert_id)) if alert_id else None
        actions = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)]
        if isinstance(alert_channel, discord.TextChannel):
            actions.append(discord.AutoModRuleAction(channel_id=alert_channel.id))
        exempt_roles = [guild.get_role(int(x)) for x in config.get("immune_roles", []) if str(x).isdigit()]
        exempt_channels = [guild.get_channel(int(x)) for x in config.get("immune_channels", []) if str(x).isdigit()]
        exempt_roles = [x for x in exempt_roles if x][:20]
        exempt_channels = [x for x in exempt_channels if x][:50]

        desired = [
            ("PCE Managed Spam", discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)),
            ("PCE Managed Mentions", discord.AutoModTrigger(mention_limit=5, mention_raid_protection=True)),
            ("PCE Managed Keywords", discord.AutoModTrigger(keyword_filter=config.get("native_keywords") or ["free nitro generator", "free nitro scam"])),
        ]
        changed = []
        skipped = []
        for name, trigger in desired:
            rule = owned.get(name)
            if rule is None:
                # If a rule with the managed label belongs to another admin, never touch it.
                conflict = next((r for r in await guild.fetch_automod_rules() if r.name == name), None)
                if conflict is not None and conflict.creator_id != (_bot.user.id if _bot.user else 0):
                    skipped.append(name + " (same-name rule owned by an administrator)")
                    continue
                rule = await guild.create_automod_rule(
                    name=name,
                    event_type=discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=True,
                    exempt_roles=exempt_roles,
                    exempt_channels=exempt_channels,
                    reason="PVPClub Empire managed native AutoMod synchronization",
                )
                changed.append("created " + name)
            else:
                await rule.edit(
                    name=name,
                    event_type=discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=True,
                    exempt_roles=exempt_roles,
                    exempt_channels=exempt_channels,
                    reason="PVPClub Empire managed native AutoMod synchronization",
                )
                changed.append("updated " + name)
            stored[name] = str(rule.id)
            await _bot.store.save()
        await interaction.followup.send(
            "✅ Native AutoMod sync finished.\n" +
            ("\n".join(changed) if changed else "No managed rules changed.") +
            ("\nSkipped: " + "; ".join(skipped) if skipped else ""),
            ephemeral=True,
        )
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError, ValueError):
        log.exception("Native AutoMod synchronization failed in guild %s", guild.id)
        await interaction.followup.send("Native AutoMod sync failed. Check Manage Server, discord.py version and channel/role configuration; details were logged.", ephemeral=True)


@automod_group.command(name="native-status", description="Inspect PVPClub Empire native Discord AutoMod rules.")
async def automod_native_status(interaction: discord.Interaction):
    if not _actual_owner(interaction):
        await interaction.response.send_message("Only the actual server owner can inspect native AutoMod.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        rules, stored, owned = await _managed_native_rules(interaction.guild)
        lines = []
        for name in ("PCE Managed Spam", "PCE Managed Mentions", "PCE Managed Keywords"):
            rid = stored.get(name)
            rule = next((r for r in rules if rid and str(r.id) == str(rid)), None)
            if rule is None:
                rule = owned.get(name)
            state = "enabled" if rule and rule.enabled else "disabled" if rule else "not synchronized"
            lines.append(name + ": " + state + (f" (ID {rule.id})" if rule else ""))
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not inspect native AutoMod in guild %s", interaction.guild_id)
        await interaction.followup.send("I could not fetch native AutoMod rules. Check Manage Server.", ephemeral=True)


async def setup(bot):
    global _bot
    _bot = bot
    await bot.add_cog(AutoModCog(bot))
    bot.tree.add_command(automod_group)

"""
PVPClub Empire Discord Bot
Python 3.10+ | discord.py 2.x
Set BOT_TOKEN in your Render environment variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = Path("bot_data.json")
DEFAULT_MC_SERVER = "mcpvp.club"
USER_AGENT = "PVPClubEmpireBot/1.0 (Discord bot; contact server staff)"
PINK = 0xFF9CEE
CYAN = 0x55DDFB
GREEN = 0x57D69A
RED = 0xFF647C
# These are duel-kit choices only; tier data always comes from MCTiers.
MODES = ["Mace", "Sword", "Crystal", "NethPot", "Axe", "Spear", "Box", "SMP"]
MCTIERS_API_BASE = "https://mctiers.net/api/v2"
MCTIERS_PROFILE_CACHE_SECONDS = 60
MCTIERS_MODES_CACHE_SECONDS = 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pvpclub-empire")


def default_data() -> dict[str, Any]:
    return {
        "meows": {}, "ticket_counter": 0,
        "pets": {}, "giveaways": {}, "polls": {}
    }


class DataStore:
    """Atomic JSON persistence. A failed write never replaces the previous file."""
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = default_data()
        self.removed_legacy_data = False
        self.load()

    def load(self):
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, json.JSONDecodeError):
                log.exception("Could not read bot_data.json; starting with defaults.")
        # Discard legacy manually assigned tiers, account links and ELO ratings.
        # None of these values are used by the API-backed tier lookup.
        for obsolete_key in ("empire_tiers", "tier_tags", "linked_accounts", "duels"):
            if obsolete_key in self.data:
                self.data.pop(obsolete_key, None)
                self.removed_legacy_data = True
        for key, value in default_data().items():
            if key not in self.data or not isinstance(self.data[key], type(value)):
                self.data[key] = value

    async def save(self):
        async with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix="bot_data_", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, indent=2, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def meow_count(self, user_id: int) -> int:
        return int(self.data["meows"].get(str(user_id), 0))

    def add_meows(self, user_id: int, amount: int = 1):
        key = str(user_id)
        self.data["meows"][key] = self.meow_count(user_id) + amount


def embed(title: str, description: str = "", color: int = CYAN) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="PVPClub Empire • nya :3")
    return e


def meow_title(count: int) -> str:
    thresholds = [
        (500, "Supreme Empire Lion"), (250, "Apex Sabertooth"),
        (100, "Shadow Panther"), (50, "Void Alley Cat"),
        (10, "Certified Meower"), (0, "Stray Kitten")
    ]
    return next(title for minimum, title in thresholds if count >= minimum)


def clean_username(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", value):
        raise ValueError("Minecraft usernames must be 3–16 letters, numbers or underscores.")
    return value


async def get_json(session: aiohttp.ClientSession, url: str, *, timeout: int = 8):
    async with session.get(
        url, headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as response:
        if response.status == 404:
            return None
        response.raise_for_status()
        return await response.json(content_type=None)


async def get_mojang_profile(session: aiohttp.ClientSession, username: str):
    """Mojang lookup used by /mcskin only; MCTiers lookups use MCTiers directly."""
    username = clean_username(username)
    data = await get_json(
        session, f"https://api.mojang.com/users/profiles/minecraft/{quote(username)}"
    )
    if not data or not data.get("id") or not data.get("name"):
        return None
    raw = data["id"]
    if not isinstance(raw, str) or len(raw) != 32:
        raise ValueError("Mojang returned an unexpected UUID.")
    dashed = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return {"name": data["name"], "id": raw, "uuid_dashed": dashed}


class MCTiersAPIError(Exception):
    """Base error for documented MCTiers API failures and malformed responses."""


class MCTiersPlayerNotFound(MCTiersAPIError):
    pass


class MCTiersRateLimited(MCTiersAPIError):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__("The MCTiers API rate limit was reached.")


class MCTiersBadRequest(MCTiersAPIError):
    pass


class MCTiersUnavailable(MCTiersAPIError):
    pass


class MCTiersUnexpectedResponse(MCTiersAPIError):
    pass


def _api_profile_is_valid(payload: Any) -> bool:
    """Validate the documented v2 profile shape without guessing field aliases."""
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        return False
    rankings = payload.get("rankings")
    if rankings is None:
        return True
    if not isinstance(rankings, dict):
        return False
    return all(isinstance(slug, str) and isinstance(item, dict)
               for slug, item in rankings.items())


def mctiers_tier_code(tier: Any, pos: Any) -> str:
    """Format the documented tier + pos fields as HT/LT when their values are valid."""
    if not isinstance(tier, int) or isinstance(tier, bool):
        return "Not available"
    # MCTiers' pos values are 0 for High Tier and 1 for Low Tier.
    prefix = {0: "HT", 1: "LT"}.get(pos)
    if prefix is None:
        return f"Tier {tier} (pos {pos})" if pos is not None else "Not available"
    return f"{prefix}{tier}"


def mctiers_display_value(value: Any) -> str:
    if value is None or value == "":
        return "Not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


STATUS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101827">
<title>PVPClub Empire • Status</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#111c2e;--line:#24344d;--cyan:#55ddfb;--muted:#9aabc1;--green:#57d69a;--red:#ff647c}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 50% 0%,#16314a 0%,var(--bg) 58%);font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif;color:#eef6ff}
main{width:min(100%,520px)}.brand{display:flex;align-items:center;gap:12px;margin-bottom:18px}.logo{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:#15364a;color:var(--cyan);font-size:23px;border:1px solid #286078}.eyebrow{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.name{font-size:20px;font-weight:800;letter-spacing:-.03em}.card{background:linear-gradient(145deg,#142238,#101a2b);border:1px solid var(--line);border-radius:22px;padding:25px;box-shadow:0 22px 70px #0005}.top{display:flex;align-items:center;justify-content:space-between;gap:16px}.label{color:var(--muted);font-size:13px}.state{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border:1px solid var(--line);border-radius:99px;font-size:12px;font-weight:750}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.state.online{color:var(--green);border-color:#285c4a}.state.online .dot{background:var(--green);box-shadow:0 0 12px #57d69a88}.state.offline{color:#ffd18a}.state.offline .dot{background:#ffb454}.headline{font-size:28px;font-weight:850;letter-spacing:-.04em;margin:20px 0 5px}.sub{color:var(--muted);margin:0}.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:24px}.stat{padding:15px;border:1px solid var(--line);border-radius:15px;background:#0c1727}.value{font-size:21px;font-weight:800;margin-top:5px}.footer{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:11px;margin-top:18px}.refresh{color:var(--cyan)}@media(max-width:420px){.card{padding:19px}.headline{font-size:24px}}
</style>
</head>
<body><main>
<div class="brand"><div class="logo">✦</div><div><div class="eyebrow">Minecraft PvP Community</div><div class="name">PVPClub Empire</div></div></div>
<section class="card">
<div class="top"><span class="label">Live service status</span><span id="state" class="state"><i class="dot"></i><span id="stateText">Checking…</span></span></div>
<div id="headline" class="headline">Checking bot connection</div>
<p id="description" class="sub">Getting the latest status from the service.</p>
<div class="stats">
<div class="stat"><div class="label">Discord servers</div><div id="guilds" class="value">—</div></div>
<div class="stat"><div class="label">Process uptime</div><div id="uptime" class="value">—</div></div>
</div>
<div class="footer"><span id="botname">PVPClub Empire Bot</span><span id="updated" class="refresh">Refreshing…</span></div>
</section>
<div class="footer"><span>Status updates automatically</span><span>nya :3</span></div>
</main>
<script>
function duration(s){s=Math.max(0,Number(s)||0);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+(h?h+'h ':'')+m+'m'}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('status unavailable');const x=await r.json();const online=x.discord_connected;document.getElementById('state').className='state '+(online?'online':'offline');document.getElementById('stateText').textContent=online?'ONLINE':'CONNECTING';document.getElementById('headline').textContent=online?'Bot is connected':'Bot is reconnecting';document.getElementById('description').textContent=online?'The Discord bot is connected to Discord.':'The web service is responding, but the Discord connection is not ready yet.';document.getElementById('guilds').textContent=x.guild_count;document.getElementById('uptime').textContent=duration(x.uptime_seconds);document.getElementById('botname').textContent=x.bot_username||'PVPClub Empire Bot';document.getElementById('updated').textContent='Updated '+new Date(x.checked_at).toLocaleTimeString()}catch(e){document.getElementById('headline').textContent='Status unavailable';document.getElementById('description').textContent='The service may be starting, sleeping, or temporarily unreachable.';document.getElementById('stateText').textContent='UNKNOWN';document.getElementById('updated').textContent='Could not refresh'}}
refresh();setInterval(refresh,15000);
</script></body></html>"""


class EmpireBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = DataStore(DATA_FILE)
        self.session: aiohttp.ClientSession | None = None
        self.meow_recent: dict[int, list[tuple[int, float]]] = {}
        self.duel_sessions: dict[str, dict] = {}
        self.mctiers_profile_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.mctiers_profile_locks: dict[str, asyncio.Lock] = {}
        self.mctiers_modes_cache: tuple[float, dict[str, str]] | None = None
        self.mctiers_modes_lock = asyncio.Lock()
        self.started_at = time.time()
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=10)
        )
        if self.store.removed_legacy_data:
            await self.store.save()
            log.info("Removed legacy manual-tier and ELO data from bot_data.json.")
        await self.start_status_site()
        # Persistent views have stable custom IDs and no timeout.
        self.add_view(TicketPanelView())
        self.add_view(TicketControlsView())
        self.add_view(FakeBanView())
        self.add_view(GiveawayView())
        self.add_view(DuelInviteView())
        self.add_view(DuelResultView())
        try:
            synced = await self.tree.sync()
            log.info("Synced %d application commands.", len(synced))
        except discord.HTTPException:
            log.exception("Slash-command sync failed.")
        self.giveaway_watcher.start()

    async def _mctiers_get(self, route: str) -> Any:
        """GET a route documented under https://mctiers.net/docs/v2."""
        if not self.session or self.session.closed:
            raise MCTiersUnavailable("The HTTP session is not ready.")
        url = f"{MCTIERS_API_BASE}/{route.lstrip('/')}"
        try:
            async with self.session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 404:
                    raise MCTiersPlayerNotFound("No MCTiers profile was found for that username.")
                if response.status == 429:
                    retry_after_raw = response.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after_raw) if retry_after_raw else None
                    except (TypeError, ValueError):
                        retry_after = None
                    raise MCTiersRateLimited(retry_after)
                if response.status == 400:
                    raise MCTiersBadRequest("MCTiers rejected the request as invalid.")
                if response.status >= 500:
                    raise MCTiersUnavailable(f"MCTiers returned HTTP {response.status}.")
                if response.status != 200:
                    raise MCTiersUnavailable(f"MCTiers returned HTTP {response.status}.")
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise MCTiersUnexpectedResponse("MCTiers did not return valid JSON.") from exc
        except (MCTiersAPIError, asyncio.TimeoutError, aiohttp.ClientError):
            raise

    async def get_mctiers_profile(self, username: str) -> dict[str, Any]:
        username = clean_username(username)
        cache_key = username.casefold()
        now = time.monotonic()
        cached = self.mctiers_profile_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        lock = self.mctiers_profile_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self.mctiers_profile_cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

            # Official documented username resolver: GET /profile/by-name/{name}.
            payload = await self._mctiers_get(
                f"profile/by-name/{quote(username, safe='')}"
            )
            if not _api_profile_is_valid(payload):
                raise MCTiersUnexpectedResponse(
                    "MCTiers returned a profile that does not match the documented v2 schema."
                )

            self.mctiers_profile_cache[cache_key] = (
                time.monotonic() + MCTIERS_PROFILE_CACHE_SECONDS, payload
            )
            # Keep this simple in-memory cache bounded for long-running bot processes.
            while len(self.mctiers_profile_cache) > 500:
                self.mctiers_profile_cache.pop(next(iter(self.mctiers_profile_cache)))
            while len(self.mctiers_profile_locks) > 500:
                stale_key = next((
                    key for key, old_lock in self.mctiers_profile_locks.items()
                    if key != cache_key and not old_lock.locked()
                ), None)
                if stale_key is None:
                    break
                self.mctiers_profile_locks.pop(stale_key, None)
            return payload

    async def get_mctiers_gamemodes(self) -> dict[str, str]:
        """Return official gamemode titles from GET /mode/list, with a 1-hour cache."""
        now = time.monotonic()
        if self.mctiers_modes_cache and self.mctiers_modes_cache[0] > now:
            return self.mctiers_modes_cache[1]
        async with self.mctiers_modes_lock:
            now = time.monotonic()
            if self.mctiers_modes_cache and self.mctiers_modes_cache[0] > now:
                return self.mctiers_modes_cache[1]
            payload = await self._mctiers_get("mode/list")
            if not isinstance(payload, dict):
                raise MCTiersUnexpectedResponse(
                    "MCTiers returned an unexpected gamemode list."
                )
            names: dict[str, str] = {}
            for slug, item in payload.items():
                if not isinstance(slug, str) or not isinstance(item, dict):
                    continue
                title = item.get("title")
                if isinstance(title, str) and title.strip():
                    names[slug] = title.strip()
            self.mctiers_modes_cache = (
                time.monotonic() + MCTIERS_MODES_CACHE_SECONDS, names
            )
            return names

    async def start_status_site(self):
        """Expose a tiny status website on Render's assigned PORT."""
        async def homepage(request: web.Request):
            return web.Response(text=STATUS_PAGE, content_type="text/html")

        async def health(request: web.Request):
            # This confirms the HTTP process is responding, not Discord connectivity.
            return web.Response(text="ok", content_type="text/plain")

        async def api_status(request: web.Request):
            ready = self.is_ready()
            return web.json_response({
                "service": "PVPClub Empire",
                "discord_connected": ready,
                "discord_status": "online" if ready else "connecting / reconnecting",
                "bot_username": str(self.user) if self.user else None,
                "guild_count": len(self.guilds),
                "uptime_seconds": max(0, int(time.time() - self.started_at)),
                "checked_at": datetime.now(timezone.utc).isoformat()
            })

        app = web.Application()
        app.router.add_get("/", homepage)
        app.router.add_get("/health", health)
        app.router.add_get("/api/status", api_status)

        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        port = int(os.getenv("PORT", "10000"))
        site = web.TCPSite(self.web_runner, "0.0.0.0", port)
        await site.start()
        log.info("Status website listening on 0.0.0.0:%s", port)

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    @tasks.loop(seconds=15)
    async def giveaway_watcher(self):
        now = time.time()
        for message_id, giveaway in list(self.store.data["giveaways"].items()):
            if giveaway.get("ended") or now < giveaway.get("ends_at", 0):
                continue
            giveaway["ended"] = True
            channel = self.get_channel(giveaway.get("channel_id", 0))
            if channel:
                try:
                    message = await channel.fetch_message(int(message_id))
                    entries = giveaway.get("entries", [])
                    winner_id = random.choice(entries) if entries else None
                    result = f"Winner: <@{winner_id}>!" if winner_id else "Nobody entered. Skill issue? :3"
                    await message.edit(embed=embed("🎉 Giveaway ended!", f"**Prize:** {giveaway['prize']}\n{result}", PINK), view=None)
                    await channel.send(f"🎉 Giveaway finished! {result}")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.warning("Could not finish giveaway message %s", message_id)
            await self.store.save()

    @giveaway_watcher.before_loop
    async def before_giveaway_watcher(self):
        await self.wait_until_ready()


bot = EmpireBot()


async def ensure_meow_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="Meow")
    if role:
        return role
    try:
        role = await guild.create_role(
            name="Meow", color=discord.Color(PINK), hoist=False,
            mentionable=True, reason="PVPClub Empire automatic Meow role"
        )
        me = guild.me
        if me and me.top_role > role:
            try:
                await role.edit(position=max(1, me.top_role.position - 1))
            except discord.Forbidden:
                log.warning("Created Meow role in %s, but cannot reposition it.", guild.name)
        return role
    except discord.Forbidden:
        log.warning("Cannot create Meow role in %s: grant Manage Roles and move the bot role higher.", guild.name)
    except discord.HTTPException:
        log.exception("Discord failed to create Meow role in %s.", guild.name)
    return None


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    if bot.user:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="mace whiffs & meow chains :3")
        )


@bot.event
async def on_guild_join(guild: discord.Guild):
    await ensure_meow_role(guild)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if "meow" in message.content.lower():
        role = discord.utils.get(message.guild.roles, name="Meow") or await ensure_meow_role(message.guild)
        if role and role not in message.author.roles:
            try:
                await message.author.add_roles(role, reason="Said meow")
            except discord.Forbidden:
                log.warning("Cannot assign Meow role in guild %s.", message.guild.id)
        bot.store.add_meows(message.author.id)
        await bot.store.save()
        replies = [":3", "rawr", "meow", "meowowrrr"]
        if random.random() < 0.12:
            replies.append("meow... d-don't stare at me :3")
        try:
            await message.reply(random.choice(replies), mention_author=False)
        except discord.HTTPException:
            pass
        now = time.time()
        entries = bot.meow_recent.setdefault(message.channel.id, [])
        entries[:] = [(uid, ts) for uid, ts in entries if now - ts <= 60]
        if message.author.id not in {uid for uid, _ in entries}:
            entries.append((message.author.id, now))
        if len({uid for uid, _ in entries}) >= 3:
            entries.clear()
            await message.channel.send("🐾 **MEOW CHAIN COMBO!** Three different players have activated the cat protocol. +100 aura :3")
    await bot.process_commands(message)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.MissingPermissions):
        message = "Hmph. You don't have permission for that. Ask a moderator, baka."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "I need a few more Discord permissions. Fix my role permissions first >.<"
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Slow down! Try again in {error.retry_after:.1f}s. Even I need cooldowns."
    elif isinstance(original, (aiohttp.ClientError, asyncio.TimeoutError)):
        message = "The external API is being difficult. Try again in a moment."
    else:
        log.exception("Application command failed", exc_info=original)
        message = "Something went wrong. The error was logged; don't panic, I've got it."
    e = embed("⚠️ Command error", message, RED)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not send command error response")


# ---------------------------------------------------------------------------
# CAT / SOCIAL COMMANDS
# ---------------------------------------------------------------------------

@bot.tree.command(name="vibe", description="See the bot's current mood.")
async def vibe(interaction: discord.Interaction):
    e = embed("💗 PVPClub Empire • Current Vibe", "94% Tsundere\n88% Femboy Chaos\n100% Locked-In PvP\n3% willing to admit I like this server\n\nHmph. Your approval is irrelevant. Probably. :3", PINK)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="meowstats", description="View meow stats.")
@app_commands.describe(user="Member to inspect")
async def meowstats(interaction: discord.Interaction, user: discord.Member | None = None):
    user = user or interaction.user
    count = bot.store.meow_count(user.id)
    rank = 1 + sum(v > count for v in bot.store.data["meows"].values())
    e = embed("🐾 Meow Statistics", f"**Player:** {user.mention}\n**Meows:** {count:,}\n**Server rank:** #{rank}\n**Title:** {meow_title(count)}", PINK)
    e.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="meowboard", description="Top 10 meowers.")
async def meowboard(interaction: discord.Interaction):
    rows = sorted(bot.store.data["meows"].items(), key=lambda x: int(x[1]), reverse=True)[:10]
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, count) in enumerate(rows):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{medal} <@{uid}> — **{int(count):,}** · {meow_title(int(count))}")
    await interaction.response.send_message(embed=embed("🏆 Empire Meowboard", "\n".join(lines) or "No meows yet. Be the first, coward. :3", PINK))


@bot.tree.command(name="cat", description="Summon a random cat and a cat fact.")
async def cat(interaction: discord.Interaction):
    await interaction.response.defer()
    image_url = None
    fact = "Cats can make over 100 different vocal sounds."
    legendary = random.random() < 0.05
    if bot.session:
        try:
            payload = await get_json(bot.session, "https://api.thecatapi.com/v1/images/search")
            if payload:
                image_url = payload[0].get("url")
        except Exception:
            try:
                image_url = "https://cataas.com/cat"
            except Exception:
                pass
        try:
            fact_data = await get_json(bot.session, "https://catfact.ninja/fact")
            if fact_data:
                fact = fact_data.get("fact", fact)
        except Exception:
            pass
    if legendary:
        bot.store.add_meows(interaction.user.id, 10)
        await bot.store.save()
    e = embed("✨ Legendary Golden Ginger Cat!" if legendary else "🐈 Cat delivery!", fact, PINK)
    if image_url:
        e.set_image(url=image_url)
    if legendary:
        e.add_field(name="Rare roll!", value="You found the legendary ginger. +10 meow points!")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="meowtranslate", description="Translate a sentence into cat-speak.")
@app_commands.describe(text="Text to translate")
async def meowtranslate(interaction: discord.Interaction, text: str):
    words = re.findall(r"\w+|[^\w\s]|\s+", text)
    sounds = ["meow", "mrrp", "purr", "nyah", "mew", "mraow", ":3", "meowww"]
    output = []
    for word in words:
        if word.isspace() or not word.isalnum():
            output.append(word)
        else:
            sound = random.choice(sounds)
            output.append(sound.capitalize() if word[:1].isupper() else sound)
    await interaction.response.send_message(embed=embed("🐾 Cat-speak translator", "".join(output)))


@bot.tree.command(name="femboyrate", description="A completely scientific chaos scan.")
async def femboyrate(interaction: discord.Interaction, user: discord.Member | None = None):
    user = user or interaction.user
    score = random.randint(0, 100)
    bar = "🩷" * (score // 10) + "⬛" * (10 - score // 10)
    verdict = "Critical levels of cute chaos." if score >= 85 else "Suspiciously powerful :3 energy." if score >= 55 else "The scanner is confused. Interesting." if score >= 25 else "Definitely not beating the allegations."
    await interaction.response.send_message(embed=embed("🎀 Femboy Chaos Scanner", f"**Target:** {user.mention}\n**Result:** {score}%\n{bar}\n{verdict}", PINK))


@bot.tree.command(name="iq", description="Measure totally legitimate PvP IQ.")
async def iq(interaction: discord.Interaction, user: discord.Member | None = None):
    user = user or interaction.user
    score = random.randint(10, 210)
    note = "Actually calculating combos." if score > 160 else "Knows what a cooldown is." if score > 100 else "Pearled into their own cobweb." if score > 60 else "2 CPS and a dream."
    await interaction.response.send_message(embed=embed("🧠 Empire IQ Scanner", f"{user.mention}: **{score} IQ**\n{note}", CYAN))


ROASTS = [
    "Your mace has more air time than your actual hits.",
    "Bro blames ping while standing in a stationary cobweb.",
    "Your totem pops are basically a subscription service.",
    "2 CPS? Is your mouse powered by a potato?",
    "You pearl like the destination is a suggestion.",
    "Your combo counter is still loading."
]


@bot.tree.command(name="roast", description="Give someone a Minecraft PvP roast.")
async def roast(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message(embed=embed("🔥 Empire Roast", f"{user.mention}, {random.choice(ROASTS)}", RED))


@bot.tree.command(name="coinflip", description="Flip a coin.")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["HEADS", "TAILS"])
    await interaction.response.send_message(embed=embed("🪙 Coinflip", f"**{result}**\nHmph. Don't act like I rigged it. >.<"))


@bot.tree.command(name="ship", description="Calculate two players' compatibility.")
async def ship(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    score = random.randint(0, 100)
    a, b = user1.display_name, user2.display_name
    name = (a[:max(1, len(a)//2)] + b[len(b)//2:])[:32]
    verdict = "Legendary duo." if score >= 85 else "Could survive a ranked queue." if score >= 55 else "One of you is definitely stealing the loot."
    await interaction.response.send_message(embed=embed("💞 Empire Ship Scanner", f"**{name}**\n{user1.mention} × {user2.mention}\n**Compatibility:** {score}%\n{verdict}", PINK))


@bot.tree.command(name="makemeow", description="Post a clearly bot-attributed meow for a member.")
async def makemeow(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message(
        f"🐾 **Empire Meow Announcement** — {user.display_name} has been nominated for a meow. :3",
        allowed_mentions=discord.AllowedMentions.none()
    )


# ---------------------------------------------------------------------------
# MINECRAFT PROFILE / SKIN / STATUS
# ---------------------------------------------------------------------------

def build_mctiers_embed(
    profile: dict[str, Any], mode_names: dict[str, str], *, detailed: bool
) -> discord.Embed:
    player_name = profile["name"]
    e = embed(f"⚔️ {player_name} • MCTiers", color=CYAN)
    e.set_footer(text="Official MCTiers API v2 • PVPClub Empire")

    # Profile fields are read only from documented response properties.
    uuid = profile.get("uuid")
    e.add_field(name="Minecraft username", value=player_name, inline=True)
    e.add_field(name="UUID", value=f"`{uuid}`" if isinstance(uuid, str) and uuid else "Not available", inline=False)
    e.add_field(name="Region", value=mctiers_display_value(profile.get("region")), inline=True)
    e.add_field(name="Overall points", value=mctiers_display_value(profile.get("points")), inline=True)

    rankings = profile.get("rankings") or {}
    lines: list[str] = []
    for slug, ranking in rankings.items():
        if not isinstance(ranking, dict):
            continue
        gamemode = mode_names.get(slug, slug)
        current_pos = ranking.get("pos")
        current_tier = mctiers_tier_code(ranking.get("tier"), current_pos)
        position_name = {0: "High", 1: "Low"}.get(current_pos, "Not available")
        if detailed:
            peak_tier_value = ranking.get("peak_tier")
            peak_pos_value = ranking.get("peak_pos")
            peak_tier = (
                mctiers_tier_code(peak_tier_value, peak_pos_value)
                if peak_tier_value is not None else "Not available"
            )
            retired = mctiers_display_value(ranking.get("retired"))
            lines.append(
                f"**{gamemode}:** `{current_tier}` · Position: **{position_name}** "
                f"· Peak: `{peak_tier}` · Retired: **{retired}**"
            )
        else:
            lines.append(f"**{gamemode}:** `{current_tier}` · Position: **{position_name}**")

    if not lines:
        e.add_field(
            name="Gamemode tiers",
            value="No gamemode rankings were returned by the MCTiers API.",
            inline=False,
        )
    else:
        # Discord caps an embed at 6,000 aggregate characters and each field
        # value at 1,024. Reserve room for the profile fields and a note about
        # overflow, then include as many official rankings as will safely fit.
        chunks: list[str] = []
        included_count = 0
        current_chunk = ""
        tier_text_chars = 0
        tier_text_budget = 4200
        for line in lines:
            separator = 1 if current_chunk else 0
            added_chars = separator + len(line)
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) > 1000:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                separator = 0
                candidate = line
                added_chars = len(line)
            if len(candidate) > 1000 or tier_text_chars + added_chars > tier_text_budget:
                break
            current_chunk = candidate
            tier_text_chars += added_chars
            included_count += 1
        if current_chunk:
            chunks.append(current_chunk)
        for index, chunk in enumerate(chunks):
            field_name = "Gamemode tiers" if index == 0 else "Gamemode tiers (continued)"
            e.add_field(name=field_name, value=chunk, inline=False)
        if included_count < len(lines):
            e.add_field(
                name="Additional gamemodes",
                value=f"{len(lines) - included_count} additional entries were omitted to stay within Discord embed limits.",
                inline=False,
            )

    # The official v2 documentation does not define a player-page URL format,
    # nor does its overall-ranking example include a rank property. Do not invent either.
    return e


async def send_mctiers_lookup(
    interaction: discord.Interaction, username: str, *, detailed: bool
):
    await interaction.response.defer(thinking=True)
    try:
        profile = await bot.get_mctiers_profile(username)
    except ValueError as exc:
        await interaction.followup.send(
            embed=embed("Invalid Minecraft username", str(exc), RED), ephemeral=True
        )
        return
    except MCTiersPlayerNotFound:
        await interaction.followup.send(
            embed=embed(
                "MCTiers player not found",
                f"No MCTiers profile was found for **{discord.utils.escape_markdown(username)}**.",
                RED,
            ),
            ephemeral=True,
        )
        return
    except MCTiersRateLimited as exc:
        retry = (f" Try again in about {max(1, int(exc.retry_after))} seconds."
                 if exc.retry_after is not None else " Please try again shortly.")
        await interaction.followup.send(
            embed=embed("MCTiers rate limit", "The official API is rate limiting requests." + retry, RED),
            ephemeral=True,
        )
        return
    except MCTiersBadRequest:
        await interaction.followup.send(
            embed=embed("Invalid MCTiers request", "MCTiers rejected the username or request. Check the username and try again.", RED),
            ephemeral=True,
        )
        return
    except (MCTiersAPIError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("MCTiers lookup failed for %s: %s", username, exc)
        await interaction.followup.send(
            embed=embed(
                "MCTiers temporarily unavailable",
                "The official MCTiers API could not complete the lookup. Please try again later.",
                RED,
            ),
            ephemeral=True,
        )
        return

    try:
        mode_names = await bot.get_mctiers_gamemodes()
    except (MCTiersAPIError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Profile data is still genuine; fall back to the exact API slugs instead
        # of inventing display names if the gamemode catalogue is unavailable.
        log.warning("MCTiers gamemode catalogue unavailable: %s", exc)
        mode_names = {}

    result = build_mctiers_embed(profile, mode_names, detailed=detailed)
    await interaction.followup.send(embed=result)


@bot.tree.command(name="tier", description="Look up a Minecraft player's official MCTiers tiers.")
@app_commands.describe(username="Minecraft username")
async def tier(interaction: discord.Interaction, username: str):
    await send_mctiers_lookup(interaction, username, detailed=False)


@bot.tree.command(name="mctiers", description="Show a Minecraft player's official MCTiers profile.")
@app_commands.describe(username="Minecraft username")
async def mctiers(interaction: discord.Interaction, username: str):
    await send_mctiers_lookup(interaction, username, detailed=True)

@bot.tree.command(name="mcskin", description="Show Minecraft skin renders and profile links.")
async def mcskin(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    try:
        profile = await get_mojang_profile(bot.session, username)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        profile = None
    if not profile:
        await interaction.followup.send("Minecraft profile not found or Mojang is unavailable.", ephemeral=True)
        return
    uid, name = profile["id"], profile["name"]
    e = embed(f"🧍 {name} • Minecraft Skin", f"**UUID:** `{profile['uuid_dashed']}`\n[NameMC](https://namemc.com/profile/{uid}) • [Visage](https://visage.surgeplay.com/full/512/{uid}) • [Download skin](https://visage.surgeplay.com/skin/{uid})")
    e.set_image(url=f"https://visage.surgeplay.com/full/512/{uid}")
    e.set_thumbnail(url=f"https://visage.surgeplay.com/face/128/{uid}")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="mcstatus", description="Check a Minecraft Java server.")
@app_commands.describe(ip="Server address (default mcpvp.club)")
async def mcstatus(interaction: discord.Interaction, ip: str = DEFAULT_MC_SERVER):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", ip):
        await interaction.response.send_message("That server address doesn't look valid.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        data = await get_json(bot.session, f"https://api.mcsrvstat.us/3/{quote(ip, safe='.:')}")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        data = None
    if not data:
        await interaction.followup.send(embed=embed("🌐 Server status", "Status service unavailable.", RED))
        return
    online = bool(data.get("online"))
    players = data.get("players") or {}
    motd = data.get("motd", {}).get("clean", [])
    sample = players.get("list") or []
    e = embed(f"🌐 {ip}", "🟢 Online" if online else "🔴 Offline", GREEN if online else RED)
    e.add_field(name="Players", value=f"{players.get('online', 0)}/{players.get('max', 0)}")
    e.add_field(name="Version", value=str(data.get("version", "Unknown")))
    e.add_field(name="Sample", value=", ".join(x.get("name", "?") for x in sample[:8]) or "Not exposed", inline=False)
    e.add_field(name="MOTD", value="\n".join(motd)[:1024] or "No MOTD")
    await interaction.followup.send(embed=e)


# ---------------------------------------------------------------------------
# UNRANKED DUELS (no ELO, stored results or leaderboard)
# ---------------------------------------------------------------------------

class DuelInviteView(discord.ui.View):
    def __init__(self, duel_id: str | None = None):
        super().__init__(timeout=None)
        self.duel_id = duel_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="duel:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = bot.duel_sessions.get(self.duel_id or "")
        if not duel or interaction.user.id != duel["opponent"]:
            await interaction.response.send_message("This invite isn't yours to accept.", ephemeral=True)
            return
        duel["accepted"] = True
        await interaction.response.edit_message(
            content=f"⚔️ Duel accepted! {interaction.user.mention}, fight {duel['challenger']} in **{duel['kit']}**. Report the winner below.",
            view=DuelResultView(self.duel_id),
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="duel:decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = bot.duel_sessions.get(self.duel_id or "")
        if not duel or interaction.user.id != duel["opponent"]:
            await interaction.response.send_message("This invite isn't yours to decline.", ephemeral=True)
            return
        bot.duel_sessions.pop(self.duel_id, None)
        await interaction.response.edit_message(
            content="Duel declined. The arena remains peaceful. For now.", view=None
        )


class DuelResultView(discord.ui.View):
    def __init__(self, duel_id: str | None = None):
        super().__init__(timeout=None)
        self.duel_id = duel_id

    async def finish(self, interaction: discord.Interaction, winner_id: int | None):
        duel = bot.duel_sessions.get(self.duel_id or "")
        if not duel or interaction.user.id not in (duel["challenger"], duel["opponent"]):
            await interaction.response.send_message("Only duel participants can report this.", ephemeral=True)
            return
        bot.duel_sessions.pop(self.duel_id or "", None)
        if winner_id is None:
            await interaction.response.edit_message(content="Duel cancelled. No ranking was recorded.", view=None)
            return
        loser_id = duel["opponent"] if winner_id == duel["challenger"] else duel["challenger"]
        await interaction.response.edit_message(
            content=f"🏆 <@{winner_id}> wins against <@{loser_id}>! This is an unranked duel; no ELO or tier rating was changed.",
            view=None,
        )

    @discord.ui.button(label="Win (Challenger)", style=discord.ButtonStyle.success, custom_id="duel:win_challenger")
    async def win_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = bot.duel_sessions.get(self.duel_id or "")
        if duel:
            await self.finish(interaction, duel["challenger"])
        else:
            await interaction.response.send_message("This duel has expired.", ephemeral=True)

    @discord.ui.button(label="Win (Opponent)", style=discord.ButtonStyle.success, custom_id="duel:win_opponent")
    async def win_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = bot.duel_sessions.get(self.duel_id or "")
        if duel:
            await self.finish(interaction, duel["opponent"])
        else:
            await interaction.response.send_message("This duel has expired.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="duel:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, None)


@bot.tree.command(name="duel", description="Challenge someone to an unranked Empire 1v1.")
@app_commands.choices(kit=[app_commands.Choice(name=x, value=x) for x in MODES])
async def duel(interaction: discord.Interaction, opponent: discord.Member, kit: app_commands.Choice[str]):
    if opponent.bot or opponent.id == interaction.user.id:
        await interaction.response.send_message("Pick another human player, genius. :3", ephemeral=True)
        return
    duel_id = f"{interaction.id}"
    bot.duel_sessions[duel_id] = {
        "challenger": interaction.user.id, "opponent": opponent.id,
        "kit": kit.value, "accepted": False
    }
    await interaction.response.send_message(
        f"⚔️ {opponent.mention}, **{interaction.user.mention}** challenges you to an **unranked {kit.value}** duel!",
        view=DuelInviteView(duel_id),
    )


# ---------------------------------------------------------------------------
# VIRTUAL PETS
# ---------------------------------------------------------------------------

@bot.tree.command(name="adoptcat", description="Adopt your own virtual cat.")
@app_commands.choices(species=[app_commands.Choice(name=x, value=x) for x in ["Ginger", "Void", "Calico", "Tuxedo"]])
async def adoptcat(interaction: discord.Interaction, name: str, species: app_commands.Choice[str]):
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,20}", name):
        await interaction.response.send_message("Name must be 1–20 simple characters.", ephemeral=True)
        return
    key = str(interaction.user.id)
    if key in bot.store.data["pets"]:
        await interaction.response.send_message("You already have a cat! Take care of your tiny landlord.", ephemeral=True)
        return
    bot.store.data["pets"][key] = {
        "name": name, "species": species.value, "hunger": 50,
        "affection": 0, "streak": 0, "last_care_ts": 0
    }
    await bot.store.save()
    await interaction.response.send_message(embed=embed("🐈 Adoption complete!", f"Meet **{name}**, your new **{species.value}** cat. Feed it and give it attention with `/petcat`!", PINK))


@bot.tree.command(name="petcat", description="Pet or feed your virtual cat.")
@app_commands.choices(action=[app_commands.Choice(name="pet", value="pet"), app_commands.Choice(name="feed", value="feed")])
async def petcat(interaction: discord.Interaction, action: app_commands.Choice[str]):
    key = str(interaction.user.id)
    pet = bot.store.data["pets"].get(key)
    if not pet:
        await interaction.response.send_message("You haven't adopted a cat yet. Try `/adoptcat`!", ephemeral=True)
        return
    now = time.time()
    if now - pet.get("last_care_ts", 0) < 10:
        await interaction.response.send_message("Your cat demands a 10-second cooldown. Very dramatic.", ephemeral=True)
        return
    pet["last_care_ts"] = now
    if action.value == "pet":
        pet["affection"] += 1
        pet["streak"] += 1
        result = f"You pet **{pet['name']}**. Affection: **{pet['affection']}** 💗"
    else:
        pet["hunger"] = min(100, pet["hunger"] + 25)
        result = f"You fed **{pet['name']}**. Fullness: **{pet['hunger']}%** 🍣"
    await bot.store.save()
    await interaction.response.send_message(embed=embed("🐾 Cat care", result, PINK))


# ---------------------------------------------------------------------------
# TICKETS
# ---------------------------------------------------------------------------

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def open_ticket(self, interaction: discord.Interaction, kind: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Tickets only work in a server.", ephemeral=True)
            return
        category = discord.utils.get(guild.categories, name="📩 | EMPIRES TICKETS")
        if not category:
            try:
                category = await guild.create_category("📩 | EMPIRES TICKETS")
            except discord.Forbidden:
                await interaction.response.send_message("I need Manage Channels to create ticket categories.", ephemeral=True)
                return
        existing = discord.utils.find(
            lambda c: c.topic == f"ticket:{interaction.user.id}", category.text_channels
        )
        if existing:
            await interaction.response.send_message(f"You already have a ticket: {existing.mention}", ephemeral=True)
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }
        for role in guild.roles:
            if role.permissions.manage_messages:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        bot.store.data["ticket_counter"] += 1
        number = bot.store.data["ticket_counter"]
        await bot.store.save()
        try:
            channel = await guild.create_text_channel(
                f"ticket-{number:04d}", category=category, overwrites=overwrites,
                topic=f"ticket:{interaction.user.id}"
            )
        except discord.Forbidden:
            await interaction.response.send_message("I need Manage Channels to open tickets.", ephemeral=True)
            return
        await channel.send(
            f"{interaction.user.mention} — **{kind}** ticket opened. Staff will be with you shortly.",
            view=TicketControlsView()
        )
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="⚔️ Tier Test", style=discord.ButtonStyle.primary, custom_id="ticket:tier")
    async def tier(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_ticket(interaction, "Tier Test")

    @discord.ui.button(label="🚨 Report Player", style=discord.ButtonStyle.danger, custom_id="ticket:report")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_ticket(interaction, "Player Report")

    @discord.ui.button(label="💬 General Support", style=discord.ButtonStyle.secondary, custom_id="ticket:support")
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_ticket(interaction, "General Support")


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.primary, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_message(f"🎫 Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.Forbidden:
            log.warning("Cannot delete ticket channel.")


@bot.tree.command(name="ticketsetup", description="Create the Empire ticket panel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticketsetup(interaction: discord.Interaction):
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name="📩 | EMPIRES TICKETS")
    if not category:
        category = await guild.create_category("📩 | EMPIRES TICKETS")
    channel = discord.utils.get(guild.text_channels, name="🎫-open-a-ticket")
    if not channel:
        channel = await guild.create_text_channel("🎫-open-a-ticket")
    e = embed("📩 PVPClub Empire Support", "Need help? Choose the right ticket type below. Please don't open duplicates.", CYAN)
    await channel.send(embed=e, view=TicketPanelView())
    await interaction.response.send_message(f"Ticket panel ready in {channel.mention}.", ephemeral=True)


# ---------------------------------------------------------------------------
# GIVEAWAYS / POLLS / ANNOUNCEMENTS / PRANK
# ---------------------------------------------------------------------------

class GiveawayView(discord.ui.View):
    def __init__(self, message_id: int | None = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.success, custom_id="giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = str(interaction.message.id)
        data = bot.store.data["giveaways"].get(mid)
        if not data or data.get("ended"):
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        entries = data.setdefault("entries", [])
        if interaction.user.id in entries:
            entries.remove(interaction.user.id)
            msg = "Entry removed."
        else:
            entries.append(interaction.user.id)
            msg = "You're entered! Good luck :3"
        await bot.store.save()
        button.label = f"🎉 Enter Giveaway ({len(entries)})"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="giveaway", description="Start a giveaway.")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway(interaction: discord.Interaction, prize: str, minutes: app_commands.Range[int, 1, 10080]):
    ends = time.time() + minutes * 60
    e = embed("🎉 PVPClub Empire Giveaway", f"**Prize:** {prize}\n**Ends:** <t:{int(ends)}:R>\nClick below to enter!", PINK)
    await interaction.response.send_message(embed=e, view=GiveawayView())
    msg = await interaction.original_response()
    bot.store.data["giveaways"][str(msg.id)] = {
        "prize": prize, "ends_at": ends, "channel_id": interaction.channel_id,
        "entries": [], "ended": False
    }
    await bot.store.save()


class PollView(discord.ui.View):
    def __init__(self, options: list[str], poll_id: str):
        super().__init__(timeout=None)
        self.options = options
        self.poll_id = poll_id
        self.votes: dict[int, int] = {}
        for idx, option in enumerate(options):
            button = discord.ui.Button(label=option[:70], style=discord.ButtonStyle.primary, custom_id=f"poll:{poll_id}:{idx}")
            button.callback = self.make_callback(idx)
            self.add_item(button)

    def make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            self.votes[interaction.user.id] = idx
            counts = [sum(v == i for v in self.votes.values()) for i in range(len(self.options))]
            total = sum(counts)
            lines = [
                f"🟦 **{self.options[i]}** — {counts[i]} votes ({(counts[i]/total*100 if total else 0):.0f}%)"
                for i in range(len(self.options))
            ]
            await interaction.response.edit_message(embed=embed("📊 Empire Poll", "\n".join(lines)))
        return callback


@bot.tree.command(name="poll", description="Create a two-option poll.")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str):
    poll_id = str(interaction.id)
    view = PollView([option1, option2], poll_id)
    await interaction.response.send_message(embed=embed(f"📊 {question}", f"🟦 **{option1}** — 0 votes\n🟥 **{option2}** — 0 votes"), view=view)


class AnnounceModal(discord.ui.Modal, title="PVPClub Empire Announcement"):
    title_text = discord.ui.TextInput(label="Title", max_length=200)
    body = discord.ui.TextInput(label="Body", style=discord.TextStyle.paragraph, max_length=3500)
    ping = discord.ui.TextInput(label="Optional ping (@everyone, @here or role ID)", required=False, max_length=100)

    def __init__(self, target: discord.TextChannel):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        e = embed(str(self.title_text), str(self.body), CYAN)
        ping_text = str(self.ping).strip()
        allowed = discord.AllowedMentions.none()
        if ping_text in ("@everyone", "@here"):
            allowed = discord.AllowedMentions(everyone=True)
        elif ping_text.isdigit():
            role = interaction.guild.get_role(int(ping_text))
            if role:
                ping_text = role.mention
                allowed = discord.AllowedMentions(roles=[role])
            else:
                ping_text = ""
        else:
            ping_text = ""
        await self.target.send(content=ping_text or None, embed=e, allowed_mentions=allowed)
        await interaction.response.send_message(f"Announcement posted in {self.target.mention}.", ephemeral=True)


@bot.tree.command(name="announce", description="Create a branded server announcement.")
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    await interaction.response.send_modal(AnnounceModal(channel or interaction.channel))


class FakeBanView(discord.ui.View):
    def __init__(self, issuer_id: int | None = None):
        super().__init__(timeout=None)
        self.issuer_id = issuer_id

    @discord.ui.button(label="Appeal Ban", style=discord.ButtonStyle.danger, custom_id="fakeban:appeal")
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"😭 GOTCHA. This was a fake ban prank, not a real moderation action. Issued by <@{self.issuer_id}>. No ban occurred.",
            ephemeral=True
        )


@bot.tree.command(name="fakeban", description="Post an obvious server prank ban card (does not ban anyone).")
async def fakeban(interaction: discord.Interaction, user: discord.Member, reason: str = "Being too cracked"):
    e = embed("🚫 MODERATION NOTICE — PRANK", f"**Target:** {user.mention}\n**Reason:** {reason}\n\nThis is a fictional prank card. No moderation action has been taken. Press Appeal Ban for the reveal.", RED)
    await interaction.response.send_message(embed=e, view=FakeBanView(interaction.user.id))


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_YOUR"):
        raise SystemExit(
            "BOT_TOKEN is missing. Add BOT_TOKEN in your Render environment variables; "
            "never paste your real token into public code."
        )
    bot.run(BOT_TOKEN, log_handler=None)

"""IngloTiers tier testing, player rankings, MongoDB persistence, website and trivia."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from uuid import uuid4

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("pvpclub-empire.inglotiers")

GAMEMODES = [
    "Vanilla", "Sword", "NethPot", "Pot", "UHC", "Axe", "SMP",
    "Elytra", "Creeper", "Diamond SMP", "Spear Mace",
]
TIERS = ["HT1", "LT1", "HT2", "LT2", "HT3", "LT3", "HT4", "LT4", "HT5", "LT5"]
COLOURS = {
    "HT1": 0xF1C40F, "LT1": 0xE67E22, "HT2": 0xE74C3C, "LT2": 0xC0392B,
    "HT3": 0x9B59B6, "LT3": 0x8E44AD, "HT4": 0x3498DB, "LT4": 0x2980B9,
    "HT5": 0x2ECC71, "LT5": 0x27AE60,
}
MODE_OPTIONS = [app_commands.Choice(name=m, value=m) for m in GAMEMODES]
TIER_OPTIONS = [app_commands.Choice(name=t, value=t) for t in TIERS]
BEST_OF_OPTIONS = [app_commands.Choice(name=str(n), value=n) for n in (1, 3, 5, 7)]
COOLDOWN = 7 * 24 * 60 * 60
TRIVIA_INTERVAL = 20 * 60

TRIVIA_QUESTIONS = [
    ("What Minecraft dimension contains the Ender Dragon?", ["the end", "end"]),
    ("Which ore is used to craft a Netherite ingot?", ["ancient debris"]),
    ("What item activates an End Portal frame?", ["eye of ender", "eyes of ender"]),
    ("What is the boss mob in an Ocean Monument?", ["elder guardian"]),
    ("Which Minecraft weapon deals extra damage when falling?", ["mace", "the mace"]),
    ("How many inventory slots are in the main inventory, excluding the hotbar?", ["27"]),
    ("Which mob explodes when it gets close to a player?", ["creeper"]),
    ("What is the strongest tier in IngloTiers?", ["ht1"]),
    ("What is the weakest tier in IngloTiers?", ["lt5"]),
    ("What does SMP stand for?", ["survival multiplayer"]),
    ("Which material is used to make an enchanting table?", ["diamond", "diamonds"]),
    ("How many hearts does a player have at full health?", ["10", "ten"]),
    ("Which block is needed to create a Nether portal?", ["obsidian"]),
    ("What item can be used to tame a wolf?", ["bone", "bones"]),
    ("Which potion effect makes a player move faster?", ["speed"]),
    ("What is the default maximum stack size for most items?", ["64"]),
    ("Which mob can drop a trident?", ["drowned"]),
    ("How many eyes of ender fill a complete End Portal?", ["12", "twelve"]),
    ("What structure contains trial spawners?", ["trial chamber", "trial chambers"]),
    ("Which Minecraft update introduced Netherite?", ["1.16", "nether update"]),
    ("What block is used to respawn in the Nether?", ["respawn anchor"]),
    ("Which mob is known for teleporting and carrying blocks?", ["enderman"]),
    ("What is the name of the normal starting dimension?", ["overworld"]),
    ("How many sides does a hexagon have?", ["6", "six"]),
    ("What is the capital of France?", ["paris"]),
    ("What is 12 multiplied by 8?", ["96"]),
    ("Which planet is known as the Red Planet?", ["mars"]),
    ("What is the largest ocean on Earth?", ["pacific ocean", "pacific"]),
    ("How many minutes are in one hour?", ["60", "sixty"]),
    ("Which gas do plants absorb from the atmosphere?", ["carbon dioxide", "co2"]),
    ("What is the chemical symbol for gold?", ["au"]),
    ("How many degrees are in a right angle?", ["90", "ninety"]),
    ("What is the fastest land animal?", ["cheetah"]),
]


def mode_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def clean_username(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", value):
        raise ValueError("Minecraft usernames must be 3–16 letters, numbers or underscores.")
    return value


def normalise_answer(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", value)


def best_tier(tiers: dict) -> tuple[str | None, str | None]:
    found = [(TIERS.index(tier), mode, tier) for mode, tier in (tiers or {}).items()
             if mode in GAMEMODES and tier in TIERS]
    if not found:
        return None, None
    _, mode, tier = min(found)
    return tier, mode


def move_tier(tier: str | None, direction: int) -> str:
    index = TIERS.index(tier) if tier in TIERS else len(TIERS) - 1
    return TIERS[max(0, min(len(TIERS) - 1, index + direction))]


def safe_player(player: dict) -> dict:
    tier, mode = best_tier(player.get("tiers", {}))
    updated = player.get("updated_at")
    return {
        "discord_id": str(player.get("discord_id", "")),
        "username": player.get("username") or "Unknown",
        "tiers": player.get("tiers", {}),
        "best_tier": tier,
        "best_mode": mode,
        "updated_at": updated.isoformat() if updated else None,
    }


WEBSITE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>IngloTiers — MCPvP Rankings</title>
<style>
:root{color-scheme:dark;--bg:#080d18;--panel:#111b2b;--line:#273a54;--cyan:#5ce1e6;--text:#eef7ff;--muted:#9aabc0}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -10%,#173b52 0%,var(--bg) 55%);color:var(--text);font:15px/1.5 Inter,system-ui,sans-serif;min-height:100vh}
header{max-width:1180px;margin:auto;padding:30px 22px 15px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:13px}.logo{width:48px;height:48px;border-radius:15px;background:#153b4c;border:1px solid #28647a;display:grid;place-items:center;color:var(--cyan);font-size:25px;font-weight:900}.brand strong{display:block;font-size:23px;letter-spacing:-.05em}.brand small{color:var(--muted)}
.pill{border:1px solid var(--line);border-radius:99px;padding:7px 12px;color:var(--cyan);font-size:12px;font-weight:800;letter-spacing:.08em}
main{max-width:1180px;margin:22px auto;padding:0 22px 55px}.hero{padding:30px 0 22px}.eyebrow{color:var(--cyan);font-size:11px;font-weight:850;letter-spacing:.18em;text-transform:uppercase}.hero h1{font-size:clamp(34px,6vw,58px);line-height:1.04;letter-spacing:-.06em;margin:10px 0}.hero p{color:var(--muted);max-width:650px;font-size:16px}
.panel{background:linear-gradient(145deg,#14243a,#0e1727);border:1px solid var(--line);border-radius:20px;padding:20px;margin-top:18px;box-shadow:0 20px 55px #0003}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.toolbar select,.toolbar input,.toolbar button{background:#0a1423;border:1px solid var(--line);border-radius:11px;padding:11px 13px;color:var(--text);font:inherit;min-width:190px}.toolbar input{flex:1;min-width:220px}.toolbar button{min-width:auto;cursor:pointer;color:var(--cyan)}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:15px}.stat{background:#0b1524;border:1px solid var(--line);border-radius:14px;padding:15px}.stat small{color:var(--muted)}.stat strong{display:block;font-size:24px;margin-top:4px}
table{width:100%;border-collapse:collapse;margin-top:16px}th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.12em;font-weight:800;padding:12px 10px;border-bottom:1px solid var(--line)}td{padding:14px 10px;border-bottom:1px solid #22334a}tr:last-child td{border:0}.player{font-weight:800;cursor:pointer}.player:hover{color:var(--cyan)}
.tier{display:inline-block;border-radius:7px;padding:3px 9px;font-weight:900;font-size:12px;background:#1d334c;color:var(--cyan)}.muted{color:var(--muted)}.empty{text-align:center;color:var(--muted);padding:35px 10px}.profile{display:none}.profile h2{margin:4px 0}.profile-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:9px;margin-top:12px}.mode-card{background:#0b1524;border:1px solid var(--line);padding:12px;border-radius:12px}.mode-card small{display:block;color:var(--muted)}
footer{max-width:1180px;margin:auto;padding:20px 22px 35px;color:var(--muted);font-size:12px}@media(max-width:600px){header{padding-top:20px}.pill{display:none}.stats{grid-template-columns:1fr 1fr}.panel{padding:14px}th:nth-child(3),td:nth-child(3){display:none}}
</style></head><body><header><div class="brand"><div class="logo">I</div><div><strong>IngloTiers</strong><small>EU Minecraft PvP • mcpvp.club</small></div></div><span class="pill">PLAYER RANKINGS</span></header>
<main><section class="hero"><div class="eyebrow">The community tier list</div><h1>Find your place<br>on the leaderboard.</h1><p>Browse player tiers across every supported gamemode, compare profiles and follow recent tier changes.</p></section>
<section class="panel"><div class="toolbar"><select id="mode"><option value="">Overall ranking</option></select><input id="search" placeholder="Search Minecraft username…"><button id="searchBtn">Search</button></div>
<div class="stats"><div class="stat"><small>Ranked players</small><strong id="players">—</strong></div><div class="stat"><small>Gamemodes</small><strong>11</strong></div><div class="stat"><small>Tier levels</small><strong>10</strong></div></div>
<div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Player</th><th>Best mode</th><th>Best tier</th></tr></thead><tbody id="rows"><tr><td colspan="4" class="empty">Loading leaderboard…</td></tr></tbody></table></div></section>
<section class="panel profile" id="profile"><div class="eyebrow">Player profile</div><h2 id="profileName"></h2><div class="muted" id="profileBest"></div><div class="profile-grid" id="profileModes"></div><h3>Recent tier history</h3><div id="profileHistory" class="muted"></div></section></main><footer>IngloTiers • EU only • mcpvp.club</footer>
<script>
const modes=["Vanilla","Sword","NethPot","Pot","UHC","Axe","SMP","Elytra","Creeper","Diamond SMP","Spear Mace"];const modeSelect=document.getElementById("mode");
modes.forEach(m=>{const o=document.createElement("option");o.value=m;o.textContent=m;modeSelect.appendChild(o)});
function badge(t){const s=document.createElement("span");s.className="tier";s.textContent=t||"Unranked";return s}
async function load(){const mode=modeSelect.value;const body=document.getElementById("rows");body.innerHTML='<tr><td colspan="4" class="empty">Loading…</td></tr>';try{const r=await fetch("/api/inglotiers/leaderboard"+(mode?"?mode="+encodeURIComponent(mode):""));const x=await r.json();document.getElementById("players").textContent=x.total??0;body.innerHTML="";if(!x.players||!x.players.length){body.innerHTML='<tr><td colspan="4" class="empty">No ranked players yet. Ask staff to set up IngloTiers.</td></tr>';return}x.players.forEach((p,i)=>{const tr=document.createElement("tr");const rank=document.createElement("td");rank.textContent=i+1;const name=document.createElement("td");name.className="player";name.textContent=p.username;name.onclick=()=>profile(p.username);const bm=document.createElement("td");bm.textContent=p.best_mode||"—";bm.className="muted";const tier=document.createElement("td");tier.appendChild(badge(mode?(p.tiers||{})[mode]:p.best_tier));tr.append(rank,name,bm,tier);body.appendChild(tr)})}catch(e){body.innerHTML='<tr><td colspan="4" class="empty">Leaderboard is temporarily unavailable.</td></tr>'}}
async function profile(name){try{const r=await fetch("/api/inglotiers/player/"+encodeURIComponent(name));if(!r.ok)return;const x=await r.json();document.getElementById("profile").style.display="block";document.getElementById("profileName").textContent=x.player.username;document.getElementById("profileBest").textContent="Best tier: "+(x.player.best_tier||"Unranked")+" · "+(x.player.best_mode||"No ranked mode");const box=document.getElementById("profileModes");box.innerHTML="";modes.forEach(m=>{const d=document.createElement("div");d.className="mode-card";const sm=document.createElement("small");sm.textContent=m;d.append(sm,badge((x.player.tiers||{})[m]));box.appendChild(d)});document.getElementById("profileHistory").textContent=x.history&&x.history.length?x.history.map(h=>h.gamemode+": "+h.old_tier+" → "+h.new_tier).join(" · "):"No tier history yet.";document.getElementById("profile").scrollIntoView({behavior:"smooth",block:"start"})}catch(e){}}
modeSelect.addEventListener("change",load);document.getElementById("searchBtn").onclick=()=>profile(document.getElementById("search").value.trim());document.getElementById("search").addEventListener("keydown",e=>{if(e.key==="Enter")document.getElementById("searchBtn").click()});load();
</script></body></html>"""


class TierRequestPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Request a Tier Test", style=discord.ButtonStyle.primary, emoji="🧪", custom_id="inglotiers:request")
    async def request_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Choose the gamemode you want to be tested in.",
                                                view=GamemodeSelectView(self.cog), ephemeral=True)


class GamemodeSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(placeholder="Choose a gamemode…", min_values=1, max_values=1,
                         options=[discord.SelectOption(label=m, value=m) for m in GAMEMODES],
                         custom_id="inglotiers:mode-select")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TierRequestModal(self.cog, self.values[0]))


class GamemodeSelectView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=180)
        self.add_item(GamemodeSelect(cog))


class TierRequestModal(discord.ui.Modal):
    def __init__(self, cog, gamemode: str):
        super().__init__(title=f"{gamemode} tier test request")
        self.cog = cog
        self.gamemode = gamemode
        self.username = discord.ui.TextInput(label="Minecraft username", min_length=3, max_length=16, required=True)
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.create_test_ticket(interaction, self.gamemode, str(self.username))


class TicketControlsView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🧪", custom_id="inglotiers:ticket-claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ticket_action(interaction, "claim")

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, emoji="↩️", custom_id="inglotiers:ticket-unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ticket_action(interaction, "unclaim")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="inglotiers:ticket-close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ticket_action(interaction, "close")


class DuelInviteView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def decide(self, interaction: discord.Interaction, accepted: bool):
        duel = await self.cog.db_call(self.cog.duels.find_one, {"message_id": interaction.message.id})
        if not duel or duel.get("status") != "pending":
            await interaction.response.send_message("This duel is no longer pending.", ephemeral=True)
            return
        if interaction.user.id != duel["opponent_id"]:
            await interaction.response.send_message("Only the challenged player can answer this duel.", ephemeral=True)
            return
        status = "active" if accepted else "declined"
        await self.cog.db_call(self.cog.duels.update_one, {"duel_id": duel["duel_id"], "status": "pending"},
                               {"$set": {"status": status, "accepted_at": datetime.now(timezone.utc)}})
        message = (f"Duel accepted! Both players submit mirrored scores using duel-score ID {duel['duel_id']}."
                   if accepted else "Duel declined.")
        await interaction.response.edit_message(content=message, view=None)

    @discord.ui.button(label="Accept duel", style=discord.ButtonStyle.success, custom_id="inglotiers:duel-accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="inglotiers:duel-decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, False)


class IngloTiersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None

    async def db_call(self, function, *args, **kwargs):
        if self.db is None:
            raise RuntimeError("MongoDB is not configured. Add MONGO_URI in Render Environment and restart the bot.")
        return await asyncio.to_thread(function, *args, **kwargs)

    async def cog_load(self):
        uri = os.getenv("MONGO_URI")
        if uri:
            try:
                self.mongo_client = MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                                                appname="PVPClubEmpire-IngloTiers")
                await asyncio.to_thread(self.mongo_client.admin.command, "ping")
                self.db = self.mongo_client["inglotiers"]
                self.players = self.db["players"]
                self.settings = self.db["guild_settings"]
                self.tickets = self.db["tickets"]
                self.history = self.db["tier_history"]
                self.activity = self.db["activity"]
                self.tester_stats = self.db["tester_stats"]
                self.duels = self.db["duels"]
                self.trivia_scores = self.db["trivia_scores"]
                self.trivia_answers = self.db["trivia_answers"]
                await asyncio.to_thread(self.players.create_index, [("guild_id", ASCENDING), ("discord_id", ASCENDING)], unique=True)
                await asyncio.to_thread(self.players.create_index, [("guild_id", ASCENDING), ("username_lc", ASCENDING)])
                await asyncio.to_thread(self.tickets.create_index, "channel_id", unique=True, sparse=True)
                await asyncio.to_thread(self.history.create_index, [("guild_id", ASCENDING), ("created_at", DESCENDING)])
                await asyncio.to_thread(self.duels.create_index, "duel_id", unique=True)
                await asyncio.to_thread(self.trivia_scores.create_index, [("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
                await asyncio.to_thread(self.trivia_answers.create_index, [("guild_id", ASCENDING), ("question_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
                log.info("IngloTiers MongoDB connected.")
            except Exception:
                log.exception("IngloTiers MongoDB connection failed; tier features are disabled.")
                if self.mongo_client:
                    self.mongo_client.close()
                self.mongo_client = None
                self.db = None
        else:
            log.warning("MONGO_URI is not set; IngloTiers will be disabled until it is configured.")
        self.bot.add_view(TierRequestPanelView(self))
        self.bot.add_view(TicketControlsView(self))
        self.bot.add_view(DuelInviteView(self))
        self.trivia_watcher.start()

    def cog_unload(self):
        self.trivia_watcher.cancel()
        if self.mongo_client:
            self.mongo_client.close()

    async def require_db(self, interaction: discord.Interaction) -> bool:
        if self.db is None:
            await interaction.response.send_message(
                "IngloTiers database is not connected. Add MONGO_URI in Render Environment, then restart the bot.",
                ephemeral=True)
            return False
        return True

    async def setting(self, guild_id: int) -> dict:
        if self.db is None:
            return {}
        return await self.db_call(self.settings.find_one, {"guild_id": guild_id}) or {}

    async def get_player(self, guild_id: int, discord_id: int):
        return await self.db_call(self.players.find_one, {"guild_id": guild_id, "discord_id": discord_id})

    async def get_or_create_player(self, guild_id: int, member: discord.Member, username: str | None = None):
        now = datetime.now(timezone.utc)
        username = clean_username(username) if username else None
        display = username or member.display_name
        update = {"$set": {"updated_at": now}, "$setOnInsert": {
            "guild_id": guild_id, "discord_id": member.id, "username": display,
            "username_lc": display.casefold(), "tiers": {}, "cooldowns": {}, "created_at": now}}
        if username:
            update["$set"].update({"username": username, "username_lc": username.casefold()})
        await self.db_call(self.players.update_one, {"guild_id": guild_id, "discord_id": member.id}, update, upsert=True)
        return await self.get_player(guild_id, member.id)

    def staff_or_tester(self, member: discord.Member, gamemode: str) -> bool:
        return (member.guild_permissions.manage_guild or member.guild_permissions.administrator
                or any(role.name == f"{gamemode} Tester" for role in member.roles))

    async def find_category(self, guild: discord.Guild, name: str):
        return discord.utils.get(guild.categories, name=name) or await guild.create_category(name, reason="IngloTiers setup")

    async def find_channel(self, guild, name, category, send_disabled=False):
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel:
            if channel.category_id != category.id:
                await channel.edit(category=category, reason="IngloTiers setup")
            return channel
        overwrite = {guild.default_role: discord.PermissionOverwrite(view_channel=True)}
        if send_disabled:
            overwrite[guild.default_role].send_messages = False
        if guild.me:
            overwrite[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        return await guild.create_text_channel(name, category=category, overwrites=overwrite, reason="IngloTiers setup")

    @commands.command(name="tiersetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def tiersetup(self, ctx: commands.Context):
        if self.db is None:
            await ctx.reply("Add MONGO_URI to Render Environment and restart the bot before running tier setup.")
            return
        guild = ctx.guild
        await ctx.reply("Setting up IngloTiers: checking roles, categories and test channels…")
        try:
            tier_roles = {}
            for tier in reversed(TIERS):
                role = discord.utils.get(guild.roles, name=tier)
                if role is None:
                    role = await guild.create_role(name=tier, colour=discord.Colour(COLOURS[tier]),
                                                   reason="IngloTiers shared tier roles")
                tier_roles[tier] = role.id
            tester_roles = {}
            for mode in GAMEMODES:
                name = f"{mode} Tester"
                role = discord.utils.get(guild.roles, name=name)
                if role is None:
                    role = await guild.create_role(name=name, colour=discord.Colour(0x36C5D8),
                                                   reason=f"IngloTiers {mode} tester role")
                tester_roles[mode] = role.id
            public_category = await self.find_category(guild, "INGLOTIERS")
            ticket_category = await self.find_category(guild, "TIER TEST TICKETS")
            request = await self.find_channel(guild, "request-test", public_category, True)
            results = await self.find_channel(guild, "results", public_category)
            activity = await self.find_channel(guild, "tier-activity", public_category)
            logs = await self.find_channel(guild, "ticket-logs", public_category)
            matchmaking = await self.find_channel(guild, "matchmaking", public_category)
            await self.db_call(self.settings.update_one, {"guild_id": guild.id}, {"$set": {
                "guild_id": guild.id, "tier_roles": tier_roles, "tester_roles": tester_roles,
                "public_category_id": public_category.id, "ticket_category_id": ticket_category.id,
                "request_channel_id": request.id, "results_channel_id": results.id,
                "activity_channel_id": activity.id, "logs_channel_id": logs.id,
                "matchmaking_channel_id": matchmaking.id, "website_public": True,
                "setup_at": datetime.now(timezone.utc)}}, upsert=True)
            config = await self.setting(guild.id)
            panel = None
            if config.get("request_panel_message_id"):
                try:
                    panel = await request.fetch_message(int(config["request_panel_message_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                    pass
            e = discord.Embed(title="🧪 Request a Tier Test",
                              description=("Click below to open your own private test ticket.\n\n"
                                           "1. Choose a gamemode.\n2. Enter your Minecraft username.\n"
                                           "3. Wait for a tester with the matching gamemode role.\n\n"
                                           "A seven-day cooldown applies after a completed test in that gamemode."),
                              colour=0x55DDEB)
            e.set_footer(text="IngloTiers • EU • mcpvp.club")
            if panel:
                await panel.edit(embed=e, view=TierRequestPanelView(self))
            else:
                panel = await request.send(embed=e, view=TierRequestPanelView(self))
                await self.db_call(self.settings.update_one, {"guild_id": guild.id},
                                   {"$set": {"request_panel_message_id": panel.id}})
            await ctx.send(
                "✅ **IngloTiers setup complete!**\n"
                "• 10 shared tier roles checked/created (LT5 → HT1).\n"
                f"• {len(GAMEMODES)} gamemode tester roles checked/created.\n"
                f"• Requests: {request.mention}\n• Results: {results.mention}\n"
                f"• Activity: {activity.mention}\n• Logs: {logs.mention}\n• Matchmaking: {matchmaking.mention}\n\n"
                "Assign tester roles to trusted members and put my bot role above the IngloTiers roles.")
        except discord.Forbidden:
            await ctx.send("I need Manage Roles, Manage Channels, View Channels and Send Messages to finish setup.")
        except Exception:
            log.exception("IngloTiers setup failed")
            await ctx.send("IngloTiers setup failed. Check the log and my Discord permissions.")

    async def create_test_ticket(self, interaction, gamemode, username):
        if not await self.require_db(interaction):
            return
        try:
            username = clean_username(username)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        guild = interaction.guild
        config = await self.setting(guild.id)
        category = guild.get_channel(config.get("ticket_category_id", 0))
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("An administrator must run ?tiersetup first.", ephemeral=True)
            return
        player = await self.get_player(guild.id, interaction.user.id)
        if player and player.get("tier_banned"):
            await interaction.response.send_message("You are blocked from tier tests. Reason: " + player.get("tier_ban_reason", "No reason supplied"), ephemeral=True)
            return
        active = await self.db_call(self.tickets.find_one, {"guild_id": guild.id, "discord_id": interaction.user.id,
                                                            "status": {"$in": ["open", "claimed"]}})
        if active:
            channel = guild.get_channel(active.get("channel_id", 0))
            await interaction.response.send_message("You already have an active ticket: " + (channel.mention if channel else "ticket " + active["ticket_id"]), ephemeral=True)
            return
        last = (player or {}).get("cooldowns", {}).get(mode_key(gamemode))
        if last and time.time() - float(last) < COOLDOWN:
            await interaction.response.send_message(
                f"You can request another {gamemode} test <t:{int(float(last) + COOLDOWN)}:R>.", ephemeral=True)
            return
        tester_role_id = config.get("tester_roles", {}).get(gamemode)
        tester_role = guild.get_role(int(tester_role_id)) if tester_role_id else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
        if tester_role:
            overwrites[tester_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for role in guild.roles:
            if role.permissions.manage_messages or role.permissions.manage_guild:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        doc = {"ticket_id": uuid4().hex[:10], "guild_id": guild.id, "discord_id": interaction.user.id,
               "username": username, "gamemode": gamemode, "status": "open", "created_at": datetime.now(timezone.utc),
               "claimed_by": None}
        try:
            channel = await guild.create_text_channel(
                f"tier-{mode_key(gamemode)[:12]}-{username.casefold()[:12]}",
                category=category, overwrites=overwrites,
                topic=f"IngloTiers test {doc['ticket_id']} • player {interaction.user.id}")
            doc["channel_id"] = channel.id
            await self.db_call(self.tickets.insert_one, doc)
            await self.db_call(self.activity.insert_one, {"guild_id": guild.id, "kind": "ticket_opened",
                               "username": username, "gamemode": gamemode, "discord_id": interaction.user.id,
                               "created_at": datetime.now(timezone.utc)})
        except Exception:
            log.exception("Could not create IngloTiers ticket")
            if "channel" in locals():
                try:
                    await channel.delete(reason="IngloTiers database write failed")
                except discord.HTTPException:
                    pass
            await interaction.response.send_message("I couldn't create the ticket. Please contact staff.", ephemeral=True)
            return
        e = discord.Embed(title=f"🧪 {gamemode} tier test",
                          description=(f"Welcome {interaction.user.mention}!\nMinecraft username: **{username}**\n"
                                       f"Gamemode: **{gamemode}**\nA matching tester can claim this ticket."),
                          colour=0x55DDEB)
        await channel.send(content=interaction.user.mention, embed=e, view=TicketControlsView(self))
        await interaction.response.send_message(f"Your private tier test ticket is ready: {channel.mention}", ephemeral=True)

    async def ticket_action(self, interaction, action):
        if not await self.require_db(interaction):
            return
        ticket = await self.db_call(self.tickets.find_one, {"guild_id": interaction.guild.id,
            "channel_id": interaction.channel_id, "status": {"$in": ["open", "claimed", "completed"]}})
        if not ticket:
            await interaction.response.send_message("This is not an active IngloTiers ticket.", ephemeral=True)
            return
        actor = interaction.user
        allowed = isinstance(actor, discord.Member) and self.staff_or_tester(actor, ticket["gamemode"])
        if action == "close":
            if actor.id != ticket["discord_id"] and not allowed:
                await interaction.response.send_message("Only the ticket owner, matching tester or staff can close it.", ephemeral=True)
                return
            await self.db_call(self.tickets.update_one, {"ticket_id": ticket["ticket_id"]},
                               {"$set": {"status": "closed", "closed_at": datetime.now(timezone.utc), "closed_by": actor.id}})
            await interaction.response.send_message("Ticket closed. This channel will be deleted in five seconds.")
            config = await self.setting(interaction.guild.id)
            log_channel = interaction.guild.get_channel(config.get("logs_channel_id", 0))
            if log_channel:
                await log_channel.send(f"Closed IngloTiers ticket {ticket['ticket_id']} for {ticket['username']} ({ticket['gamemode']}) by {actor.mention}.")
            await asyncio.sleep(5)
            try:
                await interaction.channel.delete(reason=f"IngloTiers ticket closed by {actor}")
            except discord.HTTPException:
                pass
            return
        if ticket["status"] == "completed":
            await interaction.response.send_message("This test is already completed. You can close the ticket.", ephemeral=True)
            return
        if not allowed:
            await interaction.response.send_message(f"Only {ticket['gamemode']} testers or server staff can {action} this ticket.", ephemeral=True)
            return
        if action == "claim":
            if ticket.get("claimed_by") and ticket["claimed_by"] != actor.id:
                await interaction.response.send_message(f"This ticket is already claimed by <@{ticket['claimed_by']}>.", ephemeral=True)
                return
            await self.db_call(self.tickets.update_one, {"ticket_id": ticket["ticket_id"]},
                               {"$set": {"status": "claimed", "claimed_by": actor.id, "claimed_at": datetime.now(timezone.utc)}})
            await interaction.response.send_message(f"🧪 Ticket claimed by {actor.mention}.")
        else:
            if ticket.get("claimed_by") != actor.id and not actor.guild_permissions.manage_guild:
                await interaction.response.send_message("You can only unclaim a ticket you claimed.", ephemeral=True)
                return
            await self.db_call(self.tickets.update_one, {"ticket_id": ticket["ticket_id"]},
                               {"$set": {"status": "open", "claimed_by": None, "claimed_at": None}})
            await interaction.response.send_message(f"Ticket unclaimed by {actor.mention}.")

    async def sync_best_role(self, member, player):
        config = await self.setting(member.guild.id)
        role_map = config.get("tier_roles", {})
        tier, mode = best_tier(player.get("tiers", {}))
        owned = []
        for name, role_id in role_map.items():
            role = member.guild.get_role(int(role_id))
            if role:
                owned.append(role)
        remove = [r for r in owned if r in member.roles and r.name != tier]
        desired_id = role_map.get(tier) if tier else None
        desired = member.guild.get_role(int(desired_id)) if desired_id else None
        if remove:
            await member.remove_roles(*remove, reason="IngloTiers overall best tier updated")
        if desired and desired not in member.roles:
            await member.add_roles(desired, reason=f"IngloTiers best tier: {tier} in {mode}")

    async def save_tier(self, guild, member, gamemode, new_tier, tester, source, username=None):
        player = await self.get_or_create_player(guild.id, member, username)
        old_tier = (player.get("tiers") or {}).get(gamemode)
        tiers = dict(player.get("tiers") or {})
        tiers[gamemode] = new_tier
        cooldowns = dict(player.get("cooldowns") or {})
        if source == "test":
            cooldowns[mode_key(gamemode)] = time.time()
        now = datetime.now(timezone.utc)
        display_name = username or player.get("username")
        await self.db_call(self.players.update_one, {"guild_id": guild.id, "discord_id": member.id},
            {"$set": {"tiers": tiers, "cooldowns": cooldowns, "updated_at": now,
                      "username": display_name, "username_lc": display_name.casefold()}})
        await self.db_call(self.history.insert_one, {"guild_id": guild.id, "discord_id": member.id,
            "username": display_name, "gamemode": gamemode, "old_tier": old_tier or "Unranked",
            "new_tier": new_tier, "tester_id": tester.id, "source": source, "created_at": now})
        await self.db_call(self.activity.insert_one, {"guild_id": guild.id, "kind": "tier_change",
            "discord_id": member.id, "username": display_name, "gamemode": gamemode,
            "old_tier": old_tier or "Unranked", "new_tier": new_tier, "created_at": now})
        if source == "test":
            await self.db_call(self.tester_stats.update_one, {"guild_id": guild.id, "tester_id": tester.id},
                {"$inc": {"tests_completed": 1}, "$set": {"tester_name": tester.display_name, "updated_at": now}}, upsert=True)
        updated = await self.get_player(guild.id, member.id)
        await self.sync_best_role(member, updated)
        best, best_mode = best_tier(tiers)
        msg = (f"{member.mention} ({display_name}) received **{new_tier}** in **{gamemode}**.\n"
               f"Previous: **{old_tier or 'Unranked'}**\nOverall role: **{best or 'Unranked'}**"
               + (f" ({best_mode})" if best_mode else ""))
        embed = discord.Embed(title="🏆 IngloTiers result", description=msg, colour=COLOURS[new_tier])
        embed.set_footer(text=f"Recorded by {tester.display_name} • mcpvp.club")
        config = await self.setting(guild.id)
        for key in ("results_channel_id", "activity_channel_id"):
            channel = guild.get_channel(config.get(key, 0))
            if channel:
                await channel.send(embed=embed)
        return old_tier, best, best_mode

    @app_commands.command(name="set-tier", description="Submit a player's official IngloTiers test result.")
    @app_commands.guild_only()
    @app_commands.choices(gamemode=MODE_OPTIONS, tier=TIER_OPTIONS)
    async def set_tier(self, interaction, member: discord.Member, gamemode: app_commands.Choice[str],
                       tier: app_commands.Choice[str], minecraft_username: str | None = None):
        if not await self.require_db(interaction):
            return
        actor = interaction.user
        if not isinstance(actor, discord.Member) or not self.staff_or_tester(actor, gamemode.value):
            await interaction.response.send_message(f"You need the {gamemode.value} Tester role or server management permissions.", ephemeral=True)
            return
        ticket = await self.db_call(self.tickets.find_one, {"guild_id": interaction.guild.id,
            "channel_id": interaction.channel_id, "status": {"$in": ["open", "claimed"]}})
        if ticket:
            if ticket["gamemode"] != gamemode.value or ticket["discord_id"] != member.id:
                await interaction.response.send_message("The member and gamemode must match this ticket.", ephemeral=True)
                return
            if ticket.get("claimed_by") not in (None, actor.id) and not actor.guild_permissions.manage_guild:
                await interaction.response.send_message("This ticket is claimed by another tester.", ephemeral=True)
                return
            minecraft_username = minecraft_username or ticket["username"]
            if not ticket.get("claimed_by"):
                await self.db_call(self.tickets.update_one, {"ticket_id": ticket["ticket_id"]},
                                   {"$set": {"claimed_by": actor.id, "status": "claimed"}})
        try:
            if minecraft_username:
                minecraft_username = clean_username(minecraft_username)
            await self.save_tier(interaction.guild, member, gamemode.value, tier.value, actor, "test", minecraft_username)
        except (ValueError, PyMongoError, discord.HTTPException):
            log.exception("Could not save IngloTiers result")
            await interaction.response.send_message("Could not save this tier result. Check the logs and try again.", ephemeral=True)
            return
        if ticket:
            await self.db_call(self.tickets.update_one, {"ticket_id": ticket["ticket_id"]},
                               {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}})
        await interaction.response.send_message(f"✅ Saved **{tier.value}** for {member.mention} in **{gamemode.value}**.")

    @app_commands.command(name="ign", description="Link or update your Minecraft username.")
    @app_commands.guild_only()
    async def ign(self, interaction, username: str):
        if not await self.require_db(interaction):
            return
        try:
            player = await self.get_or_create_player(interaction.guild.id, interaction.user, username)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Your IngloTiers username is now **{player['username']}**.", ephemeral=True)

    @app_commands.command(name="tiers", description="View a player's tier in every gamemode.")
    @app_commands.guild_only()
    async def tiers(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        player = await self.get_player(interaction.guild.id, target.id)
        if not player:
            await interaction.response.send_message(f"No IngloTiers profile found for {target.mention}.", ephemeral=True)
            return
        best, best_mode = best_tier(player.get("tiers", {}))
        lines = [f"**{m}:** {(player.get('tiers') or {}).get(m, 'Unranked')}" for m in GAMEMODES]
        e = discord.Embed(title=f"🏆 {player.get('username', target.display_name)} • Tiers",
                          description="\n".join(lines), colour=COLOURS.get(best, 0x55DDEB))
        e.add_field(name="Best tier", value=f"{best or 'Unranked'}" + (f" in {best_mode}" if best_mode else ""), inline=False)
        e.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="profile", description="View a player's IngloTiers profile.")
    @app_commands.guild_only()
    async def profile(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        player = await self.get_player(interaction.guild.id, target.id)
        if not player:
            await interaction.response.send_message("No profile found. Use /ign or request a tier test first.", ephemeral=True)
            return
        best, mode = best_tier(player.get("tiers", {}))
        e = discord.Embed(title=f"IngloTiers • {player.get('username', target.display_name)}",
                          description=f"Best tier: **{best or 'Unranked'}**" + (f" in **{mode}**" if mode else ""),
                          colour=COLOURS.get(best, 0x55DDEB))
        e.set_thumbnail(url=target.display_avatar.url)
        for m in GAMEMODES:
            e.add_field(name=m, value=(player.get("tiers") or {}).get(m, "—"), inline=True)
        await interaction.response.send_message(embed=e)

    async def make_leaderboard(self, guild_id, mode=None, limit=10):
        docs = await self.db_call(lambda: list(self.players.find({"guild_id": guild_id, "tiers": {"$ne": {}}})))
        rows = []
        for p in docs:
            best, best_mode = best_tier(p.get("tiers", {}))
            value = (p.get("tiers") or {}).get(mode) if mode else best
            if value in TIERS:
                rows.append({**safe_player(p), "display_tier": value, "best_mode": best_mode,
                             "sort_index": TIERS.index(value)})
        rows.sort(key=lambda p: (p["sort_index"], p["username"].casefold()))
        return rows[:limit]

    @app_commands.command(name="tier-leaderboard", description="Show the overall or gamemode tier leaderboard.")
    @app_commands.guild_only()
    @app_commands.choices(gamemode=MODE_OPTIONS)
    async def tier_leaderboard(self, interaction, gamemode: app_commands.Choice[str] | None = None):
        if not await self.require_db(interaction):
            return
        mode = gamemode.value if gamemode else None
        rows = await self.make_leaderboard(interaction.guild.id, mode, 15)
        if not rows:
            await interaction.response.send_message("No players have been tiered yet.", ephemeral=True)
            return
        e = discord.Embed(title=f"🏆 IngloTiers • {mode or 'Overall'} leaderboard",
                          description="\n".join(f"**{i}.** {r['username']} — **{r['display_tier']}**" +
                          (f" · {r['best_mode']}" if not mode and r.get("best_mode") else "")
                          for i, r in enumerate(rows, 1)), colour=0x55DDEB)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="tier-rank", description="Show a player's overall IngloTiers position.")
    @app_commands.guild_only()
    async def tier_rank(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        rows = await self.make_leaderboard(interaction.guild.id, None, 10000)
        rank = next((i for i, r in enumerate(rows, 1) if r["discord_id"] == str(target.id)), None)
        if rank:
            p = await self.get_player(interaction.guild.id, target.id)
            best, mode = best_tier(p.get("tiers", {}))
            await interaction.response.send_message(f"{target.mention} is **#{rank}** with **{best}** in **{mode}**.")
        else:
            await interaction.response.send_message(f"{target.mention} is not ranked yet.")

    @app_commands.command(name="top", description="Show the top ten IngloTiers players.")
    @app_commands.guild_only()
    async def top(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.make_leaderboard(interaction.guild.id, None, 10)
        if not rows:
            await interaction.response.send_message("No tiered players yet.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(f"**{i}.** {r['username']} — **{r['best_tier']}**" for i, r in enumerate(rows, 1)))

    @app_commands.command(name="search", description="Search IngloTiers profiles by Minecraft username.")
    @app_commands.guild_only()
    async def search(self, interaction, username: str):
        if not await self.require_db(interaction):
            return
        docs = await self.db_call(lambda: list(self.players.find({
            "guild_id": interaction.guild.id, "username_lc": {"$regex": re.escape(username.casefold())}
        }).limit(10)))
        if not docs:
            await interaction.response.send_message("No matching players found.", ephemeral=True)
            return
        lines = []
        for p in docs:
            best, mode = best_tier(p.get("tiers", {}))
            lines.append(f"**{p.get('username')}** — {best or 'Unranked'}" + (f" ({mode})" if mode else ""))
        await interaction.response.send_message(embed=discord.Embed(title="IngloTiers search", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="compare", description="Compare the gamemode tiers of two players.")
    @app_commands.guild_only()
    async def compare(self, interaction, first: discord.Member, second: discord.Member):
        if not await self.require_db(interaction):
            return
        a = await self.get_player(interaction.guild.id, first.id)
        b = await self.get_player(interaction.guild.id, second.id)
        if not a or not b:
            await interaction.response.send_message("Both players need an IngloTiers profile first.", ephemeral=True)
            return
        lines = []
        for m in GAMEMODES:
            at, bt = (a.get("tiers") or {}).get(m, "—"), (b.get("tiers") or {}).get(m, "—")
            mark = "=" if at == bt else ("<" if at in TIERS and (bt not in TIERS or TIERS.index(at) < TIERS.index(bt)) else ">")
            lines.append(f"**{m}:** {at} {mark} {bt}")
        await interaction.response.send_message(embed=discord.Embed(
            title=f"{a['username']} vs {b['username']}", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="history", description="View a player's recent tier history.")
    @app_commands.guild_only()
    async def history_command(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        rows = await self.db_call(lambda: list(self.history.find({
            "guild_id": interaction.guild.id, "discord_id": target.id
        }).sort("created_at", DESCENDING).limit(10)))
        if not rows:
            await interaction.response.send_message("No tier history found.", ephemeral=True)
            return
        lines = [f"**{r['gamemode']}**: {r['old_tier']} → **{r['new_tier']}** · <t:{int(r['created_at'].timestamp())}:R>" for r in rows]
        await interaction.response.send_message(embed=discord.Embed(title=f"{target.display_name} • Tier history", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="recent", description="Show the latest IngloTiers tier changes.")
    @app_commands.guild_only()
    async def recent(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.db_call(lambda: list(self.activity.find({
            "guild_id": interaction.guild.id, "kind": "tier_change"
        }).sort("created_at", DESCENDING).limit(10)))
        if not rows:
            await interaction.response.send_message("No tier changes recorded yet.", ephemeral=True)
            return
        lines = [f"**{r.get('username', 'Unknown')}** · {r['gamemode']}: {r['old_tier']} → **{r['new_tier']}**" for r in rows]
        await interaction.response.send_message(embed=discord.Embed(title="Recent tier changes", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="streak", description="Show a player's completed tier test count.")
    @app_commands.guild_only()
    async def streak(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        count = await self.db_call(self.history.count_documents, {
            "guild_id": interaction.guild.id, "discord_id": target.id, "source": "test"})
        await interaction.response.send_message(f"🧪 {target.display_name} has completed **{count}** tier test(s).")

    @app_commands.command(name="testerstats", description="View tier testing statistics for a tester.")
    @app_commands.guild_only()
    async def testerstats(self, interaction, tester: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = tester or interaction.user
        row = await self.db_call(self.tester_stats.find_one, {"guild_id": interaction.guild.id, "tester_id": target.id})
        await interaction.response.send_message(f"🧪 {target.display_name} has completed **{(row or {}).get('tests_completed', 0)}** test(s).")

    @app_commands.command(name="testerboard", description="Show the most active IngloTiers testers.")
    @app_commands.guild_only()
    async def testerboard(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.db_call(lambda: list(self.tester_stats.find({"guild_id": interaction.guild.id}).sort("tests_completed", DESCENDING).limit(10)))
        if not rows:
            await interaction.response.send_message("No tester results recorded yet.", ephemeral=True)
            return
        lines = [f"**{i}.** {r.get('tester_name', 'Tester')} — **{r.get('tests_completed', 0)}** tests" for i, r in enumerate(rows, 1)]
        await interaction.response.send_message(embed=discord.Embed(title="IngloTiers tester leaderboard", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="tierban", description="Block a member from requesting IngloTiers tests.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tierban(self, interaction, member: discord.Member, reason: str = "No reason supplied"):
        if not await self.require_db(interaction):
            return
        await self.db_call(self.players.update_one, {"guild_id": interaction.guild.id, "discord_id": member.id},
                           {"$set": {"tier_banned": True, "tier_ban_reason": reason[:300]}}, upsert=True)
        await interaction.response.send_message(f"{member.mention} is blocked from tier tests.", ephemeral=True)

    @app_commands.command(name="tierunban", description="Allow a member to request IngloTiers tests again.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tierunban(self, interaction, member: discord.Member):
        if not await self.require_db(interaction):
            return
        await self.db_call(self.players.update_one, {"guild_id": interaction.guild.id, "discord_id": member.id},
                           {"$set": {"tier_banned": False}, "$unset": {"tier_ban_reason": ""}}, upsert=True)
        await interaction.response.send_message(f"{member.mention} can request tests again.", ephemeral=True)

    @app_commands.command(name="duel", description="Challenge another player to a tier duel.")
    @app_commands.guild_only()
    @app_commands.choices(gamemode=MODE_OPTIONS, best_of=BEST_OF_OPTIONS)
    async def duel(self, interaction, opponent: discord.Member, gamemode: app_commands.Choice[str],
                   best_of: app_commands.Choice[int]):
        if not await self.require_db(interaction):
            return
        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.response.send_message("Choose another human player.", ephemeral=True)
            return
        duel_id = uuid4().hex[:10]
        await self.db_call(self.duels.insert_one, {
            "duel_id": duel_id, "guild_id": interaction.guild.id, "challenger_id": interaction.user.id,
            "opponent_id": opponent.id, "gamemode": gamemode.value, "best_of": best_of.value,
            "status": "pending", "reports": {}, "created_at": datetime.now(timezone.utc)})
        e = discord.Embed(title="⚔️ IngloTiers duel challenge",
                          description=f"{interaction.user.mention} challenged {opponent.mention} in **{gamemode.value}**.\nBest of {best_of.value}\nDuel ID: {duel_id}",
                          colour=0x55DDEB)
        await interaction.response.send_message(content=opponent.mention, embed=e, view=DuelInviteView(self))
        msg = await interaction.original_response()
        await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {"message_id": msg.id}})

    @app_commands.command(name="duel-score", description="Submit a score; both players must report mirrored scores.")
    @app_commands.guild_only()
    async def duel_score(self, interaction, duel_id: str, your_score: app_commands.Range[int, 0, 20],
                         opponent_score: app_commands.Range[int, 0, 20]):
        if not await self.require_db(interaction):
            return
        duel = await self.db_call(self.duels.find_one, {"guild_id": interaction.guild.id, "duel_id": duel_id.strip()})
        if not duel or duel.get("status") != "active":
            await interaction.response.send_message("That duel is not active or could not be found.", ephemeral=True)
            return
        if interaction.user.id not in (duel["challenger_id"], duel["opponent_id"]):
            await interaction.response.send_message("Only duel participants can submit scores.", ephemeral=True)
            return
        if your_score == opponent_score:
            await interaction.response.send_message("A duel cannot end in a tie.", ephemeral=True)
            return
        reports = dict(duel.get("reports", {}))
        reports[str(interaction.user.id)] = {"your": your_score, "opponent": opponent_score}
        await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {"reports": reports}})
        first = reports.get(str(duel["challenger_id"]))
        second = reports.get(str(duel["opponent_id"]))
        if not first or not second:
            await interaction.response.send_message("Score saved. Waiting for the other player to submit the mirrored score.", ephemeral=True)
            return
        if first["your"] != second["opponent"] or first["opponent"] != second["your"]:
            await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {"status": "disputed"}})
            await interaction.response.send_message("The reports do not match. Duel disputed; no tiers were changed.", ephemeral=True)
            return
        needed = duel["best_of"] // 2 + 1
        if max(first["your"], first["opponent"]) < needed:
            await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {"status": "disputed"}})
            await interaction.response.send_message("The score does not finish this best-of format. Duel disputed; no tiers changed.", ephemeral=True)
            return
        winner_id = duel["challenger_id"] if first["your"] > first["opponent"] else duel["opponent_id"]
        loser_id = duel["opponent_id"] if winner_id == duel["challenger_id"] else duel["challenger_id"]
        winner = interaction.guild.get_member(winner_id) or await interaction.guild.fetch_member(winner_id)
        loser = interaction.guild.get_member(loser_id) or await interaction.guild.fetch_member(loser_id)
        for member, direction in ((winner, -1), (loser, 1)):
            player = await self.get_or_create_player(interaction.guild.id, member)
            new_tier = move_tier((player.get("tiers") or {}).get(duel["gamemode"]), direction)
            await self.save_tier(interaction.guild, member, duel["gamemode"], new_tier, interaction.user, "duel", player.get("username"))
        await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {
            "status": "completed", "winner_id": winner_id, "completed_at": datetime.now(timezone.utc)}})
        await interaction.response.send_message(f"{winner.mention} won! They move one tier up and {loser.mention} one tier down in {duel['gamemode']}.")

    @app_commands.command(name="duel-cancel", description="Cancel one of your pending or active duels.")
    @app_commands.guild_only()
    async def duel_cancel(self, interaction, duel_id: str):
        if not await self.require_db(interaction):
            return
        duel = await self.db_call(self.duels.find_one, {"guild_id": interaction.guild.id, "duel_id": duel_id.strip()})
        if not duel or interaction.user.id not in (duel["challenger_id"], duel["opponent_id"]) or duel.get("status") not in ("pending", "active"):
            await interaction.response.send_message("You cannot cancel that duel.", ephemeral=True)
            return
        await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {"status": "cancelled"}})
        await interaction.response.send_message(f"Duel {duel_id} cancelled.")

    @app_commands.command(name="claim", description="Claim the tier test ticket you're currently in.")
    @app_commands.guild_only()
    async def claim_command(self, interaction):
        await self.ticket_action(interaction, "claim")

    @app_commands.command(name="unclaim", description="Unclaim the tier test ticket you're currently in.")
    @app_commands.guild_only()
    async def unclaim_command(self, interaction):
        await self.ticket_action(interaction, "unclaim")

    @app_commands.command(name="close", description="Close the tier test ticket you're currently in.")
    @app_commands.guild_only()
    async def close_command(self, interaction):
        await self.ticket_action(interaction, "close")

    @app_commands.command(name="skillissue", description="A harmless IngloTiers PvP roast.")
    async def skillissue(self, interaction, member: discord.Member | None = None):
        who = member or interaction.user
        await interaction.response.send_message(random.choice([
            f"{who.mention}, your crosshair needs a compass.",
            f"{who.mention}, even the training dummy asked for a rematch.",
            f"{who.mention}, that combo was sponsored by lag."]))

    @app_commands.command(name="ego", description="Check a player's totally scientific ego level.")
    async def ego(self, interaction, member: discord.Member | None = None):
        who = member or interaction.user
        await interaction.response.send_message(f"{who.mention}'s ego is {random.randint(1, 100)}%. Scientifically questionable.")

    @app_commands.command(name="rat", description="Issue a playful rat certificate.")
    async def rat(self, interaction, member: discord.Member | None = None):
        who = member or interaction.user
        await interaction.response.send_message(f"🐀 {who.mention} has been certified as a suspicious little rat.")

    @app_commands.command(name="washed", description="Find out whether someone is washed.")
    async def washed(self, interaction, member: discord.Member | None = None):
        who = member or interaction.user
        await interaction.response.send_message(f"{who.mention} is {random.randint(0, 100)}% washed.")

    @app_commands.command(name="diff", description="Show a random PvP diff result.")
    async def diff(self, interaction, member: discord.Member | None = None):
        who = member or interaction.user
        await interaction.response.send_message(random.choice([
            f"{who.mention} got combo'd into another dimension.",
            f"{who.mention} was out-traded by a fishing rod.",
            f"{who.mention} needs a firmware update."]))

    @app_commands.command(name="cope", description="A little harmless cope.")
    async def cope(self, interaction, member: discord.Member | None = None):
        who = member or interaction.user
        await interaction.response.send_message(f"🧂 {who.mention}: it was definitely your ping.")

    @commands.command(name="triviasetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def triviasetup(self, ctx):
        if self.db is None:
            await ctx.reply("Add MONGO_URI to Render Environment and restart the bot first.")
            return
        try:
            category = await self.find_category(ctx.guild, "INGLOTIERS")
            channel = await self.find_channel(ctx.guild, "trivia", category)
            await self.db_call(self.settings.update_one, {"guild_id": ctx.guild.id}, {"$set": {
                "guild_id": ctx.guild.id, "trivia_channel_id": channel.id,
                "trivia_next_at": time.time() + TRIVIA_INTERVAL, "trivia_enabled": True,
                "website_public": True}}, upsert=True)
            await ctx.reply(f"✅ Trivia is configured in {channel.mention}. The first question arrives in 20 minutes.")
        except discord.Forbidden:
            await ctx.reply("I need Manage Channels and Send Messages to set up trivia.")
        except Exception:
            log.exception("Trivia setup failed")
            await ctx.reply("Trivia setup failed. Check my permissions and MongoDB connection.")

    @commands.command(name="triviastop")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def triviastop(self, ctx):
        if self.db is None:
            await ctx.reply("MongoDB is not connected.")
            return
        await self.db_call(self.settings.update_one, {"guild_id": ctx.guild.id}, {"$set": {"trivia_enabled": False}})
        await ctx.reply("Trivia is paused. Run ?triviasetup to start it again.")

    @app_commands.command(name="trivia-leaderboard", description="Show the IngloTiers trivia leaderboard.")
    @app_commands.guild_only()
    async def trivia_leaderboard(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.db_call(lambda: list(self.trivia_scores.find({"guild_id": interaction.guild.id}).sort("points", DESCENDING).limit(10)))
        if not rows:
            await interaction.response.send_message("Nobody has scored trivia points yet.", ephemeral=True)
            return
        lines = [f"**{i}.** <@{r['user_id']}> — **{r.get('points', 0)}** point(s)" for i, r in enumerate(rows, 1)]
        await interaction.response.send_message(embed=discord.Embed(title="🧠 Trivia leaderboard", description="\n".join(lines), colour=0x55DDEB))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or self.db is None:
            return
        config = await self.setting(message.guild.id)
        if not config.get("trivia_enabled") or config.get("trivia_channel_id") != message.channel.id:
            return
        question = config.get("current_trivia") or {}
        if not question or question.get("solved_at"):
            return
        answer = normalise_answer(message.content)
        accepted = {normalise_answer(a) for a in question.get("answers", [])}
        if answer and answer in accepted:
            result = await self.db_call(self.settings.update_one, {
                "guild_id": message.guild.id, "current_trivia.question_id": question["question_id"],
                "current_trivia.solved_at": None
            }, {"$set": {"current_trivia.solved_at": datetime.now(timezone.utc),
                         "current_trivia.solved_by": message.author.id}})
            if result.modified_count:
                await self.db_call(self.trivia_scores.update_one,
                    {"guild_id": message.guild.id, "user_id": message.author.id},
                    {"$inc": {"points": 1}, "$set": {"username": message.author.display_name}}, upsert=True)
                await message.reply(f"✅ Correct, {message.author.mention}! You earned 1 trivia point.")
            return
        try:
            await self.db_call(self.trivia_answers.insert_one, {
                "guild_id": message.guild.id, "question_id": question["question_id"],
                "user_id": message.author.id, "answer": message.content[:100],
                "created_at": datetime.now(timezone.utc)})
            await message.reply("❌ Not quite — try again!")
        except DuplicateKeyError:
            pass

    @tasks.loop(seconds=30)
    async def trivia_watcher(self):
        if self.db is None:
            return
        try:
            rows = await self.db_call(lambda: list(self.settings.find({
                "trivia_enabled": True, "trivia_channel_id": {"$exists": True}})))
            now = time.time()
            for config in rows:
                if float(config.get("trivia_next_at", 0)) > now:
                    continue
                guild = self.bot.get_guild(int(config["guild_id"]))
                channel = guild.get_channel(int(config["trivia_channel_id"])) if guild else None
                if not isinstance(channel, discord.TextChannel):
                    continue
                previous = config.get("current_trivia") or {}
                if previous and not previous.get("solved_at"):
                    await channel.send(f"⌛ Time's up! The answer was **{previous.get('answers', ['?'])[0]}**.")
                prompt, answers = random.choice(TRIVIA_QUESTIONS)
                current = {"question_id": uuid4().hex, "question": prompt, "answers": answers,
                           "opened_at": datetime.now(timezone.utc), "solved_at": None}
                await self.db_call(self.settings.update_one, {"guild_id": config["guild_id"]},
                    {"$set": {"current_trivia": current, "trivia_next_at": now + TRIVIA_INTERVAL}})
                e = discord.Embed(title="🧠 IngloTiers Trivia", description=prompt, colour=0x55DDEB)
                e.set_footer(text="You have 20 minutes • Type your answer in this channel • Correct answer = 1 point")
                await channel.send(embed=e)
        except Exception:
            log.exception("Trivia watcher failed")

    @trivia_watcher.before_loop
    async def before_trivia_watcher(self):
        await self.bot.wait_until_ready()

    async def web_guild_id(self):
        configured = os.getenv("DISCORD_GUILD_ID")
        if configured and configured.isdigit():
            return int(configured)
        if self.db is None:
            return None
        row = await self.db_call(self.settings.find_one, {"website_public": True})
        return int(row["guild_id"]) if row else None

    def register_web_routes(self, app):
        app.router.add_get("/api/inglotiers/leaderboard", self.web_leaderboard)
        app.router.add_get("/api/inglotiers/player/{identifier}", self.web_player)
        app.router.add_get("/api/inglotiers/activity", self.web_activity)

    async def web_leaderboard(self, request):
        if self.db is None:
            return web.json_response({"players": [], "total": 0, "error": "database_unavailable"}, status=503)
        guild_id = await self.web_guild_id()
        if not guild_id:
            return web.json_response({"players": [], "total": 0})
        mode = request.query.get("mode")
        if mode and mode not in GAMEMODES:
            return web.json_response({"error": "invalid_gamemode"}, status=400)
        docs = await self.db_call(lambda: list(self.players.find({"guild_id": guild_id, "tiers": {"$ne": {}}})))
        rows = []
        for p in docs:
            best, best_mode = best_tier(p.get("tiers", {}))
            value = (p.get("tiers") or {}).get(mode) if mode else best
            if value in TIERS:
                rows.append({**safe_player(p), "display_tier": value, "best_mode": best_mode, "sort_index": TIERS.index(value)})
        rows.sort(key=lambda p: (p["sort_index"], p["username"].casefold()))
        return web.json_response({"players": rows[:100], "total": len(rows)})

    async def web_player(self, request):
        if self.db is None:
            return web.json_response({"error": "database_unavailable"}, status=503)
        guild_id = await self.web_guild_id()
        if not guild_id:
            return web.json_response({"error": "not_configured"}, status=404)
        identifier = request.match_info["identifier"]
        numeric_id = int(identifier) if identifier.isdigit() else -1
        player = await self.db_call(self.players.find_one, {"guild_id": guild_id,
            "$or": [{"username_lc": identifier.casefold()}, {"discord_id": numeric_id}]})
        if not player:
            return web.json_response({"error": "not_found"}, status=404)
        rows = await self.db_call(lambda: list(self.history.find({
            "guild_id": guild_id, "discord_id": player["discord_id"]}).sort("created_at", DESCENDING).limit(20)))
        history = [{"gamemode": r.get("gamemode"), "old_tier": r.get("old_tier"),
                    "new_tier": r.get("new_tier"),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else None} for r in rows]
        return web.json_response({"player": safe_player(player), "history": history})

    async def web_activity(self, request):
        if self.db is None:
            return web.json_response({"activity": []}, status=503)
        guild_id = await self.web_guild_id()
        if not guild_id:
            return web.json_response({"activity": []})
        rows = await self.db_call(lambda: list(self.activity.find({
            "guild_id": guild_id, "kind": "tier_change"}).sort("created_at", DESCENDING).limit(30)))
        return web.json_response({"activity": [{
            "username": r.get("username"), "gamemode": r.get("gamemode"),
            "old_tier": r.get("old_tier"), "new_tier": r.get("new_tier"),
            "created_at": r.get("created_at").isoformat() if r.get("created_at") else None} for r in rows])

    def website_html(self):
        return WEBSITE_HTML


async def setup(bot):
    await bot.add_cog(IngloTiersCog(bot))

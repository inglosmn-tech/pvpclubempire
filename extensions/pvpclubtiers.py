"""PvPClubTiers tier testing, player rankings, MongoDB persistence, website and trivia."""
from __future__ import annotations

import asyncio
import logging
import math
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

log = logging.getLogger("pvpclub-empire.pvpclubtiers")

GAMEMODES = [
    "Spear Mace", "Sword", "Axe", "UHC", "Elytra",
    "Creeper", "Diamond SMP", "Mace", "SMP", "Vanilla", "Cart",
]
QUEUE_CAPACITY = 20
MODE_EMOJIS = {
    "Spear Mace": "⚔️", "Sword": "🗡️", "Axe": "🪓", "UHC": "🛡️",
    "Elytra": "🪽", "Creeper": "💥", "Diamond SMP": "💎", "Mace": "🔨",
    "SMP": "🌿", "Vanilla": "🔮", "Cart": "🛒",
}
MODE_BEST_OF = {
    "Spear Mace": 4, "Sword": 4, "Axe": 4, "UHC": 4, "Elytra": 4,
    "Creeper": 3, "Diamond SMP": 4, "Mace": 4, "SMP": 4, "Vanilla": 3, "Cart": 3,
}
TIER_WEBSITE_ENV = "PVPCLUBTIERS_WEBSITE_URL"
TIERS = ["HT1", "LT1", "HT2", "LT2", "HT3", "LT3", "HT4", "LT4", "HT5", "LT5"]
POINTS = {"HT1": 60, "LT1": 45, "HT2": 30, "LT2": 20, "HT3": 10, "LT3": 6, "HT4": 4, "LT4": 3, "HT5": 2, "LT5": 1}
COLOURS = {
    "HT1": 0xF1C40F, "LT1": 0xE67E22, "HT2": 0xE74C3C, "LT2": 0xC0392B,
    "HT3": 0x9B59B6, "LT3": 0x8E44AD, "HT4": 0x3498DB, "LT4": 0x2980B9,
    "HT5": 0x2ECC71, "LT5": 0x27AE60,
}
MODE_OPTIONS = [app_commands.Choice(name=m, value=m) for m in GAMEMODES]
TIER_OPTIONS = [app_commands.Choice(name=t, value=t) for t in TIERS]
BEST_OF_OPTIONS = [app_commands.Choice(name=str(n), value=n) for n in (1, 3, 5, 7)]
TRIVIA_INTERVAL = 20 * 60

TRIVIA_QUESTIONS = [
    ("What Minecraft dimension contains the Ender Dragon?", ["the end", "end"]),
    ("Which ore is used to craft a Netherite ingot?", ["ancient debris"]),
    ("What item activates an End Portal frame?", ["eye of ender", "eyes of ender"]),
    ("What is the boss mob in an Ocean Monument?", ["elder guardian"]),
    ("Which Minecraft weapon deals extra damage when falling?", ["mace", "the mace"]),
    ("How many inventory slots are in the main inventory, excluding the hotbar?", ["27"]),
    ("Which mob explodes when it gets close to a player?", ["creeper"]),
    ("What is the strongest tier in PvPClubTiers?", ["ht1"]),
    ("What is the weakest tier in PvPClubTiers?", ["lt5"]),
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

def plain_discord_name(value: str) -> str:
    value = re.sub(r"^『[^』]+』\s*", "", value.strip())
    value = re.sub(r"^[^│|]+[│|]\s*", "", value)
    value = re.sub(r"^[^a-zA-Z0-9]+", "", value)
    return value.strip().casefold()


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


def move_tier(tier: str | None, direction: int) -> str | None:
    if tier not in TIERS:
        return "LT5" if direction < 0 else None
    index = TIERS.index(tier)
    return TIERS[max(0, min(len(TIERS) - 1, index + direction))]


# TIERS is stored strongest-first, so reverse it for upward progression from weakest to strongest.
DUEL_TIER_ORDER = list(reversed(TIERS))


def move_duel_tier(tier: str | None, steps: int) -> str:
    if tier not in DUEL_TIER_ORDER:
        return "LT5"
    index = DUEL_TIER_ORDER.index(tier)
    return DUEL_TIER_ORDER[max(0, min(len(DUEL_TIER_ORDER) - 1, index + steps))]



def total_tier_points(tiers: dict | None) -> int:
    """Count points only for currently supported PvPClubTiers gamemodes."""
    return sum(
        POINTS.get(tier, 0)
        for mode, tier in (tiers or {}).items()
        if mode in GAMEMODES and tier in TIERS
    )


def safe_player(player: dict) -> dict:
    stored_tiers = player.get("tiers", {}) or {}
    # Only currently supported PvPClubTiers gamemodes should be exposed to clients.
    tiers = {
        mode: tier for mode, tier in stored_tiers.items()
        if mode in GAMEMODES and tier in TIERS
    }
    total_points = total_tier_points(tiers)
    return {
        "discordId": str(player.get("discord_id", "")),
        "ign": player.get("username") or "Unknown",
        "username": player.get("username") or "Unknown",
        "region": "EU",
        "server": "mcpvp.club",
        "tiers": tiers,
        "totalPoints": total_points,
        "elo": int(player.get("elo", 1000)),
        "wins": int(player.get("wins", 0)),
        "losses": int(player.get("losses", 0)),
        "duelsPlayed": int(player.get("duels_played", 0)),
        "duelLossStreaks": player.get("duel_loss_streaks", {}) or {},
        "winRate": round((int(player.get("wins", 0)) / max(1, int(player.get("wins", 0)) + int(player.get("losses", 0)))) * 100, 1),
        "createdAt": player.get("created_at").isoformat() if player.get("created_at") else None,
    }


WEBSITE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PvPClubTiers — MCPvP Rankings</title>
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
</style></head><body><header><div class="brand"><div class="logo">P</div><div><strong>PvPClubTiers</strong><small>EU Minecraft PvP • mcpvp.club</small></div></div><span class="pill">PLAYER RANKINGS</span></header>
<main><section class="hero"><div class="eyebrow">The community tier list</div><h1>Find your place<br>on the leaderboard.</h1><p>Browse player tiers across every supported gamemode, compare profiles and follow recent tier changes.</p></section>
<section class="panel"><div class="toolbar"><select id="mode"><option value="">Overall ranking</option></select><input id="search" placeholder="Search Minecraft username…"><button id="searchBtn">Search</button></div>
<div class="stats"><div class="stat"><small>Ranked players</small><strong id="players">—</strong></div><div class="stat"><small>Gamemodes</small><strong>11</strong></div><div class="stat"><small>Tier levels</small><strong>10</strong></div></div>
<div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Player</th><th>Best mode</th><th>Best tier</th></tr></thead><tbody id="rows"><tr><td colspan="4" class="empty">Loading leaderboard…</td></tr></tbody></table></div></section>
<section class="panel profile" id="profile"><div class="eyebrow">Player profile</div><h2 id="profileName"></h2><div class="muted" id="profileBest"></div><div class="profile-grid" id="profileModes"></div><h3>Recent tier history</h3><div id="profileHistory" class="muted"></div></section></main><footer>PvPClubTiers • EU only • mcpvp.club</footer>
<script>
const modes=["Spear Mace","Sword","Axe","UHC","Elytra","Creeper","Diamond SMP","Mace","SMP","Vanilla","Cart"];const modeSelect=document.getElementById("mode");
modes.forEach(m=>{const o=document.createElement("option");o.value=m;o.textContent=m;modeSelect.appendChild(o)});
function badge(t){const s=document.createElement("span");s.className="tier";s.textContent=t||"Unranked";return s}
async function load(){const mode=modeSelect.value;const body=document.getElementById("rows");body.innerHTML='<tr><td colspan="4" class="empty">Loading…</td></tr>';try{const r=await fetch("/api/pvpclubtiers/leaderboard"+(mode?"?mode="+encodeURIComponent(mode):""));const x=await r.json();document.getElementById("players").textContent=x.total??0;body.innerHTML="";if(!x.players||!x.players.length){body.innerHTML='<tr><td colspan="4" class="empty">No ranked players yet. Ask staff to set up PvPClubTiers.</td></tr>';return}x.players.forEach((p,i)=>{const tr=document.createElement("tr");const rank=document.createElement("td");rank.textContent=i+1;const name=document.createElement("td");name.className="player";name.textContent=p.username;name.onclick=()=>profile(p.username);const bm=document.createElement("td");bm.textContent=p.best_mode||"—";bm.className="muted";const tier=document.createElement("td");tier.appendChild(badge(mode?(p.tiers||{})[mode]:p.best_tier));tr.append(rank,name,bm,tier);body.appendChild(tr)})}catch(e){body.innerHTML='<tr><td colspan="4" class="empty">Leaderboard is temporarily unavailable.</td></tr>'}}
async function profile(name){try{const r=await fetch("/api/pvpclubtiers/player/"+encodeURIComponent(name));if(!r.ok)return;const x=await r.json();document.getElementById("profile").style.display="block";document.getElementById("profileName").textContent=x.player.username;document.getElementById("profileBest").textContent="Best tier: "+(x.player.best_tier||"Unranked")+" · "+(x.player.best_mode||"No ranked mode");const box=document.getElementById("profileModes");box.innerHTML="";modes.forEach(m=>{const d=document.createElement("div");d.className="mode-card";const sm=document.createElement("small");sm.textContent=m;d.append(sm,badge((x.player.tiers||{})[m]));box.appendChild(d)});document.getElementById("profileHistory").textContent=x.history&&x.history.length?x.history.map(h=>h.gamemode+": "+h.old_tier+" → "+h.new_tier).join(" · "):"No tier history yet.";document.getElementById("profile").scrollIntoView({behavior:"smooth",block:"start"})}catch(e){}}
modeSelect.addEventListener("change",load);document.getElementById("searchBtn").onclick=()=>profile(document.getElementById("search").value.trim());document.getElementById("search").addEventListener("keydown",e=>{if(e.key==="Enter")document.getElementById("searchBtn").click()});load();
</script></body></html>"""


class TesterApplicationPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Apply to become a tester", style=discord.ButtonStyle.primary,
                       emoji="🧪", custom_id="pvpclubtiers:tester-app:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        # This panel is only posted by ?testerappsetup, so do not query MongoDB
        # just to display the gamemode menu. Keep the button response immediate;
        # the review-channel setting is checked after the applicant submits the modal.
        if self.cog.db is None:
            await self.cog.require_db(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Applications can only be submitted inside the server.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose the gamemode you want to become a tester for.",
            view=TesterApplicationModeSelectView(self.cog),
            ephemeral=True,
        )


class TesterApplicationModeSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(
            placeholder="Choose a gamemode…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=mode, value=mode) for mode in GAMEMODES],
            custom_id="pvpclubtiers:tester-app:mode",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(TesterApplicationModal(self.cog, self.values[0]))
        except Exception:
            log.exception("Could not open tester application modal for user %s", interaction.user.id)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "I couldn't open the application form. Please click Apply again.",
                    ephemeral=True,
                )


class TesterApplicationModeSelectView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=180)
        self.add_item(TesterApplicationModeSelect(self.cog))


class TesterApplicationModal(discord.ui.Modal):
    def __init__(self, cog, gamemode: str):
        super().__init__(title=f"{gamemode} tester application")
        self.cog = cog
        self.gamemode = gamemode
        self.minecraft_username = discord.ui.TextInput(
            label="Minecraft username", min_length=3, max_length=16, required=True,
            placeholder="Your in-game username",
        )
        self.current_tier = discord.ui.TextInput(
            label="Your current tier / rank",
            min_length=2,
            max_length=100,
            required=True,
            placeholder="Example: LT2 Sword, HT3 overall, or Unranked",
        )
        self.experience = discord.ui.TextInput(
            label="Previous testing experience",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
            placeholder="Optional",
        )
        self.reason = discord.ui.TextInput(
            label="Why would you be a good tester?",
            style=discord.TextStyle.paragraph,
            min_length=20,
            max_length=1000,
            required=True,
            placeholder="Tell us why you would be fair and reliable",
        )
        self.add_item(self.minecraft_username)
        self.add_item(self.current_tier)
        self.add_item(self.experience)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        # Acknowledge the modal before any database checks or other work.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog.submit_tester_application(
                interaction, self.gamemode, str(self.minecraft_username),
                str(self.current_tier), str(self.experience), str(self.reason),
                interaction_deferred=True,
            )
        except Exception:
            log.exception("Unhandled tester application modal submission error for user %s", interaction.user.id)
            try:
                await interaction.followup.send(
                    "I couldn't submit your application. Please try again or contact an administrator.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.exception("Could not send tester application failure message to user %s", interaction.user.id)


class TesterApplicationReviewView(discord.ui.View):
    def __init__(self, cog, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        if disabled:
            for child in self.children:
                child.disabled = True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="pvpclubtiers:tester-app:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.review_tester_application(interaction, accepted=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger,
                       emoji="✖️", custom_id="pvpclubtiers:tester-app:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.review_tester_application(interaction, accepted=False)


class TierInfoPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

        website_url = self.cog.website_url()
        if website_url:
            self.add_item(discord.ui.Button(
                label="Tier list website",
                style=discord.ButtonStyle.link,
                emoji="🌐",
                url=website_url,
                row=1,
            ))

    @discord.ui.button(
        label="Apply for Tier Tester",
        style=discord.ButtonStyle.primary,
        emoji="🧪",
        custom_id="pvpclubtiers:tier-info:apply",
        row=0,
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.cog.db is None:
            await self.cog.require_db(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Applications are available inside the server.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose the gamemode you want to become a tester for. The application form will open next.",
            view=TesterApplicationModeSelectView(self.cog),
            ephemeral=True,
        )

    @discord.ui.button(
        label="My tiers",
        style=discord.ButtonStyle.secondary,
        emoji="🏅",
        custom_id="pvpclubtiers:tier-info:my-tiers",
        row=0,
    )
    async def my_tiers(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_my_tiers(interaction)


class LeaderboardPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        website_url = self.cog.website_url()
        if website_url:
            self.add_item(discord.ui.Button(
                label="Tier list website",
                style=discord.ButtonStyle.link,
                emoji="🌐",
                url=website_url,
            ))

    @discord.ui.button(
        label="Refresh leaderboard",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="pvpclubtiers:leaderboard:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog.ensure_leaderboard_panel(interaction.guild)
            await interaction.followup.send("✅ The leaderboard has been refreshed.", ephemeral=True)
        except Exception:
            log.exception("Manual leaderboard refresh failed in guild %s", interaction.guild.id)
            await interaction.followup.send("I couldn't refresh the leaderboard. Check the Render logs.", ephemeral=True)


class OwnerSetTierSelect(discord.ui.Select):
    def __init__(self, cog, gamemode: str, username: str, player_id: int, current_tier: str | None):
        self.cog = cog
        self.gamemode = gamemode
        self.username = username
        self.player_id = player_id
        options = [
            discord.SelectOption(
                label=tier,
                value=tier,
                description=f"{POINTS[tier]} tier points",
                default=(tier == current_tier),
            )
            for tier in TIERS
        ]
        super().__init__(
            placeholder="Choose the tier to assign…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="pvpclubtiers:owner-set-tier",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog.owner_set_tier_from_select(
            interaction,
            self.gamemode,
            self.username,
            self.player_id,
            self.values[0],
        )


class OwnerSetTierView(discord.ui.View):
    def __init__(self, cog, gamemode: str, username: str, player_id: int, current_tier: str | None):
        super().__init__(timeout=180)
        self.add_item(OwnerSetTierSelect(cog, gamemode, username, player_id, current_tier))


class TestQueueJoinModal(discord.ui.Modal):
    def __init__(self, cog, gamemode: str | None = None):
        title = f"Join {gamemode} queue" if gamemode else "Sign Up"
        super().__init__(title=title)
        self.cog = cog
        self.gamemode = gamemode
        self.username = discord.ui.TextInput(
            label="Minecraft username",
            placeholder="Your Minecraft Java username",
            min_length=3,
            max_length=16,
            required=True,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        if self.gamemode:
            await self.cog.join_test_queue(interaction, self.gamemode, str(self.username))
        else:
            await self.cog.save_ign_and_show_queue_modes(interaction, str(self.username))


class TestQueueJoinModesSelect(discord.ui.Select):
    def __init__(self, cog, username: str, allowed_modes: list[str]):
        self.cog = cog
        self.username = username
        self.allowed_modes = allowed_modes
        super().__init__(
            placeholder="Choose one or more open gamemodes…",
            min_values=1,
            max_values=len(allowed_modes),
            options=[
                discord.SelectOption(label=mode, value=mode, emoji=MODE_EMOJIS.get(mode))
                for mode in allowed_modes
            ],
            custom_id="inglotiers:test-queue:join-multiple-modes",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog.join_selected_test_queues(interaction, self.values, self.username)


class TestQueueJoinModesView(discord.ui.View):
    def __init__(self, cog, username: str, allowed_modes: list[str]):
        super().__init__(timeout=180)
        self.add_item(TestQueueJoinModesSelect(cog, username, allowed_modes))


class GamemodeTestQueueView(discord.ui.View):
    """Persistent Join/Leave buttons attached to one gamemode's queue card."""

    def __init__(self, cog, gamemode: str, *, is_open: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        self.gamemode = gamemode
        key = mode_key(gamemode)

        # Closed queues are visibly disabled on the public queue card. The
        # server-side join guard remains in place as the final safety check.
        join = discord.ui.Button(
            label="Join queue" if is_open else "Queue closed",
            style=discord.ButtonStyle.success if is_open else discord.ButtonStyle.secondary,
            emoji="✅" if is_open else "🔒",
            disabled=not is_open,
            custom_id=f"inglotiers:test-queue:join:{key}",
            row=0,
        )
        leave = discord.ui.Button(
            label="Leave queue",
            style=discord.ButtonStyle.secondary,
            emoji="🚪",
            custom_id=f"inglotiers:test-queue:leave:{key}",
            row=0,
        )

        async def join_callback(interaction: discord.Interaction):
            await self.cog.handle_queue_join_button(interaction, self.gamemode)

        async def leave_callback(interaction: discord.Interaction):
            await self.cog.leave_test_queue(interaction, self.gamemode)

        join.callback = join_callback
        leave.callback = leave_callback
        self.add_item(join)
        self.add_item(leave)


class TestQueuePanelView(discord.ui.View):
    """Persistent request panel: save one IGN, join multiple waitlists, manage entries."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Sign Up",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="inglotiers:test-queue:join",
        row=0,
    )
    async def join_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog.require_db(interaction):
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Use this request panel inside the server.",
                ephemeral=True,
            )
            return
        try:
            player = await self.cog.get_player(interaction.guild.id, interaction.user.id)
        except Exception:
            log.exception("Could not load the saved Minecraft IGN for %s", interaction.user.id)
            await interaction.response.send_message(
                "I couldn't load your saved Minecraft IGN. Please try again.",
                ephemeral=True,
            )
            return

        username = (player or {}).get("username")
        if username and re.fullmatch(r"[A-Za-z0-9_]{3,16}", str(username)):
            open_modes = await self.cog.open_test_queue_modes(interaction.guild)
            if not open_modes:
                await interaction.response.send_message(
                    "No test queues are open right now. You can join as soon as a tester opens one.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Your saved Minecraft IGN is **{username}**. Choose one or more open gamemodes:",
                view=TestQueueJoinModesView(self.cog, str(username), open_modes),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(TestQueueJoinModal(self.cog))

    @discord.ui.button(
        label="Leave all waitlists",
        style=discord.ButtonStyle.danger,
        emoji="🚪",
        custom_id="inglotiers:test-queue:leave-all",
        row=1,
    )
    async def leave_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.leave_all_test_waitlists(interaction)

    @discord.ui.button(
        label="Tester Controls",
        style=discord.ButtonStyle.primary,
        emoji="🛠️",
        custom_id="inglotiers:test-queue:tester",
        row=2,
    )
    async def tester_controls(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_tester_queue_controls(interaction)


class TesterQueueModeSelect(discord.ui.Select):
    def __init__(self, parent):
        self.parent_view = parent
        super().__init__(
            placeholder="Choose a gamemode you test…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=mode, value=mode) for mode in parent.allowed_modes],
            custom_id="inglotiers:test-queue:tester-mode",
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_gamemode = self.values[0]
        await interaction.response.edit_message(
            content=f"Selected **{self.values[0]}**. Open or close its queue, or start the next player.",
            view=self.parent_view,
        )


class TesterQueueControlsView(discord.ui.View):
    def __init__(self, cog, allowed_modes: list[str]):
        super().__init__(timeout=180)
        self.cog = cog
        self.allowed_modes = allowed_modes
        self.selected_gamemode = allowed_modes[0]
        self.add_item(TesterQueueModeSelect(self))

    @discord.ui.button(label="Open / Close Queue", style=discord.ButtonStyle.primary, emoji="🔄")
    async def toggle_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle_test_queue(interaction, self.selected_gamemode)

    @discord.ui.button(label="Start Next Test", style=discord.ButtonStyle.success, emoji="🧪")
    async def start_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_next_test(interaction, self.selected_gamemode)


class QueueTestSessionView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Close Test Session",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="inglotiers:test-session:close",
    )
    async def close_session(self, interaction: discord.Interaction, button: discord.ui.Button):
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



class MatchmakingPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def set_ping_role(self, interaction, enabled):
        if not await self.cog.require_db(interaction):
            return
        config = await self.cog.setting(interaction.guild.id)
        role_id = config.get("matchmaking_ping_role_id")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if role is None:
            await interaction.response.send_message("Matchmaking pings are not configured yet. Ask staff to run ?tiersetup.", ephemeral=True)
            return
        member = interaction.user
        try:
            if enabled and role not in member.roles:
                await member.add_roles(role, reason="Opted into matchmaking pings")
                message = "✅ You will now receive matchmaking pings."
            elif not enabled and role in member.roles:
                await member.remove_roles(role, reason="Opted out of matchmaking pings")
                message = "✅ You will no longer receive matchmaking pings."
            else:
                message = "You are already set up for matchmaking pings."
        except discord.Forbidden:
            message = "I cannot update that role. Staff must move my bot role above Matchmaking Ping."
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Enable Matchmaking Pings", style=discord.ButtonStyle.success,
                       emoji="🔔", custom_id="inglotiers:mm-pings-on")
    async def enable_pings(self, interaction, button):
        await self.set_ping_role(interaction, True)

    @discord.ui.button(label="Disable Matchmaking Pings", style=discord.ButtonStyle.secondary,
                       emoji="🔕", custom_id="inglotiers:mm-pings-off")
    async def disable_pings(self, interaction, button):
        await self.set_ping_role(interaction, False)


class MatchmakingQueueView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Cancel queue", style=discord.ButtonStyle.danger,
                       emoji="✖️", custom_id="inglotiers:mm-queue-cancel")
    async def cancel(self, interaction, button):
        await self.cog.cancel_matchmaking_queue(interaction)


class DuelModeSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        super().__init__(
            placeholder="Choose the gamemode you want to duel in…",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=mode, value=mode) for mode in GAMEMODES],
            custom_id="inglotiers:mm-mode-select",
        )

    async def callback(self, interaction):
        mode = self.values[0]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚔️ Join matchmaking?",
                description=f"You selected **{mode}**. Confirm to enter the queue. You will be matched with the next available player in this gamemode. Both players must confirm the winner after the duel.",
                colour=0x55DDEB,
            ),
            view=DuelQueueConfirmView(self.cog, mode),
            ephemeral=True,
        )


class DuelGamemodeSelectView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=180)
        self.add_item(DuelModeSelect(cog))


class DuelQueueConfirmView(discord.ui.View):
    def __init__(self, cog, gamemode):
        super().__init__(timeout=180)
        self.cog = cog
        self.gamemode = gamemode

    @discord.ui.button(label="Confirm and find opponent", style=discord.ButtonStyle.success, emoji="⚔️")
    async def confirm(self, interaction, button):
        await self.cog.queue_for_duel(interaction, self.gamemode)

    @discord.ui.button(label="Go back", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Matchmaking cancelled.", embed=None, view=None)


class DuelResultView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Player 1 won", style=discord.ButtonStyle.success,
                       custom_id="inglotiers:mm-result-player-1")
    async def player_one_won(self, interaction, button):
        await self.cog.record_matchmaking_result(interaction, player_number=1)

    @discord.ui.button(label="Player 2 won", style=discord.ButtonStyle.primary,
                       custom_id="inglotiers:mm-result-player-2")
    async def player_two_won(self, interaction, button):
        await self.cog.record_matchmaking_result(interaction, player_number=2)


class PvPClubTiersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_client = None
        self.db = None
        self.matchmaking_queue_lock = asyncio.Lock()
        self.test_queue_lock = asyncio.Lock()
        self.tester_application_lock = asyncio.Lock()

    async def db_call(self, function, *args, **kwargs):
        if self.db is None:
            raise RuntimeError("MongoDB is not configured. Add MONGO_URI in Render Environment and restart the bot.")
        return await asyncio.to_thread(function, *args, **kwargs)

    async def cog_load(self):
        uri = os.getenv("MONGO_URI")
        if uri:
            try:
                self.mongo_client = MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                                                appname="PVPClubEmpire-PvPClubTiers")
                await asyncio.to_thread(self.mongo_client.admin.command, "ping")
                # Keep the original MongoDB database namespace so existing player profiles,
                # tier history, tickets and duels remain available after this rebrand.
                self.db = self.mongo_client["inglotiers"]
                self.players = self.db["players"]
                self.settings = self.db["guild_settings"]
                self.tickets = self.db["tickets"]
                self.history = self.db["tier_history"]
                self.activity = self.db["activity"]
                self.tester_stats = self.db["tester_stats"]
                self.duels = self.db["duels"]
                self.matchmaking_queue = self.db["matchmaking_queue"]
                self.test_queue_entries = self.db["test_queue_entries"]
                self.trivia_scores = self.db["trivia_scores"]
                self.trivia_answers = self.db["trivia_answers"]
                self.tester_applications = self.db["tester_applications"]
                await asyncio.to_thread(self.players.create_index, [("guild_id", ASCENDING), ("discord_id", ASCENDING)], unique=True)
                await asyncio.to_thread(self.players.create_index, [("guild_id", ASCENDING), ("username_lc", ASCENDING)])
                await asyncio.to_thread(self.tickets.create_index, "channel_id", unique=True, sparse=True)
                await asyncio.to_thread(self.history.create_index, [("guild_id", ASCENDING), ("created_at", DESCENDING)])
                await asyncio.to_thread(self.duels.create_index, "duel_id", unique=True)
                await asyncio.to_thread(self.matchmaking_queue.create_index, [("guild_id", ASCENDING), ("gamemode", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
                await asyncio.to_thread(self.matchmaking_queue.create_index, [("guild_id", ASCENDING), ("user_id", ASCENDING), ("status", ASCENDING)])
                await asyncio.to_thread(self.test_queue_entries.create_index, [("guild_id", ASCENDING), ("gamemode", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
                await asyncio.to_thread(self.test_queue_entries.create_index, [("guild_id", ASCENDING), ("discord_id", ASCENDING), ("status", ASCENDING)])
                await asyncio.to_thread(self.trivia_scores.create_index, [("guild_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
                await asyncio.to_thread(self.trivia_answers.create_index, [("guild_id", ASCENDING), ("question_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
                await asyncio.to_thread(self.tester_applications.create_index, [("guild_id", ASCENDING), ("review_message_id", ASCENDING)], unique=True)
                await asyncio.to_thread(self.tester_applications.create_index, [("guild_id", ASCENDING), ("applicant_id", ASCENDING), ("gamemode", ASCENDING), ("status", ASCENDING)])
                log.info("PvPClubTiers MongoDB connected.")
            except Exception:
                log.exception("PvPClubTiers MongoDB connection failed; tier features are disabled.")
                if self.mongo_client:
                    self.mongo_client.close()
                self.mongo_client = None
                self.db = None
        else:
            log.warning("MONGO_URI is not set; PvPClubTiers will be disabled until it is configured.")
        self.bot.add_view(TestQueuePanelView(self))
        for mode in GAMEMODES:
            self.bot.add_view(GamemodeTestQueueView(self, mode))
        self.bot.add_view(QueueTestSessionView(self))
        self.bot.add_view(TierInfoPanelView(self))
        self.bot.add_view(LeaderboardPanelView(self))
        self.bot.add_view(TesterApplicationPanelView(self))
        self.bot.add_view(TesterApplicationReviewView(self))
        self.bot.add_view(DuelInviteView(self))
        self.bot.add_view(MatchmakingPanelView(self))
        self.bot.add_view(MatchmakingQueueView(self))
        self.bot.add_view(DuelResultView(self))
        self.trivia_watcher.start()
        self.queue_recovery_watcher.start()
        self.leaderboard_refresh_watcher.start()

    def cog_unload(self):
        self.trivia_watcher.cancel()
        self.queue_recovery_watcher.cancel()
        self.leaderboard_refresh_watcher.cancel()
        if self.mongo_client:
            self.mongo_client.close()

    async def require_db(self, interaction: discord.Interaction) -> bool:
        if self.db is None:
            await interaction.response.send_message(
                "PvPClubTiers database is not connected. Add MONGO_URI in Render Environment, then restart the bot.",
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

        # Never place the same MongoDB field in both $set and $setOnInsert.
        # When a Minecraft username is supplied (for example from a claimed
        # test ticket), update the profile name with $set. Otherwise, initialize
        # the display name only when this player document is first created.
        insert_fields = {
            "guild_id": guild_id,
            "discord_id": member.id,
            "tiers": {},
            "cooldowns": {},
            "elo": 1000,
            "created_at": now,
        }
        # Keep the legacy camelCase field populated as well. Some existing
        # MongoDB deployments still have a unique index on discordId.
        set_fields = {"updated_at": now, "discordId": member.id}
        if username:
            set_fields.update({"username": username, "username_lc": username.casefold()})
        else:
            insert_fields.update({
                "username": display,
                "username_lc": display.casefold(),
            })

        update = {"$set": set_fields, "$setOnInsert": insert_fields}
        await self.db_call(
            self.players.update_one,
            {"guild_id": guild_id, "discord_id": member.id},
            update,
            upsert=True,
        )
        return await self.get_player(guild_id, member.id)

    async def staff_or_tester(self, member: discord.Member, gamemode: str) -> bool:
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True

        # Prefer the configured role ID so testers still work if a role is
        # renamed, has different capitalisation, or contains extra formatting.
        config = await self.setting(member.guild.id)
        role_id = (config.get("tester_roles") or {}).get(gamemode)
        if role_id:
            try:
                configured_role_id = int(role_id)
            except (TypeError, ValueError):
                configured_role_id = None
            if configured_role_id and any(role.id == configured_role_id for role in member.roles):
                return True

        expected_name = f"{gamemode} Tester".casefold()
        return any(role.name.casefold() == expected_name for role in member.roles)

    async def has_gamemode_tester_role(
        self,
        member: discord.Member,
        gamemode: str,
        *,
        config: dict | None = None,
    ) -> bool:
        """Check the actual gamemode Tester role; staff permissions are not a bypass."""
        if gamemode not in GAMEMODES:
            return False
        if config is None:
            config = await self.setting(member.guild.id)

        role_id = (config.get("tester_roles") or {}).get(gamemode)
        if role_id:
            try:
                configured_role_id = int(role_id)
            except (TypeError, ValueError):
                configured_role_id = None
            if configured_role_id and any(role.id == configured_role_id for role in member.roles):
                return True

        # Keep compatibility with an existing tester role if its saved ID is
        # missing, while still requiring the gamemode-specific Tester role.
        expected_name = f"{gamemode} Tester".casefold()
        return any(role.name.casefold() == expected_name for role in member.roles)

    async def find_category(self, guild: discord.Guild, name: str):
        wanted = plain_discord_name(name)
        category = next(
            (item for item in guild.categories if plain_discord_name(item.name) == wanted),
            None,
        )
        if category:
            return category
        # Rename the old category in place instead of creating a duplicate.
        legacy_name = name.replace("PVPCLUBTIERS", "INGLOTIERS")
        legacy = next(
            (item for item in guild.categories if plain_discord_name(item.name) == plain_discord_name(legacy_name)),
            None,
        )
        if legacy:
            await legacy.edit(name=name, reason="PvPClubTiers rebrand")
            return legacy
        return await guild.create_category(name, reason="PvPClubTiers setup")

    async def find_channel(self, guild, name, category, send_disabled=False):
        wanted = plain_discord_name(name)
        channel = next(
            (item for item in guild.text_channels if plain_discord_name(item.name) == wanted),
            None,
        )
        if channel:
            edits = {}
            if channel.category_id != category.id:
                edits["category"] = category
            if send_disabled:
                edits["overwrites"] = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=False,
                        create_public_threads=False,
                        create_private_threads=False,
                        send_messages_in_threads=False,
                        read_message_history=True,
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        embed_links=True,
                        read_message_history=True,
                        manage_messages=True,
                    ),
                }
            if edits:
                await channel.edit(**edits, reason="PvPClubTiers setup")
            return channel
        overwrite = {guild.default_role: discord.PermissionOverwrite(view_channel=True)}
        if send_disabled:
            overwrite[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
            )
        if guild.me:
            overwrite[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True,
            )
        return await guild.create_text_channel(
            name, category=category, overwrites=overwrite, reason="PvPClubTiers setup"
        )

    def website_url(self) -> str | None:
        value = (os.getenv(TIER_WEBSITE_ENV) or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
        if not value:
            return None
        if not value.startswith(("https://", "http://")):
            value = "https://" + value
        return value.rstrip("/") + "/"

    def build_tier_info_embed(self) -> discord.Embed:
        mode_lines = [
            f"{MODE_EMOJIS.get(mode, '🎮')} **{mode}** · first to {MODE_BEST_OF.get(mode, 4)}"
            for mode in GAMEMODES
        ]
        rules = (
            "• Be on time. Message your tester if you cannot make it.\n"
            "• No cheats, macros or banned mods. Cheating can result in a testing ban.\n"
            "• Do not argue about results in public. Ask a Tier Manager through a support ticket.\n"
            "• Alternate accounts and dodging tests are not allowed.\n"
            "• Respect your tester. They volunteer their time."
        )
        embed = discord.Embed(
            title="📜 PvPClubTiers • Tier information",
            description=(
                "Everything you need to know before requesting a competitive Minecraft PvP tier test. "
                "Choose a gamemode, join its waitlist and meet your assigned tester in a private session."
            ),
            colour=0x55DDEB,
        )
        embed.add_field(name="Gamemodes & test format", value="\n".join(mode_lines), inline=False)
        embed.add_field(name="Testing rules", value=rules, inline=False)
        embed.add_field(
            name="Become a tier tester",
            value=(
                "Good at PvP and want to help test other players? Apply below. "
                "Choose a gamemode and complete the application form. Your application is sent "
                "to the configured tester application review channel."
            ),
            inline=False,
        )
        embed.set_footer(text="PvPClubTiers • EU • mcpvp.club")
        return embed

    async def ensure_tier_info_panel(self, guild: discord.Guild):
        config = await self.setting(guild.id)
        channel_id = config.get("tier_info_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            category_id = config.get("public_category_id")
            category = guild.get_channel(int(category_id)) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                category = await self.find_category(guild, "PVPCLUBTIERS")
            channel = await self.find_channel(guild, "tier-info", category, send_disabled=True)
        panel = None
        message_id = config.get("tier_info_panel_message_id")
        if message_id:
            try:
                panel = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                panel = None
        if panel:
            await panel.edit(embed=self.build_tier_info_embed(), view=TierInfoPanelView(self))
        else:
            panel = await channel.send(
                embed=self.build_tier_info_embed(),
                view=TierInfoPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.db_call(self.settings.update_one, {"guild_id": guild.id}, {
            "$set": {"tier_info_channel_id": channel.id, "tier_info_panel_message_id": panel.id}
        }, upsert=True)
        return channel

    async def build_leaderboard_embed(self, guild: discord.Guild) -> discord.Embed:
        overall = await self.make_leaderboard(guild.id, None, 15)
        lines = ["**🏆 Overall leaderboard**"]
        if overall:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            for index, row in enumerate(overall, 1):
                mark = medals.get(index, f"{index}.")
                user_id = str(row.get("discordId") or "")
                mention = f"<@{user_id}>" if user_id.isdigit() else ""
                lines.append(
                    f"{mark} **{discord.utils.escape_markdown(str(row.get('username', 'Unknown')))}**"
                    + (f" {mention}" if mention else "")
                    + f" · **{int(row.get('totalPoints', 0))}** pts"
                )
        else:
            lines.append("No players have been tiered yet.")

        for mode in GAMEMODES:
            rows = await self.make_leaderboard(guild.id, mode, 3)
            if not rows:
                continue
            mode_lines = []
            for row in rows:
                mode_lines.append(
                    f"**{row['display_tier']}** — "
                    f"{discord.utils.escape_markdown(str(row.get('username', 'Unknown')))}"
                )
            lines.append(
                f"\n**{MODE_EMOJIS.get(mode, '🎮')} {mode}**\n" + "\n".join(mode_lines)
            )

        website = self.website_url()
        if website:
            lines.append(f"\n🌐 [Open the full tier list website]({website})")
        embed = discord.Embed(
            title="📊 Tier Leaderboard",
            description="\n".join(lines),
            colour=0xF1C40F,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="PvPClubTiers • Overall points and gamemode top 3 • Updates automatically")
        return embed

    async def ensure_leaderboard_panel(self, guild: discord.Guild):
        config = await self.setting(guild.id)
        channel_id = config.get("leaderboard_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            category_id = config.get("public_category_id")
            category = guild.get_channel(int(category_id)) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                category = await self.find_category(guild, "PVPCLUBTIERS")
            channel = await self.find_channel(guild, "tier-leaderboard", category, send_disabled=True)

        panel = None
        message_id = config.get("leaderboard_panel_message_id")
        if message_id:
            try:
                panel = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                panel = None
        embed = await self.build_leaderboard_embed(guild)
        view = LeaderboardPanelView(self)
        if panel:
            await panel.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        else:
            panel = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.db_call(self.settings.update_one, {"guild_id": guild.id}, {
            "$set": {"leaderboard_channel_id": channel.id, "leaderboard_panel_message_id": panel.id}
        }, upsert=True)
        return channel

    async def show_my_tiers(self, interaction: discord.Interaction):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await self.get_player(interaction.guild.id, interaction.user.id)
        if not player:
            await interaction.followup.send(
                "You do not have a PvPClubTiers profile yet. Save your Minecraft IGN in the request-test channel or use /ign.",
                ephemeral=True,
            )
            return
        best, best_mode = best_tier(player.get("tiers", {}))
        lines = [
            f"{MODE_EMOJIS.get(mode, '🎮')} **{mode}:** {(player.get('tiers') or {}).get(mode, 'Unranked')}"
            for mode in GAMEMODES
        ]
        embed = discord.Embed(
            title=f"🏅 {player.get('username', interaction.user.display_name)} • My tiers",
            description="\n".join(lines),
            colour=COLOURS.get(best, 0x55DDEB),
        )
        embed.add_field(
            name="Best tier",
            value=f"**{best or 'Unranked'}**" + (f" in **{best_mode}**" if best_mode else ""),
            inline=False,
        )
        embed.add_field(name="Tier points", value=str(total_tier_points(player.get("tiers", {}))), inline=True)
        embed.add_field(name="Duel ELO", value=str(player.get("elo", 1000)), inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def open_test_queue_modes(self, guild: discord.Guild) -> list[str]:
        """Return only gamemodes whose test queues are currently open."""
        config = await self.setting(guild.id)
        queue_modes = config.get("test_queue_modes") or {}
        return [
            mode for mode in GAMEMODES
            if (queue_modes.get(mode_key(mode)) or {}).get("open", False)
        ]

    async def build_test_queue_embed(self, guild: discord.Guild, gamemode: str) -> discord.Embed:
        config = await self.setting(guild.id)
        mode_config = (config.get("test_queue_modes") or {}).get(mode_key(gamemode), {})
        configured_open = bool(mode_config.get("open", False))
        count = await self.db_call(
            self.test_queue_entries.count_documents,
            {"guild_id": guild.id, "gamemode": gamemode, "status": "waiting"},
        )
        # Queue availability is controlled explicitly by a tester. Do not infer
        # online status from Member.status: the bot does not request Discord's
        # privileged Presence Intent, so cached members can all appear offline.
        if configured_open:
            status_line = "🟢 **Open.** A tester has opened this queue and is accepting players."
            colour = 0x57D69A
        else:
            status_line = "🔴 **Closed.** You cannot join while this queue is closed."
            if count:
                status_line += f" {count} player(s) already waiting will be notified when it reopens."
            colour = 0x747B8C

        embed = discord.Embed(
            title=f"{MODE_EMOJIS.get(gamemode, '🎮')} {gamemode} Queue",
            description=(
                f"{status_line}\n\n"
                f"**{count}/{QUEUE_CAPACITY} in queue · first to {MODE_BEST_OF.get(gamemode, 4)}**"
            ),
            colour=colour,
        )
        embed.set_footer(text="Join only while open • Leave any time before your test starts")
        return embed

    async def ensure_test_queue_panel(self, guild: discord.Guild):
        config = await self.setting(guild.id)
        public_category = guild.get_channel(int(config.get("public_category_id", 0) or 0))
        if not isinstance(public_category, discord.CategoryChannel):
            public_category = next(
                (item for item in guild.categories if plain_discord_name(item.name) == "pvpclubtiers"),
                None,
            )

        channel_id = config.get("test_queue_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        forum_type = getattr(discord, "ForumChannel", None)
        if forum_type is not None and isinstance(channel, forum_type):
            if (
                isinstance(public_category, discord.CategoryChannel)
                and channel.category_id == public_category.id
                and plain_discord_name(channel.name) in {"queues", "test-queues"}
            ):
                await channel.delete(reason="Replace thread-only PvPClub queues with a text channel")
            channel = None

        legacy_forum = next(
            (
                item for item in guild.channels
                if forum_type is not None
                and isinstance(item, forum_type)
                and isinstance(public_category, discord.CategoryChannel)
                and item.category_id == public_category.id
                and plain_discord_name(item.name) in {"queues", "test-queues"}
                and (channel is None or item.id != channel.id)
            ),
            None,
        )
        if legacy_forum is not None:
            await legacy_forum.delete(
                reason="Replace thread-only PvPClub queues with a text channel"
            )
        if not isinstance(channel, discord.TextChannel):
            channel = next(
                (
                    item for item in guild.text_channels
                    if public_category is not None
                    and item.category_id == public_category.id
                    and plain_discord_name(item.name) in {"queues", "test-queues"}
                ),
                None,
            )

        queue_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
            ),
        }
        if guild.me:
            queue_overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                read_message_history=True,
                manage_messages=True,
            )

        if channel is None:
            if not isinstance(public_category, discord.CategoryChannel):
                return None
            channel = await guild.create_text_channel(
                "『🎮』 queues",
                category=public_category,
                overwrites=queue_overwrites,
                reason="PvPClubTiers per-gamemode test queues",
            )
        else:
            await channel.edit(
                name="『🎮』 queues",
                category=public_category if isinstance(public_category, discord.CategoryChannel) else channel.category,
                overwrites=queue_overwrites,
                reason="PvPClubTiers public queue card permissions",
            )

        # Retire the old single-panel post; every mode now has its own clean card.
        old_panel_id = config.get("test_queue_panel_message_id")
        if old_panel_id:
            try:
                old_panel = await channel.fetch_message(int(old_panel_id))
                await old_panel.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

        message_ids = dict(config.get("test_queue_panel_message_ids") or {})
        for mode in GAMEMODES:
            message = None
            message_id = message_ids.get(mode_key(mode))
            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                    message = None
            embed = await self.build_test_queue_embed(guild, mode)
            mode_config = (config.get("test_queue_modes") or {}).get(mode_key(mode), {})
            queue_is_open = bool(mode_config.get("open", False))
            view = GamemodeTestQueueView(self, mode, is_open=queue_is_open)
            if message:
                await message.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            else:
                message = await channel.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                message_ids[mode_key(mode)] = message.id

        await self.db_call(
            self.settings.update_one,
            {"guild_id": guild.id},
            {
                "$set": {
                    "test_queue_channel_id": channel.id,
                    "test_queue_panel_message_ids": message_ids,
                },
                "$unset": {"test_queue_panel_message_id": ""},
            },
            upsert=True,
        )
        return channel

    async def ensure_test_request_panel(self, guild: discord.Guild, *, return_status: bool = False):
        config = await self.setting(guild.id)
        public_category = guild.get_channel(int(config.get("public_category_id", 0) or 0))
        if not isinstance(public_category, discord.CategoryChannel):
            public_category = next(
                (item for item in guild.categories if plain_discord_name(item.name) == "pvpclubtiers"),
                None,
            )

        channel_id = config.get("test_request_channel_id") or config.get("request_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        forum_type = getattr(discord, "ForumChannel", None)
        if forum_type is not None and isinstance(channel, forum_type):
            if (
                isinstance(public_category, discord.CategoryChannel)
                and channel.category_id == public_category.id
                and plain_discord_name(channel.name) == "request-test"
            ):
                await channel.delete(reason="Replace thread-only tier requests with a text panel")
            channel = None

        legacy_forum = next(
            (
                item for item in guild.channels
                if forum_type is not None
                and isinstance(item, forum_type)
                and isinstance(public_category, discord.CategoryChannel)
                and item.category_id == public_category.id
                and plain_discord_name(item.name) == "request-test"
                and (channel is None or item.id != channel.id)
            ),
            None,
        )
        if legacy_forum is not None:
            await legacy_forum.delete(
                reason="Replace thread-only tier requests with a text panel"
            )
        if not isinstance(channel, discord.TextChannel):
            channel = next(
                (
                    item for item in guild.text_channels
                    if public_category is not None
                    and item.category_id == public_category.id
                    and plain_discord_name(item.name) == "request-test"
                ),
                None,
            )

        if channel is None:
            if not isinstance(public_category, discord.CategoryChannel):
                return None
            channel = await guild.create_text_channel(
                "『📥』 request-test",
                category=public_category,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=False,
                        read_message_history=True,
                        create_public_threads=False,
                        create_private_threads=False,
                        send_messages_in_threads=False,
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        embed_links=True,
                        read_message_history=True,
                        manage_messages=True,
                    ),
                },
                reason="PvPClubTiers single-IGN test request panel",
            )
        else:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
                    create_public_threads=False,
                    create_private_threads=False,
                    send_messages_in_threads=False,
                ),
            }
            if guild.me:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    read_message_history=True,
                    manage_messages=True,
                )
            await channel.edit(
                name="『📥』 request-test",
                category=public_category if isinstance(public_category, discord.CategoryChannel) else channel.category,
                overwrites=overwrites,
                reason="PvPClubTiers test request panel permissions",
            )

        config = await self.setting(guild.id)
        panel = None
        message_id = config.get("test_request_panel_message_id") or config.get("request_panel_message_id")
        if message_id:
            try:
                panel = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                panel = None

        # Older setups did not always keep the request panel message ID in the
        # current database field. Find the existing bot panel by its embed title
        # or persistent Sign Up button so ?v3 edits the posted message in place.
        if panel is None and self.bot.user is not None:
            try:
                async for candidate in channel.history(limit=200, oldest_first=False):
                    if candidate.author.id != self.bot.user.id:
                        continue
                    has_signup_button = any(
                        getattr(component, "custom_id", None) == "inglotiers:test-queue:join"
                        for action_row in candidate.components
                        for component in action_row.children
                    )
                    has_request_title = any(
                        embed.title
                        and (
                            "request a tier test" in embed.title.casefold()
                            or "sign up for a tier test" in embed.title.casefold()
                        )
                        for embed in candidate.embeds
                    )
                    if has_signup_button or has_request_title:
                        panel = candidate
                        log.info(
                            "Found existing PvPClubTiers signup panel %s in channel %s; reusing it",
                            panel.id,
                            channel.id,
                        )
                        break
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not search for the existing signup panel in channel %s", channel.id)

        embed = discord.Embed(
            title="📥 Sign Up for a Tier Test",
            description=(
                "Click **Sign Up** to save your Minecraft IGN, then choose the gamemode queues that are currently open. "
                "You can join multiple open queues at the same time.\n\n"
                "You cannot join a closed queue. When a tester opens a gamemode queue, its waiting players "
                "are notified. When your turn arrives, the bot opens a private session for you and the assigned tester."
            ),
            colour=0x55DDEB,
        )
        embed.add_field(
            name="How it works",
            value=(
                "1. Press **Sign Up** and enter your Minecraft IGN.\n"
                "2. Select one or more **open** gamemodes.\n"
                "3. Use **Leave all waitlists** to cancel your waiting entries."
            ),
            inline=False,
        )
        embed.set_footer(text="One Minecraft IGN • Multiple gamemode waitlists")
        if panel:
            await panel.edit(
                content=None,
                embed=embed,
                view=TestQueuePanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            panel = await channel.send(
                embed=embed,
                view=TestQueuePanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.db_call(
            self.settings.update_one,
            {"guild_id": guild.id},
            {
                "$set": {
                    "test_request_channel_id": channel.id,
                    "test_request_panel_message_id": panel.id,
                },
                "$unset": {"request_channel_id": "", "request_panel_message_id": ""},
            },
            upsert=True,
        )
        if return_status:
            return channel, panel is not None
        return channel

    async def refresh_test_queue_panel(self, guild: discord.Guild):
        try:
            await self.ensure_test_queue_panel(guild)
        except Exception:
            log.exception("Could not refresh test queue panel in guild %s", guild.id)

    async def reset_test_queue_setup(
        self,
        guild: discord.Guild,
        *,
        public_category: discord.CategoryChannel | None = None,
        tester_roles: dict | None = None,
        reset_queue_state: bool = True,
    ):
        if self.db is None:
            raise RuntimeError("MongoDB is not connected. Add MONGO_URI in Render and restart the bot.")
        bot_member = guild.me
        if not bot_member or not bot_member.guild_permissions.manage_channels or not bot_member.guild_permissions.manage_roles:
            raise PermissionError("I need Manage Channels and Manage Roles to rebuild the test queues.")

        config = await self.setting(guild.id)
        if not isinstance(public_category, discord.CategoryChannel):
            category_id = config.get("public_category_id")
            public_category = guild.get_channel(int(category_id)) if category_id else None
        if not isinstance(public_category, discord.CategoryChannel):
            public_category = await self.find_category(guild, "PVPCLUBTIERS")

        configured_roles = dict(tester_roles or config.get("tester_roles") or {})
        resolved_roles = {}
        for mode in GAMEMODES:
            role_id = configured_roles.get(mode)
            role = guild.get_role(int(role_id)) if role_id else None
            role = role or discord.utils.get(guild.roles, name=f"{mode} Tester")
            if role is None:
                role = await guild.create_role(
                    name=f"{mode} Tester",
                    colour=discord.Colour(0x36C5D8),
                    reason=f"PvPClubTiers {mode} tester role",
                )
            if role >= bot_member.top_role:
                raise PermissionError(
                    f"My highest role must be above **{role.name}** before I can manage that tester role."
                )
            resolved_roles[mode] = role.id

        # Give each private session category a clean permission baseline. No tester
        # role or staff role is granted access here; the session channel grants access
        # only to the player, the one tester assigned to that session, and the bot.
        private_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
                use_application_commands=False,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True,
                use_application_commands=True,
            ),
        }
        session_category_id = config.get("test_session_category_id")
        session_category = guild.get_channel(int(session_category_id)) if session_category_id else None
        if not isinstance(session_category, discord.CategoryChannel):
            session_category = discord.utils.get(guild.categories, name="TEST SESSIONS")
        if session_category is None:
            session_category = await guild.create_category(
                "TEST SESSIONS",
                overwrites=private_overwrites,
                reason="Private PvPClubTiers tester sessions",
            )
        else:
            await session_category.edit(
                name="TEST SESSIONS",
                overwrites=private_overwrites,
                reason="Reset private PvPClubTiers session permissions",
            )

        # Create or repair the public queue panel channel.
        queue_channel_id = config.get("test_queue_channel_id")
        queue_channel = guild.get_channel(int(queue_channel_id)) if queue_channel_id else None
        if not isinstance(queue_channel, discord.TextChannel):
            queue_channel = next(
                (item for item in public_category.text_channels
                 if re.sub(r"^『[^』]+』\s*", "", item.name).casefold() in {"queues", "test-queues"}),
                None,
            )
        queue_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True,
                create_public_threads=False, create_private_threads=False,
                send_messages_in_threads=False,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                read_message_history=True, manage_messages=True,
            ),
        }
        if queue_channel is None:
            queue_channel = await guild.create_text_channel(
                "『🎮』 queues",
                category=public_category,
                overwrites=queue_overwrites,
                reason="PvPClubTiers public test queues",
            )
        else:
            await queue_channel.edit(
                name="『🎮』 queues",
                category=public_category,
                overwrites=queue_overwrites,
                reason="Reset public PvPClubTiers queue permissions",
            )

        # Remove only the old PvPClubTiers request panel and legacy test ticket
        # channels. Results, activity, general support tickets, logs and other
        # server channels are intentionally left untouched.
        legacy_category_id = config.get("ticket_category_id")
        legacy_category = guild.get_channel(int(legacy_category_id)) if legacy_category_id else None
        if not isinstance(legacy_category, discord.CategoryChannel):
            legacy_category = next(
                (item for item in guild.categories if plain_discord_name(item.name) == "tier test tickets"),
                None,
            )
        legacy_ticket_docs = await self.db_call(
            lambda: list(self.tickets.find({
                "guild_id": guild.id,
                "session_type": {"$ne": "queue"},
            }))
        )
        old_channel_ids = set()
        for ticket in legacy_ticket_docs:
            channel_id = ticket.get("channel_id") or ticket.get("channelId")
            if channel_id:
                old_channel_ids.add(int(channel_id))
        if isinstance(legacy_category, discord.CategoryChannel):
            old_channel_ids.update(
                channel.id for channel in list(legacy_category.channels)
                if isinstance(channel, discord.TextChannel)
            )

        # Reuse the old request-test channel for the new one-time IGN panel.
        # It is intentionally not included in the legacy channel deletion list.

        deleted_old_channels = 0
        failed_old_channels = 0
        for old_channel_id in old_channel_ids:
            old_channel = guild.get_channel(old_channel_id)
            if old_channel is None:
                try:
                    old_channel = await guild.fetch_channel(old_channel_id)
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    failed_old_channels += 1
                    log.exception("Could not fetch legacy PvPClubTiers test channel %s", old_channel_id)
                    continue
            if isinstance(old_channel, discord.TextChannel) and old_channel.id != queue_channel.id:
                try:
                    await old_channel.delete(reason="Replacing legacy PvPClubTiers test tickets with queues")
                    deleted_old_channels += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed_old_channels += 1
                    log.exception("Could not delete legacy PvPClubTiers test channel %s", old_channel_id)

        if legacy_ticket_docs:
            await self.db_call(
                self.tickets.update_many,
                {"guild_id": guild.id, "session_type": {"$ne": "queue"}},
                {"$set": {
                    "status": "closed",
                    "migration_note": "Legacy public test ticket retired by ?resetuptest",
                    "closed_at": datetime.now(timezone.utc),
                    "closed_by": bot_member.id,
                }},
            )
        if isinstance(legacy_category, discord.CategoryChannel) and not legacy_category.channels:
            try:
                await legacy_category.delete(reason="Retired legacy PvPClubTiers ticket category")
            except (discord.Forbidden, discord.HTTPException):
                failed_old_channels += 1
                log.exception("Could not delete legacy tier ticket category in guild %s", guild.id)

        # ?resetuptest clears waiting entries and closes every queue. A normal
        # ?tiersetup run preserves players already waiting and current open states.
        if reset_queue_state:
            await self.db_call(
                self.test_queue_entries.update_many,
                {"guild_id": guild.id, "status": "waiting"},
                {"$set": {
                    "status": "cancelled",
                    "cancelled_at": datetime.now(timezone.utc),
                    "cancel_reason": "Test queues were reset by server staff",
                }},
            )
            queue_modes = {mode_key(mode): {"open": False} for mode in GAMEMODES}
        else:
            queue_modes = dict(config.get("test_queue_modes") or {})
            for mode in GAMEMODES:
                queue_modes.setdefault(mode_key(mode), {"open": False})
        await self.db_call(
            self.settings.update_one,
            {"guild_id": guild.id},
            {
                "$set": {
                    "guild_id": guild.id,
                    "public_category_id": public_category.id,
                    "test_session_category_id": session_category.id,
                    "test_queue_channel_id": queue_channel.id,
                    "test_queue_modes": queue_modes,
                    "tester_roles": resolved_roles,
                    "setup_at": datetime.now(timezone.utc),
                },
                "$unset": {
                    "ticket_category_id": "",
                    "request_channel_id": "",
                    "request_panel_message_id": "",
                },
            },
            upsert=True,
        )
        await self.ensure_test_queue_panel(guild)
        await self.ensure_test_request_panel(guild)
        return queue_channel, session_category, resolved_roles, deleted_old_channels, failed_old_channels

    @commands.command(name="v3")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def v3(self, ctx: commands.Context):
        """Refresh the V3 signup and queue panels without resetting queue entries."""
        if self.db is None:
            await ctx.reply("PvPClubTiers database is not connected. Check MONGO_URI in Render and restart the bot.")
            return

        try:
            await ctx.reply("Refreshing the PvPClubTiers V3 signup and queue panels…")
            queue_channel = await self.ensure_test_queue_panel(ctx.guild)
            request_result = await self.ensure_test_request_panel(ctx.guild, return_status=True)
            if request_result is None:
                await ctx.send("I couldn't find the PvPClubTiers category. Run `?tiersetup` first.")
                return
            request_channel, panel_edited = request_result
            panel_action = "edited the existing message" if panel_edited else "posted a new message (no old panel was found)"

            await ctx.send(
                "✅ **PvPClubTiers V3 panels updated!**\n"
                f"• Sign Up panel: {request_channel.mention} — {panel_action}\n"
                f"• Gamemode queues: {queue_channel.mention if queue_channel else 'not created'}\n"
                "• Existing queue entries and open/closed queue states were preserved."
            )
        except discord.Forbidden:
            await ctx.send("I need Manage Channels, View Channels and Send Messages to update the V3 panels.")
        except Exception:
            log.exception("?v3 failed in guild %s", ctx.guild.id)
            await ctx.send("The V3 panel refresh failed. Check the Render logs for the exact error.")

    @commands.command(name="tiersetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def tiersetup(self, ctx: commands.Context):
        if self.db is None:
            await ctx.reply("Add MONGO_URI to Render Environment and restart the bot before running tier setup.")
            return
        guild = ctx.guild
        await ctx.reply("Setting up PvPClubTiers roles, queues and support channels…")
        try:
            tier_roles = {}
            for tier in reversed(TIERS):
                role = discord.utils.get(guild.roles, name=tier)
                if role is None:
                    role = await guild.create_role(
                        name=tier,
                        colour=discord.Colour(COLOURS[tier]),
                        reason="PvPClubTiers shared tier roles",
                    )
                tier_roles[tier] = role.id

            public_category = await self.find_category(guild, "PVPCLUBTIERS")
            queue_channel, session_category, tester_roles, deleted, failed = await self.reset_test_queue_setup(
                guild, public_category=public_category, reset_queue_state=False,
            )
            results = await self.find_channel(guild, "results", public_category)
            activity = await self.find_channel(guild, "tier-activity", public_category)
            tier_info = await self.find_channel(guild, "tier-info", public_category, True)
            leaderboard = await self.find_channel(guild, "tier-leaderboard", public_category, True)
            logs_category = await self.find_category(guild, "PVPCLUBTIERS LOGS")
            log_overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True,
                ),
            }
            if guild.me:
                log_overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                )
            for role in guild.roles:
                if role.permissions.manage_guild or role.permissions.manage_messages:
                    log_overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True,
                        use_application_commands=True,
                    )
            logs = await self.find_channel(guild, "ticket-logs", logs_category, True)
            if logs:
                await logs.edit(
                    category=logs_category,
                    overwrites=log_overwrites,
                    reason="PvPClubTiers test event logs",
                )
            else:
                logs = await guild.create_text_channel(
                    "ticket-logs",
                    category=logs_category,
                    overwrites=log_overwrites,
                    reason="PvPClubTiers test event logs",
                )
            matchmaking = await self.find_channel(guild, "matchmaking", public_category)
            await self.db_call(
                self.settings.update_one,
                {"guild_id": guild.id},
                {"$set": {
                    "guild_id": guild.id,
                    "tier_roles": tier_roles,
                    "tester_roles": tester_roles,
                    "public_category_id": public_category.id,
                    "test_session_category_id": session_category.id,
                    "test_queue_channel_id": queue_channel.id,
                    "results_channel_id": results.id,
                    "activity_channel_id": activity.id,
                    "tier_info_channel_id": tier_info.id,
                    "leaderboard_channel_id": leaderboard.id,
                    "logs_channel_id": logs.id,
                    "matchmaking_channel_id": matchmaking.id,
                    "website_public": True,
                    "setup_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            await self.ensure_test_queue_panel(guild)
            request_channel = await self.ensure_test_request_panel(guild)
            await self.ensure_tier_info_panel(guild)
            await self.ensure_leaderboard_panel(guild)
            await self.ensure_matchmaking_panel(guild)
            await ctx.send(
                "✅ **PvPClubTiers setup complete!**\n"
                f"• {len(TIERS)} shared tier roles checked/created.\n"
                f"• {len(GAMEMODES)} gamemode tester roles checked/created.\n"
                f"• Test queues: {queue_channel.mention}\n"
                f"• Request panel: {request_channel.mention if request_channel else 'not created'}\n"
                f"• Private test sessions: **{session_category.name}**\n"
                f"• Tier info: {tier_info.mention}\n"
                f"• Leaderboard: {leaderboard.mention}\n"
                f"• Results: {results.mention}\n"
                f"• Activity: {activity.mention}\n"
                f"• Logs: {logs.mention}\n"
                f"• Matchmaking: {matchmaking.mention}\n"
                f"• Retired old test channels: **{deleted}**\n"
                f"• Old channels not deleted: **{failed}**\n\n"
                "Testers open their own gamemode queue from the queue panel. "
                "When a tester starts the next player, the bot creates a private session "
                "visible only to that player, the assigned tester and the bot."
            )
        except discord.Forbidden:
            await ctx.send("I need Manage Roles, Manage Channels, View Channels and Send Messages to finish setup.")
        except Exception:
            log.exception("PvPClubTiers setup failed")
            await ctx.send("PvPClubTiers setup failed. Check the Render logs and my Discord permissions.")

    @commands.command(name="resetuptest")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def resetuptest(self, ctx: commands.Context):
        if self.db is None:
            await ctx.reply("PvPClubTiers database is not connected. Check MONGO_URI in Render and restart the bot.")
            return
        try:
            await ctx.reply("Rebuilding PvPClub test queues and removing the old public test-ticket setup…")
            config = await self.setting(ctx.guild.id)
            public_category_id = config.get("public_category_id")
            public_category = ctx.guild.get_channel(int(public_category_id)) if public_category_id else None
            if not isinstance(public_category, discord.CategoryChannel):
                public_category = await self.find_category(ctx.guild, "PVPCLUBTIERS")
            queue_channel, session_category, roles, deleted, failed = await self.reset_test_queue_setup(
                ctx.guild,
                public_category=public_category,
            )
            request_channel = await self.ensure_test_request_panel(ctx.guild)
            tier_info_channel = await self.ensure_tier_info_panel(ctx.guild)
            leaderboard_channel = await self.ensure_leaderboard_panel(ctx.guild)
            await ctx.send(
                "✅ **Test queues reset successfully.**\n"
                f"• Queue panel: {queue_channel.mention}\n"
                f"• Request panel: {request_channel.mention if request_channel else 'not created'}\n"
                f"• Tier info: {tier_info_channel.mention if tier_info_channel else 'not created'}\n"
                f"• Leaderboard: {leaderboard_channel.mention if leaderboard_channel else 'not created'}\n"
                f"• Private sessions: **{session_category.name}**\n"
                f"• Gamemode tester roles checked: **{len(roles)}**\n"
                f"• Old test channels deleted: **{deleted}**\n"
                f"• Old channels not deleted: **{failed}**\n"
                "• All queues start closed. Testers can open only the gamemodes they test.\n"
                "• New sessions are visible only to the queued player, their assigned tester and the bot.\n"
                "• General support tickets, results, activity and server channels were not removed."
                + ("\n⚠️ Give the bot Manage Channels to remove the remaining legacy test channels." if failed else "")
            )
        except discord.Forbidden:
            await ctx.send("Discord denied a channel or role change. Check Manage Channels, Manage Roles and the bot role position.")
        except Exception:
            log.exception("?resetuptest failed in guild %s", ctx.guild.id)
            await ctx.send("Test queue reset failed. The exact error is in the Render logs.")

    @commands.command(name="testerappsetup")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def testerappsetup(self, ctx: commands.Context, reviewchannel: discord.TextChannel):
        """Create the public tester application panel and set its review channel."""
        if self.db is None:
            await ctx.reply("Add MONGO_URI to Render Environment and restart the bot before setting up tester applications.")
            return
        guild = ctx.guild
        if guild is None or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("Run this command in a normal text channel inside your server.")
            return
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await ctx.reply("I need Manage Roles to assign tester roles after an application is accepted.")
            return

        config = await self.setting(guild.id)
        role_ids = config.get("tester_roles", {})
        missing_modes = []
        for mode in GAMEMODES:
            role_id = role_ids.get(mode)
            role = guild.get_role(int(role_id)) if role_id else None
            if role is None or role >= bot_member.top_role:
                missing_modes.append(mode)
        if missing_modes:
            await ctx.reply(
                "Some gamemode tester roles are missing or above my highest role. "
                "Run ?tiersetup first and move my bot role above the tester roles."
            )
            return

        for channel in (ctx.channel, reviewchannel):
            permissions = channel.permissions_for(bot_member)
            if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links:
                await ctx.reply(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
                return

        panel_embed = discord.Embed(
            title="🧪 Apply to become a PvPClub tester",
            description=(
                "Want to help test players in PvPClub? Click the button below, choose the gamemode "
                "you want to test, and complete the short application.\n\n"
                "Your completed form is sent to the configured review channel. **Only the server owner can accept "
                "or reject an application.** Accepted applicants receive the tester role for their "
                "selected gamemode only."
            ),
            colour=0x55DDEB,
        )
        panel_embed.add_field(
            name="Before applying",
            value="Use your real Minecraft username, enter your current tier/rank, and explain why you would be a fair, reliable tester.",
            inline=False,
        )
        panel_embed.set_footer(text="PVPClub Empire • Tester Applications")

        try:
            panel = None
            old_channel_id = config.get("tester_app_panel_channel_id")
            old_message_id = config.get("tester_app_panel_message_id")
            if old_channel_id and int(old_channel_id) == ctx.channel.id and old_message_id:
                try:
                    panel = await ctx.channel.fetch_message(int(old_message_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                    panel = None

            if panel:
                await panel.edit(embed=panel_embed, view=TesterApplicationPanelView(self))
            else:
                panel = await ctx.send(embed=panel_embed, view=TesterApplicationPanelView(self))

            await self.db_call(
                self.settings.update_one,
                {"guild_id": guild.id},
                {"$set": {
                    "guild_id": guild.id,
                    "tester_app_review_channel_id": reviewchannel.id,
                    "tester_app_panel_channel_id": ctx.channel.id,
                    "tester_app_panel_message_id": panel.id,
                    "tester_app_updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            await self.ensure_tier_info_panel(guild)
        except discord.Forbidden:
            await ctx.reply("Discord denied access. Check my permissions in both channels and my role position.")
            return
        except Exception:
            log.exception("Tester application setup failed for guild %s", guild.id)
            await ctx.reply("I couldn't set up tester applications. Check the bot log and MongoDB connection.")
            return

        await ctx.reply(
            f"✅ **Tester applications are ready!**\n"
            f"• Application panel: {ctx.channel.mention}\n"
            f"• Review channel: {reviewchannel.mention} (only the server owner can accept or reject)\n"
            f"• Applicants can choose one of the {len(GAMEMODES)} gamemodes.\n"
            "• Accepting an application assigns only that gamemode's Tester role.",
            mention_author=False,
        )

    @testerappsetup.error
    async def testerappsetup_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You need Manage Server to configure tester applications.", mention_author=False)
        elif isinstance(error, commands.BadArgument):
            await ctx.reply("Usage: ?testerappsetup #review-channel", mention_author=False)
        else:
            log.exception("?testerappsetup failed", exc_info=error)
            await ctx.reply("I couldn't configure tester applications. Check my permissions and try again.", mention_author=False)

    async def submit_tester_application(
        self,
        interaction: discord.Interaction,
        gamemode: str,
        minecraft_username: str,
        current_tier: str,
        experience: str,
        reason: str,
        *,
        interaction_deferred: bool = False,
    ):
        # The modal normally defers before calling this method. Keep this safe
        # for any other caller that may use the handler directly in the future.
        if not interaction_deferred and not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Applications can only be submitted inside the server.", ephemeral=True)
            return
        if self.db is None:
            await interaction.followup.send(
                "PvPClubTiers database is not connected. Please contact a server administrator.",
                ephemeral=True,
            )
            return
        if gamemode not in GAMEMODES:
            await interaction.followup.send("That gamemode is not supported.", ephemeral=True)
            return
        try:
            minecraft_username = clean_username(minecraft_username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        async with self.tester_application_lock:
            try:
                config = await self.setting(guild.id)
                review_channel_id = config.get("tester_app_review_channel_id")
                review_channel = guild.get_channel(int(review_channel_id)) if review_channel_id else None
                if not isinstance(review_channel, discord.TextChannel):
                    await interaction.followup.send(
                        "Tester applications are not configured. Ask an administrator to run ?testerappsetup #review-channel.",
                        ephemeral=True,
                    )
                    return

                role_id = config.get("tester_roles", {}).get(gamemode)
                tester_role = guild.get_role(int(role_id)) if role_id else None
                if tester_role is None:
                    await interaction.followup.send(
                        "The tester role for this gamemode is missing. Ask an administrator to run ?tiersetup.",
                        ephemeral=True,
                    )
                    return
                if tester_role in interaction.user.roles:
                    await interaction.followup.send(
                        f"You already have the {tester_role.mention} role.",
                        ephemeral=True,
                    )
                    return

                existing = await self.db_call(
                    self.tester_applications.find_one,
                    {
                        "guild_id": guild.id,
                        "applicant_id": interaction.user.id,
                        "gamemode": gamemode,
                        "status": "pending",
                    },
                )
                if existing:
                    await interaction.followup.send(
                        f"You already have a pending {gamemode} tester application.",
                        ephemeral=True,
                    )
                    return

                application_id = uuid4().hex[:10]
                application_embed = discord.Embed(
                    title="🧪 New tester application",
                    description=f"{interaction.user.mention} wants to become a **{gamemode} Tester**.",
                    colour=0x55DDEB,
                    timestamp=discord.utils.utcnow(),
                )
                application_embed.add_field(
                    name="Applicant",
                    value=f"{interaction.user.mention} (ID: {interaction.user.id})",
                    inline=True,
                )
                application_embed.add_field(name="Gamemode", value=gamemode, inline=True)
                application_embed.add_field(name="Minecraft username", value=minecraft_username, inline=True)
                application_embed.add_field(
                    name="Current tier / rank",
                    value=(current_tier.strip() or "Not provided")[:100],
                    inline=True,
                )
                application_embed.add_field(
                    name="Previous testing experience",
                    value=(experience.strip() or "Not provided")[:500],
                    inline=False,
                )
                application_embed.add_field(name="Why should we choose them?", value=reason.strip()[:1000], inline=False)
                application_embed.set_footer(text=f"Application {application_id} • PVPClub Empire")

                message = await review_channel.send(
                    embed=application_embed,
                    view=TesterApplicationReviewView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                application = {
                    "application_id": application_id,
                    "guild_id": guild.id,
                    "applicant_id": interaction.user.id,
                    "applicant_name": str(interaction.user),
                    "minecraft_username": minecraft_username,
                    "current_tier": current_tier.strip()[:100],
                    "gamemode": gamemode,
                    "experience": experience.strip()[:500],
                    "reason": reason.strip()[:1000],
                    "status": "pending",
                    "review_channel_id": review_channel.id,
                    "review_message_id": message.id,
                    "created_at": datetime.now(timezone.utc),
                }
                try:
                    await self.db_call(self.tester_applications.insert_one, application)
                except Exception:
                    try:
                        await message.delete(reason="Tester application could not be saved")
                    except discord.HTTPException:
                        pass
                    raise

                await interaction.followup.send(
                    f"✅ Your **{gamemode}** tester application has been sent to the configured review channel.",
                    ephemeral=True,
                )
            except Exception:
                log.exception("Could not submit tester application for user %s", interaction.user.id)
                await interaction.followup.send(
                    "I couldn't submit your application. Please try again or contact an administrator.",
                    ephemeral=True,
                )

    async def review_tester_application(self, interaction: discord.Interaction, *, accepted: bool):
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message("This application can only be reviewed inside its server.", ephemeral=True)
            return
        if interaction.user.id != guild.owner_id:
            await interaction.response.send_message(
                "Only the server owner can accept or reject tester applications.",
                ephemeral=True,
            )
            return
        if not await self.require_db(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.tester_application_lock:
            try:
                application = await self.db_call(
                    self.tester_applications.find_one,
                    {"guild_id": guild.id, "review_message_id": interaction.message.id},
                )
                if not application or application.get("status") != "pending":
                    await interaction.followup.send(
                        "This application has already been reviewed or is no longer available.",
                        ephemeral=True,
                    )
                    return

                applicant_id = int(application["applicant_id"])
                gamemode = application["gamemode"]
                applicant = guild.get_member(applicant_id)
                if applicant is None:
                    try:
                        applicant = await guild.fetch_member(applicant_id)
                    except discord.NotFound:
                        applicant = None

                if applicant is None:
                    await self.db_call(
                        self.tester_applications.update_one,
                        {"_id": application["_id"], "status": "pending"},
                        {"$set": {
                            "status": "closed",
                            "review_result": "applicant_left",
                            "reviewed_by": interaction.user.id,
                            "reviewed_at": datetime.now(timezone.utc),
                        }},
                    )
                    decision = "Closed — applicant left the server"
                    result_message = "The applicant is no longer in the server, so this application was closed."
                    colour = 0x95A5A6
                elif accepted:
                    config = await self.setting(guild.id)
                    role_id = config.get("tester_roles", {}).get(gamemode)
                    tester_role = guild.get_role(int(role_id)) if role_id else None
                    bot_member = guild.me
                    if tester_role is None:
                        await interaction.followup.send(
                            f"The {gamemode} Tester role is missing. Run ?tiersetup and try again.",
                            ephemeral=True,
                        )
                        return
                    if (
                        bot_member is None
                        or not bot_member.guild_permissions.manage_roles
                        or tester_role >= bot_member.top_role
                    ):
                        await interaction.followup.send(
                            "I cannot assign that tester role. Give me Manage Roles and move my highest role above it.",
                            ephemeral=True,
                        )
                        return
                    if tester_role not in applicant.roles:
                        await applicant.add_roles(
                            tester_role,
                            reason=f"Tester application accepted by server owner ({interaction.user.id})",
                        )
                    await self.db_call(
                        self.tester_applications.update_one,
                        {"_id": application["_id"], "status": "pending"},
                        {"$set": {
                            "status": "accepted",
                            "review_result": "accepted",
                            "reviewed_by": interaction.user.id,
                            "reviewed_at": datetime.now(timezone.utc),
                            "assigned_role_id": tester_role.id,
                        }},
                    )
                    decision = "Accepted"
                    result_message = f"✅ {applicant.mention} was accepted for **{gamemode}** and received {tester_role.mention}."
                    colour = 0x57D69A
                else:
                    await self.db_call(
                        self.tester_applications.update_one,
                        {"_id": application["_id"], "status": "pending"},
                        {"$set": {
                            "status": "rejected",
                            "review_result": "rejected",
                            "reviewed_by": interaction.user.id,
                            "reviewed_at": datetime.now(timezone.utc),
                        }},
                    )
                    decision = "Rejected"
                    result_message = f"❌ {applicant.mention}'s **{gamemode}** tester application was rejected."
                    colour = 0xFF647C

                try:
                    updated_embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
                    updated_embed.colour = colour
                    updated_embed.add_field(
                        name="Application decision",
                        value=f"**{decision}** by {interaction.user.mention}",
                        inline=False,
                    )
                    updated_embed.timestamp = discord.utils.utcnow()
                    await interaction.message.edit(
                        embed=updated_embed,
                        view=TesterApplicationReviewView(self, disabled=True),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (IndexError, discord.HTTPException):
                    log.exception("Could not update reviewed tester application message %s", interaction.message.id)

                if applicant is not None:
                    try:
                        if accepted:
                            await applicant.send(
                                f"✅ Your **{gamemode}** tester application in **{guild.name}** was accepted. "
                                f"You now have the {gamemode} Tester role."
                            )
                        else:
                            await applicant.send(
                                f"Your **{gamemode}** tester application in **{guild.name}** was not accepted."
                            )
                    except discord.HTTPException:
                        pass

                await interaction.followup.send(result_message, ephemeral=True)
            except discord.Forbidden:
                log.exception("Discord denied tester application review in guild %s", guild.id)
                await interaction.followup.send(
                    "Discord denied the role or message update. Check my permissions and role hierarchy.",
                    ephemeral=True,
                )
            except Exception:
                log.exception("Tester application review failed in guild %s", guild.id)
                await interaction.followup.send(
                    "I couldn't finish reviewing this application. The error was logged.",
                    ephemeral=True,
                )

    async def show_tester_queue_controls(self, interaction: discord.Interaction):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this control inside the server.", ephemeral=True)
            return
        config = await self.setting(interaction.guild.id)
        allowed_modes = [
            mode for mode in GAMEMODES
            if await self.has_gamemode_tester_role(interaction.user, mode, config=config)
        ]
        if not allowed_modes:
            await interaction.followup.send(
                "You need a gamemode Tester role to manage test queues.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Choose one of the gamemodes you are allowed to test:",
            view=TesterQueueControlsView(self, allowed_modes),
            ephemeral=True,
        )

    async def enqueue_test_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        gamemode: str,
        username: str,
    ) -> tuple[bool, str]:
        if gamemode not in GAMEMODES:
            return False, "That gamemode is not supported."

        config = await self.setting(guild.id)
        mode_config = (config.get("test_queue_modes") or {}).get(mode_key(gamemode), {})
        if not mode_config.get("open", False):
            return False, (
                f"The **{gamemode}** queue is closed. "
                "You can only join after a tester opens it."
            )

        try:
            username = clean_username(username)
        except ValueError as exc:
            return False, str(exc)

        player = await self.get_player(guild.id, member.id)
        if player and player.get("tier_banned"):
            return False, (
                "You are blocked from tier tests. Reason: "
                + player.get("tier_ban_reason", "No reason supplied")
            )

        active_session = await self.db_call(
            self.test_queue_entries.find_one,
            {
                "guild_id": guild.id,
                "discord_id": member.id,
                "status": {"$in": ["starting", "testing"]},
            },
        )
        if active_session:
            channel = guild.get_channel(int(active_session.get("channel_id", 0) or 0))
            return False, (
                "You already have an active test session."
                + (f" Continue in {channel.mention}." if channel else "")
            )

        active_ticket = await self.db_call(
            self.tickets.find_one,
            {
                "guild_id": guild.id,
                "discord_id": member.id,
                "status": {"$in": ["open", "claimed"]},
            },
        )
        if active_ticket:
            active_channel_id = active_ticket.get("channel_id") or active_ticket.get("channelId")
            active_channel = guild.get_channel(int(active_channel_id)) if active_channel_id else None
            return False, (
                "You already have an active PvPClubTiers test session."
                + (f" {active_channel.mention}" if active_channel else "")
            )

        already_waiting = await self.db_call(
            self.test_queue_entries.find_one,
            {
                "guild_id": guild.id,
                "discord_id": member.id,
                "gamemode": gamemode,
                "status": "waiting",
            },
        )
        if already_waiting:
            return True, f"You are already on the **{gamemode}** waitlist."

        count = await self.db_call(
            self.test_queue_entries.count_documents,
            {"guild_id": guild.id, "gamemode": gamemode, "status": "waiting"},
        )
        if count >= QUEUE_CAPACITY:
            return False, f"The **{gamemode}** waitlist is full ({QUEUE_CAPACITY}/{QUEUE_CAPACITY})."

        # Save the IGN once on the player's profile. Future queue entries reuse it.
        await self.get_or_create_player(guild.id, member, username)
        await self.db_call(
            self.test_queue_entries.insert_one,
            {
                "queue_id": uuid4().hex[:12],
                "guild_id": guild.id,
                "gamemode": gamemode,
                "discord_id": member.id,
                "username": username,
                "status": "waiting",
                "created_at": datetime.now(timezone.utc),
            },
        )
        mode_config = (await self.setting(guild.id)).get("test_queue_modes", {}).get(mode_key(gamemode), {})
        if mode_config.get("open"):
            return True, f"✅ Joined the **{gamemode}** queue as **{username}**."
        return True, (
            f"✅ Joined the **{gamemode}** waitlist as **{username}**. "
            "The queue is closed right now; you'll be pinged when a tester opens it."
        )

    async def save_ign_and_show_queue_modes(self, interaction: discord.Interaction, username: str):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this panel inside the server.", ephemeral=True)
            return
        try:
            username = clean_username(username)
            await self.get_or_create_player(interaction.guild.id, interaction.user, username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("Could not save the Minecraft IGN for %s", interaction.user.id)
            await interaction.followup.send("I couldn't save your IGN. Please try again or contact staff.", ephemeral=True)
            return

        open_modes = await self.open_test_queue_modes(interaction.guild)
        if not open_modes:
            await interaction.followup.send(
                f"✅ Saved your Minecraft IGN as **{username}**. No test queues are open right now; "
                "you can join when a tester opens one.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Saved your Minecraft IGN as **{username}**.\n"
            "Choose one or more open gamemodes below. You only need to enter your IGN once.",
            view=TestQueueJoinModesView(self, username, open_modes),
            ephemeral=True,
        )

    async def join_selected_test_queues(
        self,
        interaction: discord.Interaction,
        gamemodes: list[str],
        username: str,
    ):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this panel inside the server.", ephemeral=True)
            return
        try:
            username = clean_username(username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        results = []
        async with self.test_queue_lock:
            for gamemode in gamemodes:
                try:
                    ok, message = await self.enqueue_test_member(
                        interaction.guild, interaction.user, gamemode, username,
                    )
                except Exception:
                    log.exception("Could not queue %s for %s", interaction.user.id, gamemode)
                    ok, message = False, f"Could not join {gamemode}. Please try again."
                results.append(("✅ " if ok else "❌ ") + message)

        await self.refresh_test_queue_panel(interaction.guild)
        await interaction.followup.send("\n".join(results), ephemeral=True)

    async def handle_queue_join_button(self, interaction: discord.Interaction, gamemode: str):
        if not await self.require_db(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this queue inside the server.", ephemeral=True)
            return
        if gamemode not in GAMEMODES:
            await interaction.response.send_message("That gamemode is not supported.", ephemeral=True)
            return
        config = await self.setting(interaction.guild.id)
        mode_config = (config.get("test_queue_modes") or {}).get(mode_key(gamemode), {})
        if not mode_config.get("open", False):
            await interaction.response.send_message(
                f"The **{gamemode}** queue is closed. You can join after a tester opens it.",
                ephemeral=True,
            )
            return
        player = await self.get_player(interaction.guild.id, interaction.user.id)
        username = (player or {}).get("username")
        if not username or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", str(username)):
            await interaction.response.send_modal(TestQueueJoinModal(self, gamemode))
            return
        await self.join_test_queue(interaction, gamemode, username)

    async def join_test_queue(self, interaction: discord.Interaction, gamemode: str, username: str):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Join a test queue from inside the server.", ephemeral=True)
            return
        async with self.test_queue_lock:
            try:
                ok, message = await self.enqueue_test_member(
                    interaction.guild, interaction.user, gamemode, username,
                )
            except Exception:
                log.exception("Could not add %s to the %s test queue", interaction.user.id, gamemode)
                ok, message = False, "I couldn't add you to the queue. Please try again or contact staff."
        await self.refresh_test_queue_panel(interaction.guild)
        await interaction.followup.send(message if ok else f"❌ {message}", ephemeral=True)

    async def leave_test_queue(self, interaction: discord.Interaction, gamemode: str | None = None):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        query = {
            "guild_id": guild.id,
            "discord_id": interaction.user.id,
            "status": "waiting",
        }
        if gamemode:
            query["gamemode"] = gamemode
        async with self.test_queue_lock:
            result = await self.db_call(
                self.test_queue_entries.update_many,
                query,
                {"$set": {
                    "status": "cancelled",
                    "cancelled_at": datetime.now(timezone.utc),
                    "cancel_reason": "Player left the queue",
                }},
            )
            active = await self.db_call(
                self.test_queue_entries.find_one,
                {
                    "guild_id": guild.id,
                    "discord_id": interaction.user.id,
                    "status": {"$in": ["starting", "testing"]},
                },
            )
        if result.modified_count:
            await self.refresh_test_queue_panel(guild)
            label = f"the **{gamemode}** waitlist" if gamemode else "all your waitlists"
            await interaction.followup.send(f"✅ You left {label}.", ephemeral=True)
        elif active and active.get("status") == "testing":
            channel = guild.get_channel(int(active.get("channel_id", 0) or 0))
            await interaction.followup.send(
                "Your test has already started. "
                + (f"Continue in {channel.mention}." if channel else "Use your active test session."),
                ephemeral=True,
            )
        elif active:
            await interaction.followup.send("A tester is opening your session right now. Please wait a moment.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"You are not waiting in the **{gamemode}** queue." if gamemode
                else "You are not currently on any test waitlist.",
                ephemeral=True,
            )

    async def leave_all_test_waitlists(self, interaction: discord.Interaction):
        await self.leave_test_queue(interaction, None)

    async def toggle_test_queue(self, interaction: discord.Interaction, gamemode: str):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if gamemode not in GAMEMODES:
            await interaction.followup.send("That gamemode is not supported.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use tester controls inside the server.", ephemeral=True)
            return

        # Queue joins use this same lock, so a player cannot slip into a queue
        # at the same time a tester closes it.
        async with self.test_queue_lock:
            config = await self.setting(interaction.guild.id)
            if not await self.has_gamemode_tester_role(interaction.user, gamemode, config=config):
                authorised = False
                now_open = False
            else:
                authorised = True
                key = mode_key(gamemode)
                modes = config.get("test_queue_modes") or {}
                currently_open = bool((modes.get(key) or {}).get("open", False))
                now_open = not currently_open
                await self.db_call(
                    self.settings.update_one,
                    {"guild_id": interaction.guild.id},
                    {"$set": {f"test_queue_modes.{key}.open": now_open}},
                    upsert=True,
                )

        if not authorised:
            await interaction.followup.send(
                f"Only a member with the **{gamemode} Tester** role can open or close this queue.",
                ephemeral=True,
            )
            return

        await self.refresh_test_queue_panel(interaction.guild)
        if now_open:
            waiting = await self.db_call(
                lambda: list(self.test_queue_entries.find({
                    "guild_id": interaction.guild.id,
                    "gamemode": gamemode,
                    "status": "waiting",
                }).sort("created_at", ASCENDING).limit(QUEUE_CAPACITY))
            )
            if waiting:
                members = []
                for row in waiting:
                    member = interaction.guild.get_member(int(row["discord_id"]))
                    if member is None:
                        try:
                            member = await interaction.guild.fetch_member(int(row["discord_id"]))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            member = None
                    if member is not None:
                        members.append(member)
                queue_channel_id = config.get("test_queue_channel_id")
                queue_channel = interaction.guild.get_channel(int(queue_channel_id)) if queue_channel_id else None
                if members and isinstance(queue_channel, discord.TextChannel):
                    await queue_channel.send(
                        content=(
                            "🟢 " + " ".join(member.mention for member in members)
                            + f" — the **{gamemode}** test queue is now open. A tester is ready to take players."
                        ),
                        allowed_mentions=discord.AllowedMentions(users=members),
                    )
        await interaction.followup.send(
            f"The {gamemode} queue is now open. Players on the waitlist have been notified."
            if now_open else f"The {gamemode} queue is now closed. Players already waiting will remain queued.",
            ephemeral=True,
        )

    async def start_next_test(self, interaction: discord.Interaction, gamemode: str):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not isinstance(interaction.user, discord.Member) or not await self.staff_or_tester(interaction.user, gamemode):
            await interaction.followup.send(
                f"Only a {gamemode} Tester or server staff can start this test.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        config = await self.setting(guild.id)
        mode_config = (config.get("test_queue_modes") or {}).get(mode_key(gamemode), {})
        if not mode_config.get("open", False):
            await interaction.followup.send(
                f"Open the {gamemode} queue before starting the next test.",
                ephemeral=True,
            )
            return
        category_id = config.get("test_session_category_id")
        category = guild.get_channel(int(category_id)) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                "Private test sessions are not configured. Ask an administrator to run ?resetuptest.",
                ephemeral=True,
            )
            return
        bot_member = guild.me
        if bot_member is None:
            await interaction.followup.send("I cannot find my server member record.", ephemeral=True)
            return

        ticket_id = uuid4().hex[:10]
        entry = None
        async with self.test_queue_lock:
            # A player may join several waitlists, but can only be tested in one
            # session at a time. Skip their other entries if one session is
            # already being opened or tested.
            while True:
                candidates = await self.db_call(
                    lambda: list(self.test_queue_entries.find({
                        "guild_id": guild.id,
                        "gamemode": gamemode,
                        "status": "waiting",
                    }).sort("created_at", ASCENDING).limit(QUEUE_CAPACITY))
                )
                if not candidates:
                    break

                candidate = candidates[0]
                active_for_player = await self.db_call(
                    self.test_queue_entries.find_one,
                    {
                        "guild_id": guild.id,
                        "discord_id": int(candidate["discord_id"]),
                        "status": {"$in": ["starting", "testing"]},
                    },
                )
                if active_for_player:
                    await self.db_call(
                        self.test_queue_entries.update_one,
                        {"queue_id": candidate["queue_id"], "status": "waiting"},
                        {"$set": {
                            "status": "cancelled",
                            "cancelled_at": datetime.now(timezone.utc),
                            "cancel_reason": "Player already has another active test session",
                        }},
                    )
                    continue

                reserved = await self.db_call(
                    self.test_queue_entries.update_one,
                    {"queue_id": candidate["queue_id"], "status": "waiting"},
                    {"$set": {
                        "status": "starting",
                        "tester_id": interaction.user.id,
                        "ticket_id": ticket_id,
                        "starting_at": datetime.now(timezone.utc),
                    }},
                )
                if reserved.modified_count:
                    entry = candidate
                    break
        if not entry:
            await interaction.followup.send(f"There are no players waiting in the {gamemode} queue.", ephemeral=True)
            await self.refresh_test_queue_panel(guild)
            return

        player_id = int(entry["discord_id"])
        player = guild.get_member(player_id)
        if player is None:
            try:
                player = await guild.fetch_member(player_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                player = None
        if player is None:
            await self.db_call(
                self.test_queue_entries.update_one,
                {"queue_id": entry["queue_id"], "status": "starting"},
                {"$set": {
                    "status": "cancelled",
                    "tester_id": None,
                    "ticket_id": None,
                    "cancelled_at": datetime.now(timezone.utc),
                    "cancel_reason": "Player left the server before the test started",
                }},
            )
            await self.refresh_test_queue_panel(guild)
            await interaction.followup.send(
                "The next player has left the server, so their queue entry was removed. Press Start Next Test again.",
                ephemeral=True,
            )
            return
        if player.id == interaction.user.id:
            await self.db_call(
                self.test_queue_entries.update_one,
                {"queue_id": entry["queue_id"], "status": "starting"},
                {"$set": {
                    "status": "waiting",
                    "tester_id": None,
                    "ticket_id": None,
                    "starting_at": None,
                }},
            )
            await interaction.followup.send("You cannot test your own queue entry. Ask another tester to start it.", ephemeral=True)
            return

        safe_mode = mode_key(gamemode)[:18]
        safe_name = re.sub(r"[^a-z0-9-]+", "-", entry["username"].casefold()).strip("-")[:12] or "player"
        channel_name = f"test-{safe_mode}-{safe_name}"[:90]
        # Do not grant the gamemode tester role access: only the individual tester
        # selected from the queue gets an explicit member-level overwrite.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
                use_application_commands=False,
            ),
            player: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                use_application_commands=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                use_application_commands=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                use_application_commands=True,
            ),
        }
        channel = None
        ticket_inserted = False
        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=(
                    f"Private PvPClubTiers queue session {ticket_id} • "
                    f"player {player.id} • tester {interaction.user.id} • {gamemode}"
                ),
                reason=f"Private {gamemode} test assigned to {interaction.user}",
            )
            ticket_doc = {
                "ticket_id": ticket_id,
                "guild_id": guild.id,
                "discord_id": player.id,
                "username": entry["username"],
                "gamemode": gamemode,
                "status": "claimed",
                "session_type": "queue",
                "queue_id": entry["queue_id"],
                "created_at": datetime.now(timezone.utc),
                "claimed_by": interaction.user.id,
                "claimed_at": datetime.now(timezone.utc),
                "channel_id": channel.id,
                "channelId": channel.id,
            }
            await self.db_call(self.tickets.insert_one, ticket_doc)
            ticket_inserted = True
            session_embed = discord.Embed(
                title=f"{gamemode} test session",
                description=(
                    f"Player: {player.mention}\n"
                    f"Minecraft username: {entry['username']}\n"
                    f"Assigned tester: {interaction.user.mention}\n\n"
                    "This channel is private to the player, their assigned tester and the bot. "
                    "Use /set-tier to record the result. The tester or player can close the session."
                ),
                colour=0x55DDEB,
            )
            session_embed.set_footer(text=f"Session {ticket_id} • PvPClubTiers")
            await channel.send(
                content=f"{player.mention} {interaction.user.mention}",
                embed=session_embed,
                view=QueueTestSessionView(self),
                allowed_mentions=discord.AllowedMentions(users=[player, interaction.user]),
            )
            await self.db_call(
                self.test_queue_entries.update_one,
                {"queue_id": entry["queue_id"], "status": "starting"},
                {"$set": {
                    "status": "testing",
                    "tester_id": interaction.user.id,
                    "ticket_id": ticket_id,
                    "channel_id": channel.id,
                    "started_at": datetime.now(timezone.utc),
                }},
            )
            await self.db_call(
                self.activity.insert_one,
                {
                    "guild_id": guild.id,
                    "kind": "queue_test_started",
                    "username": entry["username"],
                    "gamemode": gamemode,
                    "discord_id": player.id,
                    "tester_id": interaction.user.id,
                    "created_at": datetime.now(timezone.utc),
                },
            )
            # A member can be on several waitlists, but once a test is
            # successfully opened their other waiting entries are no longer
            # needed. Leave them untouched if session creation fails.
            await self.db_call(
                self.test_queue_entries.update_many,
                {
                    "guild_id": guild.id,
                    "discord_id": player.id,
                    "status": "waiting",
                },
                {"$set": {
                    "status": "cancelled",
                    "cancelled_at": datetime.now(timezone.utc),
                    "cancel_reason": "Another gamemode test session has started",
                }},
            )
        except Exception:
            log.exception("Could not open private queue test session for %s", entry.get("username"))
            if ticket_inserted:
                try:
                    await self.db_call(
                        self.tickets.update_one,
                        {"ticket_id": ticket_id},
                        {"$set": {"status": "closed", "migration_note": "Session setup failed"}},
                    )
                except Exception:
                    log.exception("Could not close failed queue test record %s", ticket_id)
            if channel is not None:
                try:
                    await channel.delete(reason="PvPClubTiers queue session setup failed")
                except discord.HTTPException:
                    pass
            try:
                await self.db_call(
                    self.test_queue_entries.update_one,
                    {"queue_id": entry["queue_id"]},
                    {"$set": {
                        "status": "waiting",
                        "tester_id": None,
                        "ticket_id": None,
                        "channel_id": None,
                    }},
                )
            except Exception:
                log.exception("Could not return failed queue entry %s to waiting", entry["queue_id"])
            await interaction.followup.send(
                "I couldn't open the private session. The queue entry has been returned to the queue; check the Render logs.",
                ephemeral=True,
            )
            await self.refresh_test_queue_panel(guild)
            return

        await self.refresh_test_queue_panel(guild)
        await interaction.followup.send(
            f"✅ Private test session opened: {channel.mention}\n"
            f"Player: {entry['username']} · Gamemode: {gamemode}",
            ephemeral=True,
        )

    async def ticket_action(self, interaction, action):
        if not await self.require_db(interaction):
            return
        if action != "close":
            await interaction.response.send_message(
                "Test sessions are assigned automatically from the queue. Use the queue panel to start a session.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket = await self.db_call(
            self.tickets.find_one,
            {
                "guild_id": interaction.guild.id,
                "channel_id": interaction.channel_id,
                "status": {"$in": ["open", "claimed", "completed"]},
            },
        )
        if not ticket:
            await interaction.followup.send("This is not an active PvPClubTiers test session.", ephemeral=True)
            return

        actor = interaction.user
        if ticket.get("session_type") == "queue":
            can_close = (
                actor.id == ticket["discord_id"]
                or actor.id == ticket.get("claimed_by")
                or (
                    isinstance(actor, discord.Member)
                    and (
                        actor.guild_permissions.manage_guild
                        or actor.guild_permissions.administrator
                    )
                )
            )
        else:
            allowed = isinstance(actor, discord.Member) and await self.staff_or_tester(actor, ticket["gamemode"])
            can_close = actor.id == ticket["discord_id"] or allowed
        if not can_close:
            await interaction.followup.send(
                "Only the player, the tester assigned to this session, or an administrator can close it.",
                ephemeral=True,
            )
            return

        await self.db_call(
            self.tickets.update_one,
            {"ticket_id": ticket["ticket_id"]},
            {"$set": {
                "status": "closed",
                "closed_at": datetime.now(timezone.utc),
                "closed_by": actor.id,
            }},
        )
        if ticket.get("session_type") == "queue" and ticket.get("status") != "completed":
            await self.db_call(
                self.test_queue_entries.update_one,
                {"queue_id": ticket.get("queue_id"), "status": "testing"},
                {"$set": {"status": "closed", "closed_at": datetime.now(timezone.utc)}},
            )

        await interaction.followup.send(
            "Test session closed. This private channel will be deleted in five seconds.",
            ephemeral=True,
        )
        config = await self.setting(interaction.guild.id)
        log_channel = interaction.guild.get_channel(int(config.get("logs_channel_id", 0) or 0))
        if log_channel:
            try:
                await log_channel.send(
                    f"Closed PvPClubTiers test {ticket['ticket_id']} for "
                    f"{ticket['username']} ({ticket['gamemode']}).",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.exception("Could not log closed PvPClubTiers test %s", ticket["ticket_id"])
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"PvPClubTiers session closed by {actor}")
        except discord.HTTPException:
            log.exception("Could not delete closed PvPClubTiers test channel %s", interaction.channel_id)

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
            await member.remove_roles(*remove, reason="PvPClubTiers overall best tier updated")
        if desired and desired not in member.roles:
            await member.add_roles(desired, reason=f"PvPClubTiers best tier: {tier} in {mode}")

    async def save_tier(self, guild, member, gamemode, new_tier, tester, source, username=None, reason=None):
        player = await self.get_or_create_player(guild.id, member, username)
        old_tier = (player.get("tiers") or {}).get(gamemode)
        # Retire legacy gamemodes from active profiles without changing the historical
        # tier-history collection. Only current supported modes remain on the profile.
        tiers = {
            mode: value for mode, value in (player.get("tiers") or {}).items()
            if mode in GAMEMODES and value in TIERS
        }
        tiers[gamemode] = new_tier
        now = datetime.now(timezone.utc)
        display_name = username or player.get("username")
        await self.db_call(self.players.update_one, {"guild_id": guild.id, "discord_id": member.id},
            {"$set": {"tiers": tiers, "total_points": total_tier_points(tiers),
                      "cooldowns": {}, "updated_at": now,
                      "username": display_name, "username_lc": display_name.casefold()}})
        await self.db_call(self.history.insert_one, {"guild_id": guild.id, "discord_id": member.id,
            "username": display_name, "gamemode": gamemode, "old_tier": old_tier or "Unranked",
            "new_tier": new_tier, "tester_id": tester.id, "source": source,
            "reason": (reason or "")[:300], "created_at": now})
        await self.db_call(self.activity.insert_one, {"guild_id": guild.id, "kind": "tier_change",
            "discord_id": member.id, "username": display_name, "gamemode": gamemode,
            "old_tier": old_tier or "Unranked", "new_tier": new_tier,
            "source": source, "reason": (reason or "")[:300], "created_at": now})
        if source == "test":
            await self.db_call(self.tester_stats.update_one, {"guild_id": guild.id, "tester_id": tester.id},
                {"$inc": {"tests_completed": 1}, "$set": {"tester_name": tester.display_name, "updated_at": now}}, upsert=True)
        updated = await self.get_player(guild.id, member.id)
        await self.sync_best_role(member, updated)
        best, best_mode = best_tier(tiers)
        msg = (f"{member.mention} ({display_name}) received **{new_tier}** in **{gamemode}**.\n"
               f"Previous: **{old_tier or 'Unranked'}**\nOverall role: **{best or 'Unranked'}**"
               + (f" ({best_mode})" if best_mode else ""))
        embed = discord.Embed(title="🏆 PvPClubTiers result", description=msg, colour=COLOURS[new_tier])
        embed.set_footer(text=f"Recorded by {tester.display_name} • mcpvp.club")
        config = await self.setting(guild.id)
        for key in ("results_channel_id", "activity_channel_id"):
            channel = guild.get_channel(config.get(key, 0))
            if channel:
                await channel.send(embed=embed)
        if config.get("leaderboard_channel_id"):
            await self.ensure_leaderboard_panel(guild)
        return old_tier, best, best_mode

    @commands.command(name="settier")
    @commands.guild_only()
    async def settier(self, ctx: commands.Context, *, target: str = ""):
        """Owner-only manual tier assignment: ?settier <gamemode> <minecraft-ign>."""
        if ctx.guild is None or ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("Only the server owner can use ?settier.", mention_author=False)
            return
        if self.db is None:
            await ctx.reply("PvPClubTiers database is not connected. Check the Render environment and restart the bot.")
            return

        parts = target.rsplit(maxsplit=1)
        if len(parts) != 2:
            await ctx.reply(
                "Usage: ?settier <gamemode> <ign>\n"
                "Example: ?settier UHC _Inglo\n"
                "For multi-word gamemodes, write the full name, e.g. ?settier Spear Mace _Inglo."
            )
            return

        mode_input = parts[0].strip().strip("\"'").casefold()
        username_input = parts[1].strip()
        gamemode = next((mode for mode in GAMEMODES if mode.casefold() == mode_input), None)
        if gamemode is None:
            await ctx.reply("Unknown gamemode. Available modes: " + ", ".join(GAMEMODES))
            return
        try:
            username = clean_username(username_input)
        except ValueError as exc:
            await ctx.reply(str(exc))
            return

        try:
            player = await self.db_call(
                self.players.find_one,
                {
                    "guild_id": ctx.guild.id,
                    "$or": [
                        {"username_lc": username.casefold()},
                        {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
                    ],
                },
            )
        except Exception:
            log.exception("Owner ?settier player lookup failed for %s", username)
            await ctx.reply("I couldn't look up that player. Check the Render logs.")
            return

        if not player:
            await ctx.reply(
                f"I couldn't find a PvPClubTiers profile for **{username}**. "
                "The player must have a profile first (for example, by requesting a test or using /ign)."
            )
            return

        player_id = int(player.get("discord_id") or player.get("discordId") or 0)
        if not player_id:
            await ctx.reply("That profile has no linked Discord account, so I can't assign its tier.")
            return

        current_tier = (player.get("tiers") or {}).get(gamemode)
        embed = discord.Embed(
            title="🛠️ Owner tier assignment",
            description=(
                f"**Player:** {player.get('username', username)}\n"
                f"**Gamemode:** {gamemode}\n"
                f"**Current tier:** {current_tier or 'Unranked'}\n\n"
                "Choose the new tier below. Only the server owner can confirm it."
            ),
            colour=0x55DDEB,
        )
        await ctx.reply(embed=embed, view=OwnerSetTierView(self, gamemode, username, player_id, current_tier))

    async def owner_set_tier_from_select(
        self,
        interaction: discord.Interaction,
        gamemode: str,
        username: str,
        player_id: int,
        new_tier: str,
    ):
        guild = interaction.guild
        if guild is None or interaction.user.id != guild.owner_id:
            await interaction.response.send_message("Only the server owner can confirm this tier change.", ephemeral=True)
            return
        if new_tier not in TIERS or gamemode not in GAMEMODES:
            await interaction.response.send_message("Invalid gamemode or tier.", ephemeral=True)
            return
        if not await self.require_db(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            player = await self.db_call(
                self.players.find_one,
                {
                    "guild_id": guild.id,
                    "discord_id": player_id,
                },
            )
            if not player:
                player = await self.db_call(
                    self.players.find_one,
                    {"guild_id": guild.id, "discordId": player_id},
                )
            if not player:
                await interaction.followup.send(
                    f"The profile for **{username}** no longer exists. Run ?settier again.",
                    ephemeral=True,
                )
                return

            member = guild.get_member(player_id)
            if member is None:
                try:
                    member = await guild.fetch_member(player_id)
                except discord.NotFound:
                    member = None
                except discord.HTTPException:
                    member = None

            if member is None:
                await interaction.followup.send(
                    f"**{username}** has a saved profile but is no longer in this Discord server. "
                    "They must rejoin before their tier role can be synchronized.",
                    ephemeral=True,
                )
                return

            old_tier, best, best_mode = await self.save_tier(
                guild,
                member,
                gamemode,
                new_tier,
                interaction.user,
                "owner",
                player.get("username") or username,
                reason="Manually assigned by the server owner",
            )
            try:
                await interaction.message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await interaction.followup.send(
                f"✅ Set **{username}** to **{new_tier}** in **{gamemode}**. "
                f"Previous tier: **{old_tier or 'Unranked'}**. "
                f"Overall tier: **{best or 'Unranked'}**"
                + (f" in **{best_mode}**." if best_mode else "."),
                ephemeral=True,
            )
        except (ValueError, PyMongoError, discord.HTTPException):
            log.exception("Owner ?settier failed for %s in %s", username, gamemode)
            await interaction.followup.send(
                "I couldn't save that tier. Check the Render logs for the exact database or role error.",
                ephemeral=True,
            )
        except Exception:
            log.exception("Unexpected owner ?settier error for %s in %s", username, gamemode)
            await interaction.followup.send(
                "Something went wrong while setting the tier. Check the Render logs.",
                ephemeral=True,
            )

    @app_commands.command(name="set-tier", description="Record the result for the private test session you are assigned.")
    @app_commands.guild_only()
    @app_commands.choices(gamemode=MODE_OPTIONS, tier=TIER_OPTIONS)
    async def set_tier(self, interaction, member: discord.Member, gamemode: app_commands.Choice[str],
                       tier: app_commands.Choice[str]):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(thinking=True)
        actor = interaction.user
        if not isinstance(actor, discord.Member) or not await self.staff_or_tester(actor, gamemode.value):
            await interaction.followup.send(
                f"You need the {gamemode.value} Tester role or server management permissions.",
                ephemeral=True,
            )
            return
        ticket = await self.db_call(self.tickets.find_one, {
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel_id,
            "status": "claimed",
            "session_type": "queue",
            "claimed_by": actor.id,
        })
        if not ticket:
            await interaction.followup.send(
                "Run this command inside the private test session assigned to you. "
                "Only the tester who started that session can submit its result.",
                ephemeral=True,
            )
            return
        if ticket["gamemode"] != gamemode.value or ticket["discord_id"] != member.id:
            await interaction.followup.send(
                "The selected player and gamemode must match this private test session.",
                ephemeral=True,
            )
            return
        try:
            await self.save_tier(
                interaction.guild, member, gamemode.value, tier.value, actor,
                "test", ticket.get("username"),
            )
        except (ValueError, PyMongoError, discord.HTTPException):
            log.exception("Could not save PvPClubTiers result")
            await interaction.followup.send(
                "Could not save this tier result. Check the Render logs and try again.",
                ephemeral=True,
            )
            return
        await self.db_call(self.tickets.update_one, {"ticket_id": ticket["ticket_id"]}, {
            "$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}
        })
        await self.db_call(
            self.test_queue_entries.update_one,
            {"queue_id": ticket.get("queue_id"), "status": "testing"},
            {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}},
        )
        await interaction.followup.send(
            f"✅ Saved **{tier.value}** for {member.mention} in **{gamemode.value}**. "
            "The test is complete. Close the session when you are finished.",
        )
        await self.refresh_test_queue_panel(interaction.guild)

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
        await interaction.response.send_message(f"Your PvPClubTiers username is now **{player['username']}**.", ephemeral=True)

    @app_commands.command(name="tiers", description="View a player's tier in every gamemode.")
    @app_commands.guild_only()
    async def tiers(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        player = await self.get_player(interaction.guild.id, target.id)
        if not player:
            await interaction.response.send_message(f"No PvPClubTiers profile found for {target.mention}.", ephemeral=True)
            return
        best, best_mode = best_tier(player.get("tiers", {}))
        lines = [f"**{m}:** {(player.get('tiers') or {}).get(m, 'Unranked')}" for m in GAMEMODES]
        e = discord.Embed(title=f"🏆 {player.get('username', target.display_name)} • Tiers",
                          description="\n".join(lines), colour=COLOURS.get(best, 0x55DDEB))
        e.add_field(name="Best tier", value=f"{best or 'Unranked'}" + (f" in {best_mode}" if best_mode else ""), inline=False)
        e.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="profile", description="View a player's PvPClubTiers profile.")
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
        e = discord.Embed(title=f"PvPClubTiers • {player.get('username', target.display_name)}",
                          description=f"Best tier: **{best or 'Unranked'}**" + (f" in **{mode}**" if mode else ""),
                          colour=COLOURS.get(best, 0x55DDEB))
        wins = int(player.get("wins", 0))
        losses = int(player.get("losses", 0))
        played = wins + losses
        e.add_field(name="Duel ELO", value=str(player.get("elo", 1000)), inline=True)
        e.add_field(name="Duel record", value=f"{wins}W / {losses}L" + (f" ({wins / played * 100:.1f}%)" if played else ""), inline=True)
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
        if mode:
            rows.sort(key=lambda p: (
                p["sort_index"],
                -POINTS.get(p["display_tier"], 0),
                -p["totalPoints"],
                p["username"].casefold(),
            ))
        else:
            rows.sort(key=lambda p: (
                -p["totalPoints"],
                p["sort_index"],
                -int(p.get("elo", 1000)),
                p["username"].casefold(),
            ))
        return rows[:limit]

    @app_commands.command(name="tier-leaderboard", description="Show the overall or gamemode tier leaderboard.")
    @app_commands.guild_only()
    @app_commands.choices(gamemode=MODE_OPTIONS)
    async def tier_leaderboard(self, interaction, gamemode: app_commands.Choice[str] | None = None):
        if not await self.require_db(interaction):
            return
        await interaction.response.defer(thinking=True)
        mode = gamemode.value if gamemode else None
        if mode is None:
            embed = await self.build_leaderboard_embed(interaction.guild)
            await interaction.followup.send(embed=embed, view=LeaderboardPanelView(self))
            return
        rows = await self.make_leaderboard(interaction.guild.id, mode, 15)
        if not rows:
            await interaction.followup.send(
                f"No players have been tiered in {mode} yet.",
                ephemeral=True,
            )
            return
        lines = [
            f"**{i}.** {row['username']} — **{row['display_tier']}**"
            for i, row in enumerate(rows, 1)
        ]
        embed = discord.Embed(
            title=f"{MODE_EMOJIS.get(mode, '🎮')} {mode} leaderboard",
            description="\n".join(lines),
            colour=0x55DDEB,
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="tier-rank", description="Show a player's overall PvPClubTiers position.")
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

    @app_commands.command(name="top", description="Show the top ten PvPClubTiers players.")
    @app_commands.guild_only()
    async def top(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.make_leaderboard(interaction.guild.id, None, 10)
        if not rows:
            await interaction.response.send_message("No tiered players yet.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(f"**{i}.** {r['username']} — **{r['best_tier']}**" for i, r in enumerate(rows, 1)))

    @app_commands.command(name="search", description="Search PvPClubTiers profiles by Minecraft username.")
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
        await interaction.response.send_message(embed=discord.Embed(title="PvPClubTiers search", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="compare", description="Compare the gamemode tiers of two players.")
    @app_commands.guild_only()
    async def compare(self, interaction, first: discord.Member, second: discord.Member):
        if not await self.require_db(interaction):
            return
        a = await self.get_player(interaction.guild.id, first.id)
        b = await self.get_player(interaction.guild.id, second.id)
        if not a or not b:
            await interaction.response.send_message("Both players need an PvPClubTiers profile first.", ephemeral=True)
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

    @app_commands.command(name="recent", description="Show the latest PvPClubTiers tier changes.")
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

    @app_commands.command(name="testerboard", description="Show the most active PvPClubTiers testers.")
    @app_commands.guild_only()
    async def testerboard(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.db_call(lambda: list(self.tester_stats.find({"guild_id": interaction.guild.id}).sort("tests_completed", DESCENDING).limit(10)))
        if not rows:
            await interaction.response.send_message("No tester results recorded yet.", ephemeral=True)
            return
        lines = [f"**{i}.** {r.get('tester_name', 'Tester')} — **{r.get('tests_completed', 0)}** tests" for i, r in enumerate(rows, 1)]
        await interaction.response.send_message(embed=discord.Embed(title="PvPClubTiers tester leaderboard", description="\n".join(lines), colour=0x55DDEB))

    @app_commands.command(name="tier-adjust", description="Staff: adjust a player's tier with an audit reason.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(gamemode=MODE_OPTIONS, tier=TIER_OPTIONS)
    async def tier_adjust(self, interaction, member: discord.Member,
                          gamemode: app_commands.Choice[str], tier: app_commands.Choice[str],
                          reason: str):
        if not await self.require_db(interaction):
            return
        reason = reason.strip()
        if len(reason) < 8:
            await interaction.response.send_message("Please provide an audit reason of at least 8 characters.", ephemeral=True)
            return
        try:
            await self.save_tier(
                interaction.guild, member, gamemode.value, tier.value,
                interaction.user, "staff", reason=reason,
            )
        except Exception:
            log.exception("Staff tier adjustment failed")
            await interaction.response.send_message("Tier adjustment failed. Check the Render logs.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Set {member.mention} to **{tier.value}** in **{gamemode.value}**. "
            "The change and reason were recorded in tier history."
        )

    @app_commands.command(name="tier-recalculate", description="Repair a player's total points and shared tier role.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tier_recalculate(self, interaction, member: discord.Member):
        if not await self.require_db(interaction):
            return
        player = await self.get_player(interaction.guild.id, member.id)
        if not player:
            await interaction.response.send_message("This member has no PvPClubTiers profile yet.", ephemeral=True)
            return
        stored_tiers = player.get("tiers", {}) or {}
        tiers = {
            mode: value for mode, value in stored_tiers.items()
            if mode in GAMEMODES and value in TIERS
        }
        removed_modes = sorted(set(stored_tiers) - set(tiers))
        total = total_tier_points(tiers)
        await self.db_call(self.players.update_one, {
            "guild_id": interaction.guild.id, "discord_id": member.id
        }, {"$set": {"tiers": tiers, "total_points": total,
                     "elo": int(player.get("elo", 1000)),
                     "updated_at": datetime.now(timezone.utc)}})
        await self.sync_best_role(member, {**player, "tiers": tiers})
        best, mode = best_tier(tiers)
        cleanup_note = (
            "\nRemoved retired gamemodes: " + ", ".join(removed_modes)
            if removed_modes else ""
        )
        await interaction.response.send_message(
            f"✅ Recalculated {member.mention}: **{total} points**, best tier "
            f"**{best or 'Unranked'}**" + (f" in **{mode}**" if mode else "")
            + "." + cleanup_note
        )

    @app_commands.command(name="tier-stats", description="Show the tier distribution for a gamemode.")
    @app_commands.guild_only()
    @app_commands.choices(gamemode=MODE_OPTIONS)
    async def tier_stats(self, interaction, gamemode: app_commands.Choice[str]):
        if not await self.require_db(interaction):
            return
        docs = await self.db_call(lambda: list(self.players.find({
            "guild_id": interaction.guild.id, "tier_banned": {"$ne": True}
        }, {"tiers": 1})))
        counts = {tier: 0 for tier in TIERS}
        for player in docs:
            tier = (player.get("tiers") or {}).get(gamemode.value)
            if tier in counts:
                counts[tier] += 1
        ranked = sum(counts.values())
        lines = []
        for tier in TIERS:
            count = counts[tier]
            bar = "█" * min(12, round(count / max(1, ranked) * 12))
            lines.append(f"{tier:<3}  {count:>4}  {bar or '·'}")
        embed = discord.Embed(
            title=f"PvPClubTiers distribution • {gamemode.value}",
            description="\n".join(lines),
            colour=0x55DDEB,
        )
        embed.set_footer(text=f"{ranked} ranked players • {len(docs) - ranked} unranked")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="tier-cooldowns", description="Check the tier-test request cooldown rules.")
    @app_commands.guild_only()
    async def tier_cooldowns(self, interaction):
        await interaction.response.send_message(
            "✅ **Tier-testing cooldowns are disabled.** You can request another test in any gamemode "
            "as soon as you have no other active test ticket.",
            ephemeral=True,
        )

    @app_commands.command(name="elo", description="View a player's competitive duel rating.")
    @app_commands.guild_only()
    async def elo(self, interaction, member: discord.Member | None = None):
        if not await self.require_db(interaction):
            return
        target = member or interaction.user
        player = await self.get_or_create_player(interaction.guild.id, target)
        wins = int(player.get("wins", 0))
        losses = int(player.get("losses", 0))
        played = wins + losses
        embed = discord.Embed(
            title=f"⚔️ {player.get('username', target.display_name)} • Duel rating",
            description=f"**{int(player.get('elo', 1000))} ELO**",
            colour=0x55DDEB,
        )
        embed.add_field(name="Record", value=f"{wins}W – {losses}L", inline=True)
        embed.add_field(name="Win rate", value=f"{(wins / played * 100):.1f}%" if played else "No duels yet", inline=True)
        embed.add_field(name="Duels played", value=str(player.get("duels_played", played)), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="elo-leaderboard", description="Show the top competitive duel ratings.")
    @app_commands.guild_only()
    async def elo_leaderboard(self, interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.db_call(lambda: list(self.players.find({
            "guild_id": interaction.guild.id, "duels_played": {"$gt": 0}
        }).sort([("elo", DESCENDING), ("wins", DESCENDING)]).limit(10)))
        if not rows:
            await interaction.response.send_message("No ranked duels have been completed yet.", ephemeral=True)
            return
        lines = [
            f"**{index}.** {row.get('username', 'Player')} — **{int(row.get('elo', 1000))} ELO** "
            f"({row.get('wins', 0)}W–{row.get('losses', 0)}L)"
            for index, row in enumerate(rows, 1)
        ]
        await interaction.response.send_message(embed=discord.Embed(
            title="⚔️ PvPClubTiers ELO leaderboard",
            description="\n".join(lines), colour=0x55DDEB
        ))

    @app_commands.command(name="tierban", description="Block a member from requesting PvPClubTiers tests.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tierban(self, interaction, member: discord.Member, reason: str = "No reason supplied"):
        if not await self.require_db(interaction):
            return
        await self.db_call(self.players.update_one, {"guild_id": interaction.guild.id, "discord_id": member.id},
                           {"$set": {
                               "tier_banned": True,
                               "tier_ban_reason": reason[:300],
                               "discordId": member.id,
                           }}, upsert=True)
        await interaction.response.send_message(f"{member.mention} is blocked from tier tests.", ephemeral=True)

    @app_commands.command(name="tierunban", description="Allow a member to request PvPClubTiers tests again.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tierunban(self, interaction, member: discord.Member):
        if not await self.require_db(interaction):
            return
        await self.db_call(self.players.update_one, {"guild_id": interaction.guild.id, "discord_id": member.id},
                           {"$set": {"tier_banned": False, "discordId": member.id},
                            "$unset": {"tier_ban_reason": ""}}, upsert=True)
        await interaction.response.send_message(f"{member.mention} can request tests again.", ephemeral=True)


    async def ensure_matchmaking_panel(self, guild: discord.Guild):
        config = await self.setting(guild.id)
        channel_id = config.get("matchmaking_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        role_id = config.get("matchmaking_ping_role_id")
        ping_role = guild.get_role(int(role_id)) if role_id else None
        if ping_role is None:
            ping_role = discord.utils.get(guild.roles, name="Matchmaking Ping")
            if ping_role is None:
                ping_role = await guild.create_role(
                    name="Matchmaking Ping", colour=discord.Colour(0x55DDEB),
                    mentionable=True, reason="PvPClubTiers matchmaking notifications",
                )
            elif not ping_role.mentionable:
                await ping_role.edit(mentionable=True, reason="Enable matchmaking role mentions")
            await self.db_call(self.settings.update_one, {"guild_id": guild.id}, {
                "$set": {"matchmaking_ping_role_id": ping_role.id}
            })
        panel = discord.Embed(
            title="⚔️ Matchmaking",
            description=(
                "Use **/duel** to join the queue and choose a gamemode. You will be matched "
                "with the next player waiting in that same gamemode.\\n\\n"
                "• Win a duel: move **one tier up** in that gamemode.\\n"
                "• Lose twice in a row: move **one tier down** in that gamemode.\\n"
                "• Loss streaks are tracked separately for each gamemode.\\n"
                "• Both players must independently confirm the same winner. If you disagree, "
                "no tiers change and staff will review the result.\\n\\n"
                "Use the buttons below to subscribe or unsubscribe from matchmaking pings."
            ),
            colour=0x55DDEB,
        )
        panel.set_footer(text="PvPClubTiers • Matchmaking")
        message = None
        message_id = config.get("matchmaking_panel_message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                message = None
        if message:
            await message.edit(embed=panel, view=MatchmakingPanelView(self))
        else:
            message = await channel.send(embed=panel, view=MatchmakingPanelView(self))
            await self.db_call(self.settings.update_one, {"guild_id": guild.id}, {
                "$set": {"matchmaking_panel_message_id": message.id}
            })
        # Pin the persistent guide so it remains easy to find while queue posts arrive.
        if not message.pinned:
            try:
                await message.pin(reason="Keep the PvPClubTiers matchmaking instructions easy to find")
            except discord.Forbidden:
                log.warning("Cannot pin the matchmaking panel in %s; grant Manage Messages to the bot.", channel.id)

    async def recover_interrupted_test_starts(self, guild: discord.Guild):
        """Repair queue entries if the process stopped while a tester was opening a session."""
        if self.db is None or not hasattr(self, "test_queue_entries"):
            return
        now = datetime.now(timezone.utc)
        changed = False
        entries = await self.db_call(
            lambda: list(self.test_queue_entries.find({
                "guild_id": guild.id,
                "status": "starting",
            }))
        )
        if not entries:
            return

        config = await self.setting(guild.id)
        category_id = config.get("test_session_category_id")
        category = guild.get_channel(int(category_id)) if category_id else None

        for entry in entries:
            started_at = entry.get("starting_at")
            if isinstance(started_at, datetime):
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                if (now - started_at).total_seconds() < 300:
                    continue

            queue_id = entry.get("queue_id")
            ticket_id = entry.get("ticket_id")
            ticket = await self.db_call(
                self.tickets.find_one,
                {"guild_id": guild.id, "queue_id": queue_id, "session_type": "queue"},
            )
            if ticket:
                channel_id = ticket.get("channel_id") or ticket.get("channelId")
                channel = guild.get_channel(int(channel_id)) if channel_id else None
                if channel is None and channel_id:
                    try:
                        fetched = await guild.fetch_channel(int(channel_id))
                        channel = fetched if isinstance(fetched, discord.TextChannel) else None
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        channel = None
                if channel is not None:
                    recovered_status = (
                        "completed" if ticket.get("status") == "completed"
                        else "closed" if ticket.get("status") == "closed"
                        else "testing"
                    )
                    result = await self.db_call(
                        self.test_queue_entries.update_one,
                        {"queue_id": queue_id, "status": "starting"},
                        {"$set": {
                            "status": recovered_status,
                            "ticket_id": ticket.get("ticket_id"),
                            "channel_id": channel.id,
                            "tester_id": ticket.get("claimed_by"),
                            "recovered_at": now,
                        }},
                    )
                    changed = changed or bool(result.modified_count)
                    continue

                await self.db_call(
                    self.tickets.update_one,
                    {"ticket_id": ticket.get("ticket_id")},
                    {"$set": {
                        "status": "closed",
                        "closed_at": now,
                        "migration_note": "Session channel was missing during startup recovery",
                    }},
                )
                result = await self.db_call(
                    self.test_queue_entries.update_one,
                    {"queue_id": queue_id, "status": "starting"},
                    {"$set": {
                        "status": "closed",
                        "closed_at": now,
                        "recovery_note": "The session channel no longer exists",
                    }},
                )
                changed = changed or bool(result.modified_count)
                continue

            # No ticket record was committed. Remove a channel that may have been
            # created just before the process stopped, then safely return the player
            # to the front of the waiting queue.
            if isinstance(category, discord.CategoryChannel) and ticket_id:
                for orphan in list(category.text_channels):
                    if ticket_id in (orphan.topic or ""):
                        try:
                            await orphan.delete(reason="Removing an incomplete PvPClubTiers queue session")
                        except discord.HTTPException:
                            log.exception("Could not remove incomplete queue session %s", orphan.id)
            result = await self.db_call(
                self.test_queue_entries.update_one,
                {"queue_id": queue_id, "status": "starting"},
                {"$set": {
                    "status": "waiting",
                    "tester_id": None,
                    "ticket_id": None,
                    "channel_id": None,
                    "starting_at": None,
                    "recovered_at": now,
                    "recovery_note": "Interrupted session creation was returned to the queue",
                }},
            )
            changed = changed or bool(result.modified_count)

        if changed:
            await self.refresh_test_queue_panel(guild)

    @tasks.loop(minutes=1)
    async def queue_recovery_watcher(self):
        for guild in list(self.bot.guilds):
            try:
                await self.recover_interrupted_test_starts(guild)
            except Exception:
                log.exception("Could not recover interrupted test queue entries in guild %s", guild.id)

    @queue_recovery_watcher.before_loop
    async def before_queue_recovery_watcher(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def leaderboard_refresh_watcher(self):
        if self.db is None:
            return
        for guild in list(self.bot.guilds):
            try:
                config = await self.setting(guild.id)
                if config.get("leaderboard_channel_id"):
                    await self.ensure_leaderboard_panel(guild)
            except Exception:
                log.exception("Could not refresh tier leaderboard in guild %s", guild.id)

    @leaderboard_refresh_watcher.before_loop
    async def before_leaderboard_refresh_watcher(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.db is None:
            return
        for guild in self.bot.guilds:
            try:
                config = await self.setting(guild.id)
                if config.get("matchmaking_channel_id"):
                    await self.ensure_matchmaking_panel(guild)
                if config.get("test_queue_channel_id"):
                    await self.ensure_test_queue_panel(guild)
                    await self.ensure_test_request_panel(guild)
                if config.get("tier_info_channel_id"):
                    await self.ensure_tier_info_panel(guild)
                if config.get("leaderboard_channel_id"):
                    await self.ensure_leaderboard_panel(guild)
                await self.recover_interrupted_test_starts(guild)
                # A Mongo transaction is all-or-nothing, so stale resolving states
                # can safely be sent to staff review after a restart.
                cutoff = datetime.now(timezone.utc).timestamp() - 15 * 60
                await self.db_call(self.duels.update_many, {
                    "guild_id": guild.id, "matchmaking": True, "status": "resolving",
                    "resolving_at": {"$lt": datetime.fromtimestamp(cutoff, timezone.utc)},
                }, {"$set": {"status": "disputed", "recovery_note": "Settlement interrupted; staff review required."}})
            except Exception:
                log.exception("Could not restore matchmaking for guild %s", guild.id)

    async def queue_for_duel(self, interaction: discord.Interaction, gamemode: str):
        if not await self.require_db(interaction):
            return
        if gamemode not in GAMEMODES:
            await interaction.response.send_message("That gamemode is not supported.", ephemeral=True)
            return
        guild = interaction.guild
        config = await self.setting(guild.id)
        channel = guild.get_channel(int(config.get("matchmaking_channel_id", 0)))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Matchmaking is not set up yet. Ask staff to run ?tiersetup.", ephemeral=True
            )
            return

        async with self.matchmaking_queue_lock:
            active = await self.db_call(self.duels.find_one, {
                "guild_id": guild.id,
                "status": {"$in": ["pending", "active", "disputed", "resolving"]},
                "$or": [{"challenger_id": interaction.user.id}, {"opponent_id": interaction.user.id}],
            })
            if active:
                await interaction.response.send_message(
                    "You already have an unfinished duel. Finish or resolve it before joining matchmaking.",
                    ephemeral=True,
                )
                return
            queued = await self.db_call(self.matchmaking_queue.find_one, {
                "guild_id": guild.id, "user_id": interaction.user.id, "status": "waiting",
            })
            if queued:
                await interaction.response.send_message(
                    f"You are already queued for **{queued.get('gamemode', 'a gamemode')}**. "
                    "Use the Cancel queue button first.", ephemeral=True,
                )
                return
            candidates = await self.db_call(
                lambda: list(self.matchmaking_queue.find({
                    "guild_id": guild.id, "gamemode": gamemode, "status": "waiting",
                    "user_id": {"$ne": interaction.user.id},
                }).sort("created_at", ASCENDING).limit(100))
            )
            opponent_queue = None
            for candidate in candidates:
                candidate_id = int(candidate["user_id"])
                try:
                    candidate_member = guild.get_member(candidate_id) or await guild.fetch_member(candidate_id)
                except discord.NotFound:
                    await self.db_call(self.matchmaking_queue.update_one, {
                        "queue_id": candidate["queue_id"], "status": "waiting",
                    }, {"$set": {"status": "cancelled", "cancel_reason": "user_left_server",
                                 "cancelled_at": datetime.now(timezone.utc)}})
                    continue
                except discord.HTTPException:
                    # Do not break the queue if Discord temporarily cannot resolve a member.
                    continue
                active_candidate = await self.db_call(self.duels.find_one, {
                    "guild_id": guild.id,
                    "status": {"$in": ["pending", "active", "disputed", "resolving"]},
                    "$or": [{"challenger_id": candidate_id}, {"opponent_id": candidate_id}],
                })
                if active_candidate:
                    await self.db_call(self.matchmaking_queue.update_one, {
                        "queue_id": candidate["queue_id"], "status": "waiting",
                    }, {"$set": {"status": "cancelled", "cancel_reason": "unfinished_duel",
                                 "cancelled_at": datetime.now(timezone.utc)}})
                    stale_message_id = candidate.get("message_id")
                    if stale_message_id:
                        try:
                            stale_message = await channel.fetch_message(int(stale_message_id))
                            await stale_message.edit(
                                content=f"Queue closed for <@{candidate_id}> because they have an unfinished duel.",
                                embed=None, view=None,
                            )
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    continue
                opponent_queue = candidate
                break
            role_id = config.get("matchmaking_ping_role_id")
            ping_role = guild.get_role(int(role_id)) if role_id else None
            ping = ping_role.mention if ping_role else ""
            now = datetime.now(timezone.utc)

            if opponent_queue is None:
                queue_id = uuid4().hex
                await self.db_call(self.matchmaking_queue.insert_one, {
                    "queue_id": queue_id, "guild_id": guild.id, "user_id": interaction.user.id,
                    "gamemode": gamemode, "status": "waiting", "created_at": now,
                })
                try:
                    message = await channel.send(
                        content=f"{ping} {interaction.user.mention} is looking for a **{gamemode}** duel.",
                        embed=discord.Embed(
                            title="🔎 Looking for an opponent",
                            description=f"{interaction.user.mention} is now queued for **{gamemode}**.",
                            colour=0x55DDEB,
                        ),
                        view=MatchmakingQueueView(self),
                        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                    )
                    await self.db_call(self.matchmaking_queue.update_one, {"queue_id": queue_id}, {
                        "$set": {"message_id": message.id}
                    })
                except Exception:
                    await self.db_call(self.matchmaking_queue.update_one, {"queue_id": queue_id}, {
                        "$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}
                    })
                    raise
                await interaction.response.send_message(
                    f"✅ You are in the **{gamemode}** matchmaking queue. I will pair you with the next available player.",
                    ephemeral=True,
                )
                return

            duel_id = uuid4().hex[:10]
            player_one_id = int(opponent_queue["user_id"])
            player_two_id = interaction.user.id
            current_queue_id = uuid4().hex
            await self.db_call(self.duels.insert_one, {
                "duel_id": duel_id, "guild_id": guild.id,
                "challenger_id": player_one_id, "opponent_id": player_two_id,
                "gamemode": gamemode, "status": "active", "matchmaking": True,
                "reports": {}, "created_at": now, "started_at": now,
                "queue_ids": [opponent_queue["queue_id"], current_queue_id],
            })
            try:
                embed = discord.Embed(
                    title="⚔️ Match found — duel started",
                    description=(
                        f"**Player 1:** <@{player_one_id}>\\n"
                        f"**Player 2:** <@{player_two_id}>\\n"
                        f"**Gamemode:** {gamemode}\\n"
                        f"**Duel ID:** {duel_id}\\n\\n"
                        "Play your duel first. Afterwards, both players must select the same winner below. "
                        "Different reports go to staff review and do not change tiers."
                    ),
                    colour=0x55DDEB,
                )
                embed.set_footer(text="Each participant can submit one result.")
                match_message = await channel.send(
                    content=f"{ping} Match found: <@{player_one_id}> vs <@{player_two_id}>",
                    embed=embed, view=DuelResultView(self),
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                )
                await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {
                    "$set": {"result_message_id": match_message.id, "matchmaking_channel_id": channel.id}
                })
                await self.db_call(self.matchmaking_queue.update_one, {
                    "queue_id": opponent_queue["queue_id"], "status": "waiting",
                }, {"$set": {"status": "matched", "duel_id": duel_id, "matched_at": now}})
                await self.db_call(self.matchmaking_queue.insert_one, {
                    "queue_id": current_queue_id, "guild_id": guild.id, "user_id": player_two_id,
                    "gamemode": gamemode, "status": "matched", "duel_id": duel_id,
                    "message_id": match_message.id, "created_at": now, "matched_at": now,
                })
                old_message_id = opponent_queue.get("message_id")
                if old_message_id:
                    try:
                        old_message = await channel.fetch_message(int(old_message_id))
                        await old_message.edit(
                            content=f"⚔️ <@{player_one_id}> has been matched with <@{player_two_id}> in **{gamemode}**.",
                            embed=None, view=None,
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            except Exception:
                log.exception("Could not publish matchmaking duel %s", duel_id)
                await self.db_call(self.duels.update_one, {"duel_id": duel_id, "status": "active"}, {
                    "$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}
                })
                await self.db_call(self.matchmaking_queue.update_one, {
                    "queue_id": opponent_queue["queue_id"], "status": "waiting",
                }, {"$set": {"status": "waiting", "duel_id": None}})
                await interaction.response.send_message(
                    "I found an opponent but could not publish the match. Please try /duel again or contact staff.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"⚔️ Match found! You are paired with <@{player_one_id}> in **{gamemode}**. "
                "Both players must confirm the result with the buttons in #matchmaking.",
                ephemeral=True,
            )

    async def cancel_matchmaking_queue(self, interaction: discord.Interaction):
        if not await self.require_db(interaction):
            return
        queue = await self.db_call(self.matchmaking_queue.find_one, {
            "guild_id": interaction.guild.id, "message_id": interaction.message.id,
            "user_id": interaction.user.id, "status": "waiting",
        })
        if not queue:
            await interaction.response.send_message(
                "This queue entry is no longer active, or it belongs to another player.", ephemeral=True,
            )
            return
        updated = await self.db_call(self.matchmaking_queue.update_one, {
            "queue_id": queue["queue_id"], "status": "waiting",
        }, {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}})
        if not updated.modified_count:
            await interaction.response.send_message("This queue entry has already changed.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"❌ <@{interaction.user.id}> cancelled their **{queue['gamemode']}** matchmaking request.",
            embed=None, view=None,
        )

    async def record_matchmaking_result(self, interaction: discord.Interaction, player_number: int):
        if not await self.require_db(interaction):
            return
        duel = await self.db_call(self.duels.find_one, {
            "guild_id": interaction.guild.id, "result_message_id": interaction.message.id,
            "matchmaking": True,
        })
        if not duel or duel.get("status") != "active":
            await interaction.response.send_message("This duel is no longer accepting result confirmations.", ephemeral=True)
            return
        if interaction.user.id not in (duel["challenger_id"], duel["opponent_id"]):
            await interaction.response.send_message("Only the two players in this duel can confirm its result.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winner_id = duel["challenger_id"] if player_number == 1 else duel["opponent_id"]
        report_key = str(interaction.user.id)
        await self.db_call(self.duels.update_one, {
            "guild_id": interaction.guild.id, "duel_id": duel["duel_id"],
            "status": "active", f"reports.{report_key}": {"$exists": False},
        }, {"$set": {f"reports.{report_key}": {
            "winner_id": winner_id, "submitted_at": datetime.now(timezone.utc),
        }}})
        duel = await self.db_call(self.duels.find_one, {
            "guild_id": interaction.guild.id, "duel_id": duel["duel_id"],
        })
        reports = duel.get("reports", {})
        if report_key not in reports:
            await interaction.followup.send("You already submitted your result. It cannot be changed.", ephemeral=True)
            return
        if len(reports) < 2:
            await interaction.followup.send(
                "✅ Your result is saved. Waiting for the other player to confirm the same winner.",
                ephemeral=True,
            )
            return
        first_report = reports.get(str(duel["challenger_id"]), {}).get("winner_id")
        second_report = reports.get(str(duel["opponent_id"]), {}).get("winner_id")
        if first_report != second_report:
            await self.db_call(self.duels.update_one, {
                "guild_id": interaction.guild.id, "duel_id": duel["duel_id"], "status": "active",
            }, {"$set": {"status": "disputed", "disputed_at": datetime.now(timezone.utc)}})
            embed = discord.Embed(
                title="⚠️ Duel disputed — staff review required",
                description=(
                    f"Both players submitted different winners for duel {duel['duel_id']}. "
                    "No ELO or tier changes have been applied. Staff can inspect it with /duel-disputes."
                ),
                colour=0xE67E22,
            )
            await interaction.message.edit(embed=embed, view=None)
            await interaction.followup.send(
                "Your reports do not match. The duel is waiting for staff review; no tiers were changed.",
                ephemeral=True,
            )
            await self.send_duel_log(interaction.guild, f"Duel {duel['duel_id']} was disputed because the players disagreed.")
            return

        claimed = await self.db_call(self.duels.update_one, {
            "guild_id": interaction.guild.id, "duel_id": duel["duel_id"], "status": "active",
        }, {"$set": {
            "status": "resolving", "resolving_at": datetime.now(timezone.utc), "winner_id": first_report,
        }})
        if not claimed.modified_count:
            await interaction.followup.send("This duel is already being finalised. No duplicate tier change was applied.", ephemeral=True)
            return
        try:
            outcome = await self.settle_matchmaking_duel(
                interaction.guild, duel["duel_id"], int(first_report), interaction.user,
            )
        except Exception:
            log.exception("Duel settlement failed for %s", duel["duel_id"])
            await self.db_call(self.duels.update_one, {
                "guild_id": interaction.guild.id, "duel_id": duel["duel_id"], "status": "resolving",
            }, {"$set": {"status": "disputed", "settlement_error": "Settlement failed; staff review required."}})
            await interaction.followup.send(
                "I could not safely apply this result. The duel has been moved to staff review; no partial tier update was committed.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ Both players confirmed the result. <@{outcome['winner_id']}> won. "
            f"<@{outcome['loser_id']}> now has a loss streak of {outcome['loser_streak']} in **{duel['gamemode']}**.",
            ephemeral=True,
        )

    async def send_duel_log(self, guild: discord.Guild, message: str):
        config = await self.setting(guild.id)
        channel = guild.get_channel(int(config.get("logs_channel_id", 0)))
        if channel:
            try:
                await channel.send(message)
            except discord.HTTPException:
                log.exception("Could not send duel audit log")

    async def settle_matchmaking_duel(self, guild: discord.Guild, duel_id: str,
                                      winner_id: int, actor: discord.abc.User):
        duel = await self.db_call(self.duels.find_one, {"guild_id": guild.id, "duel_id": duel_id})
        if not duel or duel.get("status") != "resolving" or not duel.get("matchmaking"):
            raise RuntimeError("This matchmaking duel is not ready for settlement.")
        first_id, second_id = int(duel["challenger_id"]), int(duel["opponent_id"])
        if winner_id not in (first_id, second_id):
            raise ValueError("The selected winner is not a participant in this duel.")
        winner_id = int(winner_id)
        loser_id = second_id if winner_id == first_id else first_id
        members = {}
        for user_id in (winner_id, loser_id):
            members[user_id] = guild.get_member(user_id)
            if members[user_id] is None:
                members[user_id] = await guild.fetch_member(user_id)
        now = datetime.now(timezone.utc)
        outcome = {}

        def apply_transaction():
            with self.mongo_client.start_session() as session:
                def transaction_body(active_session):
                    outcome.clear()
                    current_duel = self.duels.find_one({
                        "guild_id": guild.id, "duel_id": duel_id, "status": "resolving",
                    }, session=active_session)
                    if not current_duel:
                        raise RuntimeError("Duel settlement was already completed or claimed.")
                    player_docs = {}
                    for user_id in (winner_id, loser_id):
                        member = members[user_id]
                        player = self.players.find_one({
                            "guild_id": guild.id, "discord_id": user_id,
                        }, session=active_session)
                        if player is None:
                            player = {
                                "guild_id": guild.id, "discord_id": user_id, "discordId": user_id,
                                "username": member.display_name, "username_lc": member.display_name.casefold(),
                                "tiers": {}, "elo": 1000, "wins": 0, "losses": 0,
                                "duels_played": 0, "duel_loss_streaks": {}, "created_at": now,
                            }
                        player_docs[user_id] = player

                    winner_player = player_docs[winner_id]
                    loser_player = player_docs[loser_id]
                    winner_elo = int(winner_player.get("elo", 1000))
                    loser_elo = int(loser_player.get("elo", 1000))
                    expected = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
                    elo_change = max(1, round(32 * (1 - expected)))
                    tier_changes = {}
                    loser_streak = 0

                    for user_id in (winner_id, loser_id):
                        player = player_docs[user_id]
                        tiers = {
                            mode: value for mode, value in (player.get("tiers") or {}).items()
                            if mode in GAMEMODES and value in TIERS
                        }
                        streaks = dict(player.get("duel_loss_streaks") or {})
                        streak_key = mode_key(current_duel["gamemode"])
                        old_tier = tiers.get(current_duel["gamemode"])
                        old_tier = old_tier if old_tier in DUEL_TIER_ORDER else None
                        if user_id == winner_id:
                            new_tier = move_duel_tier(old_tier, 1)
                            streaks[streak_key] = 0
                        else:
                            consecutive_losses = max(0, int(streaks.get(streak_key, 0))) + 1
                            if consecutive_losses >= 2:
                                new_tier = move_duel_tier(old_tier, -1)
                                streaks[streak_key] = 0
                            else:
                                new_tier = old_tier
                                streaks[streak_key] = consecutive_losses
                            loser_streak = int(streaks[streak_key])

                        if new_tier and new_tier != old_tier:
                            tiers[current_duel["gamemode"]] = new_tier
                            tier_changes[str(user_id)] = {"old": old_tier or "Unranked", "new": new_tier}
                            self.history.insert_one({
                                "guild_id": guild.id, "discord_id": user_id,
                                "username": player.get("username", members[user_id].display_name),
                                "gamemode": current_duel["gamemode"],
                                "old_tier": old_tier or "Unranked", "new_tier": new_tier,
                                "tester_id": actor.id, "source": "duel",
                                "reason": f"Confirmed matchmaking duel {duel_id}",
                                "created_at": now,
                            }, session=active_session)

                        is_winner = user_id == winner_id
                        new_elo = max(100, winner_elo + elo_change) if is_winner else max(100, loser_elo - elo_change)
                        self.players.update_one({
                            "guild_id": guild.id, "discord_id": user_id,
                        }, {"$set": {
                            "guild_id": guild.id, "discord_id": user_id, "discordId": user_id,
                            "username": player.get("username", members[user_id].display_name),
                            "username_lc": player.get("username_lc", members[user_id].display_name.casefold()),
                            "tiers": tiers,
                            "total_points": total_tier_points(tiers),
                            "duel_loss_streaks": streaks,
                            "elo": new_elo,
                            "duels_played": int(player.get("duels_played", 0)) + 1,
                            "wins": int(player.get("wins", 0)) + (1 if is_winner else 0),
                            "losses": int(player.get("losses", 0)) + (0 if is_winner else 1),
                            "updated_at": now,
                        }}, upsert=True, session=active_session)

                    winner_name = winner_player.get("username", members[winner_id].display_name)
                    loser_name = loser_player.get("username", members[loser_id].display_name)
                    self.activity.insert_one({
                        "guild_id": guild.id, "kind": "duel_result",
                        "winner_id": winner_id, "loser_id": loser_id,
                        "winner_ign": winner_name, "loser_ign": loser_name,
                        "gamemode": current_duel["gamemode"], "score": "confirmed",
                        "elo_change": elo_change, "tier_changes": tier_changes,
                        "duel_id": duel_id, "created_at": now,
                    }, session=active_session)
                    self.duels.update_one({
                        "guild_id": guild.id, "duel_id": duel_id, "status": "resolving",
                    }, {"$set": {
                        "status": "completed", "winner_id": winner_id, "loser_id": loser_id,
                        "completed_at": now, "elo_change": elo_change,
                        "tier_changes": tier_changes, "resolved_by": actor.id,
                    }}, session=active_session)
                    outcome.update({
                        "winner_id": winner_id, "loser_id": loser_id,
                        "winner_name": winner_name, "loser_name": loser_name,
                        "elo_change": elo_change, "loser_streak": loser_streak,
                        "tier_changes": tier_changes,
                    })
                session.with_transaction(transaction_body)

        try:
            await self.db_call(apply_transaction)
        except Exception:
            await self.db_call(self.duels.update_one, {
                "guild_id": guild.id, "duel_id": duel_id, "status": "resolving",
            }, {"$set": {"status": "disputed", "settlement_error": "MongoDB transaction failed."}})
            raise

        for user_id in (winner_id, loser_id):
            try:
                player = await self.get_player(guild.id, user_id)
                if player:
                    await self.sync_best_role(members[user_id], player)
            except discord.HTTPException:
                log.exception("Could not sync tier role after duel %s for user %s", duel_id, user_id)

        lines = [
            f"🏆 <@{winner_id}> won the **{duel['gamemode']}** duel against <@{loser_id}>.",
            f"**ELO:** +{outcome['elo_change']} / -{outcome['elo_change']}",
        ]
        for user_id, change in outcome["tier_changes"].items():
            lines.append(f"<@{user_id}>: **{change['old']} → {change['new']}**")
        if str(winner_id) not in outcome["tier_changes"]:
            lines.append("Winner is already at the top tier, or their tier was unchanged.")
        lines.append(f"Loser's consecutive loss count in {duel['gamemode']}: **{outcome['loser_streak']} / 2**.")
        final_embed = discord.Embed(title="✅ Duel result confirmed", description="\n".join(lines), colour=0x57D69A)
        config = await self.setting(guild.id)
        for key in ("results_channel_id", "activity_channel_id"):
            destination = guild.get_channel(int(config.get(key, 0)))
            if destination:
                try:
                    await destination.send(embed=final_embed)
                except discord.HTTPException:
                    log.exception("Could not announce duel result in %s", key)
        match_channel = guild.get_channel(int(duel.get("matchmaking_channel_id") or config.get("matchmaking_channel_id", 0)))
        if isinstance(match_channel, discord.TextChannel) and duel.get("result_message_id"):
            try:
                result_message = await match_channel.fetch_message(int(duel["result_message_id"]))
                await result_message.edit(embed=final_embed, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await self.send_duel_log(
            guild,
            f"Duel {duel_id} completed. Winner <@{winner_id}>, loser <@{loser_id}>, gamemode {duel['gamemode']}.",
        )
        return outcome

    @app_commands.command(name="duel-disputes", description="Staff: list matchmaking duels with conflicting results.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def duel_disputes(self, interaction: discord.Interaction):
        if not await self.require_db(interaction):
            return
        rows = await self.db_call(lambda: list(self.duels.find({
            "guild_id": interaction.guild.id, "matchmaking": True, "status": "disputed",
        }).sort("disputed_at", DESCENDING).limit(15)))
        if not rows:
            await interaction.response.send_message("There are no disputed matchmaking duels.", ephemeral=True)
            return
        lines = []
        for row in rows:
            reports = row.get("reports", {}) or {}
            first = (reports.get(str(row["challenger_id"])) or {}).get("winner_id")
            second = (reports.get(str(row["opponent_id"])) or {}).get("winner_id")
            first_text = f"<@{first}>" if first else "not submitted"
            second_text = f"<@{second}>" if second else "not submitted"
            lines.append(
                f"• `{row['duel_id']}` · **{row['gamemode']}** · "
                f"<@{row['challenger_id']}> vs <@{row['opponent_id']}>\n"
                f"  Player 1 reported: {first_text} · Player 2 reported: {second_text}"
            )
        await interaction.response.send_message(
            embed=discord.Embed(title="⚠️ Disputed matchmaking duels", description="\n".join(lines), colour=0xE67E22),
            ephemeral=True,
        )

    @app_commands.command(name="duel-review", description="Staff: resolve a disputed duel and select the verified winner.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def duel_review(self, interaction: discord.Interaction, duel_id: str,
                          winner: discord.Member, reason: str):
        if not await self.require_db(interaction):
            return
        reason = reason.strip()
        if len(reason) < 8:
            await interaction.response.send_message("Provide a review reason of at least 8 characters.", ephemeral=True)
            return
        duel = await self.db_call(self.duels.find_one, {
            "guild_id": interaction.guild.id, "duel_id": duel_id.strip(),
            "matchmaking": True, "status": "disputed",
        })
        if not duel or winner.id not in (duel["challenger_id"], duel["opponent_id"]):
            await interaction.response.send_message(
                "That disputed duel was not found, or the selected winner is not a participant.",
                ephemeral=True,
            )
            return
        claimed = await self.db_call(self.duels.update_one, {
            "guild_id": interaction.guild.id, "duel_id": duel["duel_id"], "status": "disputed",
        }, {"$set": {
            "status": "resolving", "resolving_at": datetime.now(timezone.utc),
            "winner_id": winner.id, "staff_review_reason": reason[:300],
            "staff_reviewer_id": interaction.user.id,
        }})
        if not claimed.modified_count:
            await interaction.response.send_message("Another staff member is already reviewing this duel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            outcome = await self.settle_matchmaking_duel(interaction.guild, duel["duel_id"], winner.id, interaction.user)
        except Exception:
            log.exception("Staff duel review failed for %s", duel["duel_id"])
            await self.db_call(self.duels.update_one, {
                "guild_id": interaction.guild.id, "duel_id": duel["duel_id"], "status": "resolving",
            }, {"$set": {"status": "disputed", "settlement_error": "Staff settlement failed; retry review."}})
            await interaction.followup.send("The result could not be applied. The duel remains disputed.", ephemeral=True)
            return
        await self.send_duel_log(
            interaction.guild,
            f"Staff review: duel {duel_id} resolved by {interaction.user.mention}. Winner: <@{outcome['winner_id']}>. Reason: {reason[:300]}",
        )
        await interaction.followup.send(
            f"✅ Duel {duel_id} resolved. <@{outcome['winner_id']}> is the confirmed winner.",
            ephemeral=True,
        )

    @app_commands.command(name="duel", description="Join matchmaking and find an opponent in a gamemode.")
    @app_commands.guild_only()
    async def duel(self, interaction: discord.Interaction):
        if not await self.require_db(interaction):
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚔️ Find a duel",
                description="Choose the gamemode you want to play. You will then confirm before joining the queue.",
                colour=0x55DDEB,
            ),
            view=DuelGamemodeSelectView(self),
            ephemeral=True,
        )

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
        if duel.get("matchmaking"):
            await interaction.response.send_message(
                "This is a matchmaking duel. Both players must confirm the winner with the buttons on its match message.",
                ephemeral=True,
            )
            return
        if interaction.user.id not in (duel["challenger_id"], duel["opponent_id"]):
            await interaction.response.send_message("Only duel participants can submit scores.", ephemeral=True)
            return
        needed = duel["best_of"] // 2 + 1
        if (your_score == opponent_score or max(your_score, opponent_score) != needed
                or your_score + opponent_score > duel["best_of"]):
            await interaction.response.send_message(
                f"Invalid best-of-{duel['best_of']} score. The winner must reach {needed} rounds.",
                ephemeral=True,
            )
            return
        # Write only this participant's report, then read the combined state back.
        # This avoids losing the other player's report during simultaneous submissions.
        await self.db_call(self.duels.update_one, {
            "guild_id": interaction.guild.id, "duel_id": duel_id, "status": "active"
        }, {"$set": {
            f"reports.{interaction.user.id}": {"your": your_score, "opponent": opponent_score}
        }})
        duel = await self.db_call(self.duels.find_one, {
            "guild_id": interaction.guild.id, "duel_id": duel_id
        })
        reports = dict((duel or {}).get("reports", {}))
        first = reports.get(str(duel["challenger_id"]))
        second = reports.get(str(duel["opponent_id"]))
        if not first or not second:
            await interaction.response.send_message(
                "Score saved. Waiting for the other player to submit the mirrored score.", ephemeral=True
            )
            return
        if first["your"] != second["opponent"] or first["opponent"] != second["your"]:
            await self.db_call(self.duels.update_one, {
                "guild_id": interaction.guild.id, "duel_id": duel_id, "status": "active"
            }, {"$set": {"status": "disputed"}})
            await interaction.response.send_message(
                "The reports do not match. Duel disputed; no tiers were changed.", ephemeral=True
            )
            return
        if max(first["your"], first["opponent"]) != needed or first["your"] + first["opponent"] > duel["best_of"]:
            await self.db_call(self.duels.update_one, {
                "guild_id": interaction.guild.id, "duel_id": duel_id, "status": "active"
            }, {"$set": {"status": "disputed"}})
            await interaction.response.send_message(
                "The score does not finish this best-of format. Duel disputed; no tiers changed.", ephemeral=True
            )
            return

        # Exactly one interaction can claim settlement for this duel.
        claimed = await self.db_call(self.duels.update_one, {
            "guild_id": interaction.guild.id, "duel_id": duel_id, "status": "active"
        }, {"$set": {"status": "resolving", "resolving_at": datetime.now(timezone.utc)}})
        if not claimed.modified_count:
            await interaction.response.send_message(
                "This duel is already being finalised. No duplicate rating change was applied.", ephemeral=True
            )
            return
        winner_id = duel["challenger_id"] if first["your"] > first["opponent"] else duel["opponent_id"]
        loser_id = duel["opponent_id"] if winner_id == duel["challenger_id"] else duel["challenger_id"]
        winner = interaction.guild.get_member(winner_id) or await interaction.guild.fetch_member(winner_id)
        loser = interaction.guild.get_member(loser_id) or await interaction.guild.fetch_member(loser_id)
        winner_player = await self.get_or_create_player(interaction.guild.id, winner)
        loser_player = await self.get_or_create_player(interaction.guild.id, loser)
        winner_elo = int(winner_player.get("elo", 1000))
        loser_elo = int(loser_player.get("elo", 1000))
        expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
        elo_change = max(1, round(32 * (1 - expected_winner)))
        now = datetime.now(timezone.utc)

        for member, player, direction in (
            (winner, winner_player, -1),
            (loser, loser_player, 1),
        ):
            current = (player.get("tiers") or {}).get(duel["gamemode"])
            changed_tier = move_tier(current, direction)
            if changed_tier:
                await self.save_tier(
                    interaction.guild, member, duel["gamemode"], changed_tier,
                    interaction.user, "duel", player.get("username"),
                )
            is_winner = member.id == winner_id
            rating = (winner_elo + elo_change) if is_winner else (loser_elo - elo_change)
            update = {
                "elo": max(100, rating),
                "duels_played": int(player.get("duels_played", 0)) + 1,
                "wins": int(player.get("wins", 0)) + (1 if is_winner else 0),
                "losses": int(player.get("losses", 0)) + (0 if is_winner else 1),
                "updated_at": now,
            }
            await self.db_call(self.players.update_one, {
                "guild_id": interaction.guild.id, "discord_id": member.id
            }, {"$set": update})
        await self.db_call(self.activity.insert_one, {
            "guild_id": interaction.guild.id, "kind": "duel_result",
            "winner_id": winner_id, "loser_id": loser_id,
            "winner_ign": winner_player.get("username"), "loser_ign": loser_player.get("username"),
            "gamemode": duel["gamemode"], "score": f"{first['your']}-{first['opponent']}",
            "elo_change": elo_change, "created_at": now,
        })
        await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {
            "status": "completed", "winner_id": winner_id, "completed_at": datetime.now(timezone.utc)}})
        await interaction.response.send_message(
            f"{winner.mention} won **{first['your']}-{first['opponent']}** in {duel['gamemode']}! "
            f"They gained **{elo_change} ELO** and {loser.mention} lost **{elo_change} ELO**. "
            "Their gamemode tiers and shared tier roles were recalculated."
        )

    @app_commands.command(name="duel-cancel", description="Cancel one of your pending or active duels.")
    @app_commands.guild_only()
    async def duel_cancel(self, interaction, duel_id: str):
        if not await self.require_db(interaction):
            return
        duel = await self.db_call(self.duels.find_one, {"guild_id": interaction.guild.id, "duel_id": duel_id.strip()})
        if duel and duel.get("matchmaking") and duel.get("status") in ("active", "disputed", "resolving"):
            await interaction.response.send_message(
                "A matched matchmaking duel cannot be cancelled by a participant. "
                "Both players must report the result, or staff can review it with /duel-review.",
                ephemeral=True,
            )
            return
        if not duel or interaction.user.id not in (duel["challenger_id"], duel["opponent_id"]) or duel.get("status") not in ("pending", "active"):
            await interaction.response.send_message("You cannot cancel that duel.", ephemeral=True)
            return
        await self.db_call(self.duels.update_one, {"duel_id": duel_id}, {"$set": {"status": "cancelled"}})
        await interaction.response.send_message(f"Duel {duel_id} cancelled.")

    @app_commands.command(name="close", description="Close the private test session you are currently in.")
    @app_commands.guild_only()
    async def close_command(self, interaction):
        await self.ticket_action(interaction, "close")

    @app_commands.command(name="skillissue", description="A harmless PvPClubTiers PvP roast.")
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
            category = await self.find_category(ctx.guild, "PVPCLUBTIERS")
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

    @app_commands.command(name="trivia-leaderboard", description="Show the PvPClubTiers trivia leaderboard.")
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
            # Keep incorrect-answer feedback useful without letting a member flood the channel.
            key = (message.guild.id, message.author.id)
            now = time.monotonic()
            if now - self.last_wrong_feedback.get(key, 0.0) >= 4:
                self.last_wrong_feedback[key] = now
                await message.reply("❌ Not quite — try again!")

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
                e = discord.Embed(title="🧠 PvPClubTiers Trivia", description=prompt, colour=0x55DDEB)
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
        # Keep the original Tiers-mod front-end API paths and response shapes.
        app.router.add_get("/api/config", self.web_config)
        app.router.add_get("/api/stats", self.web_stats)
        app.router.add_get("/api/players", self.web_players)
        app.router.add_get("/api/players/search", self.web_search_players)
        app.router.add_get("/api/players/{ign}", self.web_player)
        app.router.add_get("/api/leaderboard/{mode}", self.web_mode_leaderboard)
        app.router.add_get("/api/testers", self.web_testers)
        app.router.add_get("/api/activity", self.web_activity)
        app.router.add_get("/api/compare", self.web_compare)

    async def website_guild_id(self):
        configured = os.getenv("DISCORD_GUILD_ID")
        if configured and configured.isdigit():
            return int(configured)
        if self.db is None:
            return None
        row = await self.db_call(self.settings.find_one, {"website_public": True})
        return int(row["guild_id"]) if row else None

    async def web_config(self, request):
        return web.json_response({"modes": GAMEMODES, "tiers": TIERS, "server": "mcpvp.club", "region": "EU"})

    async def web_stats(self, request):
        if self.db is None:
            return web.json_response({"players": 0, "tests": 0})
        gid = await self.website_guild_id()
        if not gid:
            return web.json_response({"players": 0, "tests": 0})
        players = await self.db_call(self.players.count_documents, {
            "guild_id": gid, "banned": {"$ne": True}
        })
        tests = await self.db_call(self.history.count_documents, {
            "guild_id": gid, "source": "test"
        })
        return web.json_response({"players": players, "tests": tests})

    async def web_players(self, request):
        if self.db is None:
            return web.json_response([])
        gid = await self.website_guild_id()
        if not gid:
            return web.json_response([])
        docs = await self.db_call(lambda: list(self.players.find({
            "guild_id": gid, "banned": {"$ne": True}
        }).limit(500)))
        rows = [safe_player(p) for p in docs]
        rows.sort(key=lambda p: (-p["totalPoints"], p["ign"].casefold()))
        return web.json_response(rows)

    async def web_search_players(self, request):
        if self.db is None:
            return web.json_response([])
        gid = await self.website_guild_id()
        term = str(request.query.get("q", "")).strip()[:16]
        if not gid or not term:
            return web.json_response([])
        docs = await self.db_call(lambda: list(self.players.find({
            "guild_id": gid, "banned": {"$ne": True},
            "username_lc": {"$regex": re.escape(term.casefold())},
        }).limit(10)))
        rows = [safe_player(p) for p in docs]
        rows.sort(key=lambda p: (-p["totalPoints"], p["ign"].casefold()))
        return web.json_response(rows)

    async def web_player(self, request):
        if self.db is None:
            return web.json_response({"error": "database_unavailable"}, status=503)
        gid = await self.website_guild_id()
        ign = str(request.match_info["ign"])[:16]
        if not gid:
            return web.json_response({"error": "not_configured"}, status=404)
        player = await self.db_call(self.players.find_one, {
            "guild_id": gid, "username_lc": ign.casefold(), "banned": {"$ne": True}
        })
        if not player:
            return web.json_response({"error": "Player not found"}, status=404)
        ranking_docs = await self.db_call(lambda: list(self.players.find({
            "guild_id": gid, "banned": {"$ne": True}
        })))
        ranking_docs.sort(key=lambda p: (
            -total_tier_points(p.get("tiers") or {}),
            TIERS.index(best_tier(p.get("tiers") or {})[0]) if best_tier(p.get("tiers") or {})[0] in TIERS else len(TIERS),
            -int(p.get("elo", 1000)),
            str(p.get("username", "")).casefold(),
        ))
        rank = next(
            (index for index, entry in enumerate(ranking_docs, 1)
             if int(entry.get("discord_id", 0)) == int(player["discord_id"])),
            None,
        )
        history_rows = await self.db_call(lambda: list(self.history.find({
            "guild_id": gid, "discord_id": player["discord_id"]
        }).sort("created_at", DESCENDING).limit(20)))
        result = safe_player(player)
        result["rank"] = rank
        result["history"] = [{
            "type": "tier-change", "mode": h.get("gamemode"),
            "oldTier": h.get("old_tier"), "newTier": h.get("new_tier"),
            "createdAt": h.get("created_at").isoformat() if h.get("created_at") else None,
        } for h in history_rows]
        player_id = int(player["discord_id"])
        duel_rows = await self.db_call(lambda: list(self.duels.find({
            "guild_id": gid, "status": "completed",
            "$or": [{"challenger_id": player_id}, {"opponent_id": player_id}],
        }).sort("completed_at", DESCENDING).limit(10)))
        opponent_ids = {
            int(row["opponent_id"] if int(row["challenger_id"]) == player_id else row["challenger_id"])
            for row in duel_rows
        }
        opponents = await self.db_call(lambda: list(self.players.find({
            "guild_id": gid, "discord_id": {"$in": list(opponent_ids)}
        }, {"discord_id": 1, "username": 1}))) if opponent_ids else []
        opponent_names = {int(row["discord_id"]): row.get("username", "Player") for row in opponents}
        result["duelLossStreaks"] = player.get("duel_loss_streaks", {}) or {}
        result["duelHistory"] = [{
            "duelId": row.get("duel_id"),
            "gamemode": row.get("gamemode"),
            "opponent": opponent_names.get(
                int(row["opponent_id"] if int(row["challenger_id"]) == player_id else row["challenger_id"]),
                "Player",
            ),
            "result": "W" if int(row.get("winner_id", 0)) == player_id else "L",
            "tierChange": (row.get("tier_changes") or {}).get(str(player_id)),
            "createdAt": row.get("completed_at").isoformat() if row.get("completed_at") else None,
        } for row in duel_rows]
        return web.json_response(result)

    async def web_mode_leaderboard(self, request):
        if self.db is None:
            return web.json_response([])
        gid = await self.website_guild_id()
        mode = request.match_info["mode"]
        if mode != "Overall" and mode not in GAMEMODES:
            return web.json_response({"error": "Unknown gamemode"}, status=404)
        if not gid:
            return web.json_response([])
        rows = await self.db_call(lambda: list(self.players.find({
            "guild_id": gid, "banned": {"$ne": True}
        })))
        if mode == "Overall":
            rows = [p for p in rows if best_tier(p.get("tiers", {}))[0]]
            rows.sort(key=lambda p: (-total_tier_points(p.get("tiers") or {}),
                                     str(p.get("username", "")).casefold()))
        else:
            rows = [p for p in rows if (p.get("tiers") or {}).get(mode) in TIERS]
            rows.sort(key=lambda p: (TIERS.index((p.get("tiers") or {})[mode]),
                                     -total_tier_points(p.get("tiers") or {})))
        return web.json_response([safe_player(p) for p in rows[:100]])

    async def web_testers(self, request):
        if self.db is None:
            return web.json_response([])
        gid = await self.website_guild_id()
        if not gid:
            return web.json_response([])
        docs = await self.db_call(lambda: list(self.tester_stats.find({
            "guild_id": gid
        }).sort("tests_completed", DESCENDING).limit(50)))
        guild = self.bot.get_guild(int(gid))
        response = []
        for p in docs:
            member = guild.get_member(int(p["tester_id"])) if guild else None
            response.append({
                "testerId": str(p["tester_id"]),
                "name": member.display_name if member else p.get("tester_name", "Tester"),
                "testsCompleted": p.get("tests_completed", 0),
                "byMode": p.get("by_mode", {}),
                "lastTestAt": p.get("updated_at").isoformat() if p.get("updated_at") else None,
            })
        return web.json_response(response)

    async def web_activity(self, request):
        if self.db is None:
            return web.json_response([])
        gid = await self.website_guild_id()
        if not gid:
            return web.json_response([])
        docs = await self.db_call(lambda: list(self.activity.find({
            "guild_id": gid, "kind": {"$in": ["tier_change", "duel_result"]}
        }).sort("created_at", DESCENDING).limit(30)))
        return web.json_response([{
            **({
                "type": "duel-result", "playerIgn": p.get("winner_ign"),
                "mode": p.get("gamemode"), "oldTier": "Duel",
                "newTier": f"Won {p.get('score', '')}",
                "details": f"+{p.get('elo_change', 0)} ELO",
            } if p.get("kind") == "duel_result" else {
                "type": "tier-change", "playerIgn": p.get("username"),
                "playerId": str(p.get("discord_id", "")), "mode": p.get("gamemode"),
                "oldTier": p.get("old_tier"), "newTier": p.get("new_tier"),
                "details": p.get("source"),
            }),
            "createdAt": p.get("created_at").isoformat() if p.get("created_at") else None,
        } for p in docs])

    async def web_compare(self, request):
        if self.db is None:
            return web.json_response({"error": "database_unavailable"}, status=503)
        gid = await self.website_guild_id()
        if not gid:
            return web.json_response({"error": "not_configured"}, status=404)
        a = str(request.query.get("a", ""))[:16].casefold()
        b = str(request.query.get("b", ""))[:16].casefold()
        first, second = await asyncio.gather(
            self.db_call(self.players.find_one, {"guild_id": gid, "username_lc": a, "banned": {"$ne": True}}),
            self.db_call(self.players.find_one, {"guild_id": gid, "username_lc": b, "banned": {"$ne": True}}),
        )
        if not first or not second:
            return web.json_response({"error": "Both players must exist"}, status=404)
        return web.json_response({"a": safe_player(first), "b": safe_player(second), "modes": GAMEMODES})



async def setup(bot):
    await bot.add_cog(PvPClubTiersCog(bot))

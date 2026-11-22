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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
DATA_FILE = Path(os.getenv("DATA_FILE", "bot_data.json"))
DEFAULT_MC_SERVER = "mcpvp.club"
USER_AGENT = "PVPClubEmpireBot/1.0 (Discord bot; contact server staff)"
PINK = 0xFF9CEE
CYAN = 0x55DDFB
GREEN = 0x57D69A
RED = 0xFF647C
MCTIERS_API_BASE = "https://mctiers.net/api/v2"
MCTIERS_PROFILE_CACHE_SECONDS = 60
MCTIERS_MODES_CACHE_SECONDS = 3600
TIER_WATCH_POLL_SECONDS = 180
EVENT_PING_ROLE_NAME = "Event Ping"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("pvpclub-empire")


def default_data() -> dict[str, Any]:
    return {
        "meows": {}, "meow_streaks": {}, "levels": {}, "birthdays": {},
        "ticket_counter": 0, "pets": {}, "giveaways": {}, "polls": {},
        "warnings": {}, "modlog_channels": {}, "logs_channels": {},
        "event_settings": {}, "events": {}, "tier_watch_channels": {},
        "tier_watchlists": {}, "automod_settings": {}, "automod_config": {},
        "automod_strikes": {}, "meow_role_config": {}, "suggestion_channels": {},
        "welcome_channels": {}, "autoroles": {}, "birthday_channels": {},
        "level_config": {}, "storage_meta": {"updated_at": 0.0},
    }


def xp_required(level: int) -> int:
    """XP required to advance from the supplied level to the next one."""
    level = max(0, int(level))
    return 5 * (level ** 2) + (50 * level) + 100


def level_from_total_xp(total_xp: int) -> tuple[int, int, int]:
    """Return (level, XP already earned in this level, XP needed to advance)."""
    remaining = max(0, int(total_xp))
    level = 0
    while remaining >= xp_required(level):
        remaining -= xp_required(level)
        level += 1
        if level > 10000:  # Defensive guard for corrupted/imported values.
            break
    return level, remaining, xp_required(level)


class DataStore:
    """Compact PostgreSQL persistence with hot counters split into dedicated tables.

    PostgreSQL stores configuration/state in one small JSONB row. High-write or
    per-member data (meows, streaks, birthdays, XP) lives in indexed rows so a
    single meow never rewrites the complete bot state. A local JSON snapshot is
    retained for development and first-time migration; it is not durable on
    Render Free.
    """

    TABLE = "pvpclub_bot_state"
    MEOW_TABLE = "pvpclub_meow_counts"
    STREAK_TABLE = "pvpclub_meow_streaks"
    LEVEL_TABLE = "pvpclub_levels"
    BIRTHDAY_TABLE = "pvpclub_birthdays"
    NORMALIZED_KEYS = {"meows", "meow_streaks", "levels", "birthdays"}

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = default_data()
        self.removed_legacy_data = False
        self.pool = None
        self.backend = "local-json"
        self.database_healthy = False
        self.last_database_error: str | None = None
        self.last_database_size_bytes: int | None = None
        self.load()

    @staticmethod
    def _normalise_data(value: Any) -> dict[str, Any]:
        data = default_data()
        if isinstance(value, dict):
            data.update(value)
        for key, default_value in default_data().items():
            if key not in data or not isinstance(data[key], type(default_value)):
                data[key] = default_value
        if not isinstance(data.get("storage_meta"), dict):
            data["storage_meta"] = {"updated_at": 0.0}
        try:
            data["storage_meta"]["updated_at"] = float(data["storage_meta"].get("updated_at", 0.0))
        except (TypeError, ValueError):
            data["storage_meta"]["updated_at"] = 0.0
        return data

    def _remove_legacy_data(self):
        for key in ("empire_tiers", "tier_tags", "linked_accounts", "duels"):
            if key in self.data:
                self.data.pop(key, None)
                self.removed_legacy_data = True

    def load(self):
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = self._normalise_data(loaded)
            except (OSError, json.JSONDecodeError):
                log.exception("Could not read %s; starting with defaults.", self.path)
        self._remove_legacy_data()

    @staticmethod
    def _clean_asyncpg_dsn(dsn: str) -> str:
        parts = urlsplit(dsn)
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in {"sslmode", "channel_binding"}
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _db_payload(self) -> dict[str, Any]:
        """Exclude normalized high-volume tables from the JSONB state row."""
        return {key: value for key, value in self.data.items() if key not in self.NORMALIZED_KEYS}

    async def _migrate_local_normalized_data(self):
        """One-way, idempotent migration from old JSON-backed counters/profiles."""
        assert self.pool is not None
        meows = self.data.get("meows", {})
        if isinstance(meows, dict):
            for user_id, count in meows.items():
                try:
                    await self.pool.execute(
                        f"""INSERT INTO {self.MEOW_TABLE} (user_id, count)
                            VALUES ($1, $2)
                            ON CONFLICT (user_id) DO UPDATE
                            SET count = GREATEST({self.MEOW_TABLE}.count, EXCLUDED.count)""",
                        int(user_id), max(0, int(count)),
                    )
                except (TypeError, ValueError):
                    continue

        streaks = self.data.get("meow_streaks", {})
        if isinstance(streaks, dict):
            for key, value in streaks.items():
                try:
                    guild_id_text, user_id_text = key.split(":", 1)
                    if not isinstance(value, dict):
                        continue
                    last_day = datetime.fromisoformat(str(value.get("last_day"))).date()
                    await self.pool.execute(
                        f"""INSERT INTO {self.STREAK_TABLE}
                            (guild_id, user_id, current_streak, best_streak, last_day)
                            VALUES ($1,$2,$3,$4,$5)
                            ON CONFLICT (guild_id,user_id) DO UPDATE SET
                              current_streak = GREATEST({self.STREAK_TABLE}.current_streak, EXCLUDED.current_streak),
                              best_streak = GREATEST({self.STREAK_TABLE}.best_streak, EXCLUDED.best_streak),
                              last_day = GREATEST({self.STREAK_TABLE}.last_day, EXCLUDED.last_day)""",
                        int(guild_id_text), int(user_id_text),
                        max(0, int(value.get("current", 0))), max(0, int(value.get("best", 0))), last_day,
                    )
                except (TypeError, ValueError):
                    continue

        levels = self.data.get("levels", {})
        if isinstance(levels, dict):
            for key, value in levels.items():
                try:
                    guild_id_text, user_id_text = key.split(":", 1)
                    if not isinstance(value, dict):
                        continue
                    total_xp = max(0, int(value.get("total_xp", 0)))
                    level = max(0, int(value.get("level", 0)))
                    await self.pool.execute(
                        f"""INSERT INTO {self.LEVEL_TABLE}
                            (guild_id,user_id,total_xp,level,voice_xp,voice_seconds)
                            VALUES ($1,$2,$3,$4,$5,$6)
                            ON CONFLICT (guild_id,user_id) DO UPDATE SET
                              total_xp = GREATEST({self.LEVEL_TABLE}.total_xp, EXCLUDED.total_xp),
                              level = GREATEST({self.LEVEL_TABLE}.level, EXCLUDED.level),
                              voice_xp = GREATEST({self.LEVEL_TABLE}.voice_xp, EXCLUDED.voice_xp),
                              voice_seconds = GREATEST({self.LEVEL_TABLE}.voice_seconds, EXCLUDED.voice_seconds)""",
                        int(guild_id_text), int(user_id_text), total_xp, level,
                        max(0, int(value.get("voice_xp", 0))), max(0, int(value.get("voice_seconds", 0))),
                    )
                except (TypeError, ValueError):
                    continue

        birthdays = self.data.get("birthdays", {})
        if isinstance(birthdays, dict):
            for key, value in birthdays.items():
                try:
                    guild_id_text, user_id_text = key.split(":", 1)
                    if not isinstance(value, dict):
                        continue
                    await self.pool.execute(
                        f"""INSERT INTO {self.BIRTHDAY_TABLE}
                            (guild_id,user_id,month,day,last_announced_year)
                            VALUES ($1,$2,$3,$4,$5)
                            ON CONFLICT (guild_id,user_id) DO UPDATE SET
                              month=EXCLUDED.month, day=EXCLUDED.day,
                              last_announced_year=GREATEST({self.BIRTHDAY_TABLE}.last_announced_year, EXCLUDED.last_announced_year)""",
                        int(guild_id_text), int(user_id_text), int(value["month"]), int(value["day"]),
                        int(value.get("last_announced_year", 0)),
                    )
                except (TypeError, ValueError, KeyError):
                    continue

    async def _hydrate_normalized_data(self):
        assert self.pool is not None
        # Keep normalized tables out of the in-memory config snapshot. All access
        # uses indexed SQL queries; this prevents RAM usage growing with every user.
        self.data["meows"] = {}
        self.data["meow_streaks"] = {}
        self.data["birthdays"] = {}
        self.data["levels"] = {}

    async def connect(self):
        if not DATABASE_URL:
            if os.getenv("RENDER") and os.getenv("ALLOW_EPHEMERAL_STORAGE", "0") != "1":
                raise RuntimeError(
                    "DATABASE_URL is required on Render. Configure Neon/PostgreSQL; "
                    "set ALLOW_EPHEMERAL_STORAGE=1 only for an intentional temporary test deployment."
                )
            self.backend = "local-json"
            self.database_healthy = False
            log.warning("DATABASE_URL is not set; local JSON persistence is not durable on Render Free.")
            return

        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("DATABASE_URL is set, but asyncpg is missing. Install requirements.txt.") from exc

        dsn = self._clean_asyncpg_dsn(DATABASE_URL.replace("postgres://", "postgresql://", 1))
        try:
            self.pool = await asyncpg.create_pool(
                dsn=dsn, ssl="require", min_size=1, max_size=4, command_timeout=15,
                max_inactive_connection_lifetime=120,
                # Neon pooled URLs may use PgBouncer transaction pooling; disabling
                # asyncpg's per-connection statement cache keeps that compatible.
                statement_cache_size=0,
            )
            await self.pool.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )"""
            )
            await self.pool.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.MEOW_TABLE} (
                    user_id BIGINT PRIMARY KEY,
                    count BIGINT NOT NULL DEFAULT 0 CHECK (count >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )"""
            )
            await self.pool.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.STREAK_TABLE} (
                    guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                    current_streak INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    last_day DATE NOT NULL,
                    PRIMARY KEY (guild_id,user_id)
                )"""
            )
            await self.pool.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.LEVEL_TABLE} (
                    guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                    total_xp BIGINT NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 0,
                    voice_xp BIGINT NOT NULL DEFAULT 0,
                    voice_seconds BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id,user_id)
                )"""
            )
            await self.pool.execute(
                f"""CREATE INDEX IF NOT EXISTS pvpclub_levels_leaderboard_idx
                    ON {self.LEVEL_TABLE} (guild_id,total_xp DESC)"""
            )
            await self.pool.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.BIRTHDAY_TABLE} (
                    guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                    month SMALLINT NOT NULL CHECK (month BETWEEN 1 AND 12),
                    day SMALLINT NOT NULL CHECK (day BETWEEN 1 AND 31),
                    last_announced_year INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id,user_id)
                )"""
            )

            # Import counters from the local snapshot before loading the compact DB snapshot.
            # Upserts use GREATEST so an existing durable count can never be reduced.
            await self._migrate_local_normalized_data()
            row = await self.pool.fetchrow(f"SELECT payload,updated_at FROM {self.TABLE} WHERE id=1")
            if row is None:
                self.data = self._normalise_data(self.data)
                self._remove_legacy_data()
                # Import normalized legacy JSON data before dropping it from config storage.
                await self._migrate_local_normalized_data()
            else:
                remote_payload = row["payload"]
                if isinstance(remote_payload, str):
                    remote_payload = json.loads(remote_payload)
                remote_data = self._normalise_data(remote_payload)
                remote_updated = row["updated_at"].timestamp()
                local_updated = float(self.data.get("storage_meta", {}).get("updated_at", 0.0))
                if local_updated > remote_updated:
                    log.warning("Local config backup is newer than PostgreSQL; applying local config snapshot.")
                    await self._migrate_local_normalized_data()
                else:
                    # Preserve normalized legacy values if they are still present in an older state row.
                    self.data = remote_data
                    await self._migrate_local_normalized_data()
            self.backend = "postgresql"
            await self._hydrate_normalized_data()
            await self.save()
            self.database_healthy = True
            self.last_database_error = None
            log.info("PostgreSQL is ready; counters, streaks, birthdays and levels use normalized tables.")
        except Exception:
            if self.pool is not None:
                await self.pool.close()
                self.pool = None
            log.exception("Could not initialise PostgreSQL; refusing to start with an invalid persistent store.")
            raise

    @staticmethod
    def _write_snapshot(path: Path, snapshot: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="bot_data_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(snapshot)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _prune_ephemeral_state(self):
        now = time.time()
        giveaways = self.data.get("giveaways", {})
        if isinstance(giveaways, dict):
            retained = {}
            for key, value in giveaways.items():
                try:
                    old_ended = isinstance(value, dict) and value.get("ended") and now - float(value.get("ends_at", now)) > 90 * 86400
                except (TypeError, ValueError):
                    old_ended = False
                if not old_ended:
                    retained[key] = value
            self.data["giveaways"] = retained
        events = self.data.get("events", {})
        if isinstance(events, dict):
            retained = {}
            for key, value in events.items():
                try:
                    old_closed = isinstance(value, dict) and value.get("closed") and now - float(value.get("created_at", now)) > 180 * 86400
                except (TypeError, ValueError):
                    old_closed = False
                if not old_closed:
                    retained[key] = value
            self.data["events"] = retained
        strikes = self.data.get("automod_strikes", {})
        if isinstance(strikes, dict):
            cutoff = now - 24 * 3600
            for key in list(strikes):
                values = [float(ts) for ts in strikes.get(key, []) if isinstance(ts, (int, float)) and float(ts) >= cutoff]
                if values:
                    strikes[key] = values
                else:
                    strikes.pop(key, None)

    async def save(self):
        async with self.lock:
            self._prune_ephemeral_state()
            self.data.setdefault("storage_meta", {})["updated_at"] = time.time()
            # With PostgreSQL enabled, the local file is a compact config backup only.
            # High-write counters and XP are already stored in dedicated SQL tables.
            snapshot_data = self._db_payload() if self.pool is not None else self.data
            local_snapshot = json.dumps(snapshot_data, separators=(",", ":"), ensure_ascii=False)
            try:
                await asyncio.to_thread(self._write_snapshot, self.path, local_snapshot)
            except OSError:
                log.exception("Could not write local JSON backup to %s.", self.path)

            if self.pool is not None:
                try:
                    compact_payload = json.dumps(self._db_payload(), ensure_ascii=False, separators=(",", ":"))
                    await self.pool.execute(
                        f"""INSERT INTO {self.TABLE} (id,payload,updated_at)
                            VALUES (1,$1::jsonb,NOW())
                            ON CONFLICT (id) DO UPDATE SET
                              payload=EXCLUDED.payload,updated_at=NOW()""",
                        compact_payload,
                    )
                    self.database_healthy = True
                    self.last_database_error = None
                except Exception as exc:
                    self.database_healthy = False
                    self.last_database_error = str(exc)[:300]
                    log.exception("PostgreSQL config save failed; local snapshot was attempted.")
                    raise

    def meow_count(self, user_id: int) -> int:
        return int(self.data.get("meows", {}).get(str(user_id), 0))

    async def get_meow_count(self, user_id: int) -> int:
        if self.pool is not None:
            value = await self.pool.fetchval(
                f"SELECT count FROM {self.MEOW_TABLE} WHERE user_id=$1", int(user_id)
            )
            return int(value or 0)
        return self.meow_count(user_id)

    async def increment_meows(self, user_id: int, amount: int = 1) -> int:
        amount = max(0, int(amount))
        if amount <= 0:
            return self.meow_count(user_id)
        if self.pool is not None:
            value = await self.pool.fetchval(
                f"""INSERT INTO {self.MEOW_TABLE} (user_id,count,updated_at)
                    VALUES ($1,$2,NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET count={self.MEOW_TABLE}.count + EXCLUDED.count,updated_at=NOW()
                    RETURNING count""",
                int(user_id), amount,
            )
            # Do not cache every lifetime counter in RAM. PostgreSQL is the source of truth.
            return int(value)
        self.data.setdefault("meows", {})[str(user_id)] = self.meow_count(user_id) + amount
        await self.save()
        return self.meow_count(user_id)

    async def meow_leaderboard(self, limit: int = 10) -> list[tuple[int, int]]:
        limit = max(1, min(100, int(limit)))
        if self.pool is not None:
            rows = await self.pool.fetch(
                f"SELECT user_id,count FROM {self.MEOW_TABLE} ORDER BY count DESC,user_id ASC LIMIT $1", limit
            )
            return [(int(row["user_id"]), int(row["count"])) for row in rows]
        return sorted(((int(uid), int(count)) for uid, count in self.data.get("meows", {}).items()), key=lambda item: (-item[1], item[0]))[:limit]

    async def meow_rank(self, user_id: int) -> int:
        count = await self.get_meow_count(user_id)
        if self.pool is not None:
            greater = await self.pool.fetchval(f"SELECT COUNT(*) FROM {self.MEOW_TABLE} WHERE count > $1", count)
            return int(greater or 0) + 1
        return 1 + sum(int(v) > count for v in self.data.get("meows", {}).values())

    async def record_meow_day(self, guild_id: int, user_id: int, day=None) -> dict[str, int]:
        day = day or datetime.now(timezone.utc).date()
        if self.pool is not None:
            row = await self.pool.fetchrow(
                f"""INSERT INTO {self.STREAK_TABLE}
                    (guild_id,user_id,current_streak,best_streak,last_day)
                    VALUES ($1,$2,1,1,$3)
                    ON CONFLICT (guild_id,user_id) DO UPDATE SET
                      current_streak = CASE
                        WHEN {self.STREAK_TABLE}.last_day = EXCLUDED.last_day THEN {self.STREAK_TABLE}.current_streak
                        WHEN {self.STREAK_TABLE}.last_day = EXCLUDED.last_day - 1 THEN {self.STREAK_TABLE}.current_streak + 1
                        ELSE 1 END,
                      best_streak = GREATEST({self.STREAK_TABLE}.best_streak,
                        CASE WHEN {self.STREAK_TABLE}.last_day = EXCLUDED.last_day - 1
                             THEN {self.STREAK_TABLE}.current_streak + 1
                             ELSE CASE WHEN {self.STREAK_TABLE}.last_day = EXCLUDED.last_day
                                  THEN {self.STREAK_TABLE}.current_streak ELSE 1 END END),
                      last_day = GREATEST({self.STREAK_TABLE}.last_day,EXCLUDED.last_day)
                    RETURNING current_streak,best_streak""",
                int(guild_id), int(user_id), day,
            )
            streak = {"current": int(row["current_streak"]), "best": int(row["best_streak"])}
            # Keep streaks in their indexed table; do not grow an in-memory user map.
            return streak
        key = f"{guild_id}:{user_id}"
        current = self.data.setdefault("meow_streaks", {}).get(key, {})
        last_day = current.get("last_day")
        if last_day == day.isoformat():
            return {"current": int(current.get("current", 0)), "best": int(current.get("best", 0))}
        previous_day = (day - timedelta(days=1)).isoformat()
        count = int(current.get("current", 0)) + 1 if last_day == previous_day else 1
        best = max(int(current.get("best", 0)), count)
        self.data["meow_streaks"][key] = {"current": count, "best": best, "last_day": day.isoformat()}
        await self.save()
        return {"current": count, "best": best}

    async def get_meow_streak(self, guild_id: int, user_id: int) -> dict[str, int]:
        key = f"{guild_id}:{user_id}"
        if self.pool is not None:
            row = await self.pool.fetchrow(
                f"SELECT current_streak,best_streak,last_day FROM {self.STREAK_TABLE} WHERE guild_id=$1 AND user_id=$2",
                int(guild_id), int(user_id),
            )
            if row is None:
                return {"current": 0, "best": 0}
            current = int(row["current_streak"])
            # A yesterday streak remains active through the current UTC day.
            if row["last_day"] < datetime.now(timezone.utc).date() - timedelta(days=1):
                current = 0
            return {"current": current, "best": int(row["best_streak"])}
        value = self.data.get("meow_streaks", {}).get(key, {})
        current = int(value.get("current", 0))
        today = datetime.now(timezone.utc).date()
        last_day = value.get("last_day")
        if last_day and last_day < (today - timedelta(days=1)).isoformat():
            current = 0
        return {"current": current, "best": int(value.get("best", 0))}

    async def add_xp(self, guild_id: int, user_id: int, amount: int, *, voice: bool = False, voice_seconds: int = 0) -> dict[str, int]:
        amount = max(0, int(amount))
        if self.pool is not None:
            row = await self.pool.fetchrow(
                f"""INSERT INTO {self.LEVEL_TABLE}
                    (guild_id,user_id,total_xp,level,voice_xp,voice_seconds,updated_at)
                    VALUES ($1,$2,$3,0,$4,$5,NOW())
                    ON CONFLICT (guild_id,user_id) DO UPDATE SET
                      total_xp={self.LEVEL_TABLE}.total_xp + EXCLUDED.total_xp,
                      voice_xp={self.LEVEL_TABLE}.voice_xp + EXCLUDED.voice_xp,
                      voice_seconds={self.LEVEL_TABLE}.voice_seconds + EXCLUDED.voice_seconds,
                      updated_at=NOW()
                    RETURNING total_xp,level,voice_xp,voice_seconds""",
                int(guild_id), int(user_id), amount, amount if voice else 0, max(0, int(voice_seconds)),
            )
            total_xp = int(row["total_xp"])
            level = level_from_total_xp(total_xp)[0]
            await self.pool.execute(
                f"UPDATE {self.LEVEL_TABLE} SET level=GREATEST(level,$3),updated_at=NOW() WHERE guild_id=$1 AND user_id=$2",
                int(guild_id), int(user_id), level,
            )
            return {"total_xp": total_xp, "level": level, "voice_xp": int(row["voice_xp"]),
                    "voice_seconds": int(row["voice_seconds"])}
        key = f"{guild_id}:{user_id}"
        value = self.data.setdefault("levels", {}).setdefault(key, {
            "total_xp": 0, "level": 0, "voice_xp": 0, "voice_seconds": 0
        })
        value["total_xp"] = int(value.get("total_xp", 0)) + amount
        if voice:
            value["voice_xp"] = int(value.get("voice_xp", 0)) + amount
            value["voice_seconds"] = int(value.get("voice_seconds", 0)) + max(0, int(voice_seconds))
        value["level"] = level_from_total_xp(value["total_xp"])[0]
        await self.save()
        return {k: int(value.get(k, 0)) for k in ("total_xp", "level", "voice_xp", "voice_seconds")}

    async def get_level(self, guild_id: int, user_id: int) -> dict[str, int]:
        if self.pool is not None:
            row = await self.pool.fetchrow(
                f"SELECT total_xp,level,voice_xp,voice_seconds FROM {self.LEVEL_TABLE} WHERE guild_id=$1 AND user_id=$2",
                int(guild_id), int(user_id),
            )
            if row is None:
                return {"total_xp": 0, "level": 0, "voice_xp": 0, "voice_seconds": 0}
            return {key: int(row[key]) for key in ("total_xp", "level", "voice_xp", "voice_seconds")}
        value = self.data.get("levels", {}).get(f"{guild_id}:{user_id}", {})
        return {key: int(value.get(key, 0)) for key in ("total_xp", "level", "voice_xp", "voice_seconds")}

    async def get_level_leaderboard(self, guild_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, int]]:
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        if self.pool is not None:
            rows = await self.pool.fetch(
                f"SELECT user_id,total_xp,level,voice_xp,voice_seconds FROM {self.LEVEL_TABLE} WHERE guild_id=$1 ORDER BY total_xp DESC,user_id ASC LIMIT $2 OFFSET $3",
                int(guild_id), limit, offset,
            )
            return [{key: int(row[key]) for key in ("user_id", "total_xp", "level", "voice_xp", "voice_seconds")} for row in rows]
        values = []
        for key, data in self.data.get("levels", {}).items():
            try:
                guild_text, user_text = key.split(":", 1)
                if int(guild_text) != guild_id:
                    continue
                values.append({"user_id": int(user_text), **{field: int(data.get(field, 0)) for field in ("total_xp", "level", "voice_xp", "voice_seconds")}})
            except (ValueError, TypeError):
                continue
        return sorted(values, key=lambda row: (-row["total_xp"], row["user_id"]))[offset:offset + limit]

    async def set_level_total(self, guild_id: int, user_id: int, total_xp: int) -> dict[str, int]:
        total_xp = max(0, int(total_xp))
        level = level_from_total_xp(total_xp)[0]
        if self.pool is not None:
            await self.pool.execute(
                f"""INSERT INTO {self.LEVEL_TABLE} (guild_id,user_id,total_xp,level)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT (guild_id,user_id) DO UPDATE SET total_xp=EXCLUDED.total_xp,
                    level=EXCLUDED.level,updated_at=NOW()""",
                int(guild_id), int(user_id), total_xp, level,
            )
            return await self.get_level(guild_id, user_id)
        value = self.data.setdefault("levels", {}).setdefault(f"{guild_id}:{user_id}", {
            "total_xp": 0, "level": 0, "voice_xp": 0, "voice_seconds": 0
        })
        value["total_xp"] = total_xp
        value["level"] = level
        await self.save()
        return await self.get_level(guild_id, user_id)

    async def get_level_rank(self, guild_id: int, user_id: int) -> int | None:
        total = (await self.get_level(guild_id, user_id))["total_xp"]
        if total <= 0:
            return None
        if self.pool is not None:
            greater = await self.pool.fetchval(
                f"SELECT COUNT(*) FROM {self.LEVEL_TABLE} WHERE guild_id=$1 AND total_xp>$2",
                int(guild_id), total,
            )
            return int(greater or 0) + 1
        board = await self.get_level_leaderboard(guild_id, 100000, 0)
        return next((index for index, row in enumerate(board, 1) if row["user_id"] == user_id), None)

    async def delete_guild_levels(self, guild_id: int):
        if self.pool is not None:
            await self.pool.execute(f"DELETE FROM {self.LEVEL_TABLE} WHERE guild_id=$1", int(guild_id))
        else:
            prefix = f"{guild_id}:"
            self.data["levels"] = {key: value for key, value in self.data.get("levels", {}).items() if not key.startswith(prefix)}
            await self.save()

    async def set_birthday(self, guild_id: int, user_id: int, month: int, day: int):
        if self.pool is not None:
            await self.pool.execute(
                f"""INSERT INTO {self.BIRTHDAY_TABLE} (guild_id,user_id,month,day,last_announced_year)
                    VALUES ($1,$2,$3,$4,0)
                    ON CONFLICT (guild_id,user_id) DO UPDATE SET month=EXCLUDED.month,day=EXCLUDED.day""",
                int(guild_id), int(user_id), int(month), int(day),
            )
        if self.pool is None:
            self.data.setdefault("birthdays", {})[f"{guild_id}:{user_id}"] = {
                "month": int(month), "day": int(day), "last_announced_year": 0
            }
            await self.save()

    async def get_birthday(self, guild_id: int, user_id: int) -> dict[str, int] | None:
        if self.pool is not None:
            row = await self.pool.fetchrow(
                f"SELECT month,day,last_announced_year FROM {self.BIRTHDAY_TABLE} WHERE guild_id=$1 AND user_id=$2",
                int(guild_id), int(user_id),
            )
            if row is None:
                return None
            return {"month": int(row["month"]), "day": int(row["day"]),
                    "last_announced_year": int(row["last_announced_year"])}
        value = self.data.get("birthdays", {}).get(f"{guild_id}:{user_id}")
        return dict(value) if isinstance(value, dict) else None

    async def remove_birthday(self, guild_id: int, user_id: int):
        if self.pool is not None:
            await self.pool.execute(f"DELETE FROM {self.BIRTHDAY_TABLE} WHERE guild_id=$1 AND user_id=$2", int(guild_id), int(user_id))
        self.data.get("birthdays", {}).pop(f"{guild_id}:{user_id}", None)
        if self.pool is None:
            await self.save()

    async def due_birthdays(self, month: int, day: int, year: int) -> list[tuple[int, int]]:
        if self.pool is not None:
            leap_year = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
            celebrate_feb29 = month == 2 and day == 28 and not leap_year
            rows = await self.pool.fetch(
                f"""SELECT guild_id,user_id FROM {self.BIRTHDAY_TABLE}
                    WHERE ((month=$1 AND day=$2) OR ($4::boolean AND month=2 AND day=29))
                      AND last_announced_year<$3""",
                int(month), int(day), int(year), celebrate_feb29,
            )
            return [(int(row["guild_id"]), int(row["user_id"])) for row in rows]
        result = []
        leap_year = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
        celebrate_feb29 = month == 2 and day == 28 and not leap_year
        for key, value in self.data.get("birthdays", {}).items():
            try:
                guild_text, user_text = key.split(":", 1)
                date_matches = (int(value.get("month", 0)) == month and int(value.get("day", 0)) == day)
                date_matches = date_matches or (celebrate_feb29 and int(value.get("month", 0)) == 2 and int(value.get("day", 0)) == 29)
                if date_matches and int(value.get("last_announced_year", 0)) < year:
                    result.append((int(guild_text), int(user_text)))
            except (ValueError, TypeError):
                continue
        return result

    async def mark_birthday_announced(self, guild_id: int, user_id: int, year: int):
        if self.pool is not None:
            await self.pool.execute(
                f"UPDATE {self.BIRTHDAY_TABLE} SET last_announced_year=$3 WHERE guild_id=$1 AND user_id=$2",
                int(guild_id), int(user_id), int(year),
            )
        item = self.data.get("birthdays", {}).get(f"{guild_id}:{user_id}")
        if item is not None:
            item["last_announced_year"] = int(year)
        if self.pool is None:
            await self.save()

    async def storage_stats(self) -> dict[str, Any]:
        if self.pool is None:
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            return {"backend": "local-json", "bytes": size, "persistent": False, "healthy": False}
        size = await self.pool.fetchval("SELECT pg_database_size(current_database())")
        self.last_database_size_bytes = int(size or 0)
        try:
            limit_mb = max(0, int(os.getenv("DATABASE_STORAGE_LIMIT_MB", "0")))
        except ValueError:
            limit_mb = 0
        limit_bytes = limit_mb * 1024 * 1024
        usage_percent = (self.last_database_size_bytes / limit_bytes * 100.0) if limit_bytes else None
        return {"backend": "postgresql", "bytes": self.last_database_size_bytes,
                "persistent": True, "healthy": self.database_healthy,
                "limit_mb": limit_mb or None, "usage_percent": usage_percent}



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
        super().__init__(command_prefix=("?", "!"), intents=intents, case_insensitive=True)
        self.store = DataStore(DATA_FILE)
        self.session: aiohttp.ClientSession | None = None
        self.meow_recent: dict[int, list[tuple[int, float]]] = {}
        self.meow_webhooks: dict[int, discord.Webhook] = {}
        self.event_sticky_locks: dict[int, asyncio.Lock] = {}
        self.automod_recent_messages: dict[tuple[int, int], list[tuple[float, str]]] = {}
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
        await self.store.connect()
        if self.store.removed_legacy_data:
            await self.store.save()
            log.info("Removed obsolete legacy tier and ELO data from persistent storage.")
        # New subsystems are loaded as extensions so moderation/community/leveling
        # stay separated from the existing bot core.
        for extension in ("extensions.automod", "extensions.community", "extensions.leveling"):
            await self.load_extension(extension)
        await self.start_status_site()
        # Persistent views have stable custom IDs and no timeout.
        self.add_view(TicketPanelView())
        self.add_view(TicketControlsView())
        self.add_view(GiveawayView())
        self.add_view(EventSignupView())
        self.add_view(EventPingPanelView())
        try:
            synced = await self.tree.sync()
            log.info("Synced %d application commands.", len(synced))
        except discord.HTTPException:
            log.exception("Slash-command sync failed.")
        self.giveaway_watcher.start()
        self.tier_watch_watcher.start()

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

    async def get_meow_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        """Return the bot-owned meow webhook for a channel, creating it if needed."""
        cached = self.meow_webhooks.get(channel.id)
        if cached is not None:
            return cached

        if not self.user:
            raise RuntimeError("The bot user is not ready yet.")
        for webhook in await channel.webhooks():
            if (webhook.name == "PVPClub Meow" and webhook.user
                    and webhook.user.id == self.user.id):
                self.meow_webhooks[channel.id] = webhook
                return webhook

        webhook = await channel.create_webhook(
            name="PVPClub Meow", reason="PVPClub Empire /makemeow feature"
        )
        self.meow_webhooks[channel.id] = webhook
        return webhook

    async def start_status_site(self):
        """Expose a tiny status website on Render's assigned PORT."""
        async def homepage(request: web.Request):
            return web.Response(text=STATUS_PAGE, content_type="text/html")

        async def health(request: web.Request):
            # This confirms the HTTP process is responding, not Discord connectivity.
            return web.Response(text="ok", content_type="text/plain")

        async def api_status(request: web.Request):
            ready = self.is_ready()
            try:
                storage_stats = await self.store.storage_stats()
            except Exception as exc:
                log.warning("Storage status query failed: %s", exc)
                storage_stats = {"backend": self.store.backend, "bytes": None,
                                 "persistent": self.store.backend == "postgresql", "healthy": False}
            return web.json_response({
                "service": "PVPClub Empire",
                "discord_connected": ready,
                "discord_status": "online" if ready else "connecting / reconnecting",
                "bot_username": str(self.user) if self.user else None,
                "guild_count": len(self.guilds),
                "uptime_seconds": max(0, int(time.time() - self.started_at)),
                "storage_backend": self.store.backend,
                "storage_persistent": self.store.backend == "postgresql",
                "storage_healthy": storage_stats.get("healthy", False),
                "storage_bytes": storage_stats.get("bytes"),
                "storage_mb": round(storage_stats["bytes"] / (1024 * 1024), 2) if storage_stats.get("bytes") is not None else None,
                "storage_limit_mb": storage_stats.get("limit_mb"),
                "storage_usage_percent": round(storage_stats["usage_percent"], 2) if storage_stats.get("usage_percent") is not None else None,
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
        if self.store.pool is not None:
            await self.store.pool.close()
            self.store.pool = None
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

    @tasks.loop(seconds=TIER_WATCH_POLL_SECONDS)
    async def tier_watch_watcher(self):
        """Poll watched MCTiers profiles and announce only documented profile changes."""
        for guild_id_text, watchlist in list(self.store.data.get("tier_watchlists", {}).items()):
            try:
                guild_id = int(guild_id_text)
            except (TypeError, ValueError):
                continue
            guild = self.get_guild(guild_id)
            channel_id = self.store.data.get("tier_watch_channels", {}).get(guild_id_text)
            channel = guild.get_channel(int(channel_id)) if guild and channel_id else None
            if not guild or not isinstance(channel, discord.TextChannel) or not isinstance(watchlist, dict):
                continue

            for player_key, watch in list(watchlist.items()):
                if not isinstance(watch, dict) or not isinstance(watch.get("username"), str):
                    continue
                try:
                    profile = await self.get_mctiers_profile(watch["username"])
                    await asyncio.sleep(1.0)
                    current = snapshot_mctiers_rankings(profile.get("rankings", {}))
                    previous = watch.get("snapshot", {})
                    if not isinstance(previous, dict):
                        previous = {}
                    changes = tierwatch_deltas(previous, current)
                    if not changes:
                        # Refresh the snapshot to retain newly documented metadata without announcing it.
                        if current != previous:
                            watch["snapshot"] = current
                            watch["username"] = profile.get("name", watch["username"])
                            await self.store.save()
                        continue

                    try:
                        mode_names = await self.get_mctiers_gamemodes()
                    except (MCTiersAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                        mode_names = {}

                    player_name = profile.get("name") or watch["username"]
                    e = embed(f"🏆 MCTiers update • {player_name}",
                              "A watched player's official MCTiers profile changed.", GREEN)
                    e.set_footer(text="Official MCTiers API v2 • profile polling; not an instant test feed")
                    for delta in changes[:20]:
                        title = mode_names.get(delta["mode"], delta["mode"])
                        old_item, new_item, kind = delta["old"], delta["new"], delta["kind"]
                        old_value = mctiers_tier_code(old_item.get("tier"), old_item.get("pos")) if old_item else "Not available"
                        new_value = mctiers_tier_code(new_item.get("tier"), new_item.get("pos")) if new_item else "Not available"
                        label = "New tier added" if kind == "added" else ("Tier/test record updated" if kind == "attained" else "Tier changed")
                        details = f"**Previous:** `{old_value}`\n**Current:** `{new_value}`"
                        retired = new_item.get("retired") if new_item else None
                        if retired is not None:
                            details += f"\n**Retired:** {'Yes' if retired else 'No'}"
                        e.add_field(name=f"{title} • {label}", value=details, inline=True)
                    try:
                        await channel.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        log.exception("Could not post Tierwatch update for %s in guild %s", player_name, guild_id)
                        continue

                    watch["snapshot"] = current
                    watch["username"] = player_name
                    await self.store.save()
                except MCTiersPlayerNotFound:
                    await asyncio.sleep(1.0)
                    log.info("Tierwatch profile no longer resolves: %s", watch.get("username"))
                except MCTiersRateLimited:
                    log.warning("MCTiers rate limit reached while polling Tierwatch; pausing this cycle.")
                    return
                except (MCTiersAPIError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.warning("Tierwatch lookup failed for %s: %s", watch.get("username"), exc)
                except Exception:
                    log.exception("Unexpected Tierwatch error for %s", watch.get("username"))

    @tier_watch_watcher.before_loop
    async def before_tier_watch_watcher(self):
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


async def ensure_event_ping_role(guild: discord.Guild) -> discord.Role | None:
    role = discord.utils.get(guild.roles, name=EVENT_PING_ROLE_NAME)
    bot_member = guild.me
    if role:
        if bot_member is None or role >= bot_member.top_role:
            log.warning("Event Ping role is above the bot in guild %s", guild.id)
            return None
        if not role.mentionable:
            try:
                await role.edit(mentionable=True, reason="Enable Event Ping mentions")
            except discord.HTTPException:
                log.exception("Could not make Event Ping role mentionable in guild %s", guild.id)
                return None
        return role
    try:
        role = await guild.create_role(
            name=EVENT_PING_ROLE_NAME,
            color=discord.Color(PINK),
            mentionable=True,
            reason="PVPClub Empire event notification opt-in role",
        )
        bot_member = guild.me
        if bot_member and bot_member.top_role > role:
            await role.edit(position=max(1, bot_member.top_role.position - 1))
        return role
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not create Event Ping role in guild %s", guild.id)
        return None


def event_sticky_embed() -> discord.Embed:
    return embed(
        "🔔 Event notifications",
        "Want to hear about upcoming PVPClub Empire events?\n\n"
        "Use **Get Event Ping** to receive the `Event Ping` role, or **Remove Event Ping** "
        "to stop receiving event mentions. You can change this whenever you like.",
        PINK,
    )


async def refresh_event_sticky(channel: discord.TextChannel):
    """Move the event opt-in panel back to the bottom after a new member message."""
    guild_id_text = str(channel.guild.id)
    settings = bot.store.data.get("event_settings", {}).get(guild_id_text)
    if not isinstance(settings, dict) or int(settings.get("channel_id", 0) or 0) != channel.id:
        return
    lock = bot.event_sticky_locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        settings = bot.store.data.get("event_settings", {}).get(guild_id_text, {})
        old_id = settings.get("sticky_message_id")
        if old_id:
            try:
                old_message = await channel.fetch_message(int(old_id))
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass
        try:
            new_message = await channel.send(embed=event_sticky_embed(), view=EventPingPanelView())
        except discord.HTTPException:
            log.exception("Could not refresh Event Ping sticky panel in channel %s", channel.id)
            return
        settings["sticky_message_id"] = new_message.id
        await bot.store.save()


class EventPingPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _role_action(self, interaction: discord.Interaction, add: bool):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This button only works in a server.", ephemeral=True)
            return
        settings = bot.store.data.get("event_settings", {}).get(str(guild.id), {})
        role_id = settings.get("role_id") if isinstance(settings, dict) else None
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            role = await ensure_event_ping_role(guild)
        if role is None:
            await interaction.response.send_message(
                "I cannot access the Event Ping role. Please ask an administrator to give me Manage Roles and move my role higher.",
                ephemeral=True,
            )
            return
        if add:
            if role in interaction.user.roles:
                text = "You already have the Event Ping role."
            else:
                try:
                    await interaction.user.add_roles(role, reason="Member opted into PVPClub event announcements")
                    text = "🔔 You will now receive Event Ping announcements."
                except discord.Forbidden:
                    text = "I cannot assign that role. Please check my Manage Roles permission and role position."
        else:
            if role not in interaction.user.roles:
                text = "You do not currently have the Event Ping role."
            else:
                try:
                    await interaction.user.remove_roles(role, reason="Member opted out of PVPClub event announcements")
                    text = "🔕 Event Ping removed. You will no longer receive that role's mentions."
                except discord.Forbidden:
                    text = "I cannot remove that role. Please check my Manage Roles permission and role position."
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="🔔 Get Event Ping", style=discord.ButtonStyle.success, custom_id="eventping:get")
    async def get_event_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._role_action(interaction, True)

    @discord.ui.button(label="🔕 Remove Event Ping", style=discord.ButtonStyle.secondary, custom_id="eventping:remove")
    async def remove_event_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._role_action(interaction, False)


class EventSignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _signup(self, interaction: discord.Interaction, joining: bool):
        if interaction.message is None or interaction.guild is None:
            await interaction.response.send_message("This event signup is unavailable.", ephemeral=True)
            return
        event = bot.store.data.get("events", {}).get(str(interaction.message.id))
        if not isinstance(event, dict) or event.get("closed"):
            await interaction.response.send_message("This event signup is closed or no longer available.", ephemeral=True)
            return
        participants = event.setdefault("participants", [])
        user_id = interaction.user.id
        if joining:
            if user_id in participants:
                await interaction.response.send_message("You are already signed up.", ephemeral=True)
                return
            participants.append(user_id)
            response_text = "✅ You are signed up for this event!"
        else:
            if user_id not in participants:
                await interaction.response.send_message("You are not signed up for this event.", ephemeral=True)
                return
            participants.remove(user_id)
            response_text = "Your event signup has been removed."
        await bot.store.save()
        event_embed = build_event_embed(event)
        await interaction.response.edit_message(embed=event_embed, view=self)
        await interaction.followup.send(response_text, ephemeral=True)

    @discord.ui.button(label="✅ Sign up", style=discord.ButtonStyle.success, custom_id="event:signup")
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._signup(interaction, True)

    @discord.ui.button(label="↩️ Leave event", style=discord.ButtonStyle.secondary, custom_id="event:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._signup(interaction, False)


def build_event_embed(event: dict[str, Any]) -> discord.Embed:
    name = discord.utils.escape_markdown(str(event.get("name", "PVPClub event")))[:200]
    host_id = event.get("host_id")
    host_text = f"<@{host_id}>" if isinstance(host_id, int) else "Not available"
    description = str(event.get("description", "No description provided."))[:1000]
    when = str(event.get("when", "Not specified"))[:100]
    participants = event.get("participants", [])
    if not isinstance(participants, list):
        participants = []
    shown = [f"<@{uid}>" for uid in participants[:20] if isinstance(uid, int)]
    signup_list = "\n".join(shown) if shown else "No one has signed up yet. Be the first!"
    if len(participants) > 20:
        signup_list += f"\n…and {len(participants) - 20} more."
    e = embed(f"🎉 {name}", description, PINK)
    e.add_field(name="Host", value=host_text, inline=True)
    e.add_field(name="When", value=when, inline=True)
    e.add_field(name=f"Signups ({len(participants)})", value=signup_list[:1024], inline=False)
    e.set_footer(text="PVPClub Empire • Use the buttons to join or leave")
    return e


@bot.command(name="eventsetup")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
@commands.bot_has_guild_permissions(view_channel=True, send_messages=True, embed_links=True, manage_roles=True)
async def eventsetup(ctx: commands.Context, eventchannel: discord.TextChannel):
    """Configure the event channel and create the Event Ping opt-in panel."""
    if ctx.guild is None:
        return
    role = await ensure_event_ping_role(ctx.guild)
    if role is None:
        await ctx.reply("I couldn't create the `Event Ping` role. Give me Manage Roles and move my role above it.", mention_author=False)
        return
    bot_member = ctx.guild.me
    perms = eventchannel.permissions_for(bot_member) if bot_member else None
    if not perms or not perms.send_messages or not perms.embed_links:
        await ctx.reply("I need Send Messages and Embed Links in that event channel.", mention_author=False)
        return
    settings = bot.store.data.setdefault("event_settings", {}).setdefault(str(ctx.guild.id), {})
    previous_channel = ctx.guild.get_channel(int(settings.get("channel_id", 0) or 0))
    previous_id = settings.get("sticky_message_id")
    if isinstance(previous_channel, discord.TextChannel) and previous_id:
        try:
            previous_sticky = await previous_channel.fetch_message(int(previous_id))
            await previous_sticky.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass
    settings.update({"channel_id": eventchannel.id, "role_id": role.id, "sticky_message_id": None})
    await bot.store.save()
    try:
        sticky = await eventchannel.send(embed=event_sticky_embed(), view=EventPingPanelView())
    except discord.HTTPException:
        await ctx.reply("I saved the event channel, but couldn't send the sticky panel. Check my channel permissions.", mention_author=False)
        return
    settings["sticky_message_id"] = sticky.id
    await bot.store.save()
    await ctx.reply(f"✅ Events will be posted in {eventchannel.mention}. Created/verified {role.mention} and posted the sticky opt-in panel.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="event", description="Create a community event in the configured event channel.")
@app_commands.guild_only()
@app_commands.describe(name_event="Event name", when="Optional date/time", description="What will happen at the event")
@app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
async def event(interaction: discord.Interaction, name_event: app_commands.Range[str, 1, 100], when: str = "Not specified", description: str = "Join us for a PVPClub Empire event!"):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Events can only be created inside a server.", ephemeral=True)
        return
    settings = bot.store.data.get("event_settings", {}).get(str(guild.id), {})
    channel_id = settings.get("channel_id") if isinstance(settings, dict) else None
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("An administrator must configure the event channel first with `?eventsetup #channel`.", ephemeral=True)
        return
    role = guild.get_role(int(settings.get("role_id", 0) or 0)) or await ensure_event_ping_role(guild)
    if role is None:
        await interaction.response.send_message("I couldn't find or create the Event Ping role.", ephemeral=True)
        return
    bot_member = guild.me
    permissions = channel.permissions_for(bot_member) if bot_member else None
    if not permissions or not permissions.send_messages or not permissions.embed_links:
        await interaction.response.send_message("I cannot post in the configured event channel. Check my channel permissions.", ephemeral=True)
        return
    event_data = {
        "guild_id": guild.id,
        "channel_id": channel.id,
        "name": name_event.strip(),
        "when": when.strip()[:100] or "Not specified",
        "description": description.strip()[:1000] or "No description provided.",
        "host_id": interaction.user.id,
        "participants": [],
        "created_at": int(time.time()),
        "closed": False,
    }
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        message = await channel.send(
            content=role.mention,
            embed=build_event_embed(event_data),
            view=EventSignupView(),
            allowed_mentions=discord.AllowedMentions(roles=[role]),
        )
    except discord.HTTPException:
        await interaction.followup.send("I couldn't publish the event. Check the bot's channel permissions.", ephemeral=True)
        return
    bot.store.data.setdefault("events", {})[str(message.id)] = event_data
    await bot.store.save()
    # Keep the Event Ping opt-in panel as the last message in the channel.
    await refresh_event_sticky(channel)
    await interaction.followup.send(f"🎉 Your event **{discord.utils.escape_markdown(name_event)}** was posted in {channel.mention} and the Event Ping role was mentioned.", ephemeral=True)


@bot.command(name="tierwatchsetup")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
@commands.bot_has_guild_permissions(view_channel=True, send_messages=True, embed_links=True)
async def tierwatchsetup(ctx: commands.Context, tier_announcements_channel: discord.TextChannel):
    """Set the channel that receives official MCTiers watchlist change alerts."""
    if ctx.guild is None:
        return
    bot_member = ctx.guild.me
    perms = tier_announcements_channel.permissions_for(bot_member) if bot_member else None
    if not perms or not perms.send_messages or not perms.embed_links:
        await ctx.reply("I need Send Messages and Embed Links in that announcements channel.", mention_author=False)
        return
    bot.store.data.setdefault("tier_watch_channels", {})[str(ctx.guild.id)] = tier_announcements_channel.id
    await bot.store.save()
    await ctx.reply(f"✅ Official MCTiers watch announcements will be sent to {tier_announcements_channel.mention}.", mention_author=False)


@eventsetup.error
async def eventsetup_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("You need Manage Server to configure the event channel.", mention_author=False)
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.reply("I need Manage Roles, View Channel, Send Messages and Embed Links.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("Usage: `?eventsetup #event-channel`", mention_author=False)
    else:
        log.exception("?eventsetup failed", exc_info=error)
        await ctx.reply("I couldn't configure the event channel. Check my permissions and try again.", mention_author=False)


@tierwatchsetup.error
async def tierwatchsetup_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("You need Manage Server to configure Tierwatch announcements.", mention_author=False)
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.reply("I need View Channel, Send Messages and Embed Links in that channel.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("Usage: `?tierwatchsetup #tier-announcements`", mention_author=False)
    else:
        log.exception("?tierwatchsetup failed", exc_info=error)
        await ctx.reply("I couldn't configure the Tierwatch channel. Check my permissions and try again.", mention_author=False)


@bot.tree.command(name="tierwatch", description="Add a Minecraft player to the official MCTiers change watchlist.")
@app_commands.guild_only()
@app_commands.describe(username="Minecraft username to watch")
@app_commands.checks.cooldown(5, 60.0, key=lambda i: i.guild_id)
async def tierwatch(interaction: discord.Interaction, username: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Tierwatch only works inside a server.", ephemeral=True)
        return
    if str(guild.id) not in bot.store.data.get("tier_watch_channels", {}):
        await interaction.response.send_message("An administrator must first run `?tierwatchsetup #channel`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        profile = await bot.get_mctiers_profile(username)
    except ValueError as exc:
        await interaction.followup.send(f"Invalid Minecraft username: {exc}", ephemeral=True)
        return
    except MCTiersPlayerNotFound:
        await interaction.followup.send(f"No MCTiers profile was found for **{discord.utils.escape_markdown(username)}**.", ephemeral=True)
        return
    except (MCTiersAPIError, aiohttp.ClientError, asyncio.TimeoutError):
        await interaction.followup.send("The official MCTiers API is temporarily unavailable. Try again later.", ephemeral=True)
        return

    canonical_name = str(profile.get("name", username))
    uuid = profile.get("uuid")
    key = str(uuid).casefold() if isinstance(uuid, str) and uuid else canonical_name.casefold()
    watchlist = bot.store.data.setdefault("tier_watchlists", {}).setdefault(str(guild.id), {})
    if key in watchlist:
        await interaction.followup.send(f"**{canonical_name}** is already being watched in this server.", ephemeral=True)
        return
    if len(watchlist) >= 50:
        await interaction.followup.send("This server has reached the 50-player Tierwatch limit.", ephemeral=True)
        return
    baseline = snapshot_mctiers_rankings(profile.get("rankings", {}))
    watchlist[key] = {
        "username": canonical_name,
        "uuid": uuid if isinstance(uuid, str) else None,
        "snapshot": baseline,
        "added_by": interaction.user.id,
        "added_at": int(time.time()),
    }
    await bot.store.save()
    await interaction.followup.send(
        f"✅ **{discord.utils.escape_markdown(canonical_name)}** is now on this server's Tierwatch list. "
        "I saved their current official profile as the baseline, so only future changes will be announced.",
        ephemeral=True,
    )


@bot.tree.command(name="tierunwatch", description="Remove a Minecraft player from this server's Tierwatch list.")
@app_commands.guild_only()
async def tierunwatch(interaction: discord.Interaction, username: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Tierwatch only works inside a server.", ephemeral=True)
        return
    watchlist = bot.store.data.get("tier_watchlists", {}).get(str(guild.id), {})
    target = username.casefold()
    match = next((key for key, item in watchlist.items()
                  if key.casefold() == target or str(item.get("username", "")).casefold() == target
                  or str(item.get("uuid", "")).casefold() == target), None)
    if match is None:
        await interaction.response.send_message("That player is not on this server's Tierwatch list.", ephemeral=True)
        return
    watchlist.pop(match, None)
    await bot.store.save()
    await interaction.response.send_message(f"Removed **{discord.utils.escape_markdown(username)}** from Tierwatch.", ephemeral=True)


@bot.tree.command(name="tierwatchlist", description="Show this server's MCTiers Tierwatch list.")
@app_commands.guild_only()
async def tierwatchlist(interaction: discord.Interaction):
    watchlist = bot.store.data.get("tier_watchlists", {}).get(str(interaction.guild_id), {})
    if not watchlist:
        await interaction.response.send_message("This server is not watching any MCTiers players yet.", ephemeral=True)
        return
    names = [str(item.get("username", "Unknown")) for item in watchlist.values() if isinstance(item, dict)]
    await interaction.response.send_message(embed=embed("👀 MCTiers Tierwatch", "\n".join(f"• {discord.utils.escape_markdown(name)}" for name in names[:50]), CYAN), ephemeral=True)




@bot.tree.command(name="fban", description="Run a timed PVPClub Empire moderation simulation.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def fban(interaction: discord.Interaction, member: discord.Member, reason: str = "Staff review"):
    """A harmless bot-branded countdown; it never bans, kicks or times out anyone."""
    reason = reason.strip()[:300] or "Staff review"
    case_id = str(interaction.id)[-8:]
    e = embed(
        "🚨 PVPClub Empire • Enforcement Notice",
        f"**Member:** {discord.utils.escape_markdown(str(member))}\n"
        f"**Review reference:** `PC-{case_id}`\n"
        f"**Reason:** {discord.utils.escape_markdown(reason)}\n\n"
        "A moderation review countdown has started.\n"
        "**Resolution in:** 30 seconds",
        RED,
    )
    e.set_thumbnail(url=member.display_avatar.url)
    e.set_footer(text="PVPClub Empire • timed server-side simulation • no Discord action is applied")
    await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())
    try:
        msg = await interaction.original_response()
        await asyncio.sleep(30)
        resolved = embed(
            "✅ PVPClub Empire • Review completed",
            f"The review for **{discord.utils.escape_markdown(str(member))}** has concluded.\n\n"
            "No ban, kick or timeout was applied.",
            GREEN,
        )
        resolved.set_footer(text=f"Reference PC-{case_id} • no account action was applied")
        await msg.edit(embed=resolved)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Could not update timed moderation simulation %s", case_id)


@bot.tree.command(name="howgay", description="Learn why the bot won't guess someone's sexuality.")
async def howgay(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=embed(
            "🌈 No random labels",
            "I don't guess or score someone's sexual orientation. That's personal, not a stat to roll.\n\nTry `/meowstreak` for a real statistic instead.",
            PINK,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="role", description="Add or remove a role from a member.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.checks.bot_has_permissions(manage_roles=True)
@app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
async def role(interaction: discord.Interaction, action: app_commands.Choice[str], member: discord.Member, target_role: discord.Role):
    guild = interaction.guild
    actor = interaction.user
    bot_member = guild.me if guild else None
    if guild is None or not isinstance(actor, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if target_role.is_default() or target_role.managed:
        await interaction.response.send_message("That role cannot be assigned manually.", ephemeral=True)
        return
    if actor.id != guild.owner_id and target_role >= actor.top_role:
        await interaction.response.send_message("You can only manage roles below your highest role.", ephemeral=True)
        return
    if bot_member is None or target_role >= bot_member.top_role:
        await interaction.response.send_message("My highest role must be above the role you want to manage.", ephemeral=True)
        return
    if actor.id != guild.owner_id and member.top_role >= actor.top_role:
        await interaction.response.send_message("You can only manage roles for members below your highest role.", ephemeral=True)
        return
    try:
        if (actor.id != guild.owner_id and (
            target_role.permissions.administrator
            or target_role.permissions.manage_guild
            or target_role.permissions.manage_roles
            or target_role.permissions.ban_members
            or target_role.permissions.kick_members
            or target_role.permissions.moderate_members
        )):
            await interaction.response.send_message(
                "Only the server owner can assign roles that grant powerful server/moderation permissions.",
                ephemeral=True,
            )
            return
        if action.value == "add":
            await member.add_roles(target_role, reason=f"Role assigned by {actor} ({actor.id})")
            message = f"✅ Added {target_role.mention} to {member.mention}."
        else:
            await member.remove_roles(target_role, reason=f"Role removed by {actor} ({actor.id})")
            message = f"✅ Removed {target_role.mention} from {member.mention}."
    except discord.Forbidden:
        message = "Discord denied that role change. Check role hierarchy and permissions."
    else:
        await write_modlog(guild, "Role updated", f"{member} ({member.mention})", member.id, actor,
                           f"{action.value.title()} role {target_role.name}", color=GREEN)
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="nickname", description="Change or clear a member's server nickname.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_nicknames=True)
@app_commands.checks.bot_has_permissions(manage_nicknames=True)
@app_commands.describe(nickname="New nickname (1–32 characters), or type 'clear' to remove it", reason="Why the nickname is being changed")
async def nickname(interaction: discord.Interaction, member: discord.Member, nickname: str, reason: str = "No reason provided"):
    error = moderation_target_error(interaction, member)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    nickname = nickname.strip()
    if nickname.casefold() != "clear" and not 1 <= len(nickname) <= 32:
        await interaction.response.send_message("Nicknames must be 1–32 characters. Use `clear` to remove the nickname.", ephemeral=True)
        return
    new_nick = None if nickname.casefold() == "clear" else nickname
    reason = reason.strip()[:500] or "No reason provided"
    await interaction.response.defer(ephemeral=True, thinking=True)
    await member.edit(nick=new_nick, reason=moderation_audit_reason(interaction, reason))
    await send_mod_confirmation(
        interaction, "Nickname cleared" if new_nick is None else "Nickname changed",
        member, reason, details=f"New nickname: {new_nick or 'None'}", color=GREEN
    )


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

    automod_cog = bot.get_cog("AutoModCog")
    if automod_cog and await automod_cog.engine.check_message(message):
        return

    # XP and meow listeners run only after AutoMod has accepted the message.
    leveling_cog = bot.get_cog("LevelingCog")
    if leveling_cog:
        await leveling_cog.process_message(message)
    community_cog = bot.get_cog("CommunityCog")
    if community_cog:
        await community_cog.process_meow_message(message)

    # Preserve the existing multi-member meow-chain feature.
    if "meow" in (message.content or "").casefold():
        now = time.time()
        entries = bot.meow_recent.setdefault(message.channel.id, [])
        entries[:] = [(uid, ts) for uid, ts in entries if now - ts <= 60]
        if message.author.id not in {uid for uid, _ in entries}:
            entries.append((message.author.id, now))
        if len({uid for uid, _ in entries}) >= 3:
            entries.clear()
            await message.channel.send("🐾 **MEOW CHAIN COMBO!** Three different members have each sent a meow-containing message. :3")

    await bot.process_commands(message)
    # Repost after bot command/meow responses too, keeping the opt-in event card at the bottom.
    event_settings = bot.store.data.get("event_settings", {}).get(str(message.guild.id), {})
    if isinstance(event_settings, dict) and event_settings.get("channel_id") == message.channel.id:
        if isinstance(message.channel, discord.TextChannel):
            await refresh_event_sticky(message.channel)



@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Friendly errors for prefix setup commands instead of silent console traces."""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        message = "You do not have permission to use that setup command."
    elif isinstance(error, commands.BotMissingPermissions):
        message = "I am missing a Discord permission needed for that command."
    elif isinstance(error, commands.MissingRequiredArgument):
        message = f"Missing required option: `{error.param.name}`."
    elif isinstance(error, commands.BadArgument):
        message = "I couldn't understand that argument. Mention a channel or role in the expected format."
    elif isinstance(error, commands.NoPrivateMessage):
        message = "That command can only be used inside a server."
    else:
        log.exception("Prefix command failed", exc_info=error)
        message = "Something went wrong while running that command. The error was logged."
    try:
        await ctx.reply(message, mention_author=False, delete_after=15)
    except discord.HTTPException:
        log.debug("Could not send prefix command error response")


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
    elif isinstance(original, discord.Forbidden):
        message = "Discord refused that action. Check my permissions and role position."
    elif isinstance(original, discord.NotFound):
        message = "That member, message, or resource could not be found."
    elif isinstance(original, discord.HTTPException):
        message = "Discord could not complete that action. Check the target and try again."
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
    count = await bot.store.get_meow_count(user.id)
    rank = await bot.store.meow_rank(user.id)
    e = embed("🐾 Meow Statistics", f"**Player:** {user.mention}\n**Meows:** {count:,}\n**Global bot rank:** #{rank}\n**Title:** {meow_title(count)}", PINK)
    e.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="meowboard", description="Top 10 lifetime meowers.")
async def meowboard(interaction: discord.Interaction):
    rows = await bot.store.meow_leaderboard(10)
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, count) in enumerate(rows):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{medal} <@{uid}> — **{count:,}** · {meow_title(count)}")
    await interaction.response.send_message(embed=embed("🏆 Empire Meowboard", "\n".join(lines) or "No meows yet. Be the first to say meow! :3", PINK))

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


@bot.tree.command(name="makemeow", description="Post a meow using your own Discord name and avatar.")
@app_commands.describe(message="The meow message to post (maximum 500 characters).")
@app_commands.checks.cooldown(1, 30.0)
async def makemeow(interaction: discord.Interaction, message: str = "meow :3"):
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or channel is None:
        await interaction.response.send_message("This command only works inside a server channel.", ephemeral=True)
        return
    if not message.strip() or len(message) > 500:
        await interaction.response.send_message("Your message must be 1–500 characters.", ephemeral=True)
        return

    thread = channel if isinstance(channel, discord.Thread) else None
    parent = channel.parent if thread else channel
    if not isinstance(parent, discord.TextChannel):
        await interaction.response.send_message("Use this command in a normal text channel or its thread.", ephemeral=True)
        return

    bot_member = guild.me
    if bot_member is None or not parent.permissions_for(bot_member).manage_webhooks:
        await interaction.response.send_message(
            "I need **Manage Webhooks** in this channel to post a meow with your own avatar.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        webhook = await bot.get_meow_webhook(parent)
        send_args = {
            "content": message.strip(),
            "username": interaction.user.display_name[:80],
            "avatar_url": interaction.user.display_avatar.url,
            "allowed_mentions": discord.AllowedMentions.none(),
            "wait": True,
        }
        if thread is not None:
            send_args["thread"] = thread
        await webhook.send(**send_args)
    except discord.NotFound:
        # A cached webhook may have been deleted; recreate it once and retry.
        bot.meow_webhooks.pop(parent.id, None)
        try:
            webhook = await bot.get_meow_webhook(parent)
            if thread is not None:
                await webhook.send(
                    content=message.strip(), username=interaction.user.display_name[:80],
                    avatar_url=interaction.user.display_avatar.url,
                    allowed_mentions=discord.AllowedMentions.none(), wait=True, thread=thread
                )
            else:
                await webhook.send(
                    content=message.strip(), username=interaction.user.display_name[:80],
                    avatar_url=interaction.user.display_avatar.url,
                    allowed_mentions=discord.AllowedMentions.none(), wait=True
                )
        except discord.HTTPException:
            log.exception("Could not recreate the meow webhook in channel %s.", parent.id)
            await interaction.followup.send("I couldn't create or use the meow webhook. Check my Manage Webhooks permission.", ephemeral=True)
            return
    except discord.Forbidden:
        await interaction.followup.send("Discord denied webhook access. Give me **Manage Webhooks** in this channel.", ephemeral=True)
        return
    except discord.HTTPException:
        log.exception("Could not send the meow webhook message in channel %s.", parent.id)
        await interaction.followup.send("Discord couldn't send the meow message. Please try again.", ephemeral=True)
        return

    await interaction.followup.send(
        "🐾 Your meow was posted using **your own** display name and profile picture. The `/makemeow` interaction remains visible.",
        ephemeral=True
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


@bot.command(name="ticketsetup")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def ticketsetup(ctx: commands.Context):
    guild = ctx.guild
    category = discord.utils.get(guild.categories, name="📩 | EMPIRES TICKETS")
    if category is None:
        category = await guild.create_category("📩 | EMPIRES TICKETS", reason="PVPClub Empire ticket setup")
    channel = discord.utils.get(guild.text_channels, name="🎫-open-a-ticket")
    if channel is None:
        channel = await guild.create_text_channel("🎫-open-a-ticket", category=category, reason="PVPClub Empire ticket setup")
    else:
        category = channel.category or category
    e = embed("📩 PVPClub Empire Support", "Need help? Choose the right ticket type below. Please don't open duplicates.", CYAN)
    await channel.send(embed=e, view=TicketPanelView())
    await ctx.reply(f"✅ Ticket panel ready in {channel.mention}.", mention_author=False)


@ticketsetup.error
async def ticketsetup_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("You need Manage Server to configure tickets.", mention_author=False)
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.reply("I need Manage Channels, Send Messages and Embed Links.", mention_author=False)
    else:
        log.exception("?ticketsetup failed", exc_info=error)
        await ctx.reply("I couldn't create the ticket panel. Check my permissions.", mention_author=False)


# ---------------------------------------------------------------------------
# GIVEAWAYS

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


def snapshot_mctiers_rankings(rankings: Any) -> dict[str, dict[str, Any]]:
    """Keep only ranking fields documented by MCTiers v2 for change detection."""
    if not isinstance(rankings, dict):
        return {}
    snapshot: dict[str, dict[str, Any]] = {}
    for mode, item in rankings.items():
        if not isinstance(mode, str) or not isinstance(item, dict):
            continue
        snapshot[mode] = {
            field: item.get(field)
            for field in ("tier", "pos", "peak_tier", "peak_pos", "attained", "retired")
            if field in item
        }
    return snapshot


def tierwatch_deltas(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for mode, new_item in current.items():
        if not isinstance(new_item, dict):
            continue
        old_item = previous.get(mode)
        if not isinstance(old_item, dict):
            changes.append({"mode": mode, "old": None, "new": new_item, "kind": "added"})
            continue
        tier_changed = any(old_item.get(field) != new_item.get(field)
                           for field in ("tier", "pos", "peak_tier", "peak_pos", "retired"))
        attained_changed = (
            "attained" in old_item and "attained" in new_item
            and old_item.get("attained") != new_item.get("attained")
        )
        if tier_changed:
            changes.append({"mode": mode, "old": old_item, "new": new_item, "kind": "changed"})
        elif attained_changed:
            changes.append({"mode": mode, "old": old_item, "new": new_item, "kind": "attained"})
    return changes


# ---------------------------------------------------------------------------
# MODERATION
# ---------------------------------------------------------------------------


def moderation_audit_reason(interaction: discord.Interaction, reason: str) -> str:
    """Format an audit reason within Discord's 512-character limit."""
    return f"{interaction.user} ({interaction.user.id}): {reason}"[:512]


def moderation_target_error(interaction: discord.Interaction, member: discord.Member) -> str | None:
    """Enforce Discord's owner and role hierarchy before a moderation action."""
    guild = interaction.guild
    actor = interaction.user
    if guild is None or not isinstance(actor, discord.Member):
        return "Moderation commands can only be used in a server."
    if member.id == guild.owner_id:
        return "The server owner cannot be moderated by this bot."
    if member.id == actor.id:
        return "You cannot moderate yourself with this command."
    if bot.user and member.id == bot.user.id:
        return "I cannot moderate myself."
    if actor.id != guild.owner_id and member.top_role >= actor.top_role:
        return "You can only moderate members whose highest role is below yours."
    bot_member = guild.me
    if bot_member is not None and member.top_role >= bot_member.top_role:
        return "My highest role must be above the target's highest role."
    return None


async def write_modlog(
    guild: discord.Guild,
    action: str,
    target_label: str,
    target_id: int,
    moderator: discord.Member,
    reason: str,
    *,
    details: str | None = None,
    color: int = RED,
):
    """Write to the configured moderation channel; Discord's audit log remains authoritative."""
    configured = bot.store.data["modlog_channels"].get(str(guild.id))
    if not configured:
        return
    try:
        channel_id = int(configured)
    except (TypeError, ValueError):
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    e = embed(f"🛡️ Moderation • {action}", color=color)
    e.add_field(name="Target", value=f"{target_label} (`{target_id}`)", inline=False)
    e.add_field(name="Moderator", value=f"{moderator.mention} (`{moderator.id}`)", inline=True)
    e.add_field(name="Reason", value=(reason or "No reason provided")[:1024], inline=False)
    if details:
        e.add_field(name="Details", value=details[:1024], inline=False)
    e.timestamp = datetime.now(timezone.utc)
    try:
        await channel.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.exception("Could not post moderation log in guild %s.", guild.id)


async def send_mod_confirmation(
    interaction: discord.Interaction,
    action: str,
    member: discord.Member,
    reason: str,
    *,
    color: int = RED,
    details: str | None = None,
):
    await write_modlog(
        interaction.guild, action, f"{member} ({member.mention})", member.id,
        interaction.user, reason, details=details, color=color
    )
    e = embed(f"✅ {action}", color=color)
    e.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
    e.add_field(name="Reason", value=reason[:1024], inline=False)
    if details:
        e.add_field(name="Details", value=details[:1024], inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)




@bot.tree.command(name="ban", description="Ban a member from this server.")
@app_commands.guild_only()
@app_commands.describe(delete_message_days="Delete 0–7 days of the member's recent messages.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
    delete_message_days: app_commands.Range[int, 0, 7] = 0,
):
    error = moderation_target_error(interaction, member)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    reason = reason.strip()[:500] or "No reason provided"
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.guild.ban(
        member, reason=moderation_audit_reason(interaction, reason),
        delete_message_seconds=delete_message_days * 86400,
    )
    await send_mod_confirmation(
        interaction, "Member banned", member, reason,
        details=f"Deleted recent messages: {delete_message_days} day(s)."
    )


@bot.tree.command(name="unban", description="Unban a user by their Discord ID.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    if not re.fullmatch(r"\d{17,20}", user_id.strip()):
        await interaction.response.send_message("Enter a valid 17–20 digit Discord user ID.", ephemeral=True)
        return
    target_id = int(user_id)
    reason = reason.strip()[:500] or "No reason provided"
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.guild.unban(
        discord.Object(id=target_id),
        reason=moderation_audit_reason(interaction, reason)
    )
    await write_modlog(
        interaction.guild, "Member unbanned", f"User ID {target_id}", target_id,
        interaction.user, reason, color=GREEN
    )
    await interaction.followup.send(
        embed=embed("✅ Member unbanned", f"**User ID:** `{target_id}`\n**Reason:** {reason}", GREEN),
        ephemeral=True
    )


@bot.tree.command(name="kick", description="Kick a member from this server.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.checks.bot_has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    error = moderation_target_error(interaction, member)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    reason = reason.strip()[:500] or "No reason provided"
    await interaction.response.defer(ephemeral=True, thinking=True)
    await member.kick(reason=moderation_audit_reason(interaction, reason))
    await send_mod_confirmation(interaction, "Member kicked", member, reason)


@bot.tree.command(name="timeout", description="Timeout a member for up to 28 days.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided",
):
    error = moderation_target_error(interaction, member)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    reason = reason.strip()[:500] or "No reason provided"
    await interaction.response.defer(ephemeral=True, thinking=True)
    await member.timeout(
        timedelta(minutes=minutes),
        reason=moderation_audit_reason(interaction, reason)
    )
    await send_mod_confirmation(
        interaction, "Member timed out", member, reason,
        details=f"Duration: {minutes} minute(s)."
    )


@bot.tree.command(name="untimeout", description="Remove a member's timeout.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    error = moderation_target_error(interaction, member)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    reason = reason.strip()[:500] or "No reason provided"
    await interaction.response.defer(ephemeral=True, thinking=True)
    await member.timeout(None, reason=moderation_audit_reason(interaction, reason))
    await send_mod_confirmation(interaction, "Timeout removed", member, reason, color=GREEN)


@bot.tree.command(name="warn", description="Issue a warning and save it to the member's history.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    error = moderation_target_error(interaction, member)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return
    reason = reason.strip()[:500]
    if not reason:
        await interaction.response.send_message("A warning reason is required.", ephemeral=True)
        return
    guild_key, member_key = str(interaction.guild.id), str(member.id)
    guild_warnings = bot.store.data["warnings"].setdefault(guild_key, {})
    member_warnings = guild_warnings.setdefault(member_key, [])
    member_warnings.append({
        "reason": reason,
        "moderator_id": interaction.user.id,
        "timestamp": int(time.time()),
    })
    # Keep the latest 50 entries per member to keep the JSON file compact.
    del member_warnings[:-50]
    await bot.store.save()
    await write_modlog(
        interaction.guild, "Member warned", f"{member} ({member.mention})", member.id,
        interaction.user, reason, details=f"Warning count: {len(member_warnings)}", color=0xF3C969
    )
    await interaction.response.send_message(
        embed=embed("⚠️ Warning issued", f"**Member:** {member.mention}\n**Warning count:** {len(member_warnings)}\n**Reason:** {reason}", 0xF3C969),
        ephemeral=True
    )


@bot.tree.command(name="warnings", description="View a member's saved warning history.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    items = bot.store.data["warnings"].get(str(interaction.guild.id), {}).get(str(member.id), [])
    if not items:
        await interaction.response.send_message(f"{member.mention} has no saved warnings.", ephemeral=True)
        return
    lines = []
    for index, item in enumerate(items[-10:], start=max(1, len(items) - 9)):
        timestamp = item.get("timestamp")
        date_text = f"<t:{int(timestamp)}:R>" if isinstance(timestamp, int) else "Date unavailable"
        reason_text = str(item.get("reason", "No reason recorded"))[:250]
        moderator_id = item.get("moderator_id")
        staff_text = f" • by <@{moderator_id}>" if isinstance(moderator_id, int) else ""
        lines.append(f"**#{index}** {date_text}{staff_text}\n{reason_text}")
    e = embed(f"⚠️ Warning history • {member}", "\n\n".join(lines), 0xF3C969)
    e.set_footer(text=f"Showing latest {min(10, len(items))} of {len(items)} warning(s)")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Clear all saved warnings for a member.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member, reason: str = "Warnings cleared by staff"):
    guild_warnings = bot.store.data["warnings"].setdefault(str(interaction.guild.id), {})
    removed = len(guild_warnings.pop(str(member.id), []))
    await bot.store.save()
    reason = reason.strip()[:500] or "Warnings cleared by staff"
    await write_modlog(
        interaction.guild, "Warnings cleared", f"{member} ({member.mention})", member.id,
        interaction.user, reason, details=f"Removed warning entries: {removed}", color=GREEN
    )
    await interaction.response.send_message(
        f"Cleared **{removed}** warning(s) for {member.mention}.", ephemeral=True
    )


@bot.tree.command(name="purge", description="Delete up to 100 recent messages in this channel.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Use this command in a text channel or thread.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    deleted = await channel.purge(limit=amount, reason=f"Purged by {interaction.user} ({interaction.user.id})")
    await write_modlog(
        interaction.guild, "Messages purged", channel.mention, channel.id,
        interaction.user, "Channel cleanup", details=f"Messages deleted: {len(deleted)}", color=0xF3C969
    )
    await interaction.followup.send(f"🧹 Deleted **{len(deleted)}** message(s) in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="slowmode", description="Set a text channel's slowmode in seconds.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def slowmode(
    interaction: discord.Interaction,
    seconds: app_commands.Range[int, 0, 21600],
    channel: discord.TextChannel | None = None,
):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Choose a normal text channel for slowmode.", ephemeral=True)
        return
    await target.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {interaction.user} ({interaction.user.id})")
    await write_modlog(
        interaction.guild, "Slowmode changed", target.mention, target.id,
        interaction.user, "Channel slowmode updated", details=f"New slowmode: {seconds} second(s)", color=0xF3C969
    )
    await interaction.response.send_message(
        f"⏱️ Slowmode in {target.mention} is now **{seconds} second(s)**.", ephemeral=True
    )


async def set_channel_lock(interaction: discord.Interaction, channel: discord.TextChannel | None, locked: bool):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Choose a normal text channel.", ephemeral=True)
        return
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False if locked else None
    await target.set_permissions(
        interaction.guild.default_role, overwrite=overwrite,
        reason=f"Channel {'locked' if locked else 'unlocked'} by {interaction.user} ({interaction.user.id})"
    )
    action = "Channel locked" if locked else "Channel unlocked"
    await write_modlog(
        interaction.guild, action, target.mention, target.id, interaction.user,
        "Channel permissions updated", color=RED if locked else GREEN
    )
    await interaction.response.send_message(
        f"{'🔒' if locked else '🔓'} {target.mention} has been **{'locked' if locked else 'unlocked'}**.", ephemeral=True
    )


@bot.tree.command(name="lock", description="Prevent the @everyone role from sending messages in a channel.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    await set_channel_lock(interaction, channel, True)


@bot.tree.command(name="unlock", description="Restore the @everyone send-message permission in a channel.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    await set_channel_lock(interaction, channel, False)


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

"""Small runtime smoke checks for the security upgrade and discord.py API surface."""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Running a script by path puts scripts/ rather than the repository root on
# sys.path. Add the root so the extensions package imports consistently in CI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from extensions import automod, community, leveling, pvpclubtiers, security


def main() -> None:
    # Gateway intent fields and AutoMod constructors used by the live code.
    intents = discord.Intents.default()
    intents.auto_moderation_configuration = True
    intents.auto_moderation_execution = True
    assert intents.auto_moderation_configuration
    assert intents.auto_moderation_execution

    discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)
    discord.AutoModRuleAction(
        type=discord.AutoModRuleActionType.send_alert_message,
        channel_id=123456789012345678,
    )
    spam_trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)
    mention_trigger = discord.AutoModTrigger(mention_limit=5, mention_raid_protection=True)
    assert spam_trigger.type is discord.AutoModRuleTriggerType.spam
    assert mention_trigger.type is discord.AutoModRuleTriggerType.mention_spam

    # Validate the command groups are actually importable and contain the
    # expected entry points before slash-command sync is attempted.
    automod_names = {command.name for command in automod.automod_group.commands}
    assert {"nsfw", "sync", "native-status"}.issubset(automod_names)

    temporary_names = {command.name for command in security.temporaryrole_group.commands}
    assert {"grant", "remove", "list"}.issubset(temporary_names)

    raid_names = {command.name for command in security.raid_group.commands}
    assert {"setup", "enable", "disable", "status"}.issubset(raid_names)

    # Duration validation must reject malformed and over-limit values.
    assert security._parse_duration("10m") == 600
    assert security._parse_duration("2h") == 7200
    assert security._parse_duration("7d") == 604800
    assert security._parse_duration("30d") == 2592000
    assert security._parse_duration("31d") is None
    assert security._parse_duration("0m") is None
    assert security._parse_duration("1 week") is None

    # Every generated arithmetic challenge has an answer matching its expression.
    for _ in range(100):
        question, answer = security._challenge()
        left, operator, right = question.split()
        expected = {
            "+": int(left) + int(right),
            "-": int(left) - int(right),
            "×": int(left) * int(right),
        }[operator]
        assert str(expected) == answer
        digest = security._answer_hash("smoke-test-salt", answer)
        assert digest == security._answer_hash("smoke-test-salt", str(expected))
        assert digest != security._answer_hash("different-salt", answer)

    # Ensure the existing extension modules still import alongside new code.
    assert community.CommunityCog
    assert leveling.LevelingCog
    assert pvpclubtiers.PvPClubTiersCog
    assert automod.AutoModCog
    assert security.SecurityCog
    print("Discord API, command-group, duration and arithmetic smoke checks passed.")


if __name__ == "__main__":
    main()

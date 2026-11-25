import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import activate

from thm_bot.discord_app import AchievementBot, tracking_error_text

ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")


async def test_link_instructions_and_command_metadata_are_english(config, db):
    bot = AchievementBot(config, db)
    bot.tracker = SimpleNamespace(begin=AsyncMock(return_value="THM-verify-test-code"))
    interaction = SimpleNamespace(
        guild_id=1,
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    for command in bot.tree.get_commands():
        assert command.description and not ARABIC.search(command.description)
        for parameter in command.parameters:
            assert not ARABIC.search(parameter.description)
    await bot.tree.get_command("link").callback(interaction, "ysami5061")
    text = interaction.followup.send.call_args.args[0]
    assert "Biography" in text and "15 minutes" in text
    assert "THM-verify-test-code" in text
    assert not ARABIC.search(text)
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


async def test_profile_does_not_redisplay_legacy_localized_errors(config, db):
    link_id = activate(db)
    # An error saved by an earlier release, represented without localized source text.
    old_error = "TryHackMe HTTP 429 \u0623\u0639\u0627\u062f"
    with db.conn:
        db.conn.execute("UPDATE links SET last_error=? WHERE id=?", (old_error, link_id))
    bot = AchievementBot(config, db)
    interaction = SimpleNamespace(
        guild_id=1,
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await bot.tree.get_command("profile").callback(interaction)
    view = bot.profile_view(1, interaction.user)
    assert view["status"] == "RETRY SCHEDULED" and "HTTP 429" in view["note"]
    assert not ARABIC.search(view["note"])
    sent = interaction.followup.send.call_args
    assert not sent.args and "content" not in sent.kwargs
    assert sent.kwargs["file"].filename == "profile-2.png"
    assert sent.kwargs["ephemeral"] is True
    sent.kwargs["file"].close()
    assert db.link(1, 2)["last_error"] == old_error


def test_legacy_status_fallback_preserves_current_english_errors():
    assert tracking_error_text("The profile is private.") == "The profile is private."
    assert tracking_error_text("\u062e\u0637\u0623") == "An earlier check failed. A retry is scheduled."

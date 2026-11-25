from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from thm_bot.card import picture, render_card
from thm_bot.discord_app import AchievementBot, DiscordSender, require_admin
from thm_bot.models import Room, THMError


def test_card_long_unicode_corrupt_and_missing_images():
    valid = BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 128)).save(valid, "PNG")
    for avatar, room in [(None, None), (b"corrupt", b"not an image"), (valid.getvalue(), valid.getvalue())]:
        data = render_card(
            "عضو ديسكورد " * 30, "username_" * 25, "VeryLongRoomName" * 60, avatar, room, example=True
        )
        image = Image.open(BytesIO(data))
        assert image.size == (1400, 440)
        assert image.format == "PNG"


def test_runtime_manage_guild_check():
    for guild, manage in [(None, True), (object(), False)]:
        with pytest.raises(THMError):
            require_admin(SimpleNamespace(guild=guild, permissions=SimpleNamespace(manage_guild=manage)))
    require_admin(SimpleNamespace(guild=object(), permissions=SimpleNamespace(manage_guild=True)))


def test_registered_commands_and_minimal_intents(config, db):
    bot = AchievementBot(config, db)
    assert {c.name for c in bot.tree.get_commands()} == {
        "setup",
        "link",
        "verify",
        "unlink",
        "profile",
        "myprofile",
        "test",
    }
    assert not bot.intents.message_content
    assert not bot.intents.members
    assert not bot.intents.presences
    assert bot.tree.get_command("setup").default_permissions.manage_guild


async def test_external_text_cannot_ping_roles_everyone_or_other_users(config):
    channel = AsyncMock()
    channel.send.return_value = SimpleNamespace(id=444)
    member = SimpleNamespace(id=123, display_name="Name", display_avatar=SimpleNamespace(url=""))
    bot = SimpleNamespace(api=SimpleNamespace(image=AsyncMock(return_value=None)), config=config)
    sender = DiscordSender(bot)
    sender.channel = AsyncMock(return_value=channel)
    event = {"guild_id": 1, "user_id": 123, "username": "ysami5061", "avatar": "", "event_id": "abc"}
    await sender.send(event, Room("room", "@everyone <@&777> <@999>"), 20, interaction_user=member)
    kwargs = channel.send.call_args.kwargs
    mentions = kwargs["allowed_mentions"].to_dict()
    assert mentions.get("users", []) == []
    assert "everyone" not in mentions["parse"] and "roles" not in mentions["parse"]
    assert kwargs["content"] == "<https://tryhackme.com/room/room>"
    assert kwargs["file"].filename == "thm-abc.png"
    kwargs["file"].close()


async def test_announcement_contains_only_room_link_and_card_with_room_artwork(config):
    source = BytesIO()
    Image.new("RGB", (32, 32), (224, 35, 45)).save(source, "PNG")
    room_art = source.getvalue()
    channel = AsyncMock()
    channel.send.return_value = SimpleNamespace(id=555)
    member = SimpleNamespace(id=123, display_name="Example Member", display_avatar=SimpleNamespace(url=""))
    api = SimpleNamespace(image=AsyncMock(side_effect=[room_art, room_art]))
    sender = DiscordSender(SimpleNamespace(api=api, config=config))
    sender.channel = AsyncMock(return_value=channel)
    event = {"guild_id": 1, "user_id": 123, "username": "example", "avatar": "", "event_id": "art"}
    room = Room("redisl33t", "Red", "https://cdn-images.tryhackme.com/red.png")
    await sender.send(event, room, 20, interaction_user=member)
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] == "<https://tryhackme.com/room/redisl33t>"
    assert not kwargs["allowed_mentions"].users
    assert kwargs["file"].filename == "thm-art.png"
    with Image.open(kwargs["file"].fp) as card:
        assert card.getpixel((1200, 240)) == (224, 35, 45)
    api.image.assert_any_await(room.image_url)
    kwargs["file"].close()


@pytest.mark.parametrize("format_name,accepted", [("JPEG", True), ("PNG", False)])
def test_jpeg_thumbnail_decode_keeps_pixel_limits_for_other_formats(monkeypatch, format_name, accepted):
    source = BytesIO()
    Image.new("RGB", (200, 100), (210, 20, 30)).save(source, format_name)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 16000)
    rendered = picture(source.getvalue(), (8, 8), "ROOM")
    red, green, blue = rendered.getpixel((4, 4))
    assert (red > 190 and green < 50 and blue < 60) is accepted

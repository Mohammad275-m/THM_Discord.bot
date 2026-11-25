import asyncio
import json
import time
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import activate, snapshot
from PIL import Image

from thm_bot.db import Database
from thm_bot.discord_app import AchievementBot
from thm_bot.models import Profile, Room, Snapshot
from thm_bot.profile_card import check_label, render_profile_card
from thm_bot.service import Tracker


def make_due(db, link_id):
    with db.conn:
        db.conn.execute("UPDATE links SET next_check=0 WHERE id=?", (link_id,))


async def stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_worker_detects_and_sends_without_any_profile_command(db, config):
    link_id = activate(db)
    db.setup(1, 10)
    make_due(db, link_id)
    api = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot("old", "new")))
    delivered = asyncio.Event()

    async def send(*args):
        delivered.set()
        return 12345

    sender = SimpleNamespace(send=AsyncMock(side_effect=send), find=AsyncMock())
    tracker = Tracker(db, api, config, sender, tick_interval=0.01)
    task = asyncio.create_task(tracker.run())
    try:
        await asyncio.wait_for(delivered.wait(), 1)
        await asyncio.sleep(0.02)
        assert db.stats(link_id) == (2, 1, 0)
        assert db.link_by_id(link_id)["next_check"] > time.time()
        sender.send.assert_awaited_once()
    finally:
        await stop(task)


async def test_slow_poll_cannot_block_queued_delivery(db, config):
    link_id = activate(db)
    db.setup(1, 10)
    db.observe(link_id, snapshot("old", "queued"), 600)
    make_due(db, link_id)
    started, released, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def scan(name):
        started.set()
        await released.wait()
        return snapshot("old", "queued")

    async def send(*args):
        await started.wait()
        delivered.set()
        return 77

    tracker = Tracker(
        db,
        SimpleNamespace(snapshot=AsyncMock(side_effect=scan)),
        config,
        SimpleNamespace(send=AsyncMock(side_effect=send), find=AsyncMock()),
        tick_interval=0.01,
    )
    task = asyncio.create_task(tracker.run())
    try:
        await asyncio.wait_for(delivered.wait(), 1)
        assert not released.is_set()
        assert link_id in tracker.active_checks
    finally:
        released.set()
        await stop(task)


async def test_full_scan_timeout_preserves_history_and_schedules_retry(db, config):
    link_id = activate(db)
    original = db.link_by_id(link_id)

    async def blocked(name):
        await asyncio.Event().wait()

    tracker = Tracker(
        db,
        SimpleNamespace(snapshot=AsyncMock(side_effect=blocked)),
        replace(config, poll_timeout=0.03),
        AsyncMock(),
    )
    await tracker.poll(original)
    after = db.link_by_id(link_id)
    assert "timed out" in after["last_error"]
    assert after["last_success"] == original["last_success"]
    assert after["next_check"] > time.time()
    assert db.stats(link_id) == (1, 0, 0)
    assert link_id not in tracker.active_checks


async def test_unlink_during_scan_does_not_restore_deleted_binding(db, config):
    link_id = activate(db)
    started, released = asyncio.Event(), asyncio.Event()

    async def scan(name):
        started.set()
        await released.wait()
        return snapshot("old", "new")

    tracker = Tracker(db, SimpleNamespace(snapshot=AsyncMock(side_effect=scan)), config, AsyncMock())
    task = asyncio.create_task(tracker.poll(db.link_by_id(link_id)))
    await started.wait()
    await asyncio.wait_for(tracker.unlink(1, 2), 0.2)
    released.set()
    await task
    assert db.link(1, 2) is None
    assert not db.events()


def test_public_profile_cache_survives_restart_without_biography(db, config):
    profile = Profile(
        "ysami5061",
        "",
        "private verification code",
        1,
        rank=123,
        total_points=500,
        streak=4,
        level=8,
        badges_count=3,
    )
    link_id = activate(db, Snapshot(profile, (Room("old", "Old"),)))
    stored = db.profile_data(link_id)
    assert stored["total_points"] == 500
    assert "about" not in stored and "private verification code" not in json.dumps(stored)
    # Existing v1 installations have no cached stats until the next complete scan.
    with db.conn:
        db.conn.execute("PRAGMA user_version=1")
    db.close()
    reopened = Database(config.database)
    try:
        assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert reopened.profile_data(link_id)["badges_count"] == 3
        assert reopened.stats(link_id) == (1, 0, 0)
    finally:
        reopened.close()


@pytest.mark.parametrize("command", ["profile", "myprofile"])
async def test_profile_aliases_return_only_private_png_without_polling(db, config, command):
    activate(db)
    bot = AchievementBot(config, db)
    bot.api = SimpleNamespace(snapshot=AsyncMock(), image=AsyncMock(return_value=None))
    interaction = SimpleNamespace(
        guild_id=1,
        user=SimpleNamespace(id=2, display_name="Example"),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await bot.tree.get_command(command).callback(interaction)
    sent = interaction.followup.send.call_args
    assert not sent.args and "content" not in sent.kwargs
    assert sent.kwargs["ephemeral"] is True
    with Image.open(sent.kwargs["file"].fp) as png:
        assert png.size == (1400, 760) and png.format == "PNG"
    sent.kwargs["file"].close()
    bot.api.snapshot.assert_not_awaited()


def test_overdue_and_active_check_labels_never_display_negative_countdowns():
    assert check_label(900, now=1000) == "Queued for automatic check"
    assert check_label(900, now=1000, checking=True) == "Checking now"
    assert check_label(1061, now=1000).startswith("In 1m 01s")
    assert check_label(None, now=1000, linked=False) == "Not scheduled"


def test_profile_card_unknown_values_and_long_names_render():
    data = render_profile_card(
        {
            "display_name": "Very long name " * 50,
            "username": "example",
            "status": "QUEUED FOR AUTOMATIC CHECK",
            "next_check": 900,
            "stats": {},
        },
        now=1000,
    )
    with Image.open(BytesIO(data)) as png:
        assert png.size == (1400, 760)


def test_os_metadata_uses_only_explicit_operating_system_tags():
    assert Room("a", "A", technology_tags=("Linux", "Metasploit")).operating_system == "Linux"
    assert Room("a", "A", technology_tags=("Windows", "linux")).operating_system == "Linux / Windows"
    assert Room("a", "A", technology_tags=("Red teaming",)).operating_system == "Not provided"


def test_genuine_v1_schema_without_profile_cache_migrates_without_losing_links(db, config):
    link_id = activate(db)
    with db.conn:
        db.conn.execute("DROP TABLE profile_cache")
        db.conn.execute("PRAGMA user_version=1")
    db.close()
    reopened = Database(config.database)
    try:
        assert reopened.link(1, 2)["id"] == link_id
        assert reopened.profile_data(link_id) == {}
        assert reopened.stats(link_id) == (1, 0, 0)
        reopened.observe(link_id, snapshot("old"), 600)
        assert reopened.profile_data(link_id)["completed_count"] == 1
    finally:
        reopened.close()


def test_old_queued_rooms_without_os_metadata_remain_readable(db):
    link_id = activate(db)
    db.observe(link_id, snapshot("old", "new"), 600)
    event = db.events()[0]
    with db.conn:
        db.conn.execute(
            "UPDATE outbox SET room_json=? WHERE event_id=?",
            (
                json.dumps({"code": "new", "title": "New", "image_url": "", "difficulty": "hard"}),
                event["event_id"],
            ),
        )
    room = db.room(db.events()[0])
    assert room.difficulty == "hard" and room.operating_system == "Not provided"

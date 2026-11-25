import time
from unittest.mock import AsyncMock

import pytest
from conftest import activate, snapshot

from thm_bot.db import Database
from thm_bot.models import THMError
from thm_bot.service import Tracker


def due_events(db):
    with db.conn:
        db.conn.execute("UPDATE outbox SET next_attempt=0")
    return db.events()


def test_baseline_new_completion_no_duplicates_and_restart(db, config):
    link_id = activate(db, snapshot("old-a", "old-b"))
    assert db.stats(link_id) == (2, 0, 0)
    assert not db.events()
    db.observe(link_id, snapshot("old-b", "new", "old-a"), 600)
    assert db.stats(link_id) == (3, 1, 1)
    db.observe(link_id, snapshot("new", "old-a", "old-b"), 600)
    event = db.events()[0]
    db.sent(event["event_id"], 999, 22)
    db.close()
    reopened = Database(config.database)
    try:
        assert reopened.link(1, 2)["username"] == "ysami5061"
        assert reopened.stats(link_id) == (3, 1, 0)
        assert reopened.observe(link_id, snapshot("old-b", "old-a", "new"), 600) == 0
    finally:
        reopened.close()


def test_equal_count_still_detects_new_id_and_keeps_historical_ids(db):
    link_id = activate(db, snapshot("old"))
    assert db.observe(link_id, snapshot("new"), 600) == 1
    assert db.stats(link_id) == (2, 1, 1)
    assert db.observe(link_id, snapshot("old", "new"), 600) == 0


def test_multiple_guilds_isolated_and_unlink_cascades(db):
    first = activate(db, guild=1)
    second = activate(db, guild=99)
    db.setup(1, 10)
    db.setup(99, 20)
    for key in (first, second):
        db.observe(key, snapshot("old", "new"), 600)
    db.unlink(1, 2)
    assert db.link(1, 2) is None
    assert db.channel(99) == 20
    assert db.channel(1) == 10
    assert db.stats(second) == (2, 1, 1)
    assert len(db.events()) == 1


async def test_verification_requires_correct_token_then_baselines(db, config):
    api = AsyncMock()
    tracker = Tracker(db, api, config, AsyncMock())
    token = await tracker.begin(1, 2, "ysami5061")
    assert len(token) > 32
    api.profile.return_value = snapshot("old", about="someone else's token").profile
    with pytest.raises(THMError, match="verification code"):
        await tracker.verify(1, 2)
    assert db.link(1, 2) is None
    assert not db.events()
    with db.conn:
        db.conn.execute("UPDATE pending_links SET last_attempt=0")
    api.profile.return_value = snapshot("old", about=f"Hello {token} thanks").profile
    api.snapshot.return_value = snapshot("old", "old-2")
    assert await tracker.verify(1, 2) == 2
    assert db.pending(1, 2) is None
    assert db.stats(db.link(1, 2)["id"]) == (2, 0, 0)


async def test_expired_code_failed_snapshot_and_verification_throttle(db, config):
    api = AsyncMock()
    tracker = Tracker(db, api, config, AsyncMock())
    token = await tracker.begin(1, 2, "ysami5061")
    api.profile.return_value = snapshot(about=token).profile
    api.snapshot.side_effect = THMError("Incomplete snapshot")
    with pytest.raises(THMError):
        await tracker.verify(1, 2)
    assert db.link(1, 2) is None
    with pytest.raises(THMError, match="30"):
        await tracker.verify(1, 2)
    with db.conn:
        db.conn.execute("UPDATE pending_links SET expires=0,last_attempt=0")
    with pytest.raises(THMError, match="valid"):
        await tracker.verify(1, 2)


async def test_api_error_preserves_last_success_and_seen_state(db, config):
    link_id = activate(db)
    row = db.link_by_id(link_id)
    api = AsyncMock()
    api.snapshot.side_effect = THMError("HTTP 429", 1200)
    await Tracker(db, api, config, AsyncMock()).poll(row)
    after = db.link_by_id(link_id)
    assert after["last_success"] == row["last_success"]
    assert after["next_check"] > time.time() + 1190
    assert after["last_error"] == "HTTP 429"
    assert db.stats(link_id) == (1, 0, 0)


async def test_send_error_retries_and_restart_does_not_duplicate(db, config):
    link_id = activate(db)
    db.setup(1, 10)
    db.observe(link_id, snapshot("old", "new"), 600)
    sender = AsyncMock()
    sender.send.side_effect = OSError("Failed Discord request")
    tracker = Tracker(db, AsyncMock(), config, sender)
    await tracker.deliver(db.events()[0])
    assert db.stats(link_id) == (2, 1, 1)
    assert not db.events()  # backoff is persisted
    db.close()
    reopened = Database(config.database)
    try:
        event = due_events(reopened)[0]
        sender.find.return_value = None
        sender.send.side_effect = None
        sender.send.return_value = 12345
        await Tracker(reopened, AsyncMock(), config, sender).deliver(event)
        assert reopened.stats(link_id) == (2, 1, 0)
        assert sender.send.await_count == 2
        assert not reopened.events()
    finally:
        reopened.close()


async def test_crash_after_send_before_commit_reconciles_history(db, config):
    link_id = activate(db)
    db.setup(1, 10)
    db.observe(link_id, snapshot("old", "new"), 600)
    event = db.events()[0]
    # Simulate crash at exactly the vulnerable point: sent externally, not committed locally.
    db.mark_attempt(event["event_id"], 10)
    db.setup(1, 11)  # channel changed after crash; must search the previous channel
    sender = AsyncMock()
    sender.find.return_value = 888
    await Tracker(db, AsyncMock(), config, sender).deliver(db.events()[0])
    sender.find.assert_awaited_once()
    assert sender.find.call_args.args[1] == 10
    sender.send.assert_not_awaited()
    assert db.stats(link_id) == (2, 1, 0)


async def test_unreadable_history_blocks_unsafe_retry(db, config):
    link_id = activate(db)
    db.setup(1, 10)
    db.observe(link_id, snapshot("old", "new"), 600)
    event = db.events()[0]
    db.mark_attempt(event["event_id"], 10)
    sender = AsyncMock()
    sender.find.side_effect = THMError("Missing Read Message History")
    await Tracker(db, AsyncMock(), config, sender).deliver(db.events()[0])
    sender.send.assert_not_awaited()
    assert db.stats(link_id) == (2, 1, 1)


async def test_no_channel_leaves_pending_then_recovers(db, config):
    link_id = activate(db)
    db.observe(link_id, snapshot("old", "new"), 600)
    sender = AsyncMock()
    tracker = Tracker(db, AsyncMock(), config, sender)
    await tracker.deliver(db.events()[0])
    sender.send.assert_not_awaited()
    db.setup(1, 10)
    sender.send.return_value = 345
    await tracker.deliver(due_events(db)[0])
    assert db.stats(link_id) == (2, 1, 0)


def test_pending_and_setup_persist_and_expired_request_cannot_activate(db, config):
    db.setup(1, 10)
    db.begin_link(1, 2, "ysami5061", "test", 900)
    pending = db.pending(1, 2)
    reopened = Database(config.database)
    try:
        assert reopened.channel(1) == 10
        assert reopened.pending(1, 2)["token"] == "test"
        reopened.begin_link(1, 2, "ysami5061", "new-token", 900)
        with pytest.raises(THMError):
            reopened.activate(pending, snapshot("a"), 600)
    finally:
        reopened.close()


async def test_stale_event_cannot_send_twice_or_send_after_unlink(db, config):
    link_id = activate(db)
    db.setup(1, 10)
    db.observe(link_id, snapshot("old", "new", "second"), 600)
    first, second = db.events()
    sender = AsyncMock()
    sender.send.return_value = 123
    tracker = Tracker(db, AsyncMock(), config, sender)
    await tracker.deliver(first)
    await tracker.deliver(first)
    sender.send.assert_awaited_once()
    await tracker.unlink(1, 2)
    await tracker.deliver(second)
    sender.send.assert_awaited_once()

import asyncio
import logging
import re
import secrets
import time

from .api import username
from .models import THMError

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, db, api, config, sender, *, tick_interval=2):
        self.db, self.api, self.config, self.sender = db, api, config, sender
        # Global cleanup uses this lock; member operations have independent locks.
        self.lock = asyncio.Lock()
        self.account_locks = {}
        self.active_checks = set()
        self.running = False
        self.tick_interval = tick_interval
        self.last_poll_tick = 0.0
        self.last_delivery_tick = 0.0

    def account_lock(self, guild_id, user_id):
        return self.account_locks.setdefault((guild_id, user_id), asyncio.Lock())

    async def begin(self, guild_id, user_id, name):
        async with self.account_lock(guild_id, user_id):
            name = username(name)
            # No need to query the API until ownership verification: limits spam and latency.
            token = "THM-verify-" + secrets.token_urlsafe(24)
            self.db.begin_link(guild_id, user_id, name, token, self.config.verify_ttl)
            return token

    async def verify(self, guild_id, user_id):
        async with self.account_lock(guild_id, user_id):
            pending = self.db.attempt_verification(guild_id, user_id)
            try:
                async with asyncio.timeout(self.config.poll_timeout):
                    profile = await self.api.profile(pending["username"])
                    if not re.search(
                        r"(?<![A-Za-z0-9_-])" + re.escape(pending["token"]) + r"(?![A-Za-z0-9_-])",
                        profile.about,
                    ):
                        raise THMError(
                            "The full verification code was not found in Biography. Save it on TryHackMe, then run /verify again."
                        )
                    snapshot = await self.api.snapshot(profile.username)
            except TimeoutError:
                raise THMError(
                    "Verification scan timed out. No account link was activated. Try again later.", 120
                ) from None
            self.db.activate(pending, snapshot, self.config.poll_interval)
            return len(snapshot.rooms)

    async def unlink(self, guild_id, user_id):
        async with self.account_lock(guild_id, user_id):
            self.db.unlink(guild_id, user_id)

    async def poll(self, link):
        if not self.db.link_by_id(link["id"]):
            return
        self.active_checks.add(link["id"])
        failure = None
        try:
            # Network waits never hold a member's delivery/unlink lock.
            snapshot = await asyncio.wait_for(self.api.snapshot(link["username"]), self.config.poll_timeout)
            async with self.account_lock(link["guild_id"], link["user_id"]):
                if not self.db.link_by_id(link["id"]):
                    return
                added = self.db.observe(link["id"], snapshot, self.config.poll_interval)
                if added:
                    log.info("Queued %d completions for binding %d", added, link["id"])
        except TimeoutError:
            failure = THMError(
                "The full TryHackMe scan timed out. A retry is scheduled; completion history was not changed.",
                120,
            )
        except THMError as error:
            failure = error
        except Exception as error:
            failure = THMError(f"The scan failed ({type(error).__name__}). A retry is scheduled.", 120)
        finally:
            self.active_checks.discard(link["id"])
        if failure is not None:
            async with self.account_lock(link["guild_id"], link["user_id"]):
                self.db.poll_failed(link["id"], failure, self.config.poll_interval)
            log.warning("THM poll failed for binding %d: %s", link["id"], failure)

    async def deliver(self, event):
        async with self.account_lock(event["guild_id"], event["user_id"]):
            # Refresh after waiting for the lock: stale callers may reference a sent/deleted event.
            event = self.db.event(event["event_id"])
            if event is None:
                return
            try:
                channel_id = self.db.channel(event["guild_id"])
                if not channel_id:
                    raise THMError("No achievement channel has been configured for this server.")
                # Before any retry, reconcile the deterministic filename in bot history.
                # The previous channel is checked even if /setup changed the destination.
                if event["attempts"]:
                    previous_channel = event["sent_channel_id"] or channel_id
                    existing = await self.sender.find(event, previous_channel)
                    if existing:
                        self.db.sent(event["event_id"], existing, previous_channel)
                        return
                self.db.mark_attempt(event["event_id"], channel_id)
                message_id = await asyncio.wait_for(
                    self.sender.send(event, self.db.room(event), channel_id), self.config.poll_timeout
                )
                self.db.sent(event["event_id"], message_id, channel_id)
            except Exception as error:
                # Only safe categories are persisted/logged; transport errors may contain URLs.
                safe = str(error) if isinstance(error, THMError) else type(error).__name__
                self.db.delivery_failed(event["event_id"], safe)
                log.warning("Delivery pending for event %s (%s)", event["event_id"], safe)

    async def poll_loop(self):
        while True:
            self.last_poll_tick = time.monotonic()
            # One account per turn. API pacing remains global across all pages.
            for link in self.db.due_links(limit=1):
                await self.poll(link)
            await asyncio.sleep(self.tick_interval)

    async def delivery_loop(self):
        while True:
            self.last_delivery_tick = time.monotonic()
            for event in self.db.events(limit=1):
                await self.deliver(event)
            await asyncio.sleep(self.tick_interval)

    async def run(self):
        self.running = True
        try:
            # Neither loop needs a slash command to run. A slow API scan cannot pause delivery.
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll_loop(), name="thm-polling")
                group.create_task(self.delivery_loop(), name="discord-delivery")
        finally:
            self.running = False
            self.active_checks.clear()

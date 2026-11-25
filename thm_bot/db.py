"""SQLite transactions: immutable observations plus a durable delivery outbox."""

import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .models import Room, Snapshot, THMError


class Database:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise RuntimeError("Unsupported database schema version")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_links (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            username TEXT NOT NULL, token TEXT NOT NULL,
            expires REAL NOT NULL, last_attempt REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            username TEXT NOT NULL COLLATE NOCASE, avatar TEXT NOT NULL,
            created REAL NOT NULL, last_success REAL NOT NULL,
            next_check REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            UNIQUE(guild_id, user_id), UNIQUE(guild_id, username)
        );
        CREATE TABLE IF NOT EXISTS seen_rooms (
            link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            code TEXT NOT NULL, PRIMARY KEY(link_id, code)
        );
        CREATE TABLE IF NOT EXISTS outbox (
            event_id TEXT PRIMARY KEY,
            link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            code TEXT NOT NULL, room_json TEXT NOT NULL,
            created REAL NOT NULL, next_attempt REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            message_id INTEGER, sent_channel_id INTEGER,
            UNIQUE(link_id, code)
        );
        CREATE INDEX IF NOT EXISTS due_links ON links(next_check);
        CREATE INDEX IF NOT EXISTS due_outbox ON outbox(next_attempt) WHERE message_id IS NULL;
        CREATE TABLE IF NOT EXISTS profile_cache (
            link_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE,
            public_json TEXT NOT NULL
        );
        PRAGMA user_version=2;
        """)

    def close(self):
        self.conn.close()

    def setup(self, guild_id, channel_id):
        with self.conn:
            self.conn.execute(
                "INSERT INTO guilds VALUES (?,?) ON CONFLICT(guild_id) "
                "DO UPDATE SET channel_id=excluded.channel_id",
                (guild_id, channel_id),
            )

    def channel(self, guild_id):
        row = self.conn.execute("SELECT channel_id FROM guilds WHERE guild_id=?", (guild_id,)).fetchone()
        return row[0] if row else None

    def link(self, guild_id, user_id):
        return self.conn.execute(
            "SELECT * FROM links WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()

    def link_by_id(self, link_id):
        return self.conn.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()

    def pending(self, guild_id, user_id):
        return self.conn.execute(
            "SELECT * FROM pending_links WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()

    def begin_link(self, guild_id, user_id, name, token, ttl):
        if self.link(guild_id, user_id):
            raise THMError(
                "You already have an active account link. Use /unlink before linking another account."
            )
        with self.conn:
            self.conn.execute("DELETE FROM pending_links WHERE expires < ?", (time.time(),))
            self.conn.execute(
                "INSERT INTO pending_links VALUES (?,?,?,?,?,0) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET username=excluded.username, "
                "token=excluded.token, expires=excluded.expires,last_attempt=0",
                (guild_id, user_id, name, token, time.time() + ttl),
            )

    def attempt_verification(self, guild_id, user_id):
        pending = self.pending(guild_id, user_id)
        if not pending or pending["expires"] <= time.time():
            raise THMError("No valid linking request was found. Start with /link.")
        if time.time() - pending["last_attempt"] < 30:
            raise THMError("Wait 30 seconds between verification attempts.")
        with self.conn:
            self.conn.execute(
                "UPDATE pending_links SET last_attempt=? WHERE guild_id=? AND user_id=?",
                (time.time(), guild_id, user_id),
            )
        return pending

    def activate(self, pending, snapshot: Snapshot, interval):
        current = self.pending(pending["guild_id"], pending["user_id"])
        if not current or current["token"] != pending["token"] or current["expires"] <= time.time():
            raise THMError("Your verification code expired or changed. Run /link again.")
        now = time.time()
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO links(guild_id,user_id,username,avatar,created,last_success,next_check) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        pending["guild_id"],
                        pending["user_id"],
                        snapshot.profile.username,
                        snapshot.profile.avatar,
                        now,
                        now,
                        now + interval,
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO seen_rooms VALUES (?,?)", ((cur.lastrowid, r.code) for r in snapshot.rooms)
                )
                self.cache_profile(cur.lastrowid, snapshot.profile)
                self.conn.execute(
                    "DELETE FROM pending_links WHERE guild_id=? AND user_id=?",
                    (pending["guild_id"], pending["user_id"]),
                )
        except sqlite3.IntegrityError:
            raise THMError(
                "This Discord member or TryHackMe account is already linked in this server."
            ) from None
        return cur.lastrowid

    def unlink(self, guild_id, user_id):
        with self.conn:
            self.conn.execute("DELETE FROM links WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            self.conn.execute("DELETE FROM pending_links WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def remove_guild(self, guild_id):
        with self.conn:
            for table in ("links", "pending_links", "guilds"):
                self.conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))

    def due_links(self, limit=10):
        return self.conn.execute(
            "SELECT * FROM links WHERE next_check<=? ORDER BY next_check,id LIMIT ?", (time.time(), limit)
        ).fetchall()

    def observe(self, link_id, snapshot: Snapshot, interval):
        if not self.link_by_id(link_id):
            return 0
        seen = {r[0] for r in self.conn.execute("SELECT code FROM seen_rooms WHERE link_id=?", (link_id,))}
        # History is a union: a room later removed by THM must not erase a known completion.
        # Completeness of this snapshot is validated by THMClient, not by the count delta.
        now = time.time()
        new_rooms = [r for r in snapshot.rooms if r.code not in seen]
        with self.conn:
            for room in new_rooms:
                self.conn.execute("INSERT INTO seen_rooms VALUES (?,?)", (link_id, room.code))
                self.conn.execute(
                    "INSERT INTO outbox(event_id,link_id,code,room_json,created,next_attempt) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, link_id, room.code, json.dumps(asdict(room)), now, now),
                )
            self.conn.execute(
                "UPDATE links SET avatar=?,last_success=?,next_check=?,failures=0,last_error='' WHERE id=?",
                (snapshot.profile.avatar, now, now + interval, link_id),
            )
            self.cache_profile(link_id, snapshot.profile)
        return len(new_rooms)

    def cache_profile(self, link_id, profile):
        self.conn.execute(
            "INSERT INTO profile_cache VALUES (?,?) ON CONFLICT(link_id) "
            "DO UPDATE SET public_json=excluded.public_json",
            (link_id, json.dumps(profile.public_data())),
        )

    def profile_data(self, link_id):
        row = self.conn.execute(
            "SELECT public_json FROM profile_cache WHERE link_id=?", (link_id,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def poll_failed(self, link_id, error, interval):
        link = self.link_by_id(link_id)
        if link:
            delay = max(
                interval, getattr(error, "retry_after", 60), min(21600, 60 * 2 ** min(link["failures"], 9))
            )
            with self.conn:
                self.conn.execute(
                    "UPDATE links SET failures=failures+1,last_error=?,next_check=? WHERE id=?",
                    (str(error)[:500], time.time() + delay, link_id),
                )

    def events(self, limit=10):
        return self.conn.execute(
            "SELECT o.*,l.guild_id,l.user_id,l.username,l.avatar FROM outbox o JOIN links l ON l.id=o.link_id "
            "WHERE o.message_id IS NULL AND o.next_attempt<=? ORDER BY o.created,o.event_id LIMIT ?",
            (time.time(), limit),
        ).fetchall()

    def event(self, event_id):
        return self.conn.execute(
            "SELECT o.*,l.guild_id,l.user_id,l.username,l.avatar FROM outbox o JOIN links l ON l.id=o.link_id "
            "WHERE o.event_id=? AND o.message_id IS NULL",
            (event_id,),
        ).fetchone()

    def mark_attempt(self, event_id, channel_id):
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET attempts=attempts+1,sent_channel_id=? WHERE event_id=?",
                (channel_id, event_id),
            )

    def delivery_failed(self, event_id, error):
        row = self.conn.execute("SELECT attempts FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        if row:
            delay = min(21600, 30 * 2 ** min(row[0], 10))
            with self.conn:
                self.conn.execute(
                    "UPDATE outbox SET last_error=?,next_attempt=? WHERE event_id=?",
                    (str(error)[:500], time.time() + delay, event_id),
                )

    def sent(self, event_id, message_id, channel_id):
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET message_id=?,sent_channel_id=?,last_error='' WHERE event_id=?",
                (message_id, channel_id, event_id),
            )

    def stats(self, link_id):
        row = self.conn.execute(
            "SELECT count(*),sum(message_id IS NULL) FROM outbox WHERE link_id=?", (link_id,)
        ).fetchone()
        seen = self.conn.execute("SELECT count(*) FROM seen_rooms WHERE link_id=?", (link_id,)).fetchone()[0]
        return seen, row[0], row[1] or 0

    @staticmethod
    def room(event):
        data = json.loads(event["room_json"])
        data["technology_tags"] = tuple(data.get("technology_tags", ()))
        return Room(**data)

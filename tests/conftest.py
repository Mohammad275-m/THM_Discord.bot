from dataclasses import replace

import pytest

from thm_bot.config import Config
from thm_bot.db import Database
from thm_bot.models import Profile, Room, Snapshot


@pytest.fixture
def config(tmp_path):
    # No credentials or network. Fixtures model only the explicitly documented contract.
    return replace(Config(), database=tmp_path / "bot.sqlite3", request_interval=0)


@pytest.fixture
def db(config):
    database = Database(config.database)
    yield database
    database.close()


def snapshot(*codes, about="", count=None):
    return Snapshot(
        Profile("ysami5061", "", about, len(codes) if count is None else count),
        tuple(Room(code, f"Room {code}") for code in codes),
    )


def activate(db, snap=None, guild=1, user=2):
    db.begin_link(guild, user, "ysami5061", "test-token", 900)
    return db.activate(db.pending(guild, user), snap or snapshot("old"), 600)

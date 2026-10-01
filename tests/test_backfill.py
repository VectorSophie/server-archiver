# tests/test_backfill.py
from datetime import datetime, timezone

import discord

from archiver.backfill import backfill_all_pending, backfill_channel
from archiver.db import connect_catalog
from archiver.store import ShardStore
from tests.discord_fakes import FakeClient, FakeHistoryChannel, FakeMessage, FakeResponse

CREATED = datetime(2025, 10, 15, 3, 0, 0, tzinfo=timezone.utc)


def _seed_channel(catalog, channel_id="1", category_id="10"):
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        f"('{channel_id}','general','text','{category_id}','{category_id}',0,"
        "'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT OR IGNORE INTO category_names (category_id, name, updated_utc) VALUES "
        f"('{category_id}','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute(f"INSERT INTO coverage (channel_id, status) VALUES ('{channel_id}', 'pending')")
    catalog.commit()


async def test_backfill_channel_archives_all_messages_and_marks_complete(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    messages = [
        FakeMessage(id=100 + i, channel_id=1, author_id=2, content=f"msg{i}", created_at=CREATED)
        for i in range(5)
    ]
    channel = FakeHistoryChannel(id=1, messages=messages)

    await backfill_channel(channel, catalog, store)

    row = catalog.execute("SELECT status, message_count, backfill_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "complete"
    assert row["message_count"] == 5
    assert row["backfill_checkpoint"] == "104"


async def test_backfill_channel_resumes_from_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET backfill_checkpoint='101' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    messages = [
        FakeMessage(id=100 + i, channel_id=1, author_id=2, content=f"msg{i}", created_at=CREATED)
        for i in range(5)
    ]
    channel = FakeHistoryChannel(id=1, messages=messages)

    await backfill_channel(channel, catalog, store)

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 3  # only ids 102, 103, 104 -- 100, 101 already archived


async def test_backfill_channel_forbidden_marks_inaccessible_and_does_not_raise(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    channel = FakeHistoryChannel(id=1, messages=[], forbidden=True)

    await backfill_channel(channel, catalog, store)  # must not raise

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "inaccessible"
    assert row["gap_reason"] is not None


async def test_backfill_channel_other_http_error_marks_failed(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    channel = FakeHistoryChannel(id=1, messages=[], http_error=True)

    await backfill_channel(channel, catalog, store)  # must not raise

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "failed"
    assert row["gap_reason"] is not None


async def test_backfill_channel_arbitrary_exception_marks_failed_and_does_not_raise(tmp_path, monkeypatch):
    """The docstring promises 'never raises' for ANY failure, not just
    discord.Forbidden/HTTPException -- this proves a genuinely unexpected
    exception (e.g. a bug in map_message) is isolated the same way."""
    import archiver.backfill as backfill_module

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    messages = [FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=CREATED)]
    channel = FakeHistoryChannel(id=1, messages=messages)

    def _boom(message):
        raise ValueError("simulated bug in map_message")

    monkeypatch.setattr(backfill_module, "map_message", _boom)

    await backfill_channel(channel, catalog, store)  # must not raise

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "failed"
    assert row["gap_reason"] is not None


async def test_backfill_all_pending_isolates_arbitrary_exception_to_one_channel(tmp_path, monkeypatch):
    """Proves the fix at the sweep level: a non-discord.py exception in
    one channel's backfill must not abort the other channel's progress
    (the bug this fix addresses: asyncio.gather with no
    return_exceptions=True previously let one bad channel abort everyone)."""
    import archiver.backfill as backfill_module

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    _seed_channel(catalog, channel_id="2", category_id="10")
    store = ShardStore(tmp_path, catalog)
    good_msg = FakeMessage(id=200, channel_id=2, author_id=2, content="ok", created_at=CREATED)
    client = FakeClient({
        1: FakeHistoryChannel(id=1, messages=[FakeMessage(id=100, channel_id=1, author_id=2, content="bad", created_at=CREATED)]),
        2: FakeHistoryChannel(id=2, messages=[good_msg]),
    })

    real_map_message = backfill_module.map_message

    def _boom_for_channel_1(message):
        if str(message.channel.id) == "1":
            raise ValueError("simulated bug in map_message")
        return real_map_message(message)

    monkeypatch.setattr(backfill_module, "map_message", _boom_for_channel_1)

    await backfill_all_pending(client, catalog, store, concurrency=2)  # must not raise

    statuses = {
        row["channel_id"]: row["status"]
        for row in catalog.execute("SELECT channel_id, status FROM coverage").fetchall()
    }
    assert statuses["1"] == "failed"
    assert statuses["2"] == "complete"  # channel 2 must have completed despite channel 1's failure


async def test_backfill_all_pending_runs_every_pending_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    _seed_channel(catalog, channel_id="2", category_id="10")
    store = ShardStore(tmp_path, catalog)
    msg1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=CREATED)
    msg2 = FakeMessage(id=200, channel_id=2, author_id=2, content="b", created_at=CREATED)
    client = FakeClient({
        1: FakeHistoryChannel(id=1, messages=[msg1]),
        2: FakeHistoryChannel(id=2, messages=[msg2]),
    })

    await backfill_all_pending(client, catalog, store, concurrency=2)

    statuses = {
        row["channel_id"]: row["status"]
        for row in catalog.execute("SELECT channel_id, status FROM coverage").fetchall()
    }
    assert statuses == {"1": "complete", "2": "complete"}


async def test_backfill_all_pending_skips_complete_and_inaccessible(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    _seed_channel(catalog, channel_id="2", category_id="10")
    catalog.execute("UPDATE coverage SET status='inaccessible' WHERE channel_id='2'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({})  # would KeyError if either channel were fetched

    await backfill_all_pending(client, catalog, store)  # must not raise / not touch either

    statuses = {
        row["channel_id"]: row["status"]
        for row in catalog.execute("SELECT channel_id, status FROM coverage").fetchall()
    }
    assert statuses == {"1": "complete", "2": "inaccessible"}


async def test_backfill_all_pending_marks_gone_channel_inaccessible_without_calling_backfill(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({}, errors={1: discord.NotFound(FakeResponse(), "Unknown Channel")})

    await backfill_all_pending(client, catalog, store)

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "inaccessible"
    assert row["gap_reason"] is not None


async def test_backfill_channel_populates_users_table(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    messages = [
        FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=CREATED,
                    author_name="alice"),
        FakeMessage(id=101, channel_id=1, author_id=3, content="b", created_at=CREATED,
                    author_name="bob"),
    ]
    channel = FakeHistoryChannel(id=1, messages=messages)

    await backfill_channel(channel, catalog, store)

    rows = {row["id"]: row["username"] for row in catalog.execute("SELECT id, username FROM users").fetchall()}
    assert rows == {"2": "alice", "3": "bob"}


class _FakeForumChannel:
    """Deliberately has no .history() -- discord.ForumChannel really has
    none (confirmed: hasattr(discord.ForumChannel, 'history') is False).
    A forum channel is a container; only its threads carry messages."""
    def __init__(self, id):
        self.id = id


async def test_backfill_channel_skips_forum_channel_with_no_history_method(tmp_path):
    """Regression: a live run against a real server crashed with
    'ForumChannel' object has no attribute 'history' the first time
    backfill_all_pending swept a forum channel's own coverage row
    (forum channels are containers, discovered by Stage 2 alongside
    their threads, but carry no messages of their own)."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    channel = _FakeForumChannel(id=1)

    await backfill_channel(channel, catalog, store)  # must not raise

    row = catalog.execute("SELECT status, message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "complete"
    assert row["message_count"] == 0

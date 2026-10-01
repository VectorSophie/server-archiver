from datetime import datetime, timezone
from pathlib import Path

from archiver.db import connect_catalog
from archiver.discord_message import map_message
from archiver.store import ShardStore, UNCATEGORIZED_NAME, month_bucket, sanitize_folder_name, write_message
from tests.discord_fakes import FakeMessage

SEOUL_MIDNIGHT_UTC = datetime(2025, 10, 15, 15, 0, 0, tzinfo=timezone.utc)  # 2025-10-16 00:00 KST


def test_month_bucket_uses_seoul_timezone():
    assert month_bucket(SEOUL_MIDNIGHT_UTC) == "2025-10"
    just_before = datetime(2025, 10, 15, 14, 59, 59, tzinfo=timezone.utc)  # 2025-10-15 23:59:59 KST
    assert month_bucket(just_before) == "2025-10"
    just_after = datetime(2025, 10, 15, 15, 0, 1, tzinfo=timezone.utc)  # 2025-10-16 00:00:01 KST
    assert month_bucket(just_after) == "2025-10"


def test_month_bucket_crosses_into_next_month():
    end_of_oct_kst = datetime(2025, 10, 31, 15, 0, 1, tzinfo=timezone.utc)  # 2025-11-01 00:00:01 KST
    assert month_bucket(end_of_oct_kst) == "2025-11"


def test_sanitize_folder_name_strips_invalid_windows_chars():
    assert sanitize_folder_name("normal name") == "normal name"
    assert sanitize_folder_name('weird:name/with*bad<chars>') == "weird_name_with_bad_chars_"
    assert sanitize_folder_name("") == UNCATEGORIZED_NAME


def test_shard_store_resolves_category_folder(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.commit()

    store = ShardStore(tmp_path, catalog)
    shard_conn = store.get_shard("1", SEOUL_MIDNIGHT_UTC)

    row = shard_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    assert row is not None
    assert (tmp_path / "Friends" / "2025-10.sqlite").exists()

    catalog_row = catalog.execute(
        "SELECT shard_path, category_id FROM channel_month_shard WHERE channel_id='1' AND yyyymm='2025-10'"
    ).fetchone()
    assert catalog_row["shard_path"] == "Friends/2025-10.sqlite"
    assert catalog_row["category_id"] == "10"


def test_shard_store_uses_uncategorized_sentinel_for_channel_with_no_category(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('2','loose-channel','text',NULL,NULL,0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.commit()

    store = ShardStore(tmp_path, catalog)
    store.get_shard("2", SEOUL_MIDNIGHT_UTC)

    assert (tmp_path / "Uncategorized" / "2025-10.sqlite").exists()
    catalog_row = catalog.execute(
        "SELECT category_id FROM channel_month_shard WHERE channel_id='2'"
    ).fetchone()
    assert catalog_row["category_id"] == "uncategorized"


def test_shard_store_reuses_cached_connection(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    conn_a = store.get_shard("1", SEOUL_MIDNIGHT_UTC)
    conn_b = store.get_shard("1", SEOUL_MIDNIGHT_UTC)

    assert conn_a is conn_b


def test_shard_store_keeps_old_months_path_after_category_rename(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    store.get_shard("1", SEOUL_MIDNIGHT_UTC)

    catalog.execute(
        "UPDATE category_names SET name='Renamed' WHERE category_id='10'"
    )
    catalog.commit()
    store2 = ShardStore(tmp_path, catalog)
    store2.get_shard("1", SEOUL_MIDNIGHT_UTC)  # same channel, same month, after rename

    assert (tmp_path / "Friends" / "2025-10.sqlite").exists()
    assert not (tmp_path / "Renamed" / "2025-10.sqlite").exists()


def test_write_message_is_idempotent(tmp_path: Path):
    from archiver.db import connect_shard
    shard_conn = connect_shard(tmp_path / "2025-10.sqlite")
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=SEOUL_MIDNIGHT_UTC)
    mapped = map_message(msg)

    write_message(shard_conn, mapped)
    write_message(shard_conn, mapped)  # replay must not duplicate
    shard_conn.commit()

    count = shard_conn.execute("SELECT COUNT(*) FROM messages WHERE id='100'").fetchone()[0]
    assert count == 1


def test_write_message_upserts_all_child_tables(tmp_path: Path):
    from archiver.db import connect_shard
    from tests.discord_fakes import FakePoll, FakePollAnswer
    shard_conn = connect_shard(tmp_path / "2025-10.sqlite")
    poll = FakePoll(question="Best?", answers=[FakePollAnswer(id=1, text="A", vote_count=1)])
    msg = FakeMessage(id=101, channel_id=1, author_id=2, content="", created_at=SEOUL_MIDNIGHT_UTC, poll=poll)
    mapped = map_message(msg)

    write_message(shard_conn, mapped)
    shard_conn.commit()

    assert shard_conn.execute("SELECT 1 FROM polls WHERE message_id='101'").fetchone() is not None
    assert shard_conn.execute(
        "SELECT vote_count FROM poll_answers WHERE message_id='101' AND answer_id=1"
    ).fetchone()["vote_count"] == 1


def test_commit_page_advances_checkpoint_and_span(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    from archiver.store import commit_page
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    m2 = FakeMessage(id=101, channel_id=1, author_id=2, content="b", created_at=SEOUL_MIDNIGHT_UTC)
    pages = [(m1, map_message(m1)), (m2, map_message(m2))]

    commit_page(store, catalog, "1", pages, oldest_id="100", newest_id="101")

    row = catalog.execute(
        "SELECT backfill_checkpoint, oldest_message_id, newest_message_id, message_count "
        "FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["backfill_checkpoint"] == "101"
    assert row["oldest_message_id"] == "100"
    assert row["newest_message_id"] == "101"
    assert row["message_count"] == 2


def test_commit_page_oldest_message_id_not_overwritten_by_later_pages(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    from archiver.store import commit_page
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    commit_page(store, catalog, "1", [(m1, map_message(m1))], oldest_id="100", newest_id="100")

    m2 = FakeMessage(id=200, channel_id=1, author_id=2, content="b", created_at=SEOUL_MIDNIGHT_UTC)
    commit_page(store, catalog, "1", [(m2, map_message(m2))], oldest_id="200", newest_id="200")

    row = catalog.execute(
        "SELECT oldest_message_id, newest_message_id FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["oldest_message_id"] == "100"  # first page's oldest, never overwritten
    assert row["newest_message_id"] == "200"  # advances with each page


def test_commit_page_crash_between_shard_and_checkpoint_is_replay_safe(tmp_path: Path):
    """spec §4.1: a crash between a shard's commit and the catalog
    checkpoint commit must be safely replayable -- no duplicate rows,
    checkpoint advances exactly once when the page is finally committed
    in full."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    from archiver.store import commit_page, write_message
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    m2 = FakeMessage(id=101, channel_id=1, author_id=2, content="b", created_at=SEOUL_MIDNIGHT_UTC)

    # Simulate a crash: write and commit the shard directly (as commit_page
    # would), but never advance the catalog checkpoint.
    shard_conn = store.get_shard("1", SEOUL_MIDNIGHT_UTC)
    write_message(shard_conn, map_message(m1))
    write_message(shard_conn, map_message(m2))
    shard_conn.commit()
    # (crash here -- catalog checkpoint was never touched)

    checkpoint_before = catalog.execute(
        "SELECT backfill_checkpoint FROM coverage WHERE channel_id='1'"
    ).fetchone()["backfill_checkpoint"]
    assert checkpoint_before is None  # confirms the "crash" left no checkpoint

    # Replay: the real backfill loop would re-fetch and re-commit the same page.
    pages = [(m1, map_message(m1)), (m2, map_message(m2))]
    commit_page(store, catalog, "1", pages, oldest_id="100", newest_id="101")

    count = shard_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 2  # no duplicates from the replay
    row = catalog.execute(
        "SELECT backfill_checkpoint, message_count FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["backfill_checkpoint"] == "101"
    assert row["message_count"] == 2  # counted once, not twice


class _FlakyCommitProxy:
    """Delegates every attribute to a real sqlite3.Connection except
    commit(), which raises for the first `fail_times` calls then
    delegates normally. sqlite3.Connection instances (Python 3.14+)
    don't allow instance attribute assignment (no __dict__), so this
    proxy stands in wherever a test needs a connection whose commit()
    can be made to fail on demand."""
    def __init__(self, real_conn, fail_times=1, exc=None):
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "_remaining_fails", fail_times)
        object.__setattr__(self, "_exc", exc or __import__("sqlite3").OperationalError("simulated failure"))

    def commit(self):
        if self._remaining_fails > 0:
            object.__setattr__(self, "_remaining_fails", self._remaining_fails - 1)
            raise self._exc
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_commit_page_rolls_back_on_shard_commit_failure(tmp_path: Path):
    import sqlite3
    import pytest
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    pages = [(m1, map_message(m1))]

    real_shard_conn = store.get_shard("1", SEOUL_MIDNIGHT_UTC)
    yyyymm = month_bucket(SEOUL_MIDNIGHT_UTC)
    relative_path = catalog.execute(
        "SELECT shard_path FROM channel_month_shard WHERE channel_id='1' AND yyyymm=?", (yyyymm,)
    ).fetchone()["shard_path"]
    proxy = _FlakyCommitProxy(real_shard_conn, fail_times=999, exc=sqlite3.OperationalError("simulated disk failure"))
    store._shards[relative_path] = proxy

    from archiver.store import commit_page
    with pytest.raises(sqlite3.OperationalError):
        commit_page(store, catalog, "1", pages, oldest_id="100", newest_id="100")

    row = catalog.execute(
        "SELECT backfill_checkpoint, message_count FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["backfill_checkpoint"] is None
    assert row["message_count"] == 0


def test_commit_page_retry_after_catalog_commit_failure_does_not_double_count(tmp_path: Path):
    import sqlite3
    import pytest
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    pages = [(m1, map_message(m1))]

    flaky_catalog = _FlakyCommitProxy(catalog, fail_times=1, exc=sqlite3.OperationalError("simulated busy"))

    from archiver.store import commit_page
    with pytest.raises(sqlite3.OperationalError):
        commit_page(store, flaky_catalog, "1", pages, oldest_id="100", newest_id="100")

    commit_page(store, flaky_catalog, "1", pages, oldest_id="100", newest_id="100")

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 1  # not 2 -- rollback on first failure prevented compounding


def test_commit_page_spans_multiple_shards_across_month_boundary(tmp_path: Path):
    from datetime import datetime, timezone
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    oct_msg = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    nov_time = datetime(2025, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
    nov_msg = FakeMessage(id=101, channel_id=1, author_id=2, content="b", created_at=nov_time)

    from archiver.store import commit_page
    commit_page(store, catalog, "1", [(oct_msg, map_message(oct_msg)), (nov_msg, map_message(nov_msg))],
                oldest_id="100", newest_id="101")

    assert (tmp_path / "Friends" / "2025-10.sqlite").exists()
    assert (tmp_path / "Friends" / "2025-11.sqlite").exists()
    oct_shard = store.get_shard("1", SEOUL_MIDNIGHT_UTC)
    nov_shard = store.get_shard("1", nov_time)
    assert oct_shard.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert nov_shard.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1

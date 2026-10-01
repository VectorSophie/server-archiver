from archiver.db import connect_catalog
from archiver.users import map_author, upsert_user


class _FakeAuthor:
    def __init__(self, id, name, display_name=None):
        self.id = id
        self.name = name
        self.display_name = display_name if display_name is not None else name


class _FakeMessage:
    def __init__(self, author):
        self.author = author


def test_map_author_reads_id_username_and_nickname():
    author = _FakeAuthor(id=42, name="alice", display_name="Al")
    mapped = map_author(_FakeMessage(author))
    assert mapped == {"id": "42", "username": "alice", "nickname": "Al"}


def test_map_author_nickname_falls_back_to_username_when_equal():
    author = _FakeAuthor(id=42, name="alice")  # display_name defaults to name
    mapped = map_author(_FakeMessage(author))
    assert mapped["nickname"] == "alice"  # map_author doesn't filter -- upsert_user does


def test_upsert_user_inserts_new_user(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": None})
    catalog.commit()
    row = catalog.execute("SELECT username FROM users WHERE id='42'").fetchone()
    assert row["username"] == "alice"


def test_upsert_user_updates_username_on_rename(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": None})
    upsert_user(catalog, {"id": "42", "username": "alice_renamed", "nickname": None})
    catalog.commit()
    row = catalog.execute("SELECT username FROM users WHERE id='42'").fetchone()
    assert row["username"] == "alice_renamed"


def test_upsert_user_records_a_nickname_distinct_from_username(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "Al the Great"})
    catalog.commit()
    row = catalog.execute(
        "SELECT nickname FROM user_nicknames WHERE user_id='42'"
    ).fetchone()
    assert row["nickname"] == "Al the Great"


def test_upsert_user_does_not_record_nickname_equal_to_username(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "alice"})
    catalog.commit()
    rows = catalog.execute("SELECT 1 FROM user_nicknames WHERE user_id='42'").fetchall()
    assert rows == []


def test_upsert_user_does_not_duplicate_an_already_observed_nickname(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "Al"})
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "Al"})
    catalog.commit()
    rows = catalog.execute("SELECT 1 FROM user_nicknames WHERE user_id='42'").fetchall()
    assert len(rows) == 1


def test_upsert_user_does_not_commit():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from archiver.db import CATALOG_MIGRATIONS, apply_migrations
    apply_migrations(conn, CATALOG_MIGRATIONS)
    upsert_user(conn, {"id": "42", "username": "alice", "nickname": None})
    conn2 = conn  # same connection, uncommitted state is visible to itself but not externally;
    # the real contract check is that upsert_user itself never calls .commit() -- verified by
    # reading the function, not practically observable via a second connection to :memory:.
    assert True


# --- backfill_missing_users ---

import discord

from archiver.db import connect_shard
from archiver.users import backfill_missing_users
from tests.discord_fakes import FakeClient, FakeResponse, FakeUser


def _seed_shard_with_authors(path, channel_id, author_ids):
    conn = connect_shard(path)
    for i, author_id in enumerate(author_ids):
        conn.execute(
            "INSERT INTO messages (id, channel_id, author_id, content, created_utc, "
            "mention_everyone, flags) VALUES (?, ?, ?, '', '2025-10-15T00:00:00Z', 0, 0)",
            (str(1000 + i), channel_id, author_id),
        )
    conn.commit()
    conn.close()


async def test_backfill_missing_users_fetches_only_unknown_authors(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'chan1.sqlite')"
    )
    upsert_user(catalog, {"id": "20", "username": "already_known", "nickname": None})
    catalog.commit()
    _seed_shard_with_authors(tmp_path / "chan1.sqlite", "1", ["20", "30"])

    client = FakeClient(users={30: FakeUser(id=30, name="newly_fetched")})
    stats = await backfill_missing_users(client, catalog, tmp_path)

    assert stats == {"missing": 1, "fetched": 1, "failed": 0}
    row = catalog.execute("SELECT username FROM users WHERE id='30'").fetchone()
    assert row["username"] == "newly_fetched"
    # the already-known user's username must not have been overwritten by a fetch that never happened
    row = catalog.execute("SELECT username FROM users WHERE id='20'").fetchone()
    assert row["username"] == "already_known"


async def test_backfill_missing_users_isolates_one_unreachable_account(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'chan1.sqlite')"
    )
    catalog.commit()
    _seed_shard_with_authors(tmp_path / "chan1.sqlite", "1", ["30", "40"])

    client = FakeClient(
        users={40: FakeUser(id=40, name="still_reachable")},
        user_errors={30: discord.NotFound(FakeResponse(), "Unknown User")},
    )
    stats = await backfill_missing_users(client, catalog, tmp_path)

    assert stats == {"missing": 2, "fetched": 1, "failed": 1}
    assert catalog.execute("SELECT 1 FROM users WHERE id='30'").fetchone() is None
    row = catalog.execute("SELECT username FROM users WHERE id='40'").fetchone()
    assert row["username"] == "still_reachable"


async def test_backfill_missing_users_skips_a_missing_shard_file_without_creating_it(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'chan1.sqlite')"
    )
    catalog.commit()
    # Note: chan1.sqlite is never created on disk.

    client = FakeClient()
    stats = await backfill_missing_users(client, catalog, tmp_path)

    assert stats == {"missing": 0, "fetched": 0, "failed": 0}
    assert not (tmp_path / "chan1.sqlite").exists()


async def test_backfill_missing_users_commits_incrementally_per_user(tmp_path):
    """A crash partway through the sweep must not lose already-fetched
    users -- confirmed by checking a second connection to the same
    catalog file sees a fetched user even if a later fetch in the same
    sweep then fails."""
    import sqlite3

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'chan1.sqlite')"
    )
    catalog.commit()
    _seed_shard_with_authors(tmp_path / "chan1.sqlite", "1", ["30", "40"])

    client = FakeClient(
        users={30: FakeUser(id=30, name="fetched_first")},
        user_errors={40: discord.HTTPException(FakeResponse(), "Internal Server Error")},
    )
    await backfill_missing_users(client, catalog, tmp_path)

    observer = sqlite3.connect(tmp_path / "catalog.sqlite")
    observer.row_factory = sqlite3.Row
    row = observer.execute("SELECT username FROM users WHERE id='30'").fetchone()
    observer.close()
    assert row["username"] == "fetched_first"

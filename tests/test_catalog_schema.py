import sqlite3
from pathlib import Path

import pytest

from archiver.db import connect_catalog


EXPECTED_TABLES = {
    "channels",
    "category_names",
    "users",
    "user_nicknames",
    "channel_month_shard",
    "coverage",
    "snapshots",
}


def test_connect_catalog_creates_all_tables(tmp_path: Path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert EXPECTED_TABLES <= tables


def test_connect_catalog_is_idempotent(tmp_path: Path):
    path = tmp_path / "catalog.sqlite"
    connect_catalog(path)
    conn = connect_catalog(path)  # reopening must not raise
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_coverage_status_check_constraint(tmp_path: Path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    conn.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, "
        "is_archived, first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text',NULL,NULL,0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, "
        "is_archived, first_seen_utc, last_seen_utc) VALUES "
        "('2','other','text',NULL,NULL,0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO coverage (channel_id, status) VALUES ('1', 'pending')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO coverage (channel_id, status) VALUES ('2', 'bogus')"
        )
        conn.commit()


def test_channel_month_shard_composite_key(tmp_path: Path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    conn.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'catA', 'Friends/2025-10.sqlite')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT shard_path FROM channel_month_shard WHERE channel_id='1' AND yyyymm='2025-10'"
    ).fetchone()
    assert row["shard_path"] == "Friends/2025-10.sqlite"

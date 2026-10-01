import sqlite3
from pathlib import Path

import pytest

from archiver.db import open_db, apply_migrations


def test_apply_migrations_creates_table_and_sets_version(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    migrations = [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")]

    apply_migrations(conn, migrations)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "widgets" in tables


def test_apply_migrations_is_idempotent(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    migrations = [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")]

    apply_migrations(conn, migrations)
    apply_migrations(conn, migrations)  # must not raise "table already exists"

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_apply_migrations_applies_only_new_versions(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    apply_migrations(conn, [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")])

    apply_migrations(
        conn,
        [
            (1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);"),
            (2, "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);"),
        ],
    )

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"widgets", "gadgets"} <= tables


def test_open_db_sets_pragmas(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_open_db_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "sub" / "dir" / "test.sqlite"
    conn = open_db(nested)
    conn.execute("SELECT 1")
    assert nested.exists()


def test_apply_migrations_failed_migration_leaves_no_trace(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    apply_migrations(conn, [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")])

    bad_migration = [
        (1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);"),
        (2, "ALTER TABLE widgets ADD COLUMN z TEXT; ALTER TABLE widgets ADD COLUMN z TEXT;"),
    ]
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, bad_migration)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    columns = {row[1] for row in conn.execute("PRAGMA table_info(widgets)")}
    assert "z" not in columns

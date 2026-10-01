"""SQLite connection and migration helpers shared by the catalog and shard
databases. Migrations are tracked via PRAGMA user_version — no separate
migrations table, no third-party migration framework."""
import sqlite3
from pathlib import Path


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations: list[tuple[int, str]]) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = sorted(m for m in migrations if m[0] > current)
    for version, sql in pending:
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise


CATALOG_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    parent_id TEXT,
    category_id TEXT,
    is_archived INTEGER NOT NULL DEFAULT 0,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_names (
    category_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_nicknames (
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL,
    observed_utc TEXT NOT NULL,
    PRIMARY KEY (user_id, nickname, observed_utc),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS channel_month_shard (
    channel_id TEXT NOT NULL,
    yyyymm TEXT NOT NULL,
    category_id TEXT NOT NULL,
    shard_path TEXT NOT NULL,
    PRIMARY KEY (channel_id, yyyymm)
);

CREATE TABLE IF NOT EXISTS coverage (
    channel_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending','crawling','complete','inaccessible','failed')),
    oldest_message_id TEXT,
    newest_message_id TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    backfill_checkpoint TEXT,
    live_checkpoint TEXT,
    last_checked_utc TEXT,
    gap_reason TEXT,
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    yyyymm TEXT NOT NULL,
    tarball_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_utc TEXT NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0
);
"""

CATALOG_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS report_fingerprint (
    scope TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    generated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_last_coverage (
    channel_id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
"""

CATALOG_MIGRATIONS: list[tuple[int, str]] = [(1, CATALOG_SCHEMA_V1), (2, CATALOG_SCHEMA_V2)]


def connect_catalog(path: Path) -> sqlite3.Connection:
    conn = open_db(path)
    apply_migrations(conn, CATALOG_MIGRATIONS)
    return conn


SHARD_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_utc TEXT NOT NULL,
    edited_utc TEXT,
    reply_to_id TEXT,
    mention_everyone INTEGER NOT NULL DEFAULT 0,
    flags INTEGER NOT NULL DEFAULT 0,
    deleted_utc TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size INTEGER,
    width INTEGER,
    height INTEGER,
    duration_secs REAL,
    description TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    is_custom INTEGER NOT NULL DEFAULT 0,
    animated INTEGER NOT NULL DEFAULT 0,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, emoji),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS mentions_user (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS mentions_role (
    message_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    PRIMARY KEY (message_id, role_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS stickers (
    message_id TEXT NOT NULL,
    sticker_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (message_id, sticker_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS polls (
    message_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    multiselect INTEGER NOT NULL DEFAULT 0,
    expires_utc TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS poll_answers (
    message_id TEXT NOT NULL,
    answer_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    vote_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, answer_id),
    FOREIGN KEY (message_id) REFERENCES polls(message_id)
);

CREATE TABLE IF NOT EXISTS embeds (
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT,
    description TEXT,
    url TEXT,
    embed_type TEXT,
    PRIMARY KEY (message_id, position),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS embed_fields (
    message_id TEXT NOT NULL,
    embed_position INTEGER NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    inline INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, embed_position, position),
    FOREIGN KEY (message_id, embed_position) REFERENCES embeds(message_id, position)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('edit','delete')),
    observed_utc TEXT NOT NULL,
    detail TEXT
);
"""

SHARD_MIGRATIONS: list[tuple[int, str]] = [(1, SHARD_SCHEMA_V1)]


def connect_shard(path: Path) -> sqlite3.Connection:
    conn = open_db(path)
    apply_migrations(conn, SHARD_MIGRATIONS)
    return conn

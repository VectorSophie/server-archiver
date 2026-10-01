"""Trigram FTS5 availability detection and conditional index creation.

unicode61 tokenization cannot satisfy arbitrary-position substring matching
(including inside Korean words), so it is never used as the search backend.
If trigram support is absent, no FTS table is created at all — the search
stage checks for messages_fts in sqlite_master and falls back to a scoped
LIKE scan (spec §7.1).

Trigram matching cannot match a query under 3 characters by construction, so
the search stage must route on query length, not just on trigram
availability: queries shorter than 3 characters need the LIKE fallback even
when messages_fts exists."""
import sqlite3

_trigram_support_cache: bool | None = None


def detect_trigram_support(conn: sqlite3.Connection) -> bool:
    global _trigram_support_cache
    if _trigram_support_cache is not None:
        return _trigram_support_cache
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.fts_trigram_probe USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE temp.fts_trigram_probe")
        _trigram_support_cache = True
    except sqlite3.OperationalError:
        _trigram_support_cache = False
    return _trigram_support_cache


def ensure_messages_fts(conn: sqlite3.Connection) -> bool:
    if not detect_trigram_support(conn):
        return False

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if exists:
        return True

    with conn:
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "message_id UNINDEXED, content, tokenize='trigram')"
        )
        conn.execute(
            "CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN "
            "INSERT INTO messages_fts(rowid, message_id, content) "
            "VALUES (CAST(new.id AS INTEGER), new.id, new.content); "
            "END"
        )
        conn.execute(
            "CREATE TRIGGER messages_fts_au AFTER UPDATE OF content ON messages BEGIN "
            "DELETE FROM messages_fts WHERE rowid = CAST(old.id AS INTEGER); "
            "INSERT INTO messages_fts(rowid, message_id, content) "
            "VALUES (CAST(new.id AS INTEGER), new.id, new.content); "
            "END"
        )
        conn.execute(
            "CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN "
            "DELETE FROM messages_fts WHERE rowid = CAST(old.id AS INTEGER); "
            "END"
        )
        conn.execute(
            "INSERT INTO messages_fts(rowid, message_id, content) "
            "SELECT CAST(id AS INTEGER), id, content FROM messages"
        )
    return True

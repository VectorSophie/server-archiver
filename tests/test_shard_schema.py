import sqlite3
from pathlib import Path

import pytest

from archiver.db import connect_shard


EXPECTED_TABLES = {
    "messages",
    "attachments",
    "reactions",
    "mentions_user",
    "mentions_role",
    "stickers",
    "polls",
    "poll_answers",
    "embeds",
    "embed_fields",
    "events",
}


def test_connect_shard_creates_all_tables(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert EXPECTED_TABLES <= tables


def test_message_insert_and_roundtrip(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('100', 'chan1', 'user1', 'hello world', '2025-10-15T00:00:00Z')"
    )
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id='100'").fetchone()
    assert row["content"] == "hello world"
    assert row["edited_utc"] is None
    assert row["deleted_utc"] is None


def test_empty_text_message_with_attachment_is_valid(tmp_path: Path):
    """An empty-text message carrying only an attachment must still store,
    per spec: 'An empty-text message with an attachment or embed is still
    a message and must be stored.'"""
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('101', 'chan1', 'user1', '', '2025-10-15T00:01:00Z')"
    )
    conn.execute(
        "INSERT INTO attachments (id, message_id, filename, content_type, size) "
        "VALUES ('att1', '101', 'photo.png', 'image/png', 12345)"
    )
    conn.commit()
    msg = conn.execute("SELECT content FROM messages WHERE id='101'").fetchone()
    att = conn.execute("SELECT filename FROM attachments WHERE message_id='101'").fetchone()
    assert msg["content"] == ""
    assert att["filename"] == "photo.png"


def test_attachment_foreign_key_enforced(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO attachments (id, message_id, filename) "
            "VALUES ('att1', 'does-not-exist', 'x.png')"
        )
        conn.commit()


def test_poll_and_answers_roundtrip(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('102', 'chan1', 'user1', '', '2025-10-15T00:02:00Z')"
    )
    conn.execute(
        "INSERT INTO polls (message_id, question, multiselect) VALUES ('102', 'Best game?', 0)"
    )
    conn.execute(
        "INSERT INTO poll_answers (message_id, answer_id, text, vote_count) "
        "VALUES ('102', 1, 'Chess', 3)"
    )
    conn.commit()
    answer = conn.execute(
        "SELECT text, vote_count FROM poll_answers WHERE message_id='102' AND answer_id=1"
    ).fetchone()
    assert answer["text"] == "Chess"
    assert answer["vote_count"] == 3

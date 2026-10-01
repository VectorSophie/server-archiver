from pathlib import Path

from archiver.db import connect_shard
from archiver.fts import detect_trigram_support, ensure_messages_fts


def test_detect_trigram_support_returns_bool(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    result = detect_trigram_support(conn)
    assert isinstance(result, bool)


def test_detect_trigram_support_does_not_leave_probe_table(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    detect_trigram_support(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_temp_master WHERE type='table'"
        )
    }
    assert not any("probe" in t for t in tables)


def test_ensure_messages_fts_matches_detection(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    supported = detect_trigram_support(conn)

    created = ensure_messages_fts(conn)

    assert created == supported
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert ("messages_fts" in tables) == supported


def test_fts_trigger_syncs_on_insert_when_supported(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not ensure_messages_fts(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', 'the chess tournament starts soon', '2025-10-01T00:00:00Z')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'hes'"
    ).fetchall()
    assert [r["message_id"] for r in rows] == ["1"]


def test_fts_trigger_syncs_on_content_update_when_supported(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not ensure_messages_fts(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', 'original text', '2025-10-01T00:00:00Z')"
    )
    conn.commit()
    conn.execute("UPDATE messages SET content='updated wording', edited_utc='2025-10-01T00:05:00Z' WHERE id='1'")
    conn.commit()

    old = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'orig'"
    ).fetchall()
    new = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'word'"
    ).fetchall()
    assert old == []
    assert [r["message_id"] for r in new] == ["1"]


def test_ensure_messages_fts_backfills_existing_rows(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not detect_trigram_support(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', 'preexisting content here', '2025-10-01T00:00:00Z')"
    )
    conn.commit()

    ensure_messages_fts(conn)

    rows = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'existing'"
    ).fetchall()
    assert [r["message_id"] for r in rows] == ["1"]


def test_korean_midword_fragment_matches_when_trigram_supported(tmp_path: Path):
    """spec §7.1: a fragment from the middle of a Korean word must match."""
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not ensure_messages_fts(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', '오늘 저녁에 치킨 먹으러 가자 안녕하세요', '2025-10-01T00:00:00Z')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH '녕하세'"
    ).fetchall()
    assert [r["message_id"] for r in rows] == ["1"]

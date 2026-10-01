from archiver.db import connect_catalog
from archiver.export import render_text, render_markdown


def _seed(catalog):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) VALUES ('2', 'alice', ?, ?)",
        (now, now),
    )
    catalog.commit()


def _row(id, created_utc, content):
    return {
        "id": id, "channel_id": "1", "author_id": "2", "content": content,
        "created_utc": created_utc, "edited_utc": None, "reply_to_id": None,
        "mention_everyone": 0, "flags": 0, "message_type": 0, "deleted_utc": None,
    }


def test_render_text_one_line_per_message_in_order(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog)
    results = [
        _row("100", "2025-10-15T00:00:00Z", "hello"),
        _row("101", "2025-10-15T00:01:00Z", "world"),
    ]
    text = render_text(catalog, results)
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "hello" in lines[0]
    assert "world" in lines[1]
    assert "alice" in lines[0]


def test_render_markdown_groups_by_seoul_date(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog)
    results = [
        _row("100", "2025-10-14T16:00:00Z", "late night"),  # 2025-10-15 01:00 KST
        _row("101", "2025-10-15T01:00:00Z", "next day"),     # 2025-10-15 10:00 KST
    ]
    text = render_markdown(catalog, results)
    assert text.count("## 2025-10-15") == 1  # both messages fall on the same KST date -> one header
    assert "late night" in text
    assert "next day" in text

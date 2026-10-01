from pathlib import Path

from archiver.db import connect_shard
from tests.fixtures import make_message, make_attachment, seed_shard


def test_make_message_has_required_fields_and_overrides():
    msg = make_message(id="42", content="hi")
    assert msg["id"] == "42"
    assert msg["content"] == "hi"
    assert msg["channel_id"]  # has a sane default
    assert msg["created_utc"]


def test_make_attachment_links_to_message():
    att = make_attachment(message_id="42", filename="a.pdf")
    assert att["message_id"] == "42"
    assert att["filename"] == "a.pdf"


def test_seed_shard_inserts_expected_kinds(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)

    expected_kinds = {
        "plain", "empty_with_attachment", "edited", "reply",
        "with_reactions", "with_mentions", "with_sticker",
        "with_poll", "with_embed",
    }
    assert expected_kinds <= ids.keys()

    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == len(set().union(*[
        v if isinstance(v, list) else [v] for v in ids.values()
    ]))


def test_seed_shard_edited_message_has_edited_utc(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)
    row = conn.execute(
        "SELECT edited_utc FROM messages WHERE id=?", (ids["edited"],)
    ).fetchone()
    assert row["edited_utc"] is not None


def test_seed_shard_reply_references_parent(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)
    row = conn.execute(
        "SELECT reply_to_id FROM messages WHERE id=?", (ids["reply"],)
    ).fetchone()
    assert row["reply_to_id"] == ids["plain"]


def test_seed_shard_empty_with_attachment_has_blank_content(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)
    row = conn.execute(
        "SELECT content FROM messages WHERE id=?", (ids["empty_with_attachment"],)
    ).fetchone()
    assert row["content"] == ""
    att = conn.execute(
        "SELECT filename FROM attachments WHERE message_id=?",
        (ids["empty_with_attachment"],),
    ).fetchone()
    assert att is not None

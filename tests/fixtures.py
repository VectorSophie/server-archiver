"""Hand-built fixture row builders — deliberately not MagicMock, so a
builder that doesn't set an attribute a test needs fails loudly instead
of silently returning a Mock object."""
import sqlite3

_counter = {"n": 0}


def _next_id() -> str:
    _counter["n"] += 1
    return str(1000 + _counter["n"])


def make_message(**overrides) -> dict:
    msg = {
        "id": _next_id(),
        "channel_id": "chan1",
        "author_id": "user1",
        "content": "hello",
        "created_utc": "2025-10-15T00:00:00Z",
        "edited_utc": None,
        "reply_to_id": None,
        "mention_everyone": 0,
        "flags": 0,
        "deleted_utc": None,
    }
    msg.update(overrides)
    return msg


def make_attachment(message_id: str, **overrides) -> dict:
    att = {
        "id": _next_id(),
        "message_id": message_id,
        "filename": "file.png",
        "content_type": "image/png",
        "size": 1024,
        "width": 512,
        "height": 512,
        "duration_secs": None,
        "description": None,
    }
    att.update(overrides)
    return att


def make_reaction(message_id: str, **overrides) -> dict:
    r = {
        "message_id": message_id,
        "emoji": "👍",
        "is_custom": 0,
        "animated": 0,
        "count": 1,
    }
    r.update(overrides)
    return r


def make_poll(message_id: str, **overrides) -> dict:
    p = {
        "message_id": message_id,
        "question": "Best game?",
        "multiselect": 0,
        "expires_utc": None,
    }
    p.update(overrides)
    return p


def make_poll_answer(message_id: str, answer_id: int, **overrides) -> dict:
    a = {
        "message_id": message_id,
        "answer_id": answer_id,
        "text": "Chess",
        "vote_count": 0,
    }
    a.update(overrides)
    return a


def make_embed(message_id: str, **overrides) -> dict:
    e = {
        "message_id": message_id,
        "position": 0,
        "title": "A link preview",
        "description": None,
        "url": "https://example.com",
        "embed_type": "link",
    }
    e.update(overrides)
    return e


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(row.values())
    )


def seed_shard(conn: sqlite3.Connection) -> dict:
    ids: dict = {}

    plain = make_message(content="plain text message")
    _insert(conn, "messages", plain)
    ids["plain"] = plain["id"]

    empty_with_attachment = make_message(content="", created_utc="2025-10-15T00:01:00Z")
    _insert(conn, "messages", empty_with_attachment)
    _insert(conn, "attachments", make_attachment(empty_with_attachment["id"]))
    ids["empty_with_attachment"] = empty_with_attachment["id"]

    edited = make_message(
        content="fixed typo",
        created_utc="2025-10-15T00:02:00Z",
        edited_utc="2025-10-15T00:03:00Z",
    )
    _insert(conn, "messages", edited)
    ids["edited"] = edited["id"]

    reply = make_message(
        content="agreed",
        created_utc="2025-10-15T00:04:00Z",
        reply_to_id=plain["id"],
    )
    _insert(conn, "messages", reply)
    ids["reply"] = reply["id"]

    with_reactions = make_message(content="funny", created_utc="2025-10-15T00:05:00Z")
    _insert(conn, "messages", with_reactions)
    _insert(conn, "reactions", make_reaction(with_reactions["id"], emoji="😂", count=3))
    ids["with_reactions"] = with_reactions["id"]

    with_mentions = make_message(content="hey @someone", created_utc="2025-10-15T00:06:00Z")
    _insert(conn, "messages", with_mentions)
    conn.execute(
        "INSERT INTO mentions_user (message_id, user_id) VALUES (?, ?)",
        (with_mentions["id"], "user2"),
    )
    ids["with_mentions"] = with_mentions["id"]

    with_sticker = make_message(content="", created_utc="2025-10-15T00:07:00Z")
    _insert(conn, "messages", with_sticker)
    conn.execute(
        "INSERT INTO stickers (message_id, sticker_id, name) VALUES (?, ?, ?)",
        (with_sticker["id"], "sticker1", "PogChamp"),
    )
    ids["with_sticker"] = with_sticker["id"]

    with_poll = make_message(content="", created_utc="2025-10-15T00:08:00Z")
    _insert(conn, "messages", with_poll)
    _insert(conn, "polls", make_poll(with_poll["id"]))
    _insert(conn, "poll_answers", make_poll_answer(with_poll["id"], 1, text="Chess", vote_count=2))
    _insert(conn, "poll_answers", make_poll_answer(with_poll["id"], 2, text="Go", vote_count=5))
    ids["with_poll"] = with_poll["id"]

    with_embed = make_message(content="check this out", created_utc="2025-10-15T00:09:00Z")
    _insert(conn, "messages", with_embed)
    embed = make_embed(with_embed["id"])
    conn.execute(
        "INSERT INTO embeds (message_id, position, title, description, url, embed_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (embed["message_id"], embed["position"], embed["title"], embed["description"], embed["url"], embed["embed_type"]),
    )
    ids["with_embed"] = with_embed["id"]

    conn.commit()
    return ids

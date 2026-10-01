"""Shard resolution, connection caching, and idempotent message writes.
One ShardStore per backfill/live-capture run holds open shard
connections so repeated writes to the same month don't re-open the
file every time."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from archiver.db import connect_shard
from archiver.fts import ensure_messages_fts

SEOUL = ZoneInfo("Asia/Seoul")
UNCATEGORIZED_ID = "uncategorized"
UNCATEGORIZED_NAME = "Uncategorized"
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')


def month_bucket(created_utc: datetime) -> str:
    if created_utc.tzinfo is None:
        created_utc = created_utc.replace(tzinfo=timezone.utc)
    return created_utc.astimezone(SEOUL).strftime("%Y-%m")


def sanitize_folder_name(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or UNCATEGORIZED_NAME


class ShardStore:
    def __init__(self, data_dir: Path, catalog_conn: sqlite3.Connection):
        self.data_dir = data_dir
        self.catalog_conn = catalog_conn
        self._shards: dict[str, sqlite3.Connection] = {}

    def get_shard(self, channel_id: str, created_utc: datetime) -> sqlite3.Connection:
        yyyymm = month_bucket(created_utc)
        row = self.catalog_conn.execute(
            "SELECT shard_path FROM channel_month_shard WHERE channel_id=? AND yyyymm=?",
            (channel_id, yyyymm),
        ).fetchone()
        if row is not None:
            relative_path = row["shard_path"]
        else:
            relative_path = self._resolve_new_path(channel_id, yyyymm)
            category_id, _ = self._resolve_category(channel_id)
            self.catalog_conn.execute(
                "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, yyyymm, category_id, relative_path),
            )
            self.catalog_conn.commit()

        if relative_path not in self._shards:
            conn = connect_shard(self.data_dir / relative_path)
            ensure_messages_fts(conn)  # no-op if trigram FTS5 isn't available (spec §7.1)
            self._shards[relative_path] = conn
        return self._shards[relative_path]

    def _resolve_new_path(self, channel_id: str, yyyymm: str) -> str:
        """One shard file per channel per month, grouped under its
        category folder. Channel folder names get a short id suffix
        only when another channel in the same category shares the same
        sanitized name (a real case on this server: duplicate channel
        names across different categories/ids) -- collision-checked
        once at first resolution, not on every write."""
        category_id, category_name = self._resolve_category(channel_id)
        category_folder = sanitize_folder_name(category_name)

        chan_row = self.catalog_conn.execute(
            "SELECT name, category_id FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        channel_name = chan_row["name"] if chan_row else channel_id
        raw_category_id = chan_row["category_id"] if chan_row else None
        channel_folder = sanitize_folder_name(channel_name)

        collision = self.catalog_conn.execute(
            "SELECT 1 FROM channels WHERE id != ? AND LOWER(name) = LOWER(?) "
            "AND category_id IS ? LIMIT 1",
            (channel_id, channel_name, raw_category_id),
        ).fetchone()
        if collision is not None:
            channel_folder = f"{channel_folder}-{channel_id[-6:]}"

        return f"{category_folder}/{channel_folder}/{yyyymm}.sqlite"

    def _resolve_category(self, channel_id: str) -> tuple[str, str]:
        row = self.catalog_conn.execute(
            "SELECT category_id FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        category_id = row["category_id"] if row and row["category_id"] else None
        if category_id is None:
            return UNCATEGORIZED_ID, UNCATEGORIZED_NAME
        name_row = self.catalog_conn.execute(
            "SELECT name FROM category_names WHERE category_id=?", (category_id,)
        ).fetchone()
        name = name_row["name"] if name_row else category_id
        return category_id, name

    def close_all(self) -> None:
        for conn in self._shards.values():
            conn.close()
        self._shards.clear()


def write_message(shard_conn: sqlite3.Connection, mapped: dict) -> bool:
    """Idempotent upsert of one mapped message and all its child rows.
    Safe to call twice with the same data (replay after a crash). Does
    not commit -- callers batch a page's writes under one commit.
    Returns True if this call inserted a brand-new message row, False
    if the id already existed (an edit, a replay, or the same message
    arriving via two different code paths -- e.g. live capture and a
    backfill sweep touching the same id) -- callers use this to count
    coverage.message_count accurately instead of once per write
    attempt."""
    m = mapped["message"]
    is_new = shard_conn.execute(
        "SELECT 1 FROM messages WHERE id=?", (m["id"],)
    ).fetchone() is None

    shard_conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc, "
        "edited_utc, reply_to_id, mention_everyone, flags, message_type, deleted_utc) "
        "VALUES (:id, :channel_id, :author_id, :content, :created_utc, :edited_utc, "
        ":reply_to_id, :mention_everyone, :flags, :message_type, :deleted_utc) "
        "ON CONFLICT(id) DO UPDATE SET content=excluded.content, "
        "edited_utc=excluded.edited_utc, mention_everyone=excluded.mention_everyone, "
        "flags=excluded.flags",
        m,
    )

    for a in mapped["attachments"]:
        shard_conn.execute(
            "INSERT INTO attachments (id, message_id, filename, content_type, size, "
            "width, height, duration_secs, description) VALUES "
            "(:id, :message_id, :filename, :content_type, :size, :width, :height, "
            ":duration_secs, :description) ON CONFLICT(id) DO UPDATE SET "
            "filename=excluded.filename",
            a,
        )

    for r in mapped["reactions"]:
        shard_conn.execute(
            "INSERT INTO reactions (message_id, emoji, is_custom, animated, count) "
            "VALUES (:message_id, :emoji, :is_custom, :animated, :count) "
            "ON CONFLICT(message_id, emoji) DO UPDATE SET count=excluded.count",
            r,
        )

    for mu in mapped["mentions_user"]:
        shard_conn.execute(
            "INSERT INTO mentions_user (message_id, user_id) VALUES (:message_id, :user_id) "
            "ON CONFLICT(message_id, user_id) DO NOTHING",
            mu,
        )

    for mr in mapped["mentions_role"]:
        shard_conn.execute(
            "INSERT INTO mentions_role (message_id, role_id) VALUES (:message_id, :role_id) "
            "ON CONFLICT(message_id, role_id) DO NOTHING",
            mr,
        )

    for s in mapped["stickers"]:
        shard_conn.execute(
            "INSERT INTO stickers (message_id, sticker_id, name) "
            "VALUES (:message_id, :sticker_id, :name) "
            "ON CONFLICT(message_id, sticker_id) DO NOTHING",
            s,
        )

    if mapped["poll"] is not None:
        shard_conn.execute(
            "INSERT INTO polls (message_id, question, multiselect, expires_utc) "
            "VALUES (:message_id, :question, :multiselect, :expires_utc) "
            "ON CONFLICT(message_id) DO UPDATE SET question=excluded.question",
            mapped["poll"],
        )
    for pa in mapped["poll_answers"]:
        shard_conn.execute(
            "INSERT INTO poll_answers (message_id, answer_id, text, vote_count) "
            "VALUES (:message_id, :answer_id, :text, :vote_count) "
            "ON CONFLICT(message_id, answer_id) DO UPDATE SET vote_count=excluded.vote_count",
            pa,
        )

    for e in mapped["embeds"]:
        shard_conn.execute(
            "INSERT INTO embeds (message_id, position, title, description, url, embed_type) "
            "VALUES (:message_id, :position, :title, :description, :url, :embed_type) "
            "ON CONFLICT(message_id, position) DO UPDATE SET title=excluded.title, "
            "description=excluded.description",
            e,
        )
    for ef in mapped["embed_fields"]:
        shard_conn.execute(
            "INSERT INTO embed_fields (message_id, embed_position, position, name, value, inline) "
            "VALUES (:message_id, :embed_position, :position, :name, :value, :inline) "
            "ON CONFLICT(message_id, embed_position, position) DO UPDATE SET value=excluded.value",
            ef,
        )

    return is_new


def commit_page(store: ShardStore, catalog_conn: sqlite3.Connection, channel_id: str,
                 pages: list, oldest_id: str, newest_id: str) -> None:
    """Write one backfill page's messages to their shard(s), then advance
    the channel's coverage row (checkpoint, message span, count) in a
    single catalog transaction -- only after every affected shard has
    committed (spec §4.1). If the process crashes between a shard's
    commit and this catalog commit, the checkpoint still points at the
    previous page on restart; replaying the page is safe because every
    shard write is an idempotent upsert. The message_count increment is
    NOT independently idempotent -- its safety comes from committing
    atomically with the checkpoint in the same transaction as this
    function's own rollback-on-failure handling below, so a failed
    attempt never leaves a partial increment for a retry to compound on.

    `pages` is a list of (discord_message, mapped_dict) tuples."""
    touched_shards: set[sqlite3.Connection] = set()
    try:
        new_count = 0
        for message, mapped in pages:
            shard_conn = store.get_shard(channel_id, message.created_at)
            if write_message(shard_conn, mapped):
                new_count += 1
            touched_shards.add(shard_conn)

        for shard_conn in touched_shards:
            shard_conn.commit()

        cursor = catalog_conn.execute(
            "UPDATE coverage SET backfill_checkpoint=?, "
            "oldest_message_id=COALESCE(oldest_message_id, ?), newest_message_id=?, "
            "message_count=message_count+? WHERE channel_id=?",
            (newest_id, oldest_id, newest_id, new_count, channel_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"commit_page: no coverage row for channel_id={channel_id}")
        catalog_conn.commit()
    except BaseException:
        catalog_conn.rollback()
        for shard_conn in touched_shards:
            shard_conn.rollback()
        raise

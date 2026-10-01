"""User identity tracking: upserts into catalog.users/user_nicknames
whenever a message's author is observed, so search/reports can show
usernames instead of raw ids (spec §4). Append-only for nicknames --
only an actually-observed value is ever recorded, never a retroactively
invented one."""
import sqlite3
from datetime import datetime, timezone

import discord

from archiver.db import connect_shard


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def map_author(message) -> dict:
    author = message.author
    return {
        "id": str(author.id),
        "username": author.name,
        "nickname": getattr(author, "display_name", None),
    }


def upsert_user(catalog_conn: sqlite3.Connection, author: dict) -> None:
    """Idempotent upsert of a user's identity, observed from a message's
    author. Does not commit -- caller batches this into its own
    existing transaction, matching write_message/commit_page's
    established convention."""
    now = _now()
    catalog_conn.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "username=excluded.username, last_seen_utc=excluded.last_seen_utc",
        (author["id"], author["username"], now, now),
    )
    nickname = author.get("nickname")
    if nickname and nickname != author["username"]:
        existing = catalog_conn.execute(
            "SELECT 1 FROM user_nicknames WHERE user_id=? AND nickname=?",
            (author["id"], nickname),
        ).fetchone()
        if existing is None:
            catalog_conn.execute(
                "INSERT INTO user_nicknames (user_id, nickname, observed_utc) VALUES (?, ?, ?)",
                (author["id"], nickname, now),
            )


async def backfill_missing_users(client, catalog_conn: sqlite3.Connection, data_dir) -> dict:
    """One-off sweep: for every author_id that appears in any archived
    shard but has no users row yet (messages written before user
    tracking existed), fetch their current username via the Discord API
    and upsert it. Best-effort -- an account that's left Discord
    entirely (NotFound) or can't be fetched (Forbidden/HTTPException) is
    skipped, not retried forever; this is a one-time catch-up, not an
    ongoing requirement, matching this project's stated principle that
    historical identity from before observation began can't be
    reconstructed."""
    shard_paths = {
        row["shard_path"] for row in
        catalog_conn.execute("SELECT DISTINCT shard_path FROM channel_month_shard").fetchall()
    }
    known_ids = {row["id"] for row in catalog_conn.execute("SELECT id FROM users").fetchall()}
    missing_ids: set[str] = set()
    for shard_path in shard_paths:
        full_path = data_dir / shard_path
        if not full_path.exists():
            continue
        shard_conn = connect_shard(full_path)
        try:
            for row in shard_conn.execute("SELECT DISTINCT author_id FROM messages").fetchall():
                if row["author_id"] not in known_ids:
                    missing_ids.add(row["author_id"])
        finally:
            shard_conn.close()

    fetched, failed = 0, 0
    for user_id in sorted(missing_ids):
        try:
            user = await client.fetch_user(int(user_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            failed += 1
            continue
        upsert_user(catalog_conn, {"id": str(user.id), "username": user.name, "nickname": None})
        catalog_conn.commit()
        fetched += 1
    return {"missing": len(missing_ids), "fetched": fetched, "failed": failed}

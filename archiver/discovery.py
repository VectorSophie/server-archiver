"""Guild-wide discovery: walks channels, categories, and threads, and
upserts what it finds into the catalog. Never trusts a cached channel
list — callers pass a freshly fetched guild each run (spec §5)."""
import sqlite3
from datetime import datetime, timezone

import discord

from archiver.discord_io import classify_channel_type, discover_threads, resolve_category_id

IN_SCOPE_TOP_LEVEL = {"text", "announcement", "forum", "media", "voice"}
THREAD_PARENT_KINDS = {"text", "announcement", "forum", "media"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def discover_guild(guild, catalog_conn: sqlite3.Connection) -> dict:
    now = _now()
    seen_ids: set[str] = set()
    stats = {"discovered": 0, "new": 0, "inaccessible": 0}

    top_level = await guild.fetch_channels()

    for channel in top_level:
        if channel.type == discord.ChannelType.category:
            _upsert_category(catalog_conn, str(channel.id), channel.name, now)

    for channel in top_level:
        kind = classify_channel_type(channel)
        if kind not in IN_SCOPE_TOP_LEVEL:
            continue
        category_id = resolve_category_id(channel)
        seen_ids.add(str(channel.id))
        is_new = _upsert_channel(
            catalog_conn, str(channel.id), channel.name, kind,
            parent_id=category_id, category_id=category_id, now=now,
        )
        stats["discovered"] += 1
        stats["new"] += is_new

        if kind in THREAD_PARENT_KINDS:
            try:
                thread_infos, gap_reason = await discover_threads(channel)
            except discord.Forbidden:
                gap_reason = "cannot list archived threads (missing Read Message History)"
                thread_infos = []
                known_children = catalog_conn.execute(
                    "SELECT id FROM channels WHERE parent_id=?", (str(channel.id),)
                ).fetchall()
                for row in known_children:
                    seen_ids.add(row["id"])

            for t in thread_infos:
                seen_ids.add(t["id"])
                is_new = _upsert_channel(
                    catalog_conn, t["id"], t["name"], t["type"],
                    parent_id=t["parent_id"], category_id=category_id, now=now,
                )
                stats["discovered"] += 1
                stats["new"] += is_new
                _set_gap_reason(catalog_conn, t["id"], gap_reason)
            _set_gap_reason(catalog_conn, str(channel.id), gap_reason)

    channel_by_id = {c.id: c for c in top_level}
    for thread in await guild.active_threads():
        seen_ids.add(str(thread.id))
        parent = channel_by_id.get(thread.parent_id)
        category_id = resolve_category_id(parent) if parent else None
        is_new = _upsert_channel(
            catalog_conn, str(thread.id), thread.name, classify_channel_type(thread),
            parent_id=str(thread.parent_id), category_id=category_id, now=now,
        )
        stats["discovered"] += 1
        stats["new"] += is_new

    stats["inaccessible"] = _mark_vanished(catalog_conn, seen_ids, now)
    catalog_conn.commit()
    return stats


def _upsert_category(conn, category_id, name, now) -> None:
    conn.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(category_id) DO UPDATE SET name=excluded.name, updated_utc=excluded.updated_utc",
        (category_id, name, now),
    )


def _upsert_channel(conn, channel_id, name, kind, parent_id, category_id, now) -> int:
    existing = conn.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone()
    conn.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type, "
        "parent_id=excluded.parent_id, category_id=excluded.category_id, "
        "last_seen_utc=excluded.last_seen_utc",
        (channel_id, name, kind, parent_id, category_id, now, now),
    )
    if existing is None:
        conn.execute(
            "INSERT INTO coverage (channel_id, status) VALUES (?, 'pending') "
            "ON CONFLICT(channel_id) DO NOTHING",
            (channel_id,),
        )
        return 1
    conn.execute(
        "UPDATE coverage SET status='pending', gap_reason=NULL, last_checked_utc=? "
        "WHERE channel_id=? AND status='inaccessible' AND gap_reason='no longer discoverable'",
        (now, channel_id),
    )
    conn.execute("UPDATE coverage SET last_checked_utc=? WHERE channel_id=?", (now, channel_id))
    return 0


def _set_gap_reason(conn, channel_id, gap_reason) -> None:
    conn.execute(
        "UPDATE coverage SET gap_reason=? WHERE channel_id=? AND ("
        "gap_reason IS NULL "
        "OR gap_reason LIKE 'private archived threads:%' "
        "OR gap_reason LIKE 'cannot list archived threads%' "
        "OR gap_reason = 'no longer discoverable'"
        ")",
        (gap_reason, channel_id),
    )


def _mark_vanished(conn, seen_ids: set[str], now: str) -> int:
    known = {row[0] for row in conn.execute("SELECT id FROM channels").fetchall()}
    vanished = known - seen_ids
    for channel_id in vanished:
        conn.execute(
            "UPDATE coverage SET status='inaccessible', gap_reason='no longer discoverable', "
            "last_checked_utc=? WHERE channel_id=?",
            (now, channel_id),
        )
    return len(vanished)

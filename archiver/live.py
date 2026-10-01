"""Live gateway capture: message creation, partial-edit application,
delete handling, and startup catch-up. Full discord.py Message objects
(on_message, catch-up history pages) reuse Stage 3's map_message/
write_message unchanged. Raw partial payloads (on_raw_message_edit,
on_raw_message_delete) get their own narrow handling -- verified via
inspect.getsource that discord.py's own Message construction from a
partial gateway payload cannot be trusted to represent "what changed",
so those handlers read the raw dict/IDs directly, never a constructed
Message object."""
import sqlite3
from datetime import datetime, timedelta, timezone

import discord

from archiver.discord_message import map_message
from archiver.store import ShardStore, write_message
from archiver.users import map_author, upsert_user

_EDIT_FIELD_MAP = {"content": "content", "flags": "flags"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_tracked_channel(catalog_conn: sqlite3.Connection, channel_id: str) -> bool:
    return catalog_conn.execute(
        "SELECT 1 FROM channels WHERE id=?", (channel_id,)
    ).fetchone() is not None


def apply_live_message(store: ShardStore, catalog_conn: sqlite3.Connection, message,
                        *, advance_checkpoint: bool = True) -> None:
    """Write a newly created message and, unless the channel's startup
    catch-up sweep hasn't finished this session (advance_checkpoint=False),
    advance the channel's live checkpoint -- the only place the checkpoint
    moves forward (spec §6). Untracked channels (a different guild on the
    same token, or a channel never discovered) are silently ignored.

    The shard write and the catalog checkpoint write are separate
    try/except blocks, each rolling back only its own connection, so a
    failure in one can't leave the other holding a dangling transaction.
    message_count only increments when write_message reports the
    message was genuinely new to this shard -- independent of whether
    the checkpoint advances, since a message can be new-to-the-archive
    on a channel that hasn't finished catch-up, or already-archived
    (e.g. a duplicate delivery, or backfill already wrote it) on a
    channel that has."""
    channel_id = str(message.channel.id)
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(message.id)
    mapped = map_message(message)
    shard_conn = store.get_shard(channel_id, message.created_at)
    try:
        is_new = write_message(shard_conn, mapped)
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise

    try:
        if advance_checkpoint:
            catalog_conn.execute(
                "UPDATE coverage SET live_checkpoint=? "
                "WHERE channel_id=? AND (live_checkpoint IS NULL "
                "OR CAST(? AS INTEGER) > CAST(live_checkpoint AS INTEGER))",
                (message_id, channel_id, message_id),
            )
        if is_new:
            catalog_conn.execute(
                "UPDATE coverage SET message_count=message_count+1 WHERE channel_id=?",
                (channel_id,),
            )
        upsert_user(catalog_conn, map_author(message))
        catalog_conn.commit()
    except BaseException:
        catalog_conn.rollback()
        raise


def apply_raw_edit(store: ShardStore, catalog_conn: sqlite3.Connection,
                    channel_id: str, raw_data: dict) -> None:
    """Apply a raw MESSAGE_UPDATE payload to the stored message row, if
    one exists -- reads raw_data directly (never a constructed Message
    object; see module docstring). Only columns whose raw key is
    present get touched. Never advances live_checkpoint (spec §6)."""
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(raw_data["id"])
    created_at = discord.utils.snowflake_time(int(message_id))
    shard_conn = store.get_shard(channel_id, created_at)

    try:
        set_clauses = []
        params: list = []
        for raw_key, column in _EDIT_FIELD_MAP.items():
            if raw_key in raw_data:
                set_clauses.append(f"{column}=?")
                params.append(raw_data[raw_key])
        if raw_data.get("edited_timestamp"):
            edited_dt = discord.utils.parse_time(raw_data["edited_timestamp"])
            set_clauses.append("edited_utc=?")
            params.append(_iso(edited_dt))

        if not set_clauses:
            return

        params.append(message_id)
        cursor = shard_conn.execute(
            f"UPDATE messages SET {', '.join(set_clauses)} WHERE id=?", params
        )
        if cursor.rowcount == 0:
            shard_conn.rollback()
            return

        shard_conn.execute(
            "INSERT INTO events (message_id, event_type, observed_utc, detail) "
            "VALUES (?, 'edit', ?, ?)",
            (message_id, _now(), ",".join(c.split("=")[0] for c in set_clauses)),
        )
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise


def apply_raw_delete(store: ShardStore, catalog_conn: sqlite3.Connection,
                      channel_id: str, message_id) -> None:
    """Mark a message deleted from an explicit gateway delete event.
    Never records a delete for a message this archive never confirmed
    it had (spec §12: absence is not proof of deletion)."""
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(message_id)
    created_at = discord.utils.snowflake_time(int(message_id))
    shard_conn = store.get_shard(channel_id, created_at)

    try:
        cursor = shard_conn.execute(
            "UPDATE messages SET deleted_utc=? WHERE id=? AND deleted_utc IS NULL",
            (_now(), message_id),
        )
        if cursor.rowcount > 0:
            shard_conn.execute(
                "INSERT INTO events (message_id, event_type, observed_utc, detail) "
                "VALUES (?, 'delete', ?, NULL)",
                (message_id, _now()),
            )
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise


async def catch_up_missed_messages(client, catalog_conn: sqlite3.Connection, store: ShardStore,
                                    caught_up_channels: set[str] | None = None) -> None:
    """For every 'complete' channel, fetch anything created after the
    live checkpoint (falling back to the backfill checkpoint on the
    first-ever live startup) while the bot was offline (spec §6). A
    channel successfully swept to completion is added to
    caught_up_channels, when given, so a live message arriving before
    catch-up for a still-incomplete channel finishes can be told apart
    from one arriving after (see apply_live_message's advance_checkpoint)."""
    rows = catalog_conn.execute(
        "SELECT channel_id, COALESCE(live_checkpoint, backfill_checkpoint, '0') AS checkpoint "
        "FROM coverage WHERE status='complete'"
    ).fetchall()
    for row in rows:
        channel_id = row["channel_id"]
        checkpoint = row["checkpoint"]
        try:
            discord_channel = await client.fetch_channel(int(channel_id))
        except Exception:
            continue
        if not hasattr(discord_channel, "history"):
            continue
        try:
            async for message in discord_channel.history(
                after=discord.Object(id=int(checkpoint)), oldest_first=True, limit=None,
            ):
                apply_live_message(store, catalog_conn, message)
        except Exception:
            continue
        if caught_up_channels is not None:
            caught_up_channels.add(channel_id)


async def rescan_recent_window(client, catalog_conn: sqlite3.Connection, store: ShardStore,
                                 days: int = 3) -> None:
    """Re-fetch the last `days` of history for every 'complete' channel
    and re-apply it via the idempotent writer directly, to catch
    edits/reactions missed during a reconnect gap (spec §6). Safe to
    re-run -- every write is an upsert. Deliberately does NOT advance
    live_checkpoint; this is a supplementary re-sync, not the primary
    forward-progress signal."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = catalog_conn.execute("SELECT channel_id FROM coverage WHERE status='complete'").fetchall()
    for row in rows:
        channel_id = row["channel_id"]
        if not _is_tracked_channel(catalog_conn, channel_id):
            continue
        try:
            discord_channel = await client.fetch_channel(int(channel_id))
        except Exception:
            continue
        if not hasattr(discord_channel, "history"):
            continue
        try:
            async for message in discord_channel.history(after=cutoff, oldest_first=True, limit=None):
                mapped = map_message(message)
                shard_conn = store.get_shard(channel_id, message.created_at)
                try:
                    write_message(shard_conn, mapped)
                    shard_conn.commit()
                except BaseException:
                    shard_conn.rollback()
                    raise
        except Exception:
            continue

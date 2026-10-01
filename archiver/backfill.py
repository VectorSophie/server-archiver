# archiver/backfill.py
"""Historical backfill: oldest-first crawl per channel with per-channel
failure isolation, and (Task 6) bounded-concurrency orchestration across
every pending channel in the catalog."""
import asyncio
import sqlite3
from datetime import datetime, timezone

import discord

from archiver.discord_message import map_message
from archiver.store import ShardStore, commit_page

PAGE_SIZE = 100


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def backfill_channel(discord_channel, catalog_conn: sqlite3.Connection, store: ShardStore) -> None:
    """Backfill one channel/thread from its current checkpoint to the
    present, oldest-first, committing and checkpointing every page (spec
    §4.1). Never raises -- any failure (a discord.py API error, or
    anything else) is recorded as status='inaccessible'/'failed' with
    the error as gap_reason, and the function returns normally, so the
    caller can move on to the next channel (spec §5, discrawl #27/#30)."""
    channel_id = str(discord_channel.id)

    if not hasattr(discord_channel, "history"):
        # Forum/media channels are containers -- they carry no messages of
        # their own, only their threads (posts) do, and those threads are
        # separate coverage rows discovered independently (spec §5). A live
        # run against a real server surfaced this: ForumChannel has no
        # .history() method at all.
        catalog_conn.execute(
            "UPDATE coverage SET status='complete', last_checked_utc=? WHERE channel_id=?",
            (_now(), channel_id),
        )
        catalog_conn.commit()
        return

    row = catalog_conn.execute(
        "SELECT backfill_checkpoint FROM coverage WHERE channel_id=?", (channel_id,)
    ).fetchone()
    checkpoint = row["backfill_checkpoint"] if row and row["backfill_checkpoint"] else "0"

    catalog_conn.execute(
        "UPDATE coverage SET status='crawling' WHERE channel_id=? AND status != 'complete'",
        (channel_id,),
    )
    catalog_conn.commit()

    try:
        while True:
            messages = [
                m async for m in discord_channel.history(
                    limit=PAGE_SIZE, after=discord.Object(id=int(checkpoint)), oldest_first=True,
                )
            ]
            if not messages:
                break

            pages = [(m, map_message(m)) for m in messages]
            oldest_id = str(messages[0].id)
            newest_id = str(messages[-1].id)
            commit_page(store, catalog_conn, channel_id, pages, oldest_id, newest_id)
            checkpoint = newest_id

            if len(messages) < PAGE_SIZE:
                break

        catalog_conn.execute(
            "UPDATE coverage SET status='complete', last_checked_utc=? WHERE channel_id=?",
            (_now(), channel_id),
        )
        catalog_conn.commit()
    except discord.Forbidden as e:
        catalog_conn.execute(
            "UPDATE coverage SET status='inaccessible', gap_reason=?, last_checked_utc=? "
            "WHERE channel_id=?",
            (f"backfill failed: {e}", _now(), channel_id),
        )
        catalog_conn.commit()
    except discord.HTTPException as e:
        catalog_conn.execute(
            "UPDATE coverage SET status='failed', gap_reason=?, last_checked_utc=? "
            "WHERE channel_id=?",
            (f"backfill failed: {e}", _now(), channel_id),
        )
        catalog_conn.commit()
    except Exception as e:
        catalog_conn.execute(
            "UPDATE coverage SET status='failed', gap_reason=?, last_checked_utc=? "
            "WHERE channel_id=?",
            (f"backfill failed: {e}", _now(), channel_id),
        )
        catalog_conn.commit()


async def backfill_all_pending(client, catalog_conn: sqlite3.Connection, store: ShardStore,
                                 concurrency: int = 3) -> None:
    """Run backfill_channel for every channel/thread whose coverage
    status is 'pending', 'crawling', or 'failed', bounded by a semaphore
    so backfill never starves the gateway heartbeat or the rate-limit
    bucket shared with other bots on the same token (spec §2)."""
    rows = catalog_conn.execute(
        "SELECT channel_id FROM coverage WHERE status IN ('pending', 'crawling', 'failed')"
    ).fetchall()
    channel_ids = [row["channel_id"] for row in rows]
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(channel_id: str) -> None:
        async with semaphore:
            try:
                discord_channel = await client.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden) as e:
                catalog_conn.execute(
                    "UPDATE coverage SET status='inaccessible', gap_reason=?, last_checked_utc=? "
                    "WHERE channel_id=?",
                    (f"backfill failed: {e}", _now(), channel_id),
                )
                catalog_conn.commit()
                return
            await backfill_channel(discord_channel, catalog_conn, store)

    await asyncio.gather(*(_run_one(cid) for cid in channel_ids))

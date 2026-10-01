"""One-time migration: split existing category-month shard files (which
hold multiple channels' messages together) into per-channel-month shard
files, matching ShardStore's current path-resolution scheme.

Reads every row in catalog.channel_month_shard, computes each row's NEW
path via ShardStore's own resolution logic, and for any row whose
recorded path differs from the new scheme:
  1. Creates/opens the new per-channel shard file (full schema via
     connect_shard).
  2. ATTACHes the OLD category-month file to the new connection and
     copies every row belonging to that channel_id across all shard
     tables (messages + every child table), in one transaction.
  3. Verifies the row count copied for `messages` matches the source
     count exactly before proceeding.
  4. Updates channel_month_shard.shard_path to the new location.

Old category-month files are never deleted or modified -- they are left
in place as an implicit backup. Safe to re-run: any row already pointing
at a new-scheme path is skipped.

Usage: python scripts/migrate_to_per_channel_shards.py <data_dir>
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from archiver.db import connect_catalog, connect_shard
from archiver.fts import ensure_messages_fts
from archiver.store import ShardStore

SHARD_TABLES = [
    "messages", "attachments", "reactions", "mentions_user", "mentions_role",
    "stickers", "polls", "poll_answers", "embeds", "embed_fields", "events",
]
# Tables keyed directly by message_id (for the WHERE clause filtering to
# this channel's messages); "messages" itself is filtered by channel_id.
MESSAGE_KEYED_TABLES = [t for t in SHARD_TABLES if t != "messages"]


def migrate(data_dir: Path) -> None:
    catalog_conn = connect_catalog(data_dir / "catalog.sqlite")
    store = ShardStore(data_dir, catalog_conn)

    rows = catalog_conn.execute(
        "SELECT channel_id, yyyymm, shard_path FROM channel_month_shard"
    ).fetchall()

    migrated = 0
    skipped_already_new = 0
    skipped_missing_source = 0

    for row in rows:
        channel_id, yyyymm, old_relative_path = row["channel_id"], row["yyyymm"], row["shard_path"]
        new_relative_path = store._resolve_new_path(channel_id, yyyymm)

        if old_relative_path == new_relative_path:
            skipped_already_new += 1
            continue

        old_path = data_dir / old_relative_path
        if not old_path.exists():
            print(f"  SKIP (source missing): {channel_id} {yyyymm} -> {old_relative_path}")
            skipped_missing_source += 1
            continue

        new_path = data_dir / new_relative_path
        new_conn = connect_shard(new_path)
        ensure_messages_fts(new_conn)  # no-op if trigram FTS5 isn't available

        source_count = sqlite3.connect(old_path).execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)
        ).fetchone()[0]

        if source_count == 0:
            # Nothing to copy for this channel in this old file (it was
            # discovered but never actually wrote a message here) --
            # just repoint the catalog row.
            catalog_conn.execute(
                "UPDATE channel_month_shard SET shard_path=? WHERE channel_id=? AND yyyymm=?",
                (new_relative_path, channel_id, yyyymm),
            )
            catalog_conn.commit()
            migrated += 1
            continue

        new_conn.execute("ATTACH DATABASE ? AS old", (str(old_path),))
        try:
            new_conn.execute(
                "INSERT OR IGNORE INTO messages SELECT * FROM old.messages WHERE channel_id=?",
                (channel_id,),
            )
            for table in MESSAGE_KEYED_TABLES:
                new_conn.execute(
                    f"INSERT OR IGNORE INTO {table} SELECT * FROM old.{table} "
                    f"WHERE message_id IN (SELECT id FROM old.messages WHERE channel_id=?)",
                    (channel_id,),
                )
            new_conn.commit()
        finally:
            new_conn.execute("DETACH DATABASE old")

        dest_count = new_conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id=?", (channel_id,)
        ).fetchone()[0]
        if dest_count != source_count:
            raise RuntimeError(
                f"Migration verification failed for channel {channel_id} {yyyymm}: "
                f"source had {source_count} messages, destination has {dest_count}. "
                f"Old file left untouched at {old_path}."
            )

        catalog_conn.execute(
            "UPDATE channel_month_shard SET shard_path=? WHERE channel_id=? AND yyyymm=?",
            (new_relative_path, channel_id, yyyymm),
        )
        catalog_conn.commit()
        migrated += 1
        print(f"  OK: {channel_id} {yyyymm}: {old_relative_path} -> {new_relative_path} "
              f"({source_count} messages)")

    store.close_all()
    print(
        f"\nDone. migrated={migrated} already_new_scheme={skipped_already_new} "
        f"missing_source={skipped_missing_source}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/migrate_to_per_channel_shards.py <data_dir>")
        raise SystemExit(2)
    migrate(Path(sys.argv[1]))

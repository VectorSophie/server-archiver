"""Report dirty-detection and coverage-diffing. See the Stage 6 plan's
"Design ruling" for why this uses a fingerprint comparison rather than
a writer-maintained dirty flag: coverage.message_count/status/
newest_message_id change on most relevant writes, but not all of them
(an edit, a delete, or a rescan-recovered message can land in a shard
without moving any of those three columns) -- so the fingerprint also
stats every shard file belonging to the scope (mtime + size, and the
same for its `-wal` sidecar when present, since shards are opened in
WAL mode and a recent write may sit only in that sidecar). This is
still a proxy, not a content hash, but it's a cheap one that no longer
misses shard-level writes: pure `os.stat`, no shard connection opened
here."""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stat_part(path: Path) -> str:
    try:
        st = path.stat()
    except FileNotFoundError:
        return f"{path}:missing"
    return f"{path}:{st.st_mtime_ns}:{st.st_size}"


def compute_scope_fingerprint(catalog_conn: sqlite3.Connection, data_dir: Path, channel_ids: list[str]) -> str:
    if not channel_ids:
        return hashlib.sha256(b"empty").hexdigest()
    parts = []
    for channel_id in sorted(channel_ids):
        row = catalog_conn.execute(
            "SELECT status, message_count, newest_message_id FROM coverage WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        if row is None:
            parts.append(f"{channel_id}:missing")
        else:
            parts.append(f"{channel_id}:{row['status']}:{row['message_count']}:{row['newest_message_id']}")

        shard_rows = catalog_conn.execute(
            "SELECT DISTINCT shard_path FROM channel_month_shard WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
        for shard_path in sorted(r["shard_path"] for r in shard_rows):
            full_path = data_dir / shard_path
            parts.append(_stat_part(full_path))
            wal_path = full_path.with_name(full_path.name + "-wal")
            if wal_path.exists():
                parts.append(_stat_part(wal_path))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def is_scope_dirty(catalog_conn: sqlite3.Connection, scope: str, current_fingerprint: str) -> bool:
    row = catalog_conn.execute(
        "SELECT fingerprint FROM report_fingerprint WHERE scope=?", (scope,)
    ).fetchone()
    if row is None:
        return True
    return row["fingerprint"] != current_fingerprint


def mark_scope_generated(catalog_conn: sqlite3.Connection, scope: str, fingerprint: str) -> None:
    catalog_conn.execute(
        "INSERT INTO report_fingerprint (scope, fingerprint, generated_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(scope) DO UPDATE SET fingerprint=excluded.fingerprint, "
        "generated_utc=excluded.generated_utc",
        (scope, fingerprint, _now()),
    )
    catalog_conn.commit()


def diff_newly_inaccessible(catalog_conn: sqlite3.Connection, current_coverage: list[dict]) -> list[dict]:
    """Channels present in the previous report's coverage snapshot with
    a non-'inaccessible' status that are 'inaccessible' now. A channel
    with no prior snapshot (brand new) is never reported here -- there
    is nothing to diff against yet, and it isn't a *change*."""
    newly_inaccessible = []
    for entry in current_coverage:
        if entry["status"] != "inaccessible":
            continue
        prior = catalog_conn.execute(
            "SELECT status FROM report_last_coverage WHERE channel_id=?",
            (entry["channel_id"],),
        ).fetchone()
        if prior is not None and prior["status"] != "inaccessible":
            newly_inaccessible.append(entry)
    return newly_inaccessible


def save_coverage_snapshot(catalog_conn: sqlite3.Connection, current_coverage: list[dict]) -> None:
    for entry in current_coverage:
        catalog_conn.execute(
            "INSERT INTO report_last_coverage (channel_id, status) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET status=excluded.status",
            (entry["channel_id"], entry["status"]),
        )
    catalog_conn.commit()

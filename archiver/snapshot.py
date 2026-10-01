"""Point-in-time snapshots of shard files, with verify/restore (spec
§9). Uses sqlite3.Connection.backup() for a true consistent copy of
each shard -- never a raw filesystem copy of a file `archive live`
might be writing to concurrently -- then bundles the copies into one
tar.gz per (category, month), with a sha256 + manifest JSON alongside.

Design ruling: spec §9 says snapshots are "triggered daily for the
current month, and immediately whenever an older month's shard
receives a late write." Rather than wiring an invasive trigger into
every write path across archiver/store.py and archiver/live.py (the
exact kind of change this project has deliberately avoided before, in
Stage 6's report-dirty-detection design, for the same risk reasons --
see archiver/report_state.py's own docstring), this module reuses that
same proven technique: a cheap stat-based fingerprint (mtime+size) over
each scope's shard files, checked at `archive snapshot` run time. A
scope whose fingerprint changed since its last snapshot is considered
stale and gets re-snapshotted. Run on a daily schedule (Stage 8's job
to wire up via Task Scheduler), this single mechanism satisfies BOTH
triggers at once: the current month's shards are written to constantly
while `archive live` runs, so they're always stale on the next daily
run; an older month's shard only changes on a genuine late write (an
edit/delete on an old message), which the very next run detects and
re-snapshots -- "immediately" in the sense of "the next scheduled pass
notices it," not literally within milliseconds of the write, which
is proportionate for a personal archive tool with no other real-time
infrastructure."""
import hashlib
import json
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from archiver.db import connect_shard


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_snapshot_scopes(catalog_conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every (category_id, yyyymm) pair that has at least one shard file."""
    rows = catalog_conn.execute(
        "SELECT DISTINCT category_id, yyyymm FROM channel_month_shard ORDER BY category_id, yyyymm"
    ).fetchall()
    return [(r["category_id"], r["yyyymm"]) for r in rows]


def compute_shard_fingerprint(catalog_conn: sqlite3.Connection, data_dir: Path,
                               category_id: str, yyyymm: str) -> str:
    """A cheap stat-based fingerprint (mtime+size, no file content read)
    over every shard file belonging to this category+month -- the same
    technique archiver/report_state.py already uses for the same
    reason: it catches any write to the underlying files without
    opening or hashing them."""
    rows = catalog_conn.execute(
        "SELECT shard_path FROM channel_month_shard WHERE category_id=? AND yyyymm=? ORDER BY shard_path",
        (category_id, yyyymm),
    ).fetchall()
    parts = []
    for row in rows:
        path = data_dir / row["shard_path"]
        try:
            st = path.stat()
            parts.append(f"{row['shard_path']}:{st.st_mtime_ns}:{st.st_size}")
        except FileNotFoundError:
            parts.append(f"{row['shard_path']}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def is_snapshot_stale(catalog_conn: sqlite3.Connection, data_dir: Path,
                       category_id: str, yyyymm: str) -> bool:
    current = compute_shard_fingerprint(catalog_conn, data_dir, category_id, yyyymm)
    row = catalog_conn.execute(
        "SELECT fingerprint FROM snapshots WHERE category=? AND yyyymm=?",
        (category_id, yyyymm),
    ).fetchone()
    return row is None or row["fingerprint"] != current


def create_snapshot(catalog_conn: sqlite3.Connection, data_dir: Path, category_id: str,
                     yyyymm: str, snapshots_dir: Path, *, is_current_month: bool) -> dict:
    """Backs up every shard file for (category_id, yyyymm) via
    sqlite3's own .backup() API, bundles the copies into one tar.gz,
    computes its sha256, writes a self-describing manifest JSON
    alongside, and atomically replaces the prior snapshot pair (both
    files, and the catalog row) for this scope."""
    rows = catalog_conn.execute(
        "SELECT channel_id, shard_path FROM channel_month_shard WHERE category_id=? AND yyyymm=?",
        (category_id, yyyymm),
    ).fetchall()

    category_row = catalog_conn.execute(
        "SELECT name FROM category_names WHERE category_id=?", (category_id,)
    ).fetchone()
    category_name = category_row["name"] if category_row else category_id

    scope_dir = snapshots_dir / category_id
    scope_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = scope_dir / f".tmp-{yyyymm}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        row_count = 0
        for row in rows:
            src_path = data_dir / row["shard_path"]
            if not src_path.exists():
                continue
            dest_path = tmp_dir / f"{row['channel_id']}.sqlite"
            src_conn = connect_shard(src_path)
            try:
                dest_conn = sqlite3.connect(dest_path)
                try:
                    src_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
                row_count += src_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            finally:
                src_conn.close()

        tmp_tarball = scope_dir / f".tmp-{yyyymm}.tar.gz"
        with tarfile.open(tmp_tarball, "w:gz") as tar:
            for child in sorted(tmp_dir.iterdir()):
                tar.add(child, arcname=child.name)

        sha256 = _sha256_file(tmp_tarball)
        final_tarball = scope_dir / f"{yyyymm}.tar.gz"
        tmp_tarball.replace(final_tarball)  # atomic rename on the same volume

        created_utc = _now()
        manifest = {
            "category": category_id, "category_name": category_name, "yyyymm": yyyymm,
            "sha256": sha256, "row_count": row_count, "created_utc": created_utc,
            "is_partial": bool(is_current_month),
        }
        tmp_manifest = scope_dir / f".tmp-{yyyymm}.manifest.json"
        tmp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        final_manifest = scope_dir / f"{yyyymm}.manifest.json"
        tmp_manifest.replace(final_manifest)

        fingerprint = compute_shard_fingerprint(catalog_conn, data_dir, category_id, yyyymm)
        catalog_conn.execute(
            "DELETE FROM snapshots WHERE category=? AND yyyymm=?", (category_id, yyyymm)
        )
        catalog_conn.execute(
            "INSERT INTO snapshots (category, yyyymm, tarball_path, sha256, row_count, "
            "created_utc, is_partial, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (category_id, yyyymm, str(final_tarball.relative_to(data_dir)), sha256,
             row_count, created_utc, int(is_current_month), fingerprint),
        )
        catalog_conn.commit()
        return manifest
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def verify_snapshot(data_dir: Path, tarball_path: str) -> dict:
    """Re-extracts, checks the tarball's sha256 against its own
    manifest, and runs PRAGMA integrity_check on every shard file
    inside. Self-contained -- doesn't need the live catalog.sqlite,
    since a disaster-recovery scenario might not have one."""
    full_tarball = data_dir / tarball_path
    manifest_path = full_tarball.parent / (full_tarball.stem.removesuffix(".tar") + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    actual_sha256 = _sha256_file(full_tarball)
    sha256_ok = actual_sha256 == manifest["sha256"]

    extract_dir = full_tarball.parent / f".verify-{manifest['yyyymm']}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    try:
        with tarfile.open(full_tarball, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")
        shard_results = {}
        for shard_file in sorted(extract_dir.iterdir()):
            conn = sqlite3.connect(shard_file)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                shard_results[shard_file.name] = (result == "ok")
            finally:
                conn.close()
        integrity_ok = bool(shard_results) and all(shard_results.values())
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    return {
        "sha256_ok": sha256_ok, "integrity_ok": integrity_ok,
        "shard_results": shard_results, "manifest": manifest,
    }


def restore_snapshot(data_dir: Path, tarball_path: str, *, target_dir: Path | None = None,
                      force: bool = False) -> dict:
    """Verifies before ever touching a destination path -- a tarball
    that fails sha256 or integrity verification is never extracted.
    Within the destination, an existing file is never silently
    overwritten; pass force=True to allow it, or a different
    (non-colliding) target_dir to avoid needing force at all."""
    result = verify_snapshot(data_dir, tarball_path)
    if not result["sha256_ok"]:
        raise ValueError(f"snapshot sha256 mismatch for {tarball_path} -- refusing to restore")
    if not result["integrity_ok"]:
        raise ValueError(f"snapshot failed integrity check for {tarball_path} -- refusing to restore")

    manifest = result["manifest"]
    full_tarball = data_dir / tarball_path
    dest_dir = target_dir if target_dir is not None else (
        data_dir / "restored" / manifest["category"] / manifest["yyyymm"]
    )
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(full_tarball, "r:gz") as tar:
        for member in tar.getmembers():
            dest_path = dest_dir / member.name
            if dest_path.exists() and not force:
                raise FileExistsError(
                    f"{dest_path} already exists -- use --force or a different --target to restore"
                )
        tar.extractall(dest_dir, filter="data")

    return {"restored_to": str(dest_dir), "manifest": manifest}

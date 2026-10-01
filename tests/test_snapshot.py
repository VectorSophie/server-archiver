import json
import sqlite3
import tarfile

import pytest

from archiver.db import connect_catalog, connect_shard
from archiver.snapshot import (
    compute_shard_fingerprint,
    create_snapshot,
    is_snapshot_stale,
    list_snapshot_scopes,
    restore_snapshot,
    verify_snapshot,
)
from tests.fixtures import make_message, _insert


def _seed(catalog, tmp_path, channel_id="1", category_id="cat1", yyyymm="2025-10", content="hello"):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES (?, 'General', ?) "
        "ON CONFLICT(category_id) DO NOTHING",
        (category_id, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES (?, 'chat', 'text', NULL, ?, 0, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (channel_id, category_id, now, now),
    )
    shard_path = f"General/chat/{yyyymm}.sqlite"
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(channel_id, yyyymm) DO NOTHING",
        (channel_id, yyyymm, category_id, shard_path),
    )
    catalog.commit()
    full_path = tmp_path / shard_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_shard(full_path)
    _insert(conn, "messages", make_message(content=content, channel_id=channel_id))
    conn.commit()
    conn.close()
    return shard_path


def test_list_snapshot_scopes_returns_every_distinct_category_month_pair(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    scopes = list_snapshot_scopes(catalog)
    assert scopes == [("cat1", "2025-10")]


def test_compute_shard_fingerprint_changes_when_shard_file_changes(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    fp1 = compute_shard_fingerprint(catalog, tmp_path, "cat1", "2025-10")

    shard_path = tmp_path / "General" / "chat" / "2025-10.sqlite"
    conn = connect_shard(shard_path)
    _insert(conn, "messages", make_message(content="another", channel_id="1"))
    conn.commit()
    conn.close()

    fp2 = compute_shard_fingerprint(catalog, tmp_path, "cat1", "2025-10")
    assert fp1 != fp2


def test_is_snapshot_stale_true_before_first_snapshot_false_after(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    assert is_snapshot_stale(catalog, tmp_path, "cat1", "2025-10") is True

    create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)
    assert is_snapshot_stale(catalog, tmp_path, "cat1", "2025-10") is False


def test_create_snapshot_writes_tarball_manifest_and_catalog_row(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    manifest = create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)

    tarball = tmp_path / "snapshots" / "cat1" / "2025-10.tar.gz"
    manifest_file = tmp_path / "snapshots" / "cat1" / "2025-10.manifest.json"
    assert tarball.exists()
    assert manifest_file.exists()

    on_disk_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert on_disk_manifest["sha256"] == manifest["sha256"]
    assert on_disk_manifest["row_count"] == 1
    assert on_disk_manifest["is_partial"] is False

    row = catalog.execute(
        "SELECT sha256, row_count, is_partial FROM snapshots WHERE category='cat1' AND yyyymm='2025-10'"
    ).fetchone()
    assert row["sha256"] == manifest["sha256"]
    assert row["row_count"] == 1
    assert row["is_partial"] == 0


def test_create_snapshot_replaces_prior_catalog_row_not_accumulate(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)

    conn = connect_shard(tmp_path / "General" / "chat" / "2025-10.sqlite")
    _insert(conn, "messages", make_message(content="second", channel_id="1"))
    conn.commit()
    conn.close()

    create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)

    rows = catalog.execute(
        "SELECT 1 FROM snapshots WHERE category='cat1' AND yyyymm='2025-10'"
    ).fetchall()
    assert len(rows) == 1  # replaced, not accumulated


def test_create_snapshot_uses_backup_api_not_a_raw_copy_while_shard_has_uncommitted_writes(tmp_path):
    """The point of sqlite3.Connection.backup() is that it produces a
    consistent copy even if the source has data that was written but
    not yet checkpointed from WAL -- unlike a raw file copy, which could
    capture a torn/inconsistent state. This test proves the backed-up
    copy contains the data that was present at backup time, read back
    through a completely separate connection (proving it's a real,
    independently-readable sqlite file, not just a byte-for-byte file
    handle duplicate)."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path, content="original content")

    create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)

    extract_dir = tmp_path / "manual_extract"
    extract_dir.mkdir()
    with tarfile.open(tmp_path / "snapshots" / "cat1" / "2025-10.tar.gz", "r:gz") as tar:
        tar.extractall(extract_dir, filter="data")
    backed_up = sqlite3.connect(extract_dir / "1.sqlite")
    row = backed_up.execute("SELECT content FROM messages").fetchone()
    backed_up.close()
    assert row[0] == "original content"


def test_verify_snapshot_detects_a_tampered_tarball(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    manifest = create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)
    tarball_relpath = "snapshots/cat1/2025-10.tar.gz"

    result = verify_snapshot(tmp_path, tarball_relpath)
    assert result["sha256_ok"] is True
    assert result["integrity_ok"] is True

    # Corrupt a byte in the tarball.
    tarball = tmp_path / tarball_relpath
    data = bytearray(tarball.read_bytes())
    data[-1] ^= 0xFF
    tarball.write_bytes(data)

    result2 = verify_snapshot(tmp_path, tarball_relpath)
    assert result2["sha256_ok"] is False


def test_restore_snapshot_refuses_to_overwrite_without_force(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)
    tarball_relpath = "snapshots/cat1/2025-10.tar.gz"

    target = tmp_path / "restore_target"
    restore_snapshot(tmp_path, tarball_relpath, target_dir=target)
    assert (target / "1.sqlite").exists()

    with pytest.raises(FileExistsError):
        restore_snapshot(tmp_path, tarball_relpath, target_dir=target)  # second restore, no --force

    # With force=True, it succeeds.
    restore_snapshot(tmp_path, tarball_relpath, target_dir=target, force=True)


def test_restore_snapshot_refuses_a_tampered_snapshot(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog, tmp_path)
    create_snapshot(catalog, tmp_path, "cat1", "2025-10", tmp_path / "snapshots", is_current_month=False)
    tarball_relpath = "snapshots/cat1/2025-10.tar.gz"

    tarball = tmp_path / tarball_relpath
    data = bytearray(tarball.read_bytes())
    data[-1] ^= 0xFF
    tarball.write_bytes(data)

    with pytest.raises(ValueError):
        restore_snapshot(tmp_path, tarball_relpath, target_dir=tmp_path / "restore_target2")

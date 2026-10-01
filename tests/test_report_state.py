from archiver.db import connect_catalog
from archiver.report_state import (
    compute_scope_fingerprint, is_scope_dirty, mark_scope_generated,
    diff_newly_inaccessible, save_coverage_snapshot,
    get_previous_filename, all_known_scopes, forget_scope,
)


def _seed_channel(catalog, channel_id, status="complete", message_count=5, newest_message_id="100"):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES (?, 'chan', 'text', NULL, NULL, 0, ?, ?)",
        (channel_id, now, now),
    )
    catalog.execute(
        "INSERT INTO coverage (channel_id, status, message_count, newest_message_id) "
        "VALUES (?, ?, ?, ?)",
        (channel_id, status, message_count, newest_message_id),
    )
    catalog.commit()


def test_fingerprint_is_stable_for_unchanged_data(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    fp1 = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    fp2 = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    assert fp1 == fp2


def test_fingerprint_changes_when_message_count_changes(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1", message_count=5)
    fp1 = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    catalog.execute("UPDATE coverage SET message_count=6 WHERE channel_id='1'")
    catalog.commit()
    fp2 = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    assert fp1 != fp2


def test_fingerprint_independent_of_channel_id_order(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    _seed_channel(catalog, "2")
    fp_a = compute_scope_fingerprint(catalog, tmp_path, ["1", "2"])
    fp_b = compute_scope_fingerprint(catalog, tmp_path, ["2", "1"])
    assert fp_a == fp_b


def test_fingerprint_changes_when_shard_file_is_modified_without_coverage_change(tmp_path):
    # Reproduces the failure mode the final review demonstrated: an edit or
    # delete inside a shard doesn't move coverage.status/message_count/
    # newest_message_id at all, so only a fingerprint that also stats the
    # shard file itself can detect it.
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chan/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chan" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    shard_path.write_bytes(b"original content")

    current_fingerprint = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    mark_scope_generated(catalog, "server", current_fingerprint, "server.md")
    assert is_scope_dirty(catalog, "server", current_fingerprint) is False

    shard_path.write_bytes(b"original content, but edited")

    new_fingerprint = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    assert is_scope_dirty(catalog, "server", new_fingerprint) is True


def test_scope_is_dirty_before_first_generation(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    fingerprint = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    assert is_scope_dirty(catalog, "server", fingerprint) is True


def test_scope_not_dirty_after_marking_generated_with_unchanged_data(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    fingerprint = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    mark_scope_generated(catalog, "server", fingerprint, "server.md")
    assert is_scope_dirty(catalog, "server", fingerprint) is False


def test_scope_dirty_again_after_data_changes(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1", message_count=5)
    fingerprint = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    mark_scope_generated(catalog, "server", fingerprint, "server.md")
    catalog.execute("UPDATE coverage SET message_count=6 WHERE channel_id='1'")
    catalog.commit()
    new_fingerprint = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    assert is_scope_dirty(catalog, "server", new_fingerprint) is True


def test_diff_newly_inaccessible_flags_only_status_change(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1", status="complete")
    _seed_channel(catalog, "2", status="inaccessible")
    save_coverage_snapshot(catalog, [
        {"channel_id": "1", "status": "complete"},
        {"channel_id": "2", "status": "complete"},  # was complete last run
    ])
    current = [
        {"channel_id": "1", "status": "complete"},       # unchanged
        {"channel_id": "2", "status": "inaccessible"},   # just went inaccessible
    ]
    newly_inaccessible = diff_newly_inaccessible(catalog, current)
    assert [c["channel_id"] for c in newly_inaccessible] == ["2"]


def test_diff_newly_inaccessible_ignores_channel_with_no_prior_snapshot(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    current = [{"channel_id": "99", "status": "inaccessible"}]  # brand new channel, never snapshotted
    newly_inaccessible = diff_newly_inaccessible(catalog, current)
    assert newly_inaccessible == []


def test_mark_scope_generated_stores_filename(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    fp = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    mark_scope_generated(catalog, "server", fp, "server.md")
    assert get_previous_filename(catalog, "server") == "server.md"


def test_get_previous_filename_none_before_first_generation(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    assert get_previous_filename(catalog, "server") is None


def test_all_known_scopes_lists_every_recorded_scope_and_filename(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    fp = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    mark_scope_generated(catalog, "server", fp, "server.md")
    mark_scope_generated(catalog, "category:cat1", fp, "General-abc123.md")
    assert all_known_scopes(catalog) == {"server": "server.md", "category:cat1": "General-abc123.md"}


def test_forget_scope_removes_it_from_all_known_scopes(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, "1")
    fp = compute_scope_fingerprint(catalog, tmp_path, ["1"])
    mark_scope_generated(catalog, "category:cat1", fp, "General-abc123.md")
    forget_scope(catalog, "category:cat1")
    assert all_known_scopes(catalog) == {}

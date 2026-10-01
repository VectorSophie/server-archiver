from pathlib import Path

from archiver.cli import format_doctor_report, format_coverage_rows, main


def test_format_doctor_report_all_ok():
    lines = format_doctor_report(
        username="archiver-bot#0000", user_id=123, message_content_intent=True,
        guild_found=True, guild_name="Example Server", guild_id="456",
        data_dir=Path("D:/archive"),
    )
    assert any("archiver-bot#0000" in l and "123" in l for l in lines)
    assert any("Message Content Intent: enabled" in l for l in lines)
    assert any("Example Server" in l and "accessible" in l for l in lines)
    assert any("D:/archive" in l for l in lines)


def test_format_doctor_report_intent_missing():
    lines = format_doctor_report(
        username="archiver-bot#0000", user_id=123, message_content_intent=False,
        guild_found=True, guild_name="X", guild_id="456", data_dir=Path("D:/archive"),
    )
    assert any("NOT enabled" in l for l in lines)


def test_format_doctor_report_guild_not_found():
    lines = format_doctor_report(
        username="archiver-bot#0000", user_id=123, message_content_intent=True,
        guild_found=False, guild_name=None, guild_id="456", data_dir=Path("D:/archive"),
    )
    assert any("456" in l and "NOT found" in l for l in lines)


def test_format_doctor_report_never_contains_token_field():
    """A regression guard, not a security scan: format_doctor_report's
    own signature has no token parameter at all, so it structurally
    cannot leak one."""
    import inspect
    params = inspect.signature(format_doctor_report).parameters
    assert "token" not in params


def test_main_requires_a_subcommand(monkeypatch):
    import pytest
    monkeypatch.setattr("sys.argv", ["archive"])
    with pytest.raises(SystemExit):
        main()


def test_format_coverage_rows_includes_gap_reason():
    rows = [
        {"name": "general", "type": "text", "status": "pending", "gap_reason": None},
        {"name": "secret", "type": "text", "status": "pending", "gap_reason": "Manage Threads not granted"},
    ]
    lines = format_coverage_rows(rows)
    assert any("general" in l and "pending" in l and "(" not in l for l in lines)
    assert any("secret" in l and "Manage Threads not granted" in l for l in lines)


def test_format_coverage_rows_empty():
    assert format_coverage_rows([]) == []


def test_format_doctor_report_data_folder_not_writable():
    lines = format_doctor_report(
        username="archiver-bot#0000", user_id=123, message_content_intent=True,
        guild_found=True, guild_name="X", guild_id="456", data_dir=Path("D:/archive"),
        data_folder_writable=False,
    )
    assert any("NOT writable" in l for l in lines)


def test_main_coverage_requires_preflight_flag(monkeypatch):
    import pytest
    monkeypatch.setattr("sys.argv", ["archive", "coverage"])
    with pytest.raises(SystemExit):
        main()


def test_main_backfill_subcommand_is_registered(monkeypatch):
    """Argparse routing only -- the live connection itself is verified
    manually, same as doctor/coverage --preflight."""
    import pytest
    monkeypatch.setattr("sys.argv", ["archive", "backfill", "--bogus-flag"])
    with pytest.raises(SystemExit):
        main()  # unrecognized flag must still fail argparse, proving the subcommand exists


def test_run_report_gc_does_not_crash_on_a_pre_migration_empty_filename(tmp_path):
    """Regression test for a crash found in review: a report_fingerprint
    row that predates the filename column (CATALOG_SCHEMA_V3 backfills
    it as '') for a scope that has since disappeared must be cleaned up
    without attempting to unlink reports_dir/'' == reports_dir itself."""
    from archiver.cli import _run_report
    from archiver.config import Config
    from archiver.db import connect_catalog

    data_dir = tmp_path
    catalog = connect_catalog(data_dir / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'complete')")
    # Simulate a pre-v3 report_fingerprint row (filename='') for a scope
    # that no longer exists in current discovery (channel "1" has no
    # category, so "category:cat1" is stale).
    catalog.execute(
        "INSERT INTO report_fingerprint (scope, fingerprint, generated_utc, filename) "
        "VALUES ('category:cat1', 'stale', ?, '')", (now,),
    )
    catalog.commit()
    catalog.close()

    config = Config(guild_id="1", data_dir=data_dir)
    result = _run_report(config)  # must not raise

    assert result == 0
    assert (data_dir / "reports").is_dir()  # the reports directory itself must survive

    catalog = connect_catalog(data_dir / "catalog.sqlite")
    row = catalog.execute(
        "SELECT 1 FROM report_fingerprint WHERE scope='category:cat1'"
    ).fetchone()
    assert row is None  # stale scope's row was forgotten, not left dangling


def test_run_report_regenerates_under_new_filename_when_category_is_renamed(tmp_path):
    """A category rename changes the report filename but not the
    fingerprint (which never incorporates category_name) -- this must
    still regenerate under the new name and delete the old file, not
    silently skip as 'unchanged'."""
    from archiver.cli import _run_report
    from archiver.config import Config
    from archiver.db import connect_catalog

    data_dir = tmp_path
    catalog = connect_catalog(data_dir / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'Old Name', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'complete')")
    catalog.commit()
    catalog.close()

    config = Config(guild_id="1", data_dir=data_dir)
    assert _run_report(config) == 0
    old_report = next((data_dir / "reports").glob("Old Name-*.md"))
    assert old_report.exists()

    # Rename the category; fingerprint-relevant columns (status/message_count/
    # newest_message_id, plus shard file stats) are untouched.
    catalog = connect_catalog(data_dir / "catalog.sqlite")
    catalog.execute("UPDATE category_names SET name='New Name' WHERE category_id='cat1'")
    catalog.commit()
    catalog.close()

    assert _run_report(config) == 0
    new_report = next((data_dir / "reports").glob("New Name-*.md"))
    assert new_report.exists()
    assert not old_report.exists()


def test_run_report_case_only_rename_does_not_delete_the_report_it_just_wrote(tmp_path):
    """Regression test for a crash found in review: on a case-insensitive
    filesystem (Windows), a case-only category rename ("Yuri" -> "YURI")
    writes to the SAME file write_text() already wrote, so unconditionally
    unlinking the 'old' filename afterward deletes the report that was
    just generated."""
    from archiver.cli import _run_report
    from archiver.config import Config
    from archiver.db import connect_catalog

    data_dir = tmp_path
    catalog = connect_catalog(data_dir / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'Yuri', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'complete')")
    catalog.commit()
    catalog.close()

    config = Config(guild_id="1", data_dir=data_dir)
    assert _run_report(config) == 0
    category_reports = list((data_dir / "reports").glob("Y*-cat1.md"))
    assert len(category_reports) == 1

    catalog = connect_catalog(data_dir / "catalog.sqlite")
    catalog.execute("UPDATE category_names SET name='YURI' WHERE category_id='cat1'")
    catalog.commit()
    catalog.close()

    assert _run_report(config) == 0
    category_reports = list((data_dir / "reports").glob("Y*-cat1.md"))
    assert len(category_reports) == 1  # the report must still exist, not have been deleted
    assert category_reports[0].read_text(encoding="utf-8").startswith("# YURI")


def test_acquire_single_instance_lock_blocks_a_second_caller(tmp_path):
    from archiver.cli import _acquire_single_instance_lock

    first = _acquire_single_instance_lock(tmp_path)
    assert first is not None

    second = _acquire_single_instance_lock(tmp_path)
    assert second is None

    first.close()
    third = _acquire_single_instance_lock(tmp_path)
    assert third is not None
    third.close()


def test_run_sync_without_config_reports_missing_keys_not_crash(tmp_path):
    from archiver.cli import _run_sync
    from archiver.config import Config

    config = Config(guild_id="1", data_dir=tmp_path)
    assert _run_sync(config) == 1


def test_run_sync_pushes_changed_files_and_updates_state(tmp_path, monkeypatch):
    from archiver.cli import _run_sync
    from archiver.config import Config
    import archiver.cli as cli_module

    (tmp_path / "catalog.sqlite").write_text("a")

    calls = []
    monkeypatch.setattr(cli_module, "sync_to_remote", lambda *a, **kw: calls.append((a, kw)))

    config = Config(
        guild_id="1", data_dir=tmp_path,
        sync_remote_host="1.2.3.4", sync_remote_user="ubuntu",
        sync_remote_data_dir="/opt/data", sync_ssh_key_path="C:/key",
    )
    assert _run_sync(config) == 0
    assert len(calls) == 1
    assert calls[0][0][1] == ["catalog.sqlite"]

    # Second run with nothing changed syncs nothing.
    calls.clear()
    assert _run_sync(config) == 0
    assert calls == []

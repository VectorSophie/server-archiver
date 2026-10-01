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

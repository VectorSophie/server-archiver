from pathlib import Path

from archiver.sync import changed_files, load_sync_state, save_sync_state, sync_to_remote


def test_changed_files_includes_every_file_on_first_run(tmp_path):
    (tmp_path / "catalog.sqlite").write_text("a")
    (tmp_path / "General").mkdir()
    (tmp_path / "General" / "chat-2025-10.sqlite").write_text("b")
    changed = changed_files(tmp_path, previous_state={})
    assert set(changed) == {"catalog.sqlite", "General/chat-2025-10.sqlite"}


def test_changed_files_skips_unmodified_files_on_second_run(tmp_path):
    (tmp_path / "catalog.sqlite").write_text("a")
    first = changed_files(tmp_path, previous_state={})
    state = {rel: list(_stat(tmp_path / rel)) for rel in first}
    changed = changed_files(tmp_path, previous_state=state)
    assert changed == []


def test_changed_files_detects_a_modified_file(tmp_path):
    path = tmp_path / "catalog.sqlite"
    path.write_text("a")
    first = changed_files(tmp_path, previous_state={})
    state = {rel: list(_stat(tmp_path / rel)) for rel in first}

    path.write_text("a longer new content")
    changed = changed_files(tmp_path, previous_state=state)
    assert changed == ["catalog.sqlite"]


def test_changed_files_detects_a_new_file_without_touching_unrelated_ones(tmp_path):
    (tmp_path / "catalog.sqlite").write_text("a")
    first = changed_files(tmp_path, previous_state={})
    state = {rel: list(_stat(tmp_path / rel)) for rel in first}

    (tmp_path / "General").mkdir()
    (tmp_path / "General" / "new-2025-11.sqlite").write_text("c")
    changed = changed_files(tmp_path, previous_state=state)
    assert changed == ["General/new-2025-11.sqlite"]


def test_changed_files_ignores_the_sync_state_file_itself(tmp_path):
    (tmp_path / ".sync_state.json").write_text("{}")
    (tmp_path / "catalog.sqlite").write_text("a")
    changed = changed_files(tmp_path, previous_state={})
    assert changed == ["catalog.sqlite"]


def test_save_then_load_sync_state_round_trips(tmp_path):
    state_path = tmp_path / ".sync_state.json"
    save_sync_state(state_path, {"catalog.sqlite": [123, 4]})
    assert load_sync_state(state_path) == {"catalog.sqlite": [123, 4]}


def test_load_sync_state_missing_file_returns_empty_dict(tmp_path):
    assert load_sync_state(tmp_path / "does-not-exist.json") == {}


def test_sync_to_remote_calls_mkdir_then_scp_per_file(tmp_path, monkeypatch):
    (tmp_path / "General").mkdir()
    (tmp_path / "General" / "chat-2025-10.sqlite").write_text("b")
    (tmp_path / "catalog.sqlite").write_text("a")

    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)
        assert check is True

    monkeypatch.setattr("archiver.sync.subprocess.run", fake_run)

    sync_to_remote(
        tmp_path, ["General/chat-2025-10.sqlite", "catalog.sqlite"],
        ssh_key=Path("C:/key"), remote_user="ubuntu", remote_host="1.2.3.4",
        remote_data_dir="/opt/sxwt-archive/data",
    )

    mkdir_calls = [c for c in calls if "mkdir" in c]
    scp_calls = [c for c in calls if c[0] == "scp"]
    assert len(scp_calls) == 2
    assert any("/opt/sxwt-archive/data/General" in " ".join(c) for c in mkdir_calls)
    assert any(str(tmp_path / "catalog.sqlite") in c for c in scp_calls)


def _stat(path: Path):
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)

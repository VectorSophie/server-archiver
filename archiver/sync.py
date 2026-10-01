"""Syncs archive data from the Windows capture machine to the deployed
read-only mirror (the Oracle VM). Transfers only changed files,
detected via the same stat-based fingerprint technique used by
archiver/report_state.py and archiver/snapshot.py (mtime+size, no
content read). The remote app opens a fresh sqlite connection per
request (archiver/search.py), so no remote service restart is needed
after a sync -- new files are visible on the very next search.

ponytail: a file deleted/renamed on the Windows side is never removed
from the remote -- it just sits there orphaned. Add real deletion sync
if that ever actually causes a problem; for a personal archive that
only grows, it hasn't."""
import json
import posixpath
import subprocess
from pathlib import Path

SYNC_STATE_FILENAME = ".sync_state.json"


def _walk_files(data_dir: Path) -> dict[str, list[int]]:
    out = {}
    for path in data_dir.rglob("*"):
        if path.is_file() and path.name != SYNC_STATE_FILENAME:
            rel = path.relative_to(data_dir).as_posix()
            st = path.stat()
            out[rel] = [st.st_mtime_ns, st.st_size]
    return out


def load_sync_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sync_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def changed_files(data_dir: Path, previous_state: dict) -> list[str]:
    current = _walk_files(data_dir)
    return sorted(rel for rel, fp in current.items() if previous_state.get(rel) != fp)


def sync_to_remote(data_dir: Path, relpaths: list[str], *, ssh_key: Path,
                    remote_user: str, remote_host: str, remote_data_dir: str) -> None:
    for rel in relpaths:
        remote_parent = posixpath.join(remote_data_dir, posixpath.dirname(rel)) if "/" in rel else remote_data_dir
        subprocess.run(
            ["ssh", "-i", str(ssh_key), f"{remote_user}@{remote_host}", "mkdir", "-p", remote_parent],
            check=True,
        )
        subprocess.run(
            ["scp", "-i", str(ssh_key), str(data_dir / rel), f"{remote_user}@{remote_host}:{remote_data_dir}/{rel}"],
            check=True,
        )

"""Project configuration: guild_id and data_dir loaded from config.json,
next to run.py. config.json is machine-specific (real guild ID, real
local path) and is gitignored; config.example.json is the committed
template. Paths are configurable via pathlib — never hardcode a Windows
username or drive letter."""
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    guild_id: str
    data_dir: Path
    excluded_ranking_author_ids: tuple[str, ...] = ()
    secure_cookies: bool = True
    sync_remote_host: str | None = None
    sync_remote_user: str | None = None
    sync_remote_data_dir: str | None = None
    sync_ssh_key_path: str | None = None


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("guild_id", "data_dir") if k not in raw]
    if missing:
        raise ValueError(f"config.json missing required key(s): {', '.join(missing)}")
    return Config(
        guild_id=str(raw["guild_id"]),
        data_dir=Path(raw["data_dir"]),
        excluded_ranking_author_ids=tuple(str(x) for x in raw.get("excluded_ranking_author_ids", [])),
        secure_cookies=bool(raw.get("secure_cookies", True)),
        sync_remote_host=raw.get("sync_remote_host"),
        sync_remote_user=raw.get("sync_remote_user"),
        sync_remote_data_dir=raw.get("sync_remote_data_dir"),
        sync_ssh_key_path=raw.get("sync_ssh_key_path"),
    )

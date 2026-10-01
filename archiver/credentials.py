"""League-member login credentials for the web search app. Codename +
bcrypt-hashed password, set only via `archive useradd` -- there is no
web-facing account management of any kind."""
import json
import secrets
from pathlib import Path

import bcrypt


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"secret_key": secrets.token_hex(32), "users": {}}


def verify_login(codename: str, password: str, path: Path) -> bool:
    data = _load(path)
    stored_hash = data.get("users", {}).get(codename)
    if stored_hash is None:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))


def add_user(codename: str, password: str, path: Path) -> None:
    data = _load(path)
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    data.setdefault("users", {})[codename] = hashed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_secret_key(path: Path) -> str:
    data = _load(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data["secret_key"]

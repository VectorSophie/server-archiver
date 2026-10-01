# Stage 2: Full Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover every in-scope channel and thread in the target Discord guild (text/announcement/forum/media/voice channels, their active and archived threads, public and — where permitted — private) and record what was found in the catalog's `channels`/`category_names`/`coverage` tables, plus `doctor` and `coverage --preflight` CLI commands that surface bot identity, intent status, and per-channel accessibility without doing any message backfill.

**Architecture:** `archiver/config.py` loads `guild_id`/`data_dir` from `config.json`. `archiver/discord_io.py` holds pure, discord.py-object-shaped helpers (channel-type classification, per-channel thread pagination with the Manage-Threads-missing fallback, token loading) that don't need a live connection to test. `archiver/discovery.py` orchestrates a full guild walk using those helpers and upserts into the catalog opened via Stage 1's `connect_catalog`. `archiver/cli.py` is a thin `argparse` wrapper that opens one `discord.Client`, runs the discovery/identity check inside `on_ready`, and closes — its formatting logic is extracted into pure functions so it's testable without a live connection.

**Tech Stack:** `discord.py>=2.5` (already a dependency, unused until now), stdlib `argparse`/`asyncio`/`json`/`dataclasses`. `pytest-asyncio` is a new dev dependency for testing the async discovery/thread-pagination code. Hand-built fake discord.py objects (`tests/discord_fakes.py`) — never `unittest.mock.MagicMock` — for the same reason as Stage 1's fixtures: a fake missing an attribute the code reads should fail loudly.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (see §3 single-instance/doctor, §5 Discovery, §10.3 coexistence/doctor, §11 Testing, §15 Stage 2 scope)

## Global Constraints

- Never print the bot token, anywhere, for any reason (spec §3, §10.3).
- Discovery never trusts a cached channel list — every call re-fetches from Discord (spec §5). This stage has no "cache reuse" path at all yet; that's a non-goal here.
- A channel/thread previously known but not seen in a discovery pass is marked `coverage.status='inaccessible'`, never deleted from `coverage` (spec §5, §4).
- Reduced private-thread coverage (joined-only fallback when `Manage Threads` isn't granted) is recorded explicitly via `coverage.gap_reason`, never silently (spec §5).
- Discord IDs are stored and compared as `TEXT`, matching Stage 1's schema (spec §4).
- No hardcoded paths, usernames, or drive letters — `config.json` + `pathlib` (spec §2). `config.json` itself is machine-specific (real guild ID, real local data path) and must NOT be committed; a `config.example.json` with placeholder values is committed instead, and `config.json` is added to `.gitignore`.
- Rely on discord.py's own pagination (`async for ... in channel.archived_threads(...)`) and its own rate-limit handling — no custom pagination or rate-limiter logic (ponytail: stdlib/already-installed-dependency first).
- `doctor` never claims success partially — it reports bot identity, Message Content Intent status, configured-guild accessibility, and data-folder writability as separate, explicit lines (spec §3, §10.3).

---

## File Structure

- `archiver/config.py` — `Config` dataclass, `load_config(path)`.
- `archiver/discord_io.py` — `classify_channel_type`, `resolve_category_id`, `discover_threads`, `load_token`.
- `archiver/discovery.py` — `discover_guild(guild, catalog_conn) -> dict` (the orchestrator) plus its private upsert helpers.
- `archiver/cli.py` — `doctor` and `coverage --preflight` commands: pure formatters (`format_doctor_report`, `format_coverage_rows`) plus thin async orchestration (`_run_doctor`, `_run_coverage_preflight`) and `main()`.
- `config.example.json` — committed template with placeholder values.
- `.gitignore` — add `config.json`.
- `tests/discord_fakes.py` — hand-built fake `FakeResponse`, `FakeThread`, `FakeChannel`, `FakeCategory`, `FakeGuild`.
- `tests/test_config.py`, `tests/test_discord_io.py`, `tests/test_discovery.py`, `tests/test_cli.py`.
- `requirements.txt` — add `pytest-asyncio`.
- `pytest.ini` — add `asyncio_mode = auto`.

---

### Task 1: Config loading

**Files:**
- Create: `archiver/config.py`
- Create: `config.example.json`
- Modify: `.gitignore` (add `config.json`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `archiver.config.Config` — frozen dataclass with `guild_id: str`, `data_dir: pathlib.Path`.
  - `archiver.config.load_config(path: pathlib.Path) -> Config` — raises `ValueError` naming the missing key(s) if `guild_id` or `data_dir` is absent from the JSON file.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json
from pathlib import Path

import pytest

from archiver.config import load_config


def test_load_config_reads_guild_id_and_data_dir(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"guild_id": "123456789012345678", "data_dir": "D:/archive"}),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.guild_id == "123456789012345678"
    assert config.data_dir == Path("D:/archive")


def test_load_config_missing_key_raises(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"guild_id": "123"}), encoding="utf-8")
    with pytest.raises(ValueError, match="data_dir"):
        load_config(config_path)


def test_load_config_coerces_numeric_guild_id_to_string(tmp_path: Path):
    """config.json authored by hand might have an unquoted numeric guild_id;
    the catalog always stores/compares IDs as TEXT (spec §4)."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"guild_id": 123456789012345678, "data_dir": "D:/archive"}),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.guild_id == "123456789012345678"
    assert isinstance(config.guild_id, str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.config'`.

- [ ] **Step 3: Write `archiver/config.py`**

```python
# archiver/config.py
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


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("guild_id", "data_dir") if k not in raw]
    if missing:
        raise ValueError(f"config.json missing required key(s): {', '.join(missing)}")
    return Config(guild_id=str(raw["guild_id"]), data_dir=Path(raw["data_dir"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Create the committed example config and gitignore the real one**

`config.example.json`:
```json
{
  "guild_id": "000000000000000000",
  "data_dir": "C:/ChangeMe/server-archive"
}
```

Append to `.gitignore`:
```
config.json
```

- [ ] **Step 6: Commit**

```bash
git add archiver/config.py tests/test_config.py config.example.json .gitignore
git commit -m "feat: load guild_id/data_dir from config.json"
```

---

### Task 2: Channel type classification

**Files:**
- Create: `archiver/discord_io.py`
- Test: `tests/test_discord_io.py`

**Interfaces:**
- Produces:
  - `archiver.discord_io.classify_channel_type(channel) -> str | None` — maps a discord.py channel/thread object's `.type` (a `discord.ChannelType`) to our schema's type string (`"text"`, `"announcement"`, `"forum"`, `"media"`, `"voice"`, `"public_thread"`, `"private_thread"`); returns `None` for out-of-scope types (categories, stage channels, etc.) so callers can skip them.
  - `archiver.discord_io.resolve_category_id(channel) -> str | None` — reads `channel.category_id` (present on top-level channels; threads don't carry this attribute directly, callers resolve a thread's category via its parent channel — see Task 4).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_discord_io.py
import discord

from archiver.discord_io import classify_channel_type, resolve_category_id


class _Obj:
    """Minimal stand-in carrying only the attributes classify/resolve read."""
    def __init__(self, type_, category_id=None):
        self.type = type_
        self.category_id = category_id


def test_classify_text_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.text)) == "text"


def test_classify_announcement_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.news)) == "announcement"


def test_classify_forum_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.forum)) == "forum"


def test_classify_media_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.media)) == "media"


def test_classify_voice_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.voice)) == "voice"


def test_classify_public_thread():
    assert classify_channel_type(_Obj(discord.ChannelType.public_thread)) == "public_thread"


def test_classify_private_thread():
    assert classify_channel_type(_Obj(discord.ChannelType.private_thread)) == "private_thread"


def test_classify_out_of_scope_type_returns_none():
    assert classify_channel_type(_Obj(discord.ChannelType.category)) is None
    assert classify_channel_type(_Obj(discord.ChannelType.stage_voice)) is None


def test_resolve_category_id_present():
    assert resolve_category_id(_Obj(discord.ChannelType.text, category_id=555)) == "555"


def test_resolve_category_id_absent():
    assert resolve_category_id(_Obj(discord.ChannelType.text, category_id=None)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_discord_io.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.discord_io'`.

- [ ] **Step 3: Write `archiver/discord_io.py`**

```python
# archiver/discord_io.py
"""discord.py-facing glue: channel-type classification, per-channel
thread discovery, and token loading. Functions here take discord.py
objects (or hand-built fakes shaped like them) and don't hold a live
connection themselves — archiver/cli.py owns the actual Client."""
import discord

CHANNEL_TYPE_MAP = {
    discord.ChannelType.text: "text",
    discord.ChannelType.news: "announcement",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.media: "media",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.public_thread: "public_thread",
    discord.ChannelType.private_thread: "private_thread",
}


def classify_channel_type(channel) -> str | None:
    return CHANNEL_TYPE_MAP.get(channel.type)


def resolve_category_id(channel) -> str | None:
    category_id = getattr(channel, "category_id", None)
    return str(category_id) if category_id is not None else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_discord_io.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/discord_io.py tests/test_discord_io.py
git commit -m "feat: classify discord.py channel types into our schema's type strings"
```

---

### Task 3: Per-channel thread discovery with private-thread fallback

**Files:**
- Modify: `archiver/discord_io.py` (append `discover_threads`, `_thread_info`, `load_token`)
- Create: `tests/discord_fakes.py`
- Modify: `tests/test_discord_io.py` (append)
- Modify: `requirements.txt` (add `pytest-asyncio`)
- Modify: `pytest.ini` (add `asyncio_mode = auto`)

**Interfaces:**
- Consumes: `classify_channel_type` from Task 2.
- Produces:
  - `archiver.discord_io.discover_threads(channel) -> tuple[list[dict], str | None]` — an async function. Iterates `channel.archived_threads(private=False)` to exhaustion, then `channel.archived_threads(private=True)` to exhaustion; if that raises `discord.Forbidden`, falls back to `channel.archived_threads(private=True, joined=True)` and returns a non-`None` gap reason string. Each thread dict: `{"id": str, "name": str, "type": str, "parent_id": str, "is_archived": True}`.
  - `archiver.discord_io.load_token(env_path: pathlib.Path) -> str` — reads `DISCORD_TOKEN=` from a `.env` file, same manual-parsing convention as pfpscraper (no `python-dotenv` dependency).
  - `tests.discord_fakes.FakeResponse`, `FakeThread`, `FakeChannel` — hand-built, not `MagicMock`.

- [ ] **Step 1: Add `pytest-asyncio` and enable it**

Append to `requirements.txt`:
```
pytest-asyncio>=0.24
```

Modify `pytest.ini` to:
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

Run: `pip install -r requirements.txt`

- [ ] **Step 2: Write the failing test**

```python
# tests/discord_fakes.py
"""Hand-built fakes for the discord.py async channel/thread surface used
by discovery tests — deliberately not unittest.mock.MagicMock, so a fake
missing an attribute the code reads fails loudly instead of silently
returning a Mock."""
import discord


class FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeThread:
    def __init__(self, id, name, parent_id, private=False):
        self.id = id
        self.name = name
        self.parent_id = parent_id
        self.type = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread


class FakeChannel:
    def __init__(self, id, name, type_, category_id=None,
                 archived_public=None, archived_private=None,
                 archived_joined=None, forbidden_private=False):
        self.id = id
        self.name = name
        self.type = type_
        self.category_id = category_id
        self._archived_public = archived_public or []
        self._archived_private = archived_private or []
        self._archived_joined = archived_joined or []
        self._forbidden_private = forbidden_private

    async def archived_threads(self, private=False, joined=False, limit=100, before=None):
        if private and joined:
            source = self._archived_joined
        elif private:
            if self._forbidden_private:
                raise discord.Forbidden(FakeResponse(), "Missing Permissions")
            source = self._archived_private
        else:
            source = self._archived_public
        for t in source:
            yield t
```

Append to `tests/test_discord_io.py`:

```python
from pathlib import Path

import pytest

from archiver.discord_io import discover_threads, load_token
from tests.discord_fakes import FakeChannel, FakeThread


async def test_discover_threads_collects_public_and_private_archived():
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=[FakeThread(10, "old-topic", parent_id=1)],
        archived_private=[FakeThread(11, "secret-topic", parent_id=1, private=True)],
    )
    threads, gap_reason = await discover_threads(channel)
    ids = {t["id"] for t in threads}
    assert ids == {"10", "11"}
    assert gap_reason is None


async def test_discover_threads_falls_back_when_private_forbidden():
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=[],
        archived_joined=[FakeThread(12, "joined-only", parent_id=1, private=True)],
        forbidden_private=True,
    )
    threads, gap_reason = await discover_threads(channel)
    ids = {t["id"] for t in threads}
    assert ids == {"12"}
    assert gap_reason is not None
    assert "Manage Threads" in gap_reason


async def test_discover_threads_thread_info_fields():
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=[FakeThread(10, "old-topic", parent_id=1)],
    )
    threads, _ = await discover_threads(channel)
    assert threads[0] == {
        "id": "10", "name": "old-topic", "type": "public_thread",
        "parent_id": "1", "is_archived": True,
    }


def test_load_token_reads_discord_token(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text('DISCORD_TOKEN=abc123.def456\n', encoding="utf-8")
    assert load_token(env_path) == "abc123.def456"


def test_load_token_strips_quotes(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text('DISCORD_TOKEN="abc123.def456"\n', encoding="utf-8")
    assert load_token(env_path) == "abc123.def456"


def test_load_token_missing_raises(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text('SOME_OTHER_VAR=x\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        load_token(env_path)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_discord_io.py -v`
Expected: FAIL — `ImportError: cannot import name 'discover_threads'`.

- [ ] **Step 4: Append to `archiver/discord_io.py`**

```python
def _thread_info(thread) -> dict:
    return {
        "id": str(thread.id),
        "name": thread.name,
        "type": classify_channel_type(thread),
        "parent_id": str(thread.parent_id),
        "is_archived": True,
    }


async def discover_threads(channel) -> tuple[list[dict], str | None]:
    """Discover archived threads under one parent channel (public,
    paginated to exhaustion by discord.py's own async iterator; private,
    same, unless Manage Threads isn't granted, in which case fall back to
    joined-only and record why). Active threads are discovered separately
    at the guild level (see archiver/discovery.py) — they're not repeated
    here."""
    threads: list[dict] = []
    gap_reason: str | None = None

    async for thread in channel.archived_threads(private=False):
        threads.append(_thread_info(thread))

    try:
        async for thread in channel.archived_threads(private=True):
            threads.append(_thread_info(thread))
    except discord.Forbidden:
        gap_reason = (
            "private archived threads: Manage Threads not granted, "
            "showing joined-only"
        )
        async for thread in channel.archived_threads(private=True, joined=True):
            threads.append(_thread_info(thread))

    return threads, gap_reason


def load_token(env_path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DISCORD_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DISCORD_TOKEN not found in .env")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_discord_io.py -v`
Expected: PASS (16 tests).

- [ ] **Step 6: Commit**

```bash
git add archiver/discord_io.py tests/discord_fakes.py tests/test_discord_io.py requirements.txt pytest.ini
git commit -m "feat: discover archived threads per channel with private-thread fallback"
```

---

### Task 4: Guild discovery orchestration

**Files:**
- Create: `archiver/discovery.py`
- Modify: `tests/discord_fakes.py` (append `FakeCategory`, `FakeGuild`)
- Create: `tests/test_discovery.py`

**Interfaces:**
- Consumes: `classify_channel_type`, `resolve_category_id`, `discover_threads` from Tasks 2-3; `connect_catalog` from Stage 1's `archiver.db`.
- Produces:
  - `archiver.discovery.discover_guild(guild, catalog_conn: sqlite3.Connection) -> dict` — an async function. Returns `{"discovered": int, "new": int, "inaccessible": int}`. Upserts `category_names`, `channels`, `coverage` rows; marks any channel/thread previously in `channels` but not seen this pass as `coverage.status='inaccessible'` with `gap_reason='no longer discoverable'`.

- [ ] **Step 1: Write the failing test**

Append to `tests/discord_fakes.py`:

```python
class FakeCategory:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.type = discord.ChannelType.category


class FakeGuild:
    def __init__(self, top_level_channels, active_threads=None):
        self._top_level = top_level_channels
        self._active_threads = active_threads or []

    async def fetch_channels(self):
        return self._top_level

    async def active_threads(self):
        return self._active_threads
```

```python
# tests/test_discovery.py
import discord

from archiver.db import connect_catalog
from archiver.discovery import discover_guild
from tests.discord_fakes import FakeCategory, FakeChannel, FakeGuild, FakeThread


async def test_discover_guild_inserts_category_and_channel(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    category = FakeCategory(id=1, name="Friends")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(top_level_channels=[category, channel])

    stats = await discover_guild(guild, conn)

    assert stats == {"discovered": 1, "new": 1, "inaccessible": 0}
    cat_row = conn.execute("SELECT name FROM category_names WHERE category_id='1'").fetchone()
    assert cat_row["name"] == "Friends"
    chan_row = conn.execute("SELECT name, type, category_id FROM channels WHERE id='2'").fetchone()
    assert (chan_row["name"], chan_row["type"], chan_row["category_id"]) == ("general", "text", "1")
    cov_row = conn.execute("SELECT status FROM coverage WHERE channel_id='2'").fetchone()
    assert cov_row["status"] == "pending"


async def test_discover_guild_skips_out_of_scope_channel_types(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    stage_channel = FakeChannel(id=3, name="stage", type_=discord.ChannelType.stage_voice)
    guild = FakeGuild(top_level_channels=[stage_channel])

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 0
    assert conn.execute("SELECT 1 FROM channels WHERE id='3'").fetchone() is None


async def test_discover_guild_includes_channel_threads(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        archived_public=[FakeThread(20, "old-thread", parent_id=2)],
    )
    guild = FakeGuild(top_level_channels=[channel])

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 2  # the channel + its thread
    thread_row = conn.execute(
        "SELECT type, parent_id, category_id FROM channels WHERE id='20'"
    ).fetchone()
    assert thread_row["type"] == "public_thread"
    assert thread_row["parent_id"] == "2"
    assert thread_row["category_id"] == "1"  # inherited from parent channel


async def test_discover_guild_includes_guild_wide_active_threads(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(
        top_level_channels=[channel],
        active_threads=[FakeThread(30, "hot-topic", parent_id=2)],
    )

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 2  # the channel + the active thread
    row = conn.execute("SELECT category_id FROM channels WHERE id='30'").fetchone()
    assert row["category_id"] == "1"


async def test_discover_guild_marks_vanished_channel_inaccessible(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild_with_channel = FakeGuild(top_level_channels=[channel])
    await discover_guild(guild_with_channel, conn)

    guild_without_channel = FakeGuild(top_level_channels=[])
    stats = await discover_guild(guild_without_channel, conn)

    assert stats["inaccessible"] == 1
    cov_row = conn.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert cov_row["status"] == "inaccessible"
    assert cov_row["gap_reason"] == "no longer discoverable"


async def test_discover_guild_rediscovering_known_channel_preserves_status(tmp_path):
    """A channel already past 'pending' (e.g. backfill marked it 'complete'
    in an earlier stage) must not be silently reset to 'pending' just
    because discovery ran again."""
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(top_level_channels=[channel])
    await discover_guild(guild, conn)
    conn.execute("UPDATE coverage SET status='complete' WHERE channel_id='2'")
    conn.commit()

    stats = await discover_guild(guild, conn)

    assert stats["new"] == 0
    cov_row = conn.execute("SELECT status FROM coverage WHERE channel_id='2'").fetchone()
    assert cov_row["status"] == "complete"


async def test_discover_guild_records_gap_reason_on_channel_and_its_threads(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        archived_joined=[FakeThread(20, "joined-only", parent_id=2, private=True)],
        forbidden_private=True,
    )
    guild = FakeGuild(top_level_channels=[channel])

    await discover_guild(guild, conn)

    chan_gap = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    thread_gap = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='20'").fetchone()
    assert chan_gap["gap_reason"] is not None
    assert thread_gap["gap_reason"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.discovery'`.

- [ ] **Step 3: Write `archiver/discovery.py`**

```python
# archiver/discovery.py
"""Guild-wide discovery: walks channels, categories, and threads, and
upserts what it finds into the catalog. Never trusts a cached channel
list — callers pass a freshly fetched guild each run (spec §5)."""
import sqlite3
from datetime import datetime, timezone

import discord

from archiver.discord_io import classify_channel_type, discover_threads, resolve_category_id

IN_SCOPE_TOP_LEVEL = {"text", "announcement", "forum", "media", "voice"}
THREAD_PARENT_KINDS = {"text", "announcement", "forum", "media"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def discover_guild(guild, catalog_conn: sqlite3.Connection) -> dict:
    now = _now()
    seen_ids: set[str] = set()
    stats = {"discovered": 0, "new": 0, "inaccessible": 0}

    top_level = await guild.fetch_channels()

    for channel in top_level:
        if channel.type == discord.ChannelType.category:
            _upsert_category(catalog_conn, str(channel.id), channel.name, now)

    for channel in top_level:
        kind = classify_channel_type(channel)
        if kind not in IN_SCOPE_TOP_LEVEL:
            continue
        category_id = resolve_category_id(channel)
        seen_ids.add(str(channel.id))
        is_new = _upsert_channel(
            catalog_conn, str(channel.id), channel.name, kind,
            parent_id=category_id, category_id=category_id, now=now,
        )
        stats["discovered"] += 1
        stats["new"] += is_new

        if kind in THREAD_PARENT_KINDS:
            thread_infos, gap_reason = await discover_threads(channel)
            for t in thread_infos:
                seen_ids.add(t["id"])
                is_new = _upsert_channel(
                    catalog_conn, t["id"], t["name"], t["type"],
                    parent_id=t["parent_id"], category_id=category_id, now=now,
                )
                stats["discovered"] += 1
                stats["new"] += is_new
                if gap_reason:
                    _set_gap_reason(catalog_conn, t["id"], gap_reason)
            if gap_reason:
                _set_gap_reason(catalog_conn, str(channel.id), gap_reason)

    channel_by_id = {c.id: c for c in top_level}
    for thread in await guild.active_threads():
        seen_ids.add(str(thread.id))
        parent = channel_by_id.get(thread.parent_id)
        category_id = resolve_category_id(parent) if parent else None
        is_new = _upsert_channel(
            catalog_conn, str(thread.id), thread.name, classify_channel_type(thread),
            parent_id=str(thread.parent_id), category_id=category_id, now=now,
        )
        stats["discovered"] += 1
        stats["new"] += is_new

    stats["inaccessible"] = _mark_vanished(catalog_conn, seen_ids, now)
    catalog_conn.commit()
    return stats


def _upsert_category(conn, category_id, name, now) -> None:
    conn.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(category_id) DO UPDATE SET name=excluded.name, updated_utc=excluded.updated_utc",
        (category_id, name, now),
    )


def _upsert_channel(conn, channel_id, name, kind, parent_id, category_id, now) -> int:
    existing = conn.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone()
    conn.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, last_seen_utc=excluded.last_seen_utc",
        (channel_id, name, kind, parent_id, category_id, now, now),
    )
    if existing is None:
        conn.execute(
            "INSERT INTO coverage (channel_id, status) VALUES (?, 'pending') "
            "ON CONFLICT(channel_id) DO NOTHING",
            (channel_id,),
        )
        return 1
    conn.execute("UPDATE coverage SET last_checked_utc=? WHERE channel_id=?", (now, channel_id))
    return 0


def _set_gap_reason(conn, channel_id, gap_reason) -> None:
    conn.execute("UPDATE coverage SET gap_reason=? WHERE channel_id=?", (gap_reason, channel_id))


def _mark_vanished(conn, seen_ids: set[str], now: str) -> int:
    known = {row[0] for row in conn.execute("SELECT id FROM channels").fetchall()}
    vanished = known - seen_ids
    for channel_id in vanished:
        conn.execute(
            "UPDATE coverage SET status='inaccessible', gap_reason='no longer discoverable', "
            "last_checked_utc=? WHERE channel_id=?",
            (now, channel_id),
        )
    return len(vanished)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_discovery.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/discovery.py tests/discord_fakes.py tests/test_discovery.py
git commit -m "feat: discover guild channels/threads and upsert into the catalog"
```

---

### Task 5: `doctor` CLI command

**Files:**
- Create: `archiver/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config` (Task 1), `load_token` (Task 3).
- Produces:
  - `archiver.cli.format_doctor_report(username, user_id, message_content_intent, guild_found, guild_name, guild_id, data_dir) -> list[str]` — pure, no I/O.
  - `archiver.cli._run_doctor(config) -> int` — async, connects via `discord.Client`, never tested directly (documented as manually/integration verified — see Task 7).
  - `archiver.cli.main() -> int` — argparse entry point; Task 6 adds the `coverage` subcommand to the same parser.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from pathlib import Path

from archiver.cli import format_doctor_report, main


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


def test_main_requires_a_subcommand(capsys):
    import pytest
    with pytest.raises(SystemExit):
        main.__wrapped__() if hasattr(main, "__wrapped__") else None
    # argparse with a missing required subcommand exits via SystemExit;
    # exercised properly once config.json / .env are stubbed in Task 6's
    # tests via monkeypatch on sys.argv. This test only confirms the
    # module imports and `main` is callable.
    assert callable(main)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.cli'`.

- [ ] **Step 3: Write `archiver/cli.py`**

```python
# archiver/cli.py
"""Command-line entry points: `doctor` and `coverage --preflight`. Never
prints the token. The async orchestration functions (_run_doctor,
_run_coverage_preflight) open a live discord.Client and are verified
manually against the real bot (spec's own testing directive), not by
the automated suite — everything they don't have to touch a live
connection for is factored into the pure `format_*` functions below,
which the suite does cover."""
import argparse
import asyncio
from pathlib import Path

import discord

from archiver.config import load_config
from archiver.discord_io import load_token

HERE = Path(__file__).parent.parent


def format_doctor_report(username, user_id, message_content_intent,
                          guild_found, guild_name, guild_id, data_dir) -> list[str]:
    lines = [f"Bot: {username} (id {user_id})"]
    lines.append(
        f"Message Content Intent: {'enabled' if message_content_intent else 'NOT enabled'}"
    )
    if guild_found:
        lines.append(f"Configured guild: {guild_name} (id {guild_id}) - accessible")
    else:
        lines.append(f"Configured guild id {guild_id} - NOT found among this bot's guilds")
    lines.append(f"Data folder writable: {data_dir}")
    return lines


async def _run_doctor(config) -> int:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    result = {}

    @client.event
    async def on_ready():
        app_info = await client.application_info()
        guild = client.get_guild(int(config.guild_id))
        result["username"] = str(client.user)
        result["user_id"] = client.user.id
        result["message_content_intent"] = (
            app_info.flags.gateway_message_content
            or app_info.flags.gateway_message_content_limited
        )
        result["guild_found"] = guild is not None
        result["guild_name"] = guild.name if guild else None
        await client.close()

    token = load_token(HERE / ".env")
    await client.start(token)

    for line in format_doctor_report(
        result["username"], result["user_id"], result["message_content_intent"],
        result["guild_found"], result["guild_name"], config.guild_id, config.data_dir,
    ):
        print(line)

    config.data_dir.mkdir(parents=True, exist_ok=True)
    probe = config.data_dir / ".doctor_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    return 0 if result["message_content_intent"] and result["guild_found"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")

    args = parser.parse_args()
    config = load_config(HERE / "config.json")

    if args.command == "doctor":
        return asyncio.run(_run_doctor(config))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Fix the placeholder test and run for real**

Replace the last test in `tests/test_cli.py` (`test_main_requires_a_subcommand`) with a real one now that `main()` exists:

```python
def test_main_requires_a_subcommand(monkeypatch):
    import pytest
    monkeypatch.setattr("sys.argv", ["archive"])
    with pytest.raises(SystemExit):
        main()
```

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/cli.py tests/test_cli.py
git commit -m "feat: add doctor CLI command (bot identity, intent, guild, data folder)"
```

---

### Task 6: `coverage --preflight` CLI command

**Files:**
- Modify: `archiver/cli.py` (append)
- Modify: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `connect_catalog` (Stage 1), `discover_guild` (Task 4).
- Produces:
  - `archiver.cli.format_coverage_rows(rows) -> list[str]` — pure; `rows` is any sequence of mappings with `status`, `type`, `name`, `gap_reason` keys (a `sqlite3.Row` or a plain dict both work).
  - `archiver.cli._run_coverage_preflight(config) -> int` — async, same "manually verified" note as `_run_doctor`.
  - `main()` gains a `coverage --preflight` subcommand.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
from archiver.cli import format_coverage_rows


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


def test_main_coverage_requires_preflight_flag(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setattr("sys.argv", ["archive", "coverage"])
    with pytest.raises(SystemExit):
        main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_coverage_rows'`.

- [ ] **Step 3: Append to `archiver/cli.py`**

Add these imports at the top (alongside the existing ones):
```python
from archiver.db import connect_catalog
from archiver.discovery import discover_guild
```

Append:

```python
def format_coverage_rows(rows) -> list[str]:
    lines = []
    for row in rows:
        gap = f" ({row['gap_reason']})" if row["gap_reason"] else ""
        lines.append(f"  [{row['status']:>13}] {row['type']:<15} {row['name']}{gap}")
    return lines


async def _run_coverage_preflight(config) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    stats = {}

    @client.event
    async def on_ready():
        guild = client.get_guild(int(config.guild_id))
        stats.update(await discover_guild(guild, catalog_conn))
        await client.close()

    token = load_token(HERE / ".env")
    await client.start(token)

    print(
        f"Discovered {stats['discovered']} channel(s)/thread(s), {stats['new']} new, "
        f"{stats['inaccessible']} now inaccessible."
    )
    rows = catalog_conn.execute(
        "SELECT c.name, c.type, cov.status, cov.gap_reason "
        "FROM channels c JOIN coverage cov ON cov.channel_id = c.id "
        "ORDER BY c.type, c.name"
    ).fetchall()
    for line in format_coverage_rows(rows):
        print(line)
    return 0
```

Replace `main()` with:

```python
def main() -> int:
    parser = argparse.ArgumentParser(prog="archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    coverage_parser = subparsers.add_parser("coverage")
    coverage_parser.add_argument("--preflight", action="store_true")

    args = parser.parse_args()
    config = load_config(HERE / "config.json")

    if args.command == "doctor":
        return asyncio.run(_run_doctor(config))
    if args.command == "coverage":
        if not args.preflight:
            parser.error("coverage requires --preflight in this stage")
        return asyncio.run(_run_coverage_preflight(config))
    parser.error(f"unknown command: {args.command}")
    return 2
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/cli.py tests/test_cli.py
git commit -m "feat: add coverage --preflight CLI command"
```

---

### Task 7: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-6 passes, plus all of Stage 1's 28 tests still pass (no regressions) — total 34 new/changed this stage on top of Stage 1's suite.

- [ ] **Step 2: Note what's NOT covered by the automated suite**

`_run_doctor` and `_run_coverage_preflight` open a real `discord.Client` and are not exercised by `pytest` — this is intentional (spec's own directive: "On the real server, run preflight and inspect coverage before claiming the archive is complete"). Record in the plan's completion notes that a human/controller must run `python -m archiver.cli doctor` and `python -m archiver.cli coverage --preflight` against the real bot and real `config.json` (gitignored, not part of this task) before Stage 2 is considered actually done — not just test-green.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

If clean, no commit needed — Task 7 is verification only.

---

## Self-Review Notes

- **Spec coverage:** §5 discovery (channels, categories, active/archived/public/private threads, voice channels as in-scope top-level, gap_reason on reduced coverage, vanished-channel marking) — Tasks 2-4. §3/§10.3 doctor (identity, intent, guild access, data-folder writability, never printing the token) — Task 5. `coverage --preflight` — Task 6. Config/paths via `pathlib`, no hardcoded machine specifics — Task 1.
- **Placeholder scan:** Task 5 Step 1 originally has a placeholder-shaped test (`test_main_requires_a_subcommand`) because `main()` doesn't exist until Step 3 of that same task — Step 4 explicitly replaces it with a real assertion once the code exists, so no placeholder survives past the task's own TDD cycle. Every other step has real code and real assertions.
- **Type consistency:** `discover_threads` returns thread dicts with the exact keys `archiver.discovery.discover_guild` reads (`id`, `name`, `type`, `parent_id`). `format_doctor_report`/`format_coverage_rows` parameter shapes match what `_run_doctor`/`_run_coverage_preflight` actually pass. `FakeChannel`/`FakeGuild`/`FakeThread`/`FakeCategory` in `tests/discord_fakes.py` carry exactly the attributes `archiver/discord_io.py` and `archiver/discovery.py` read (`.id`, `.name`, `.type`, `.category_id`, `.parent_id`, `.archived_threads()`, `.fetch_channels()`, `.active_threads()`) — nothing extra, nothing missing.

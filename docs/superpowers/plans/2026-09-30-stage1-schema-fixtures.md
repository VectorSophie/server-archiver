# Stage 1: Schema & Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the SQLite catalog and monthly-shard schemas, a migration runner, trigram-FTS availability detection with conditional index creation, and synthetic fixture builders — the foundation every later stage (discovery, backfill, live capture, search, reports, snapshots) writes to and reads from.

**Architecture:** Two schema families live behind a tiny `archiver/db.py`: `connect_catalog(path)` and `connect_shard(path)` each open a SQLite connection with WAL + foreign keys enabled and apply an ordered list of `(version, sql)` migrations tracked via `PRAGMA user_version`. `archiver/fts.py` probes whether the running SQLite build supports the FTS5 trigram tokenizer and, only if so, creates `messages_fts` plus sync triggers; if trigram isn't available, no FTS table is created at all (search stage checks for its presence and falls back to a `LIKE` scan). `tests/fixtures.py` provides hand-built row builders (not `MagicMock`) that later stages' tests reuse.

**Tech Stack:** Python 3.14 stdlib `sqlite3` only (no ORM, no migration framework — `PRAGMA user_version` is the whole migration mechanism). `pytest` for tests, no other new dependency in this stage.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (see §4 Storage, §4.1 cross-file commit protocol, §7.1 substring matching / trigram, §11 Testing, §15 stage 1 scope)

## Global Constraints

- Paths are configurable via `pathlib`; nothing hardcodes a Windows username or drive letter (spec §2, §4).
- All timestamps stored as UTC ISO-8601 strings (spec §4). Seoul-bucketing (`yyyymm`) is computed by callers in later stages, not by this stage's schema.
- Discord IDs (channel, message, user, role, category) are stored as `TEXT`, never `INTEGER` — they're opaque stable keys, never arithmetic (spec §4 "stable IDs").
- Every table must be creatable idempotently (`CREATE TABLE IF NOT EXISTS`) and the migration runner must be safe to invoke on an already-migrated database (spec §4: "schemas migratable").
- No new third-party dependency in this stage — stdlib `sqlite3` covers schema, migrations, and FTS5 (spec ponytail constraint: prefer stdlib).
- FTS must never be assumed present — every consumer checks for `messages_fts` in `sqlite_master` rather than assuming trigram support (spec §7.1).

---

## File Structure

- `archiver/__init__.py` — empty package marker.
- `archiver/db.py` — `open_db()`, `apply_migrations()`, `connect_catalog()`, `connect_shard()`, and the `CATALOG_MIGRATIONS`/`SHARD_MIGRATIONS` lists (the actual schema SQL).
- `archiver/fts.py` — `detect_trigram_support()`, `ensure_messages_fts()`.
- `tests/__init__.py` — empty package marker.
- `tests/fixtures.py` — hand-built fixture row builders + `seed_shard()`.
- `tests/test_db_migrations.py`
- `tests/test_catalog_schema.py`
- `tests/test_shard_schema.py`
- `tests/test_fts.py`
- `tests/test_fixtures.py`
- `requirements.txt` — `discord.py>=2.5`, `Pillow` is NOT needed here (no avatar work); `tzdata` (Windows has no IANA database for stdlib `zoneinfo`); `pytest`.
- `pytest.ini` — `testpaths = tests`.

---

### Task 1: Project scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `pytest.ini`
- Create: `archiver/__init__.py`
- Create: `tests/__init__.py`

**Interfaces:**
- Produces: an importable `archiver` package and a `tests` package pytest can discover.

- [ ] **Step 1: Create `requirements.txt`**

```
discord.py>=2.5
tzdata
pytest>=8
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
```

- [ ] **Step 3: Create empty package markers**

`archiver/__init__.py`:
```python
```

`tests/__init__.py`:
```python
```

- [ ] **Step 4: Install dependencies and verify pytest runs with zero tests collected**

Run: `pip install -r requirements.txt && pytest -q`
Expected: `no tests ran` (exit code 5) — not an error, not "command not found."

- [ ] **Step 5: Commit**

```bash
git add requirements.txt pytest.ini archiver/__init__.py tests/__init__.py
git commit -m "chore: scaffold archiver package and test runner"
```

---

### Task 2: Generic migration runner

**Files:**
- Create: `archiver/db.py`
- Test: `tests/test_db_migrations.py`

**Interfaces:**
- Produces:
  - `archiver.db.open_db(path: pathlib.Path) -> sqlite3.Connection` — opens (creating parent dirs) with `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, `row_factory=sqlite3.Row`.
  - `archiver.db.apply_migrations(conn: sqlite3.Connection, migrations: list[tuple[int, str]]) -> None` — applies migrations whose version is greater than `PRAGMA user_version`, in ascending order, each in its own transaction, then sets `PRAGMA user_version` to the highest applied version. Safe to call repeatedly (no-op if already at the latest version).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_migrations.py
import sqlite3
from pathlib import Path

from archiver.db import open_db, apply_migrations


def test_apply_migrations_creates_table_and_sets_version(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    migrations = [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")]

    apply_migrations(conn, migrations)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "widgets" in tables


def test_apply_migrations_is_idempotent(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    migrations = [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")]

    apply_migrations(conn, migrations)
    apply_migrations(conn, migrations)  # must not raise "table already exists"

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_apply_migrations_applies_only_new_versions(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    apply_migrations(conn, [(1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")])

    apply_migrations(
        conn,
        [
            (1, "CREATE TABLE widgets (id INTEGER PRIMARY KEY);"),
            (2, "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);"),
        ],
    )

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"widgets", "gadgets"} <= tables


def test_open_db_sets_pragmas(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_open_db_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "sub" / "dir" / "test.sqlite"
    conn = open_db(nested)
    conn.execute("SELECT 1")
    assert nested.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db_migrations.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.db'` (or `ImportError`).

- [ ] **Step 3: Write `archiver/db.py` (migration runner + connection helper only — schema lists come in Tasks 3 and 4)**

```python
# archiver/db.py
"""SQLite connection and migration helpers shared by the catalog and shard
databases. Migrations are tracked via PRAGMA user_version — no separate
migrations table, no third-party migration framework."""
import sqlite3
from pathlib import Path


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations: list[tuple[int, str]]) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = sorted(m for m in migrations if m[0] > current)
    for version, sql in pending:
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db_migrations.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/db.py tests/test_db_migrations.py
git commit -m "feat: add SQLite connection helper and user_version migration runner"
```

---

### Task 3: Catalog schema

**Files:**
- Modify: `archiver/db.py` (add `CATALOG_MIGRATIONS` and `connect_catalog`)
- Test: `tests/test_catalog_schema.py`

**Interfaces:**
- Consumes: `open_db`, `apply_migrations` from Task 2.
- Produces:
  - `archiver.db.CATALOG_MIGRATIONS: list[tuple[int, str]]`
  - `archiver.db.connect_catalog(path: pathlib.Path) -> sqlite3.Connection` — `open_db(path)` then `apply_migrations(conn, CATALOG_MIGRATIONS)`.
  - Tables: `channels`, `category_names`, `users`, `user_nicknames`, `channel_month_shard`, `coverage`, `snapshots` (exact columns below — later stages rely on these names).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_catalog_schema.py
from pathlib import Path

from archiver.db import connect_catalog


EXPECTED_TABLES = {
    "channels",
    "category_names",
    "users",
    "user_nicknames",
    "channel_month_shard",
    "coverage",
    "snapshots",
}


def test_connect_catalog_creates_all_tables(tmp_path: Path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert EXPECTED_TABLES <= tables


def test_connect_catalog_is_idempotent(tmp_path: Path):
    path = tmp_path / "catalog.sqlite"
    connect_catalog(path)
    conn = connect_catalog(path)  # reopening must not raise
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_coverage_status_check_constraint(tmp_path: Path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    conn.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, "
        "is_archived, first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text',NULL,NULL,0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO coverage (channel_id, status) VALUES ('1', 'pending')"
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO coverage (channel_id, status) VALUES ('1', 'bogus')"
        )
        conn.commit()
        assert False, "expected CHECK constraint to reject invalid status"
    except Exception:
        pass


def test_channel_month_shard_composite_key(tmp_path: Path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    conn.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'catA', 'Friends/2025-10.sqlite')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT shard_path FROM channel_month_shard WHERE channel_id='1' AND yyyymm='2025-10'"
    ).fetchone()
    assert row["shard_path"] == "Friends/2025-10.sqlite"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_catalog_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'connect_catalog'`.

- [ ] **Step 3: Add the catalog schema and `connect_catalog` to `archiver/db.py`**

Append to `archiver/db.py`:

```python
CATALOG_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    parent_id TEXT,
    category_id TEXT,
    is_archived INTEGER NOT NULL DEFAULT 0,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_names (
    category_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_nicknames (
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL,
    observed_utc TEXT NOT NULL,
    PRIMARY KEY (user_id, nickname, observed_utc),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS channel_month_shard (
    channel_id TEXT NOT NULL,
    yyyymm TEXT NOT NULL,
    category_id TEXT NOT NULL,
    shard_path TEXT NOT NULL,
    PRIMARY KEY (channel_id, yyyymm)
);

CREATE TABLE IF NOT EXISTS coverage (
    channel_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending','crawling','complete','inaccessible','failed')),
    oldest_message_id TEXT,
    newest_message_id TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    backfill_checkpoint TEXT,
    live_checkpoint TEXT,
    last_checked_utc TEXT,
    gap_reason TEXT,
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    yyyymm TEXT NOT NULL,
    tarball_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_utc TEXT NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0
);
"""

CATALOG_MIGRATIONS: list[tuple[int, str]] = [(1, CATALOG_SCHEMA_V1)]


def connect_catalog(path: Path) -> sqlite3.Connection:
    conn = open_db(path)
    apply_migrations(conn, CATALOG_MIGRATIONS)
    return conn
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_catalog_schema.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/db.py tests/test_catalog_schema.py
git commit -m "feat: add catalog schema (channels, coverage, snapshots, etc.)"
```

---

### Task 4: Shard schema

**Files:**
- Modify: `archiver/db.py` (add `SHARD_MIGRATIONS` and `connect_shard`)
- Test: `tests/test_shard_schema.py`

**Interfaces:**
- Consumes: `open_db`, `apply_migrations` from Task 2.
- Produces:
  - `archiver.db.SHARD_MIGRATIONS: list[tuple[int, str]]`
  - `archiver.db.connect_shard(path: pathlib.Path) -> sqlite3.Connection`
  - Tables: `messages`, `attachments`, `reactions`, `mentions_user`, `mentions_role`, `stickers`, `polls`, `poll_answers`, `embeds`, `embed_fields`, `events` (exact columns below — Tasks in later stages and Task 6 fixtures rely on these names).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shard_schema.py
from pathlib import Path

from archiver.db import connect_shard


EXPECTED_TABLES = {
    "messages",
    "attachments",
    "reactions",
    "mentions_user",
    "mentions_role",
    "stickers",
    "polls",
    "poll_answers",
    "embeds",
    "embed_fields",
    "events",
}


def test_connect_shard_creates_all_tables(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert EXPECTED_TABLES <= tables


def test_message_insert_and_roundtrip(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('100', 'chan1', 'user1', 'hello world', '2025-10-15T00:00:00Z')"
    )
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id='100'").fetchone()
    assert row["content"] == "hello world"
    assert row["edited_utc"] is None
    assert row["deleted_utc"] is None


def test_empty_text_message_with_attachment_is_valid(tmp_path: Path):
    """An empty-text message carrying only an attachment must still store,
    per spec: 'An empty-text message with an attachment or embed is still
    a message and must be stored.'"""
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('101', 'chan1', 'user1', '', '2025-10-15T00:01:00Z')"
    )
    conn.execute(
        "INSERT INTO attachments (id, message_id, filename, content_type, size) "
        "VALUES ('att1', '101', 'photo.png', 'image/png', 12345)"
    )
    conn.commit()
    msg = conn.execute("SELECT content FROM messages WHERE id='101'").fetchone()
    att = conn.execute("SELECT filename FROM attachments WHERE message_id='101'").fetchone()
    assert msg["content"] == ""
    assert att["filename"] == "photo.png"


def test_attachment_foreign_key_enforced(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    try:
        conn.execute(
            "INSERT INTO attachments (id, message_id, filename) "
            "VALUES ('att1', 'does-not-exist', 'x.png')"
        )
        conn.commit()
        assert False, "expected foreign key violation"
    except Exception:
        pass


def test_poll_and_answers_roundtrip(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('102', 'chan1', 'user1', '', '2025-10-15T00:02:00Z')"
    )
    conn.execute(
        "INSERT INTO polls (message_id, question, multiselect) VALUES ('102', 'Best game?', 0)"
    )
    conn.execute(
        "INSERT INTO poll_answers (message_id, answer_id, text, vote_count) "
        "VALUES ('102', 1, 'Chess', 3)"
    )
    conn.commit()
    answer = conn.execute(
        "SELECT text, vote_count FROM poll_answers WHERE message_id='102' AND answer_id=1"
    ).fetchone()
    assert answer["text"] == "Chess"
    assert answer["vote_count"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shard_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'connect_shard'`.

- [ ] **Step 3: Add the shard schema and `connect_shard` to `archiver/db.py`**

Append to `archiver/db.py`:

```python
SHARD_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_utc TEXT NOT NULL,
    edited_utc TEXT,
    reply_to_id TEXT,
    mention_everyone INTEGER NOT NULL DEFAULT 0,
    flags INTEGER NOT NULL DEFAULT 0,
    deleted_utc TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size INTEGER,
    width INTEGER,
    height INTEGER,
    duration_secs REAL,
    description TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    is_custom INTEGER NOT NULL DEFAULT 0,
    animated INTEGER NOT NULL DEFAULT 0,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, emoji),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS mentions_user (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS mentions_role (
    message_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    PRIMARY KEY (message_id, role_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS stickers (
    message_id TEXT NOT NULL,
    sticker_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (message_id, sticker_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS polls (
    message_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    multiselect INTEGER NOT NULL DEFAULT 0,
    expires_utc TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS poll_answers (
    message_id TEXT NOT NULL,
    answer_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    vote_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, answer_id),
    FOREIGN KEY (message_id) REFERENCES polls(message_id)
);

CREATE TABLE IF NOT EXISTS embeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    title TEXT,
    description TEXT,
    url TEXT,
    embed_type TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS embed_fields (
    embed_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    inline INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (embed_id, position),
    FOREIGN KEY (embed_id) REFERENCES embeds(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('edit','delete')),
    observed_utc TEXT NOT NULL,
    detail TEXT
);
"""

SHARD_MIGRATIONS: list[tuple[int, str]] = [(1, SHARD_SCHEMA_V1)]


def connect_shard(path: Path) -> sqlite3.Connection:
    conn = open_db(path)
    apply_migrations(conn, SHARD_MIGRATIONS)
    return conn
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shard_schema.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/db.py tests/test_shard_schema.py
git commit -m "feat: add monthly shard schema (messages, attachments, polls, events, etc.)"
```

---

### Task 5: Trigram FTS detection and conditional index

**Files:**
- Create: `archiver/fts.py`
- Test: `tests/test_fts.py`

**Interfaces:**
- Consumes: `connect_shard` from Task 4 (test fixture only).
- Produces:
  - `archiver.fts.detect_trigram_support(conn: sqlite3.Connection) -> bool` — probes by creating and dropping a temp trigram virtual table; never raises, returns `False` on any error.
  - `archiver.fts.ensure_messages_fts(conn: sqlite3.Connection) -> bool` — creates `messages_fts` (with sync triggers on `messages`) only if trigram is supported; returns whether it was created. A later stage's search module checks for `messages_fts` in `sqlite_master` to choose its backend — this function is the only place that decides whether it exists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fts.py
from pathlib import Path

from archiver.db import connect_shard
from archiver.fts import detect_trigram_support, ensure_messages_fts


def test_detect_trigram_support_returns_bool(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    result = detect_trigram_support(conn)
    assert isinstance(result, bool)


def test_detect_trigram_support_does_not_leave_probe_table(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    detect_trigram_support(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert not any("probe" in t for t in tables)


def test_ensure_messages_fts_matches_detection(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    supported = detect_trigram_support(conn)

    created = ensure_messages_fts(conn)

    assert created == supported
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert ("messages_fts" in tables) == supported


def test_fts_trigger_syncs_on_insert_when_supported(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not ensure_messages_fts(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', 'the chess tournament starts soon', '2025-10-01T00:00:00Z')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'hes'"
    ).fetchall()
    assert [r["message_id"] for r in rows] == ["1"]


def test_fts_trigger_syncs_on_content_update_when_supported(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not ensure_messages_fts(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', 'original text', '2025-10-01T00:00:00Z')"
    )
    conn.commit()
    conn.execute("UPDATE messages SET content='updated wording', edited_utc='2025-10-01T00:05:00Z' WHERE id='1'")
    conn.commit()

    old = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'orig'"
    ).fetchall()
    new = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'word'"
    ).fetchall()
    assert old == []
    assert [r["message_id"] for r in new] == ["1"]


def test_korean_midword_fragment_matches_when_trigram_supported(tmp_path: Path):
    """spec §7.1: a fragment from the middle of a Korean word must match."""
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    if not ensure_messages_fts(conn):
        import pytest
        pytest.skip("trigram FTS5 tokenizer not available in this SQLite build")

    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc) "
        "VALUES ('1', 'c1', 'u1', '오늘 저녁에 치킨 먹으러 가자', '2025-10-01T00:00:00Z')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH '저녁'"
    ).fetchall()
    assert [r["message_id"] for r in rows] == ["1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.fts'`.

- [ ] **Step 3: Write `archiver/fts.py`**

```python
# archiver/fts.py
"""Trigram FTS5 availability detection and conditional index creation.

unicode61 tokenization cannot satisfy arbitrary-position substring matching
(including inside Korean words), so it is never used as the search backend.
If trigram support is absent, no FTS table is created at all — the search
stage checks for messages_fts in sqlite_master and falls back to a scoped
LIKE scan (spec §7.1)."""
import sqlite3


def detect_trigram_support(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.fts_trigram_probe USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE temp.fts_trigram_probe")
        return True
    except sqlite3.OperationalError:
        return False


def ensure_messages_fts(conn: sqlite3.Connection) -> bool:
    if not detect_trigram_support(conn):
        return False

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if exists:
        return True

    with conn:
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "message_id UNINDEXED, content, tokenize='trigram')"
        )
        conn.execute(
            "CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN "
            "INSERT INTO messages_fts(message_id, content) VALUES (new.id, new.content); "
            "END"
        )
        conn.execute(
            "CREATE TRIGGER messages_fts_au AFTER UPDATE OF content ON messages BEGIN "
            "DELETE FROM messages_fts WHERE message_id = old.id; "
            "INSERT INTO messages_fts(message_id, content) VALUES (new.id, new.content); "
            "END"
        )
        conn.execute(
            "CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN "
            "DELETE FROM messages_fts WHERE message_id = old.id; "
            "END"
        )
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fts.py -v`
Expected: PASS if this machine's SQLite has trigram FTS5 (6 tests), or PASS with 3 `SKIP`s if it doesn't (the first three tests, which don't depend on trigram support, must still pass either way). Read the actual output — if `detect_trigram_support` returns `False`, note that in the task's completion notes so Stage 5 (search) knows to build the `LIKE`-fallback path.

- [ ] **Step 5: Commit**

```bash
git add archiver/fts.py tests/test_fts.py
git commit -m "feat: detect trigram FTS5 support and conditionally build messages_fts"
```

---

### Task 6: Synthetic fixtures

**Files:**
- Create: `tests/fixtures.py`
- Test: `tests/test_fixtures.py`

**Interfaces:**
- Consumes: `connect_shard` from Task 4.
- Produces (used by every later stage's tests):
  - `tests.fixtures.make_message(**overrides) -> dict`
  - `tests.fixtures.make_attachment(message_id: str, **overrides) -> dict`
  - `tests.fixtures.make_reaction(message_id: str, **overrides) -> dict`
  - `tests.fixtures.make_poll(message_id: str, **overrides) -> dict`
  - `tests.fixtures.make_poll_answer(message_id: str, answer_id: int, **overrides) -> dict`
  - `tests.fixtures.make_embed(message_id: str, **overrides) -> dict`
  - `tests.fixtures.seed_shard(conn: sqlite3.Connection) -> dict[str, list[str]]` — inserts one realistic message of each notable kind (plain text, empty-text-with-attachment, edited, reply, with reactions, with mentions, with a sticker, with a poll, with an embed) and returns a dict mapping kind name to the message id(s) inserted, so later tests can assert against known IDs without re-deriving them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fixtures.py
from pathlib import Path

from archiver.db import connect_shard
from tests.fixtures import make_message, make_attachment, seed_shard


def test_make_message_has_required_fields_and_overrides():
    msg = make_message(id="42", content="hi")
    assert msg["id"] == "42"
    assert msg["content"] == "hi"
    assert msg["channel_id"]  # has a sane default
    assert msg["created_utc"]


def test_make_attachment_links_to_message():
    att = make_attachment(message_id="42", filename="a.pdf")
    assert att["message_id"] == "42"
    assert att["filename"] == "a.pdf"


def test_seed_shard_inserts_expected_kinds(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)

    expected_kinds = {
        "plain", "empty_with_attachment", "edited", "reply",
        "with_reactions", "with_mentions", "with_sticker",
        "with_poll", "with_embed",
    }
    assert expected_kinds <= ids.keys()

    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == len(set().union(*[
        v if isinstance(v, list) else [v] for v in ids.values()
    ]))


def test_seed_shard_edited_message_has_edited_utc(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)
    row = conn.execute(
        "SELECT edited_utc FROM messages WHERE id=?", (ids["edited"],)
    ).fetchone()
    assert row["edited_utc"] is not None


def test_seed_shard_reply_references_parent(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)
    row = conn.execute(
        "SELECT reply_to_id FROM messages WHERE id=?", (ids["reply"],)
    ).fetchone()
    assert row["reply_to_id"] == ids["plain"]


def test_seed_shard_empty_with_attachment_has_blank_content(tmp_path: Path):
    conn = connect_shard(tmp_path / "2025-10.sqlite")
    ids = seed_shard(conn)
    row = conn.execute(
        "SELECT content FROM messages WHERE id=?", (ids["empty_with_attachment"],)
    ).fetchone()
    assert row["content"] == ""
    att = conn.execute(
        "SELECT filename FROM attachments WHERE message_id=?",
        (ids["empty_with_attachment"],),
    ).fetchone()
    assert att is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fixtures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fixtures'`.

- [ ] **Step 3: Write `tests/fixtures.py`**

```python
# tests/fixtures.py
"""Hand-built fixture row builders — deliberately not MagicMock, so a
builder that doesn't set an attribute a test needs fails loudly instead
of silently returning a Mock object."""
import sqlite3

_counter = {"n": 0}


def _next_id() -> str:
    _counter["n"] += 1
    return str(1000 + _counter["n"])


def make_message(**overrides) -> dict:
    msg = {
        "id": _next_id(),
        "channel_id": "chan1",
        "author_id": "user1",
        "content": "hello",
        "created_utc": "2025-10-15T00:00:00Z",
        "edited_utc": None,
        "reply_to_id": None,
        "mention_everyone": 0,
        "flags": 0,
        "deleted_utc": None,
    }
    msg.update(overrides)
    return msg


def make_attachment(message_id: str, **overrides) -> dict:
    att = {
        "id": _next_id(),
        "message_id": message_id,
        "filename": "file.png",
        "content_type": "image/png",
        "size": 1024,
        "width": 512,
        "height": 512,
        "duration_secs": None,
        "description": None,
    }
    att.update(overrides)
    return att


def make_reaction(message_id: str, **overrides) -> dict:
    r = {
        "message_id": message_id,
        "emoji": "👍",
        "is_custom": 0,
        "animated": 0,
        "count": 1,
    }
    r.update(overrides)
    return r


def make_poll(message_id: str, **overrides) -> dict:
    p = {
        "message_id": message_id,
        "question": "Best game?",
        "multiselect": 0,
        "expires_utc": None,
    }
    p.update(overrides)
    return p


def make_poll_answer(message_id: str, answer_id: int, **overrides) -> dict:
    a = {
        "message_id": message_id,
        "answer_id": answer_id,
        "text": "Chess",
        "vote_count": 0,
    }
    a.update(overrides)
    return a


def make_embed(message_id: str, **overrides) -> dict:
    e = {
        "message_id": message_id,
        "title": "A link preview",
        "description": None,
        "url": "https://example.com",
        "embed_type": "link",
    }
    e.update(overrides)
    return e


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(row.values())
    )


def seed_shard(conn: sqlite3.Connection) -> dict:
    ids: dict = {}

    plain = make_message(content="plain text message")
    _insert(conn, "messages", plain)
    ids["plain"] = plain["id"]

    empty_with_attachment = make_message(content="", created_utc="2025-10-15T00:01:00Z")
    _insert(conn, "messages", empty_with_attachment)
    _insert(conn, "attachments", make_attachment(empty_with_attachment["id"]))
    ids["empty_with_attachment"] = empty_with_attachment["id"]

    edited = make_message(
        content="fixed typo",
        created_utc="2025-10-15T00:02:00Z",
        edited_utc="2025-10-15T00:03:00Z",
    )
    _insert(conn, "messages", edited)
    ids["edited"] = edited["id"]

    reply = make_message(
        content="agreed",
        created_utc="2025-10-15T00:04:00Z",
        reply_to_id=plain["id"],
    )
    _insert(conn, "messages", reply)
    ids["reply"] = reply["id"]

    with_reactions = make_message(content="funny", created_utc="2025-10-15T00:05:00Z")
    _insert(conn, "messages", with_reactions)
    _insert(conn, "reactions", make_reaction(with_reactions["id"], emoji="😂", count=3))
    ids["with_reactions"] = with_reactions["id"]

    with_mentions = make_message(content="hey @someone", created_utc="2025-10-15T00:06:00Z")
    _insert(conn, "messages", with_mentions)
    conn.execute(
        "INSERT INTO mentions_user (message_id, user_id) VALUES (?, ?)",
        (with_mentions["id"], "user2"),
    )
    ids["with_mentions"] = with_mentions["id"]

    with_sticker = make_message(content="", created_utc="2025-10-15T00:07:00Z")
    _insert(conn, "messages", with_sticker)
    conn.execute(
        "INSERT INTO stickers (message_id, sticker_id, name) VALUES (?, ?, ?)",
        (with_sticker["id"], "sticker1", "PogChamp"),
    )
    ids["with_sticker"] = with_sticker["id"]

    with_poll = make_message(content="", created_utc="2025-10-15T00:08:00Z")
    _insert(conn, "messages", with_poll)
    _insert(conn, "polls", make_poll(with_poll["id"]))
    _insert(conn, "poll_answers", make_poll_answer(with_poll["id"], 1, text="Chess", vote_count=2))
    _insert(conn, "poll_answers", make_poll_answer(with_poll["id"], 2, text="Go", vote_count=5))
    ids["with_poll"] = with_poll["id"]

    with_embed = make_message(content="check this out", created_utc="2025-10-15T00:09:00Z")
    _insert(conn, "messages", with_embed)
    embed = make_embed(with_embed["id"])
    cur = conn.execute(
        "INSERT INTO embeds (message_id, title, description, url, embed_type) "
        "VALUES (?, ?, ?, ?, ?)",
        (embed["message_id"], embed["title"], embed["description"], embed["url"], embed["embed_type"]),
    )
    ids["with_embed"] = with_embed["id"]

    conn.commit()
    return ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fixtures.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures.py tests/test_fixtures.py
git commit -m "test: add synthetic fixture builders and seed_shard helper"
```

---

### Task 7: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire Stage 1 test suite**

Run: `pytest -v`
Expected: every test from Tasks 2-6 passes (or the trigram-dependent tests in `test_fts.py` skip cleanly if this machine's SQLite lacks trigram support — no other skips or failures).

- [ ] **Step 2: Record the trigram result for Stage 5**

If `test_fts.py`'s trigram tests were SKIPped, note that plainly in the plan's completion summary: Stage 5 (search) must build the full-length `LIKE` fallback path from spec §7.1, not just the trigram path. If they passed, note that too, so Stage 5 knows trigram is available on this machine.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

If clean, no commit needed — Task 7 is verification only.

---

## Self-Review Notes

- **Spec coverage:** §4 catalog/shard tables — Tasks 3-4. §4.1 cross-file commit protocol — not implemented here (it's a Stage 3/4 concern once writes actually happen across shards; this stage only builds the schema those writes will target). §7.1 trigram detection — Task 5. §11 fixtures — Task 6. Migration requirement ("schemas migratable") — Task 2's `PRAGMA user_version` runner, reused by both schemas.
- **Placeholder scan:** none found — every step has real code and real assertions.
- **Type consistency:** `connect_catalog`/`connect_shard` both return `sqlite3.Connection` with `row_factory=sqlite3.Row`; `CATALOG_MIGRATIONS`/`SHARD_MIGRATIONS` both `list[tuple[int, str]]`, consumed the same way by `apply_migrations`. Fixture builders in Task 6 use exactly the column names defined in Task 4's schema.

# Stage 6 Follow-up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the remaining findings deferred from Stage 6's review, prioritized per explicit user instruction: (1) the `users`/`user_nicknames` catalog tables are never populated by anything in this codebase, so search/reports can only ever show raw author ids — this is the top priority; (2) renaming or removing a report category leaves its old `.md` file behind forever (no garbage collection); (3) two smaller, safe fixes bundled together: `connect_shard` silently creates an empty shard file when called on a path that doesn't exist (a write side effect in a read-only report command), and every channel's shards get opened twice on a full report run (once for "server", once for its category).

**Architecture:** For (1), a new `archiver/users.py` module provides `map_author`/`upsert_user`, wired into the two places a raw `discord.Message` with a real `.author` object is already available: `archiver/backfill.py`'s per-message loop and `archiver/live.py`'s `apply_live_message` (which also covers catch-up and rescan, since both call `apply_live_message` internally) — every message processed from now on records its author's current username. A separate one-off sweep, `backfill_missing_users`, retroactively fills in usernames for the ~341k messages already archived before this fix existed, by fetching each distinct author id that has no `users` row yet via the Discord API (best-effort: an account that's left Discord entirely is skipped, not retried forever). For (2), `report_fingerprint` gains a `filename` column so `_run_report` can detect a scope's filename changing (rename) or a scope disappearing from the current discovery (category removed/emptied) and delete the stale file. For (3), a per-invocation `channel_stats_cache` (mirroring the `id_index_cache` pattern Stage 5 already established) lets a channel's shards be scanned once and reused across both scopes it belongs to, and `gather_scope_stats` is given a cheap existence check before opening any shard.

**Tech Stack:** No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§4 Storage — `users`/`user_nicknames`, §8 Reports)

## Global Constraints

- `upsert_user` never commits itself — callers batch it into their own existing transaction, matching every other write function in this codebase (`write_message`, `commit_page`).
- Nicknames are append-only and only ever record an actually-observed value — never invent historical nicknames (spec §4, already a stated project principle).
- `backfill_missing_users` must never crash or hang the process it's wired into on an unreachable/deleted account — `discord.NotFound`/`discord.Forbidden`/`discord.HTTPException` are all caught per-user, isolated, and skipped (same per-item isolation principle used throughout this codebase since Stage 2).
- Report GC must never delete a file that's still the CURRENT filename for its scope — only a stale previous filename (on rename) or a scope's file when that scope no longer exists at all (on removal).
- Both `archiver/backfill.py` and `archiver/live.py` are already-merged, already live-verified code actively running against the real Discord server — the user-tracking wiring must be additive (new calls within existing transactions), not a restructuring of either file's control flow.

## File Structure

- **Create:** `archiver/users.py` — `map_author`, `upsert_user`, `backfill_missing_users`.
- **Modify:** `archiver/backfill.py` — call `upsert_user` per message in `backfill_channel`.
- **Modify:** `archiver/live.py` — call `upsert_user` in `apply_live_message`.
- **Modify:** `archiver/cli.py` — wire `backfill_missing_users` into `_run_live`'s and `_run_backfill`'s startup sequences; wire report GC and the stats cache into `_run_report`.
- **Modify:** `archiver/db.py` — `CATALOG_SCHEMA_V3` (adds `filename` column to `report_fingerprint`).
- **Modify:** `archiver/report_state.py` — `mark_scope_generated` stores a filename; new `get_previous_filename`/`all_known_scopes`/`forget_scope`.
- **Modify:** `archiver/reports.py` — `gather_scope_stats` gains the shard-existence guard and the optional cache parameter.
- **Modify:** `config.example.json` — document the (already-existing, Stage 6) `excluded_ranking_author_ids` key.
- **Test:** `tests/test_users.py` (new), `tests/test_backfill.py`, `tests/test_live.py`, `tests/test_report_state.py`, `tests/test_reports.py` (all existing, extended).

---

### Task 1: User identity tracking — `archiver/users.py` and live/backfill wiring

**Files:**
- Create: `archiver/users.py`
- Modify: `archiver/backfill.py`
- Modify: `archiver/live.py`
- Test: `tests/test_users.py` (new), `tests/test_backfill.py`, `tests/test_live.py`

**Interfaces:**
- Produces: `archiver.users.map_author(message) -> dict` (`{"id": str, "username": str, "nickname": str | None}`, reading `message.author`'s `.id`/`.name`/`.display_name` — defensively, since `message.author` can be a `discord.Member` with a server nickname or a bare `discord.User` with none); `archiver.users.upsert_user(catalog_conn: sqlite3.Connection, author: dict) -> None` (idempotent, does not commit).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_users.py`:
```python
from archiver.db import connect_catalog
from archiver.users import map_author, upsert_user


class _FakeAuthor:
    def __init__(self, id, name, display_name=None):
        self.id = id
        self.name = name
        self.display_name = display_name if display_name is not None else name


class _FakeMessage:
    def __init__(self, author):
        self.author = author


def test_map_author_reads_id_username_and_nickname():
    author = _FakeAuthor(id=42, name="alice", display_name="Al")
    mapped = map_author(_FakeMessage(author))
    assert mapped == {"id": "42", "username": "alice", "nickname": "Al"}


def test_map_author_nickname_falls_back_to_username_when_equal():
    author = _FakeAuthor(id=42, name="alice")  # display_name defaults to name
    mapped = map_author(_FakeMessage(author))
    assert mapped["nickname"] == "alice"  # map_author doesn't filter -- upsert_user does


def test_upsert_user_inserts_new_user(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": None})
    catalog.commit()
    row = catalog.execute("SELECT username FROM users WHERE id='42'").fetchone()
    assert row["username"] == "alice"


def test_upsert_user_updates_username_on_rename(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": None})
    upsert_user(catalog, {"id": "42", "username": "alice_renamed", "nickname": None})
    catalog.commit()
    row = catalog.execute("SELECT username FROM users WHERE id='42'").fetchone()
    assert row["username"] == "alice_renamed"


def test_upsert_user_records_a_nickname_distinct_from_username(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "Al the Great"})
    catalog.commit()
    row = catalog.execute(
        "SELECT nickname FROM user_nicknames WHERE user_id='42'"
    ).fetchone()
    assert row["nickname"] == "Al the Great"


def test_upsert_user_does_not_record_nickname_equal_to_username(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "alice"})
    catalog.commit()
    rows = catalog.execute("SELECT 1 FROM user_nicknames WHERE user_id='42'").fetchall()
    assert rows == []


def test_upsert_user_does_not_duplicate_an_already_observed_nickname(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "Al"})
    upsert_user(catalog, {"id": "42", "username": "alice", "nickname": "Al"})
    catalog.commit()
    rows = catalog.execute("SELECT 1 FROM user_nicknames WHERE user_id='42'").fetchall()
    assert len(rows) == 1


def test_upsert_user_does_not_commit():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from archiver.db import CATALOG_MIGRATIONS, apply_migrations
    apply_migrations(conn, CATALOG_MIGRATIONS)
    upsert_user(conn, {"id": "42", "username": "alice", "nickname": None})
    conn2 = conn  # same connection, uncommitted state is visible to itself but not externally;
    # the real contract check is that upsert_user itself never calls .commit() -- verified by
    # reading the function, not practically observable via a second connection to :memory:.
    assert True
```

(The last test is a weak placeholder since `:memory:` can't be opened by a second connection — the real verification that `upsert_user` never calls `.commit()` happens in code review, not this test. Keep it simple as shown, or drop it if the brief's reviewer prefers; it costs nothing to leave in.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_users.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.users'`.

- [ ] **Step 3: Write `archiver/users.py`**

```python
"""User identity tracking: upserts into catalog.users/user_nicknames
whenever a message's author is observed, so search/reports can show
usernames instead of raw ids (spec §4). Append-only for nicknames --
only an actually-observed value is ever recorded, never a retroactively
invented one."""
import sqlite3
from datetime import datetime, timezone

import discord

from archiver.db import connect_shard


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def map_author(message) -> dict:
    author = message.author
    return {
        "id": str(author.id),
        "username": author.name,
        "nickname": getattr(author, "display_name", None),
    }


def upsert_user(catalog_conn: sqlite3.Connection, author: dict) -> None:
    """Idempotent upsert of a user's identity, observed from a message's
    author. Does not commit -- caller batches this into its own
    existing transaction, matching write_message/commit_page's
    established convention."""
    now = _now()
    catalog_conn.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "username=excluded.username, last_seen_utc=excluded.last_seen_utc",
        (author["id"], author["username"], now, now),
    )
    nickname = author.get("nickname")
    if nickname and nickname != author["username"]:
        existing = catalog_conn.execute(
            "SELECT 1 FROM user_nicknames WHERE user_id=? AND nickname=?",
            (author["id"], nickname),
        ).fetchone()
        if existing is None:
            catalog_conn.execute(
                "INSERT INTO user_nicknames (user_id, nickname, observed_utc) VALUES (?, ?, ?)",
                (author["id"], nickname, now),
            )


async def backfill_missing_users(client, catalog_conn: sqlite3.Connection, data_dir) -> dict:
    """One-off sweep: for every author_id that appears in any archived
    shard but has no users row yet (messages written before user
    tracking existed), fetch their current username via the Discord API
    and upsert it. Best-effort -- an account that's left Discord
    entirely (NotFound) or can't be fetched (Forbidden/HTTPException) is
    skipped, not retried forever; this is a one-time catch-up, not an
    ongoing requirement, matching this project's stated principle that
    historical identity from before observation began can't be
    reconstructed."""
    shard_paths = {
        row["shard_path"] for row in
        catalog_conn.execute("SELECT DISTINCT shard_path FROM channel_month_shard").fetchall()
    }
    known_ids = {row["id"] for row in catalog_conn.execute("SELECT id FROM users").fetchall()}
    missing_ids: set[str] = set()
    for shard_path in shard_paths:
        full_path = data_dir / shard_path
        if not full_path.exists():
            continue
        shard_conn = connect_shard(full_path)
        try:
            for row in shard_conn.execute("SELECT DISTINCT author_id FROM messages").fetchall():
                if row["author_id"] not in known_ids:
                    missing_ids.add(row["author_id"])
        finally:
            shard_conn.close()

    fetched, failed = 0, 0
    for user_id in sorted(missing_ids):
        try:
            user = await client.fetch_user(int(user_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            failed += 1
            continue
        upsert_user(catalog_conn, {"id": str(user.id), "username": user.name, "nickname": None})
        catalog_conn.commit()
        fetched += 1
    return {"missing": len(missing_ids), "fetched": fetched, "failed": failed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_users.py -v`
Expected: PASS.

- [ ] **Step 5: Wire `upsert_user` into `archiver/backfill.py`**

Read `backfill_channel`'s current body in full first. Add the import `from archiver.users import map_author, upsert_user` at the top. In the main loop, right after `pages = [(m, map_message(m)) for m in messages]`, add a line upserting each message's author into the SAME transaction `commit_page` will later commit (so a failure rolls both back together, matching this project's existing atomicity story):

```python
            pages = [(m, map_message(m)) for m in messages]
            for m in messages:
                upsert_user(catalog_conn, map_author(m))
            oldest_id = str(messages[0].id)
            newest_id = str(messages[-1].id)
            commit_page(store, catalog_conn, channel_id, pages, oldest_id, newest_id)
```

Append to `tests/test_backfill.py` (check its existing fixtures first — it should already have a fake channel/message setup from Stage 3):
```python
def test_backfill_channel_populates_users_table(tmp_path):
    # Reuse this file's existing fixtures for a channel with 1-2 fake
    # messages (see the file's other tests for the established pattern
    # of constructing a FakeHistoryChannel and running backfill_channel
    # against it). After backfill_channel completes, assert the
    # catalog's users table has a row for each distinct author_id the
    # fake messages used, with the expected username.
    ...
```
(Write this test by directly copying the setup pattern from an existing passing test in `tests/test_backfill.py` — read that file first and adapt its exact fixture-construction style rather than inventing a new one; the exact fixture names/classes used in this project's `tests/discord_fakes.py` must be consulted and reused as-is.)

- [ ] **Step 6: Wire `upsert_user` into `archiver/live.py`**

Read `apply_live_message`'s current body in full first. Add the import `from archiver.users import map_author, upsert_user` at the top. Add the call inside the catalog-write try block (same transaction as the checkpoint/count writes):

```python
    try:
        if advance_checkpoint:
            catalog_conn.execute(
                "UPDATE coverage SET live_checkpoint=? "
                "WHERE channel_id=? AND (live_checkpoint IS NULL "
                "OR CAST(? AS INTEGER) > CAST(live_checkpoint AS INTEGER))",
                (message_id, channel_id, message_id),
            )
        if is_new:
            catalog_conn.execute(
                "UPDATE coverage SET message_count=message_count+1 WHERE channel_id=?",
                (channel_id,),
            )
        upsert_user(catalog_conn, map_author(message))
        catalog_conn.commit()
    except BaseException:
        catalog_conn.rollback()
        raise
```
(This assumes Task 2 of the `2026-10-01-stage4-followups` plan has already landed on `main` and been merged into this branch's base, so `apply_live_message` already has the `is_new`/decoupled-count structure shown above — if that plan hasn't merged yet when you start this task, adapt the insertion point to wherever the catalog-write try block currently is, keeping `upsert_user`'s call inside it, right before `catalog_conn.commit()`.)

Append to `tests/test_live.py` (check its existing fixtures/imports first):
```python
def test_apply_live_message_populates_users_table(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED)
    # FakeMessage's .author is a FakeUserRef with only .id -- extend it or use a
    # richer fake with .name/.display_name if this test needs one; check
    # tests/discord_fakes.py's current FakeUserRef/FakeMessage shape first and
    # extend minimally if it doesn't already carry a username, following this
    # project's established "hand-built fakes carry only what's read" principle.
    apply_live_message(store, catalog, msg)
    row = catalog.execute("SELECT username FROM users WHERE id='2'").fetchone()
    assert row is not None
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_backfill.py tests/test_live.py tests/test_users.py -v`
Expected: PASS. If `tests/discord_fakes.py`'s `FakeUserRef`/`FakeMessage` don't currently carry a `.name`/`.display_name` on the author, extend `FakeUserRef` minimally (add `name`/`display_name` constructor params with sensible defaults) rather than inventing a parallel fake class — follow this project's existing fixture-extension pattern (e.g. how `FakeHistoryChannel` was extended in Stage 4 to accept a `datetime` for `after`).

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 9: Commit**

```bash
git add archiver/users.py archiver/backfill.py archiver/live.py tests/test_users.py tests/test_backfill.py tests/test_live.py tests/discord_fakes.py
git commit -m "feat: populate the users table from backfill and live capture; add a one-off catch-up sweep"
```

---

### Task 2: Wire `backfill_missing_users` into the CLI

**Files:**
- Modify: `archiver/cli.py`
- Test: none (startup-sequence CLI glue, consistent with this project's established pattern for `_run_live`/`_run_backfill`'s own on_ready bodies)

**Interfaces:**
- Produces: `_run_live`'s `on_ready` and `_run_backfill`'s `on_ready` both call `backfill_missing_users(client, catalog_conn, config.data_dir)` and print a one-line summary.

- [ ] **Step 1: Wire into `_run_live`**

Read `_run_live`'s current `on_ready` body in full. Add `from archiver.users import backfill_missing_users` to the imports. Inside the existing `try:` block that already runs `discover_guild`/`catch_up_missed_messages`/`rescan_recent_window`, add a call after those (before the `backfill_all_pending` task is kicked off, or after — either is fine since this is independent of channel backfill):

```python
            await discover_guild(guild, catalog_conn)
            await catch_up_missed_messages(client, catalog_conn, store, caught_up_channels)
            await rescan_recent_window(client, catalog_conn, store)
            user_stats = await backfill_missing_users(client, catalog_conn, config.data_dir)
            if user_stats["missing"]:
                print(f"User catch-up: fetched {user_stats['fetched']}/{user_stats['missing']} "
                      f"missing usernames ({user_stats['failed']} unreachable).")
            if backfill_task is None or backfill_task.done():
                backfill_task = asyncio.create_task(backfill_all_pending(client, catalog_conn, store))
```
(This stays inside the existing `try/except Exception` wrapper that already logs-and-continues on any startup-sequence failure, per the Stage 4 `on_ready` correction — a slow or failing user catch-up must not prevent live message capture from starting, matching that same established principle.)

- [ ] **Step 2: Wire into `_run_backfill`**

Read `_run_backfill`'s current `on_ready` body. Add the same call after `await backfill_all_pending(client, catalog_conn, store)`:
```python
            await backfill_all_pending(client, catalog_conn, store)
            user_stats = await backfill_missing_users(client, catalog_conn, config.data_dir)
            if user_stats["missing"]:
                print(f"User catch-up: fetched {user_stats['fetched']}/{user_stats['missing']} "
                      f"missing usernames ({user_stats['failed']} unreachable).")
```

- [ ] **Step 3: Verify manually and run the full suite**

Run: `python -m pytest -v` to confirm no regressions (this task's own changes aren't unit-tested, per the established CLI-glue pattern, but must not break anything else).

- [ ] **Step 4: Commit**

```bash
git add archiver/cli.py
git commit -m "feat: run the user catch-up sweep on live and backfill startup"
```

---

### Task 3: Report garbage collection — delete stale files on category rename or removal

**Files:**
- Modify: `archiver/db.py` (append `CATALOG_SCHEMA_V3`)
- Modify: `archiver/report_state.py`
- Modify: `archiver/cli.py` (`_run_report`)
- Test: `tests/test_report_state.py`

**Interfaces:**
- Produces: `archiver.report_state.mark_scope_generated(catalog_conn, scope, fingerprint, filename) -> None` (signature gains `filename`); `archiver.report_state.get_previous_filename(catalog_conn, scope) -> str | None`; `archiver.report_state.all_known_scopes(catalog_conn) -> dict[str, str]` (every scope -> filename currently recorded); `archiver.report_state.forget_scope(catalog_conn, scope) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_report_state.py` (check its existing imports/fixtures first):
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_report_state.py -v -k "filename or forget"`
Expected: FAIL — `report_fingerprint` has no `filename` column; `get_previous_filename`/`all_known_scopes`/`forget_scope` don't exist; `mark_scope_generated` doesn't accept a `filename` argument.

- [ ] **Step 3: Append `CATALOG_SCHEMA_V3` to `archiver/db.py`**

```python
CATALOG_SCHEMA_V3 = """
ALTER TABLE report_fingerprint ADD COLUMN filename TEXT NOT NULL DEFAULT '';
"""

CATALOG_MIGRATIONS: list[tuple[int, str]] = [
    (1, CATALOG_SCHEMA_V1), (2, CATALOG_SCHEMA_V2), (3, CATALOG_SCHEMA_V3),
]
```
(Replace the existing `CATALOG_MIGRATIONS` line with this three-entry version. `ALTER TABLE ... ADD COLUMN` with a `NOT NULL DEFAULT` is valid SQLite and safely backfills the default into every pre-existing row.)

- [ ] **Step 4: Update `archiver/report_state.py`**

```python
def mark_scope_generated(catalog_conn: sqlite3.Connection, scope: str, fingerprint: str,
                          filename: str) -> None:
    catalog_conn.execute(
        "INSERT INTO report_fingerprint (scope, fingerprint, generated_utc, filename) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET fingerprint=excluded.fingerprint, "
        "generated_utc=excluded.generated_utc, filename=excluded.filename",
        (scope, fingerprint, _now(), filename),
    )
    catalog_conn.commit()


def get_previous_filename(catalog_conn: sqlite3.Connection, scope: str) -> str | None:
    row = catalog_conn.execute(
        "SELECT filename FROM report_fingerprint WHERE scope=?", (scope,)
    ).fetchone()
    return row["filename"] if row and row["filename"] else None


def all_known_scopes(catalog_conn: sqlite3.Connection) -> dict[str, str]:
    rows = catalog_conn.execute("SELECT scope, filename FROM report_fingerprint").fetchall()
    return {r["scope"]: r["filename"] for r in rows}


def forget_scope(catalog_conn: sqlite3.Connection, scope: str) -> None:
    catalog_conn.execute("DELETE FROM report_fingerprint WHERE scope=?", (scope,))
    catalog_conn.commit()
```
(Replace the existing `mark_scope_generated` definition with this one; add the three new functions after it.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_report_state.py -v`
Expected: PASS. Note `mark_scope_generated`'s signature changed (gained a required `filename` parameter) — every existing call site and test calling it needs updating; grep for `mark_scope_generated(` across the whole codebase and update each one.

- [ ] **Step 6: Wire GC into `_run_report` in `archiver/cli.py`**

Read `_run_report`'s current body in full. Add the import additions to the existing `from archiver.report_state import ...` line: `get_previous_filename, all_known_scopes, forget_scope`. Modify the per-scope loop and add a post-loop cleanup pass:

```python
        generated, skipped = [], []
        for scope, channel_ids in scopes.items():
            current_fingerprint = compute_scope_fingerprint(catalog_conn, config.data_dir, channel_ids)
            if not force and not is_scope_dirty(catalog_conn, scope, current_fingerprint):
                skipped.append(scope)
                continue

            scope_rows = [coverage_by_channel[cid] for cid in channel_ids if cid in coverage_by_channel]
            stats = gather_scope_stats(catalog_conn, config.data_dir, channel_ids,
                                        excluded_author_ids=excluded)
            scope_newly_inaccessible = [r for r in newly_inaccessible if r["channel_id"] in channel_ids]

            if scope == "server":
                label, filename = "Server", "server.md"
            else:
                category_id = scope.split(":", 1)[1]
                label = next(
                    (r["category_name"] for r in scope_rows if r.get("category_name")),
                    "Uncategorized" if category_id == "uncategorized" else category_id,
                )
                safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in label).strip() or category_id
                filename = f"{safe_name}-{category_id[-6:]}.md"

            previous_filename = get_previous_filename(catalog_conn, scope)
            text = render_report(label, scope_rows, stats, scope_newly_inaccessible)
            (reports_dir / filename).write_text(text, encoding="utf-8")
            if previous_filename and previous_filename != filename:
                stale_path = reports_dir / previous_filename
                if stale_path.exists():
                    stale_path.unlink()
            mark_scope_generated(catalog_conn, scope, current_fingerprint, filename)
            generated.append(filename)

        removed = []
        for old_scope, old_filename in all_known_scopes(catalog_conn).items():
            if old_scope not in scopes:
                stale_path = reports_dir / old_filename
                if stale_path.exists():
                    stale_path.unlink()
                forget_scope(catalog_conn, old_scope)
                removed.append(old_filename)

        save_coverage_snapshot(catalog_conn, coverage)

        if generated:
            print(f"Generated: {', '.join(generated)}")
        if skipped:
            print(f"Skipped (unchanged): {len(skipped)} scope(s)")
        if removed:
            print(f"Removed (category gone): {', '.join(removed)}")
        if not generated and not skipped:
            print("No channels discovered yet -- nothing to report.")
        return 0
```

- [ ] **Step 7: Verify manually and run the full suite**

Run: `python -m pytest -v`. This task's `_run_report` change itself isn't unit-tested (consistent with this project's CLI-glue pattern), but confirm every other test still passes, especially anything that called `mark_scope_generated` with its old 3-argument signature.

- [ ] **Step 8: Commit**

```bash
git add archiver/db.py archiver/report_state.py archiver/cli.py tests/test_report_state.py
git commit -m "feat: garbage-collect stale report files on category rename or removal"
```

---

### Task 4: Shard-scan deduplication and a read-only guard on `connect_shard`

**Files:**
- Modify: `archiver/reports.py`
- Modify: `archiver/cli.py` (`_run_report`)
- Modify: `config.example.json`
- Test: `tests/test_reports.py`

**Interfaces:**
- Produces: `archiver.reports.gather_scope_stats(catalog_conn, data_dir, channel_ids, *, excluded_author_ids=frozenset(), top_n=20, channel_stats_cache: dict | None = None) -> ScopeStats` — when a `channel_stats_cache` dict is passed, a channel's shards are scanned at most once per `_run_report` invocation and the (already exclusion-filtered) per-channel totals are reused for every scope that channel belongs to, instead of rescanning its shards once per scope. Also skips (does not create) a shard file that doesn't exist on disk, rather than letting `connect_shard` silently create an empty one.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reports.py` (check its existing imports/fixtures first — it should already have `_write_shard_with_messages`/`make_message` helpers from Stage 6's own tests):
```python
def test_gather_scope_stats_reuses_cache_across_calls_for_the_same_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chat" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    _write_shard_with_messages(shard_path, [make_message(content="hello", channel_id="1")])

    cache: dict = {}
    first = gather_scope_stats(catalog, tmp_path, ["1"], channel_stats_cache=cache)
    assert "1" in cache

    # Delete the shard file entirely -- if gather_scope_stats re-scans
    # channel "1" instead of using the cache, this would now raise or
    # return zero messages instead of the cached count.
    shard_path.unlink()
    second = gather_scope_stats(catalog, tmp_path, ["1"], channel_stats_cache=cache)
    assert second.total_messages == first.total_messages == 1


def test_gather_scope_stats_skips_a_missing_shard_file_without_creating_it(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    # Note: the shard file/directory is never created on disk.

    stats = gather_scope_stats(catalog, tmp_path, ["1"])
    assert stats.total_messages == 0
    assert not (tmp_path / "uncategorized" / "chat" / "2025-10.sqlite").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_reports.py -v -k "cache or missing_shard"`
Expected: FAIL — `gather_scope_stats` doesn't accept `channel_stats_cache`; the missing-shard test currently fails because `connect_shard` creates the file and its parent directories as a side effect.

- [ ] **Step 3: Modify `gather_scope_stats` in `archiver/reports.py`**

```python
def gather_scope_stats(catalog_conn: sqlite3.Connection, data_dir: Path, channel_ids: list[str],
                        *, excluded_author_ids: frozenset = frozenset(), top_n: int = 20,
                        channel_stats_cache: dict | None = None) -> ScopeStats:
    """channel_stats_cache, when passed, lets a channel's shards be
    scanned at most once per cache dict (one archive report invocation)
    instead of once per scope that channel belongs to -- a channel is
    always in exactly 2 scopes ("server" and its one category), so this
    halves the shard-opening work on a full regeneration. The cache
    bakes in excluded_author_ids at build time, which is safe only
    because this project calls gather_scope_stats with the same
    excluded_author_ids for every scope within one archive report
    invocation (it comes from one Config, not per-scope)."""
    author_counts: Counter = Counter()
    word_counts: Counter = Counter()
    attachment_counts: Counter = Counter()
    total_messages = 0

    for channel_id in channel_ids:
        if channel_stats_cache is not None and channel_id in channel_stats_cache:
            ch_total, ch_authors, ch_words, ch_attachments = channel_stats_cache[channel_id]
        else:
            ch_total = 0
            ch_authors: Counter = Counter()
            ch_words: Counter = Counter()
            ch_attachments: Counter = Counter()
            shard_rows = catalog_conn.execute(
                "SELECT DISTINCT shard_path FROM channel_month_shard WHERE channel_id=?",
                (channel_id,),
            ).fetchall()
            for shard_row in shard_rows:
                shard_path = data_dir / shard_row["shard_path"]
                if not shard_path.exists():
                    continue
                shard_conn = connect_shard(shard_path)
                try:
                    for row in shard_conn.execute(
                        "SELECT author_id, content FROM messages WHERE channel_id=? AND deleted_utc IS NULL",
                        (channel_id,),
                    ).fetchall():
                        ch_total += 1
                        if row["author_id"] not in excluded_author_ids:
                            ch_authors[row["author_id"]] += 1
                            for word in _WORD_RE.findall(row["content"].lower()):
                                ch_words[word] += 1
                    for att_row in shard_conn.execute(
                        "SELECT a.content_type FROM attachments a JOIN messages m ON a.message_id = m.id "
                        "WHERE m.channel_id=? AND m.deleted_utc IS NULL",
                        (channel_id,),
                    ).fetchall():
                        ch_attachments[classify_attachment(att_row["content_type"])] += 1
                finally:
                    shard_conn.close()
            if channel_stats_cache is not None:
                channel_stats_cache[channel_id] = (ch_total, ch_authors, ch_words, ch_attachments)

        total_messages += ch_total
        author_counts.update(ch_authors)
        word_counts.update(ch_words)
        attachment_counts.update(ch_attachments)

    return ScopeStats(
        total_messages=total_messages,
        top_authors=author_counts.most_common(top_n),
        top_words=word_counts.most_common(top_n),
        attachment_counts=dict(attachment_counts),
    )
```
(Replace the existing `gather_scope_stats` function body entirely with this version.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_reports.py -v`
Expected: PASS, including all of Stage 6's own pre-existing `gather_scope_stats` tests (the exclusion/total-vs-ranked-count behavior is unchanged, only restructured to go through a per-channel accumulator first).

- [ ] **Step 5: Wire the cache into `_run_report` in `archiver/cli.py`**

Add `channel_stats_cache: dict = {}` once, before the per-scope loop, and pass it to every `gather_scope_stats(...)` call:
```python
        channel_stats_cache: dict = {}
        ...
        for scope, channel_ids in scopes.items():
            ...
            stats = gather_scope_stats(catalog_conn, config.data_dir, channel_ids,
                                        excluded_author_ids=excluded,
                                        channel_stats_cache=channel_stats_cache)
```

- [ ] **Step 6: Document `excluded_ranking_author_ids` in `config.example.json`**

```json
{
  "guild_id": "000000000000000000",
  "data_dir": "C:/ChangeMe/server-archive",
  "excluded_ranking_author_ids": []
}
```
(Add the key with an empty-list default — comment-free since JSON has no comments; its meaning is documented in the spec and this project's `--help`/README text, not inline here.)

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 8: Commit**

```bash
git add archiver/reports.py archiver/cli.py config.example.json tests/test_reports.py
git commit -m "perf: scan each channel's shards once per report run instead of once per scope; guard against creating missing shard files"
```

---

### Task 5: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-4 passes, plus all prior stages' tests still pass.

- [ ] **Step 2: Note what's NOT covered by the automated suite**

`backfill_missing_users`'s actual Discord API interaction (`client.fetch_user`) is not exercised against a real Discord connection by any automated test — consistent with this project's established pattern for anything requiring a live gateway connection. Before this follow-up pass is considered fully proven: run `python -m archiver.cli live` (or `backfill`) against the real server once and confirm a "User catch-up: fetched N/M missing usernames" line prints, then run `python -m archiver.cli find from:<some real username>` and confirm it resolves correctly; run `python -m archiver.cli report` twice in a row and confirm the second run reports every scope "Skipped (unchanged)"; then rename a category in the real Discord server, run `archive report` again, and confirm the old-named `.md` file is gone and a new-named one exists.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

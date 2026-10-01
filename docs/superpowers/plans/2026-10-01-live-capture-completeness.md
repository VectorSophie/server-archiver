# Live Capture Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three documented gaps in live capture: (1) a thread/forum-post created mid-session isn't tracked until the next restart's full discovery sweep; (2) a reaction added/removed after a message's initial capture isn't reflected until the periodic rescan window or a restart; (3) there's no periodic re-discovery at all, only the one sweep at startup.

**Architecture:** (1) discord.py fires `on_thread_create` the moment a thread is created — a new `apply_live_thread_create` upserts it into `channels`/`coverage` immediately (status `pending`, picked up by the next `backfill_all_pending` sweep), mirroring `discovery.py`'s existing per-channel upsert logic but scoped to one thread. (2) A raw reaction add/remove event (`on_raw_reaction_add`/`on_raw_reaction_remove`) carries only *who changed what*, never the message's current aggregate reaction counts — the only correct way to get an authoritative count (spec: aggregate only) is to re-fetch the message and re-map it through the existing `map_message`/`write_message` pipeline, same idempotent-upsert story as everywhere else in this codebase. (3) A simple `asyncio.sleep`-loop background task, structured the same way `backfill_task` already is in `_run_live`, re-runs `discover_guild` on an interval.

**Tech Stack:** No new dependencies — `asyncio.sleep` for the periodic loop.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§5 Discovery, §6 Live capture)

## Global Constraints

- None of these three features may advance `live_checkpoint` or `message_count` — a reaction update isn't a new message, and thread discovery/periodic rediscovery don't touch messages at all.
- Each handler/loop iteration must isolate its own failures (consistent with every other live-capture entry point in this codebase) — one bad event or one failed rediscovery pass must never crash the live daemon or block message capture.
- `archiver/live.py` and `archiver/cli.py` are already-merged, already live-verified code actively running as a background process — all three additions are purely additive (new functions, new event handlers, one new background task), never a restructuring of existing control flow.

## File Structure

- **Modify:** `archiver/live.py` — `apply_live_thread_create`, `apply_live_reaction_change`, `run_periodic_rediscovery_once`.
- **Modify:** `archiver/cli.py` — wire all three into `_run_live`.
- **Modify:** `tests/discord_fakes.py` — `FakeHistoryChannel` gains `fetch_message`; `FakeClient` gains `get_guild`.
- **Test:** `tests/test_live.py` (extended).

---

### Task 1: Live thread/forum-post creation tracking

**Files:**
- Modify: `archiver/live.py`
- Modify: `archiver/cli.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Produces: `archiver.live.apply_live_thread_create(catalog_conn: sqlite3.Connection, thread) -> None` — upserts `thread` into `channels` (status derived via `archiver.discord_io.classify_channel_type`, `category_id` inherited from the thread's parent channel if known) and, if newly inserted, a `coverage` row with `status='pending'`. Commits internally (this is a one-shot event handler call, not part of a larger batched transaction, unlike `apply_live_message`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live.py` (check its existing imports/fixtures first — `_seed_channel`, `connect_catalog`, and `tests.discord_fakes.FakeThread` should already be usable):
```python
from tests.discord_fakes import FakeThread


def test_apply_live_thread_create_adds_channel_and_pending_coverage_row(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)  # seeds channel "1" as the thread's parent
    thread = FakeThread(id=999, name="new thread", parent_id=1)

    apply_live_thread_create(catalog, thread)

    channel_row = catalog.execute("SELECT name, type, parent_id FROM channels WHERE id='999'").fetchone()
    assert channel_row["name"] == "new thread"
    assert channel_row["type"] == "public_thread"
    assert channel_row["parent_id"] == "1"
    coverage_row = catalog.execute("SELECT status FROM coverage WHERE channel_id='999'").fetchone()
    assert coverage_row["status"] == "pending"


def test_apply_live_thread_create_inherits_parent_category(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1", category_id="cat1")
    thread = FakeThread(id=999, name="new thread", parent_id=1)

    apply_live_thread_create(catalog, thread)

    row = catalog.execute("SELECT category_id FROM channels WHERE id='999'").fetchone()
    assert row["category_id"] == "cat1"


def test_apply_live_thread_create_is_idempotent_on_replay(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    thread = FakeThread(id=999, name="new thread", parent_id=1)

    apply_live_thread_create(catalog, thread)
    apply_live_thread_create(catalog, thread)  # e.g. a duplicate gateway event

    rows = catalog.execute("SELECT 1 FROM coverage WHERE channel_id='999'").fetchall()
    assert len(rows) == 1  # not duplicated, and status stays 'pending' not reset
```

Check `_seed_channel`'s actual current signature in `tests/test_live.py` before using `category_id=` — adapt the test to whatever parameters it actually accepts (it should already support seeding a `category_id`, since earlier stages' tests use it; if not, read the fixture and adapt).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v -k thread_create`
Expected: FAIL — `ImportError: cannot import name 'apply_live_thread_create'`.

- [ ] **Step 3: Append `apply_live_thread_create` to `archiver/live.py`**

Add `from archiver.discord_io import classify_channel_type` to the imports. Then:
```python
def apply_live_thread_create(catalog_conn: sqlite3.Connection, thread) -> None:
    """A thread/forum post created during a live session is tracked
    immediately rather than only on the next restart's full discovery
    sweep (a previously documented gap) -- mirrors discovery.py's
    per-channel upsert, scoped to one new thread. status starts
    'pending', picked up by the next backfill_all_pending sweep."""
    channel_id = str(thread.id)
    parent_id = str(thread.parent_id) if thread.parent_id else None
    category_id = None
    if parent_id is not None:
        parent_row = catalog_conn.execute(
            "SELECT category_id FROM channels WHERE id=?", (parent_id,)
        ).fetchone()
        category_id = parent_row["category_id"] if parent_row else None
    kind = classify_channel_type(thread)
    now = _now()

    existing = catalog_conn.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone()
    catalog_conn.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, last_seen_utc=excluded.last_seen_utc",
        (channel_id, thread.name, kind, parent_id, category_id, now, now),
    )
    if existing is None:
        catalog_conn.execute(
            "INSERT INTO coverage (channel_id, status) VALUES (?, 'pending') "
            "ON CONFLICT(channel_id) DO NOTHING",
            (channel_id,),
        )
    catalog_conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v -k thread_create`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire into `_run_live` in `archiver/cli.py`**

Add `apply_live_thread_create` to the existing `from archiver.live import ...` line. Add alongside the other `@client.event` handlers in `_run_live`:
```python
    @client.event
    async def on_thread_create(thread):
        apply_live_thread_create(catalog_conn, thread)
```

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 7: Commit**

```bash
git add archiver/live.py archiver/cli.py tests/test_live.py
git commit -m "feat: track threads/forum posts created during a live session immediately, not only on restart"
```

---

### Task 2: Live reaction updates

**Files:**
- Modify: `archiver/live.py`
- Modify: `archiver/cli.py`
- Modify: `tests/discord_fakes.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Produces: `archiver.live.apply_live_reaction_change(client, store: ShardStore, catalog_conn: sqlite3.Connection, channel_id, message_id) -> None` — async. Re-fetches the message (a raw reaction event carries no aggregate count, only who changed what) and re-applies it via `map_message`/`write_message`, never touching `live_checkpoint`/`message_count` (this re-syncs an existing message, never creates one). Isolates every failure (untracked channel, channel/message fetch failure, message deleted before the fetch completes) by simply returning, matching `rescan_recent_window`'s established isolation style.

- [ ] **Step 1: Write the failing test**

First, extend `tests/discord_fakes.py`'s `FakeHistoryChannel` with a `fetch_message` method:
```python
    async def fetch_message(self, message_id: int):
        for m in self._messages:
            if m.id == message_id:
                return m
        raise discord.NotFound(FakeResponse(), "Unknown Message")
```
(Add this method to the existing `FakeHistoryChannel` class, alongside its existing `history` method — don't change anything else about the class.)

Then append to `tests/test_live.py`:
```python
from tests.discord_fakes import FakeHistoryChannel, FakeClient, FakeReaction


async def test_apply_live_reaction_change_refetches_and_updates_reaction_count(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED,
                       reactions=[FakeReaction("\N{THUMBS UP SIGN}", count=3)])
    channel = FakeHistoryChannel(id=1, messages=[msg])
    client = FakeClient(channels={1: channel})

    await apply_live_reaction_change(client, store, catalog, "1", 100)

    shard = store.get_shard("1", CREATED)
    row = shard.execute("SELECT count FROM reactions WHERE message_id='100'").fetchone()
    assert row["count"] == 3


async def test_apply_live_reaction_change_never_touches_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute(
        "UPDATE coverage SET status='complete', live_checkpoint='50' WHERE channel_id='1'"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED)
    channel = FakeHistoryChannel(id=1, messages=[msg])
    client = FakeClient(channels={1: channel})

    await apply_live_reaction_change(client, store, catalog, "1", 100)

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "50"  # unchanged despite message id 100 > 50


async def test_apply_live_reaction_change_isolates_a_message_fetch_failure(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    channel = FakeHistoryChannel(id=1, messages=[])  # message 100 doesn't exist -> fetch_message raises NotFound
    client = FakeClient(channels={1: channel})

    await apply_live_reaction_change(client, store, catalog, "1", 100)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v -k reaction_change`
Expected: FAIL — `ImportError: cannot import name 'apply_live_reaction_change'`.

- [ ] **Step 3: Append `apply_live_reaction_change` to `archiver/live.py`**

```python
async def apply_live_reaction_change(client, store: ShardStore, catalog_conn: sqlite3.Connection,
                                       channel_id, message_id) -> None:
    """A raw reaction add/remove event carries only who changed what,
    never the message's current aggregate reaction counts -- the only
    way to get an authoritative count (spec: aggregate only, no
    per-reactor lists) is to re-fetch the message and re-map it through
    the same idempotent write_message pipeline every other message
    write uses. Never advances live_checkpoint/message_count -- this
    re-syncs an existing message, it never creates one."""
    channel_id = str(channel_id)
    if not _is_tracked_channel(catalog_conn, channel_id):
        return
    try:
        discord_channel = await client.fetch_channel(int(channel_id))
    except Exception:
        return
    if not hasattr(discord_channel, "fetch_message"):
        return
    try:
        message = await discord_channel.fetch_message(int(message_id))
    except Exception:
        return

    mapped = map_message(message)
    shard_conn = store.get_shard(channel_id, message.created_at)
    try:
        write_message(shard_conn, mapped)
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v -k reaction_change`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire into `_run_live` in `archiver/cli.py`**

Add `apply_live_reaction_change` to the existing `from archiver.live import ...` line. Add:
```python
    @client.event
    async def on_raw_reaction_add(payload):
        await apply_live_reaction_change(client, store, catalog_conn, payload.channel_id, payload.message_id)

    @client.event
    async def on_raw_reaction_remove(payload):
        await apply_live_reaction_change(client, store, catalog_conn, payload.channel_id, payload.message_id)
```

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 7: Commit**

```bash
git add archiver/live.py archiver/cli.py tests/discord_fakes.py tests/test_live.py
git commit -m "feat: re-sync a message's reaction counts live when a reaction is added or removed"
```

---

### Task 3: Periodic re-discovery

**Files:**
- Modify: `archiver/live.py`
- Modify: `archiver/cli.py`
- Modify: `tests/discord_fakes.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Produces: `archiver.live.run_periodic_rediscovery_once(client, guild_id: str, catalog_conn: sqlite3.Connection) -> None` — async. One pass: looks up the guild via `client.get_guild`, runs `discover_guild` if found, isolates any failure (never raises). `_run_live` wraps this in an infinite `asyncio.sleep`-then-call loop, started as its own background task the same way `backfill_task` already is.

- [ ] **Step 1: Write the failing test**

First, add a `get_guild` method to `tests/discord_fakes.py`'s `FakeClient`:
```python
    def get_guild(self, guild_id: int):
        return self._guild
```
and a `guild=None` constructor parameter, stored as `self._guild = guild`. (Add alongside the existing constructor parameters — don't change the existing ones' behavior.)

Then append to `tests/test_live.py`:
```python
from tests.discord_fakes import FakeGuild


async def test_run_periodic_rediscovery_once_runs_discover_guild(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=1, name="general", type_=discord.ChannelType.text)
    guild = FakeGuild([channel])
    client = FakeClient(guild=guild)

    await run_periodic_rediscovery_once(client, "999", catalog)

    row = catalog.execute("SELECT 1 FROM channels WHERE id='1'").fetchone()
    assert row is not None


async def test_run_periodic_rediscovery_once_no_guild_does_not_raise(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    client = FakeClient(guild=None)

    await run_periodic_rediscovery_once(client, "999", catalog)  # must not raise


async def test_run_periodic_rediscovery_once_isolates_a_discover_guild_failure(tmp_path, monkeypatch):
    import archiver.live as live_module

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    guild = FakeGuild([])
    client = FakeClient(guild=guild)

    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(live_module, "discover_guild", _raise)

    await run_periodic_rediscovery_once(client, "999", catalog)  # must not raise
```
(Check `tests/discord_fakes.py`'s `FakeChannel` constructor signature first and adapt the first test's `FakeChannel(...)` call to match exactly — it's used elsewhere in this codebase's discovery tests, so match that established usage.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v -k periodic_rediscovery`
Expected: FAIL — `ImportError: cannot import name 'run_periodic_rediscovery_once'`.

- [ ] **Step 3: Append `run_periodic_rediscovery_once` to `archiver/live.py`**

Add `from archiver.discovery import discover_guild` to the imports. Then:
```python
async def run_periodic_rediscovery_once(client, guild_id: str, catalog_conn: sqlite3.Connection) -> None:
    """One periodic re-discovery pass, factored out of the sleep loop
    in _run_live so it's independently testable. Isolates any failure
    -- a bad pass must not kill the periodic task or the live daemon."""
    guild = client.get_guild(int(guild_id))
    if guild is None:
        return
    try:
        await discover_guild(guild, catalog_conn)
    except Exception:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v -k periodic_rediscovery`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire the sleep loop into `_run_live` in `archiver/cli.py`**

Add `run_periodic_rediscovery_once` to the existing `from archiver.live import ...` line. Inside `_run_live`, before `on_ready` is defined, add:
```python
    rediscovery_task: asyncio.Task | None = None

    async def _periodic_rediscovery_loop(interval_seconds: int = 1800) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await run_periodic_rediscovery_once(client, config.guild_id, catalog_conn)
```
Inside `on_ready`'s `try` block, alongside the existing `backfill_task` guard, add:
```python
            nonlocal rediscovery_task
            if rediscovery_task is None or rediscovery_task.done():
                rediscovery_task = asyncio.create_task(_periodic_rediscovery_loop())
```
(Add `nonlocal rediscovery_task` to `on_ready`'s existing `nonlocal` line if one exists, or as its own line — check the function's current structure first. Place this guard the same way the existing `backfill_task` guard works, so a reconnect-triggered re-`on_ready` doesn't spawn a second infinite loop.)

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 7: Commit**

```bash
git add archiver/live.py archiver/cli.py tests/discord_fakes.py tests/test_live.py
git commit -m "feat: periodic re-discovery every 30 minutes, not just at startup"
```

---

### Task 4: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-3 passes, plus all prior tests still pass.

- [ ] **Step 2: Note what's NOT covered by the automated suite**

The actual infinite `_periodic_rediscovery_loop` in `_run_live` (the `while True: sleep; call` wrapper itself, as opposed to the one-pass `run_periodic_rediscovery_once` this plan tests directly) is not unit-tested, consistent with this project's established pattern for `_run_live`'s other background-task loops. `on_thread_create`/`on_raw_reaction_add`/`on_raw_reaction_remove`'s actual wiring (as opposed to the functions they call) is also CLI glue, verified manually, same pattern as every other event handler in `_run_live`. Before this plan is considered fully proven: run `python -m archiver.cli live` against the real server, create a test thread and confirm it shows up in `coverage` as `pending` without restarting; react to an already-archived message and confirm the shard's `reactions` table updates; wait (or temporarily shorten the interval) to confirm a periodic rediscovery pass actually runs and logs/updates as expected.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

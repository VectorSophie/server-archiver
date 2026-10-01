# Stage 4: Live Capture & Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the archive current while the bot is running (new messages, edits, deletes captured live via the gateway) and catch it up correctly after any offline gap (missed messages, missed edits, newly created threads), without ever overwriting a complete stored row with a partial one, and without ever advancing the live checkpoint on anything but confirmed new-message creation (spec §6).

**Architecture:** `archiver/live.py` holds the actual write logic — `apply_live_message` (a full `discord.Message`, reuses Stage 3's `map_message`/`write_message`), `apply_raw_edit` and `apply_raw_delete` (operate on raw gateway dicts/IDs only, per the verified fact that discord.py's own `Message.__init__`/`_update` machinery cannot be trusted to represent "what changed" for a partial payload), and the two startup catch-up passes. `archiver/cli.py` gains a `live` command: a persistent `discord.Client` that never disconnects, wiring gateway events to `live.py`'s functions.

**Verified before writing any code** (all via `inspect.getsource`/`hasattr` against the real installed discord.py 2.7.1, not documentation):
- `ConnectionState.parse_message_update` constructs a full `Message(channel=channel, data=data, state=self)` from the raw partial payload — and `Message.__init__` reads `self.content: str = data['content']` via direct (non-`.get()`) indexing. This means `payload.message`'s convenience attributes cannot reliably tell you which fields were actually present in a partial update. **Every handler in this stage reads `payload.data` (the raw dict) directly, never `payload.message`.**
- `RawMessageDeleteEvent` carries only `message_id`/`channel_id`/`guild_id` — no timestamp, no content.
- `discord.utils.snowflake_time(message_id)` derives an exact UTC creation datetime purely from the ID, no API call needed — this is how edit/delete handlers resolve which month's shard a message lives in without ever having seen a full `Message` object for it.

**Tech Stack:** discord.py (already a dependency), stdlib only otherwise. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§6 Live capture & offline recovery, §4.1 cross-file commit protocol, §11 Testing, §15 Stage 4 scope)

## Global Constraints

- Edit/delete handlers use `payload.data`/raw IDs only — never `payload.message` (verified reason above).
- A partial edit updates only the columns whose raw key is present in the payload; an absent key leaves the stored value untouched (spec §6, Stage 1's own explicit test requirement).
- A delete is recorded only from an explicit `on_raw_message_delete` event carrying a message ID — absence during any scan is never treated as evidence of deletion (spec §6, §12). Deletions that occur while the bot is offline cannot be reconstructed by a later rescan; this stage's startup catch-up only fetches messages that still exist.
- The live checkpoint (`coverage.live_checkpoint`) advances **only** from confirmed new-message creation (`on_message`, or the startup catch-up's `history(after=...)` pass) — **never** from processing an edit or a delete on an old message, regardless of that message's own timestamp.
- `live_checkpoint` comparisons use `CAST(id AS INTEGER)`, never a bare TEXT `>` comparison — TEXT-sorted snowflakes with differing digit counts compare incorrectly (flagged in Stage 2's final review, load-bearing here for the first time).
- Every live write function (`apply_live_message`, `apply_raw_edit`, `apply_raw_delete`) wraps its body in try/except with rollback-on-failure before re-raising, matching the pattern Stage 3's `commit_page` was hardened with — a shared long-lived connection must never be left mid-transaction after an unhandled exception, since the *next* gateway event on that same connection would then be corrupted by it.
- A handler only ever touches a channel/message whose `channel_id` is present in the catalog's `channels` table (discovered by Stage 2) — this cheaply filters out gateway events from any other guild the bot's token is also in (e.g. pfpscraper's), without needing a separate guild-ID check on every event.
- The `live` command never disconnects on its own — it is the actual long-running background process this whole project exists to run. (Windows Task Scheduler supervision, restart-on-failure, and the single-instance OS lock are Stage 8's job, not this one — this stage only builds the process itself.)

---

## File Structure

- `archiver/live.py` — `apply_live_message`, `apply_raw_edit`, `apply_raw_delete`, `catch_up_missed_messages`, `rescan_recent_window`.
- `archiver/cli.py` — append `live` command.
- `tests/test_live.py` — new.

---

### Task 1: Live message creation with correct checkpoint advance

**Files:**
- Create: `archiver/live.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Consumes: `map_message`, `write_message`, `ShardStore` (Stage 3).
- Produces: `archiver.live.apply_live_message(store: ShardStore, catalog_conn: sqlite3.Connection, message) -> None` — writes the message via Stage 3's mapper/writer, then advances `coverage.live_checkpoint`/`message_count` only if the channel is tracked (present in `channels`) and only forward (`CAST(id AS INTEGER)` comparison). Rolls back and re-raises on any failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_live.py
from datetime import datetime, timezone

from archiver.db import connect_catalog
from archiver.live import apply_live_message
from archiver.store import ShardStore
from tests.discord_fakes import FakeMessage

CREATED = datetime(2025, 10, 15, 3, 0, 0, tzinfo=timezone.utc)


def _seed_channel(catalog, channel_id="1", category_id="10"):
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        f"('{channel_id}','general','text','{category_id}','{category_id}',0,"
        "'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        f"('{category_id}','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute(f"INSERT INTO coverage (channel_id, status) VALUES ('{channel_id}', 'complete')")
    catalog.commit()


def test_apply_live_message_writes_and_advances_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED)

    apply_live_message(store, catalog, msg)

    row = catalog.execute(
        "SELECT live_checkpoint, message_count FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["live_checkpoint"] == "100"
    assert row["message_count"] == 1
    shard = store.get_shard("1", CREATED)
    assert shard.execute("SELECT content FROM messages WHERE id='100'").fetchone()["content"] == "hi"


def test_apply_live_message_never_moves_checkpoint_backward(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    newer = FakeMessage(id=200, channel_id=1, author_id=2, content="b", created_at=CREATED)
    older = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=CREATED)

    apply_live_message(store, catalog, newer)
    apply_live_message(store, catalog, older)  # arrives out of order

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "200"  # stays at the newer id, not overwritten by the older one


def test_apply_live_message_checkpoint_compares_as_integer_not_text(tmp_path):
    """A larger-digit-count snowflake must compare correctly against a
    smaller one -- TEXT '>' would get this backwards (e.g. '9' > '10')."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    small_digit_count = FakeMessage(id=9, channel_id=1, author_id=2, content="a", created_at=CREATED)
    large_digit_count = FakeMessage(id=10, channel_id=1, author_id=2, content="b", created_at=CREATED)

    apply_live_message(store, catalog, small_digit_count)
    apply_live_message(store, catalog, large_digit_count)

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "10"


def test_apply_live_message_ignores_untracked_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    store = ShardStore(tmp_path, catalog)  # no channel seeded at all
    msg = FakeMessage(id=100, channel_id=999, author_id=2, content="hi", created_at=CREATED)

    apply_live_message(store, catalog, msg)  # must not raise

    row = catalog.execute("SELECT 1 FROM coverage WHERE channel_id='999'").fetchone()
    assert row is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.live'`.

- [ ] **Step 3: Write `archiver/live.py`**

```python
# archiver/live.py
"""Live gateway capture: message creation, partial-edit application,
delete handling, and startup catch-up. Full discord.py Message objects
(on_message, catch-up history pages) reuse Stage 3's map_message/
write_message unchanged. Raw partial payloads (on_raw_message_edit,
on_raw_message_delete) get their own narrow handling -- verified via
inspect.getsource that discord.py's own Message construction from a
partial gateway payload cannot be trusted to represent "what changed",
so those handlers read the raw dict/IDs directly, never a constructed
Message object."""
import sqlite3
from datetime import datetime, timedelta, timezone

import discord

from archiver.discord_message import map_message
from archiver.store import ShardStore, write_message

_EDIT_FIELD_MAP = {"content": "content", "flags": "flags"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_tracked_channel(catalog_conn: sqlite3.Connection, channel_id: str) -> bool:
    return catalog_conn.execute(
        "SELECT 1 FROM channels WHERE id=?", (channel_id,)
    ).fetchone() is not None


def apply_live_message(store: ShardStore, catalog_conn: sqlite3.Connection, message) -> None:
    """Write a newly created message and advance the channel's live
    checkpoint -- the only place the checkpoint moves forward (spec
    §6). Untracked channels (a different guild on the same token, or a
    channel never discovered) are silently ignored."""
    channel_id = str(message.channel.id)
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(message.id)
    try:
        mapped = map_message(message)
        shard_conn = store.get_shard(channel_id, message.created_at)
        write_message(shard_conn, mapped)
        shard_conn.commit()

        catalog_conn.execute(
            "UPDATE coverage SET live_checkpoint=?, message_count=message_count+1 "
            "WHERE channel_id=? AND (live_checkpoint IS NULL "
            "OR CAST(? AS INTEGER) > CAST(live_checkpoint AS INTEGER))",
            (message_id, channel_id, message_id),
        )
        catalog_conn.commit()
    except BaseException:
        catalog_conn.rollback()
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/live.py tests/test_live.py
git commit -m "feat: live message creation with correct forward-only checkpoint"
```

---

### Task 2: Partial raw-edit application

**Files:**
- Modify: `archiver/live.py` (append)
- Modify: `tests/test_live.py` (append)

**Interfaces:**
- Consumes: `ShardStore` (Stage 3), `_is_tracked_channel`/`_now` (Task 1).
- Produces: `archiver.live.apply_raw_edit(store: ShardStore, catalog_conn: sqlite3.Connection, channel_id: str, raw_data: dict) -> None` — applies only the fields present in `raw_data` to the stored `messages` row (`content`, `flags`, and `edited_utc` from `edited_timestamp`). A key absent from `raw_data` leaves the stored value untouched. Never touches `coverage.live_checkpoint`. Logs an `events` row (`event_type='edit'`) only when a stored row actually existed to update.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live.py`:

```python
from archiver.discord_message import map_message
from archiver.live import apply_raw_edit
from archiver.store import write_message


def _seed_message(store, catalog, channel_id, message_id, content="original", flags=0):
    msg = FakeMessage(id=message_id, channel_id=int(channel_id), author_id=2, content=content,
                       created_at=CREATED, flags_value=flags)
    shard_conn = store.get_shard(channel_id, CREATED)
    write_message(shard_conn, map_message(msg))
    shard_conn.commit()
    return shard_conn


def test_apply_raw_edit_updates_only_present_fields(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100, content="original", flags=0)

    apply_raw_edit(store, catalog, "1", {"id": "100", "content": "edited content"})

    row = shard_conn.execute("SELECT content, flags, edited_utc FROM messages WHERE id='100'").fetchone()
    assert row["content"] == "edited content"
    assert row["flags"] == 0  # untouched -- absent from the raw payload
    assert row["edited_utc"] is None  # untouched -- absent from the raw payload


def test_apply_raw_edit_does_not_null_out_absent_fields():
    """The exact property spec §6 requires: a partial payload must
    never overwrite a complete stored row's fields with absence."""


def test_apply_raw_edit_sets_edited_utc_when_present(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_edit(store, catalog, "1", {
        "id": "100", "content": "edited", "edited_timestamp": "2025-10-15T04:00:00.000000+00:00",
    })

    row = shard_conn.execute("SELECT edited_utc FROM messages WHERE id='100'").fetchone()
    assert row["edited_utc"] == "2025-10-15T04:00:00Z"


def test_apply_raw_edit_never_advances_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    _seed_message(store, catalog, "1", 100)

    apply_raw_edit(store, catalog, "1", {"id": "100", "content": "edited"})

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] is None  # writing a message did NOT go through apply_live_message


def test_apply_raw_edit_on_message_never_archived_is_a_safe_no_op(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)

    apply_raw_edit(store, catalog, "1", {"id": "999", "content": "edited"})  # must not raise

    shard_conn = store.get_shard("1", CREATED)
    assert shard_conn.execute("SELECT 1 FROM messages WHERE id='999'").fetchone() is None


def test_apply_raw_edit_logs_an_edit_event(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_edit(store, catalog, "1", {"id": "100", "content": "edited"})

    row = shard_conn.execute(
        "SELECT event_type FROM events WHERE message_id='100' AND event_type='edit'"
    ).fetchone()
    assert row is not None
```

(Delete the empty `test_apply_raw_edit_does_not_null_out_absent_fields` stub above — it was written as a placeholder heading and its actual assertion is already covered by `test_apply_raw_edit_updates_only_present_fields`; leaving an empty test function in would itself be a no-op placeholder, which this plan's own rules forbid. Remove that function entirely before running.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_raw_edit'`.

- [ ] **Step 3: Append `apply_raw_edit` to `archiver/live.py`**

```python
def apply_raw_edit(store: ShardStore, catalog_conn: sqlite3.Connection,
                    channel_id: str, raw_data: dict) -> None:
    """Apply a raw MESSAGE_UPDATE payload to the stored message row, if
    one exists -- reads raw_data directly (never a constructed Message
    object; see module docstring). Only columns whose raw key is
    present get touched. Never advances live_checkpoint (spec §6)."""
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(raw_data["id"])
    created_at = discord.utils.snowflake_time(int(message_id))
    shard_conn = store.get_shard(channel_id, created_at)

    try:
        set_clauses = []
        params: list = []
        for raw_key, column in _EDIT_FIELD_MAP.items():
            if raw_key in raw_data:
                set_clauses.append(f"{column}=?")
                params.append(raw_data[raw_key])
        if raw_data.get("edited_timestamp"):
            edited_dt = discord.utils.parse_time(raw_data["edited_timestamp"])
            set_clauses.append("edited_utc=?")
            params.append(_iso(edited_dt))

        if not set_clauses:
            return

        params.append(message_id)
        cursor = shard_conn.execute(
            f"UPDATE messages SET {', '.join(set_clauses)} WHERE id=?", params
        )
        if cursor.rowcount == 0:
            shard_conn.rollback()
            return

        shard_conn.execute(
            "INSERT INTO events (message_id, event_type, observed_utc, detail) "
            "VALUES (?, 'edit', ?, ?)",
            (message_id, _now(), ",".join(c.split("=")[0] for c in set_clauses)),
        )
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/live.py tests/test_live.py
git commit -m "feat: apply partial raw-edit payloads without nulling absent fields"
```

---

### Task 3: Delete handling

**Files:**
- Modify: `archiver/live.py` (append)
- Modify: `tests/test_live.py` (append)

**Interfaces:**
- Consumes: `ShardStore`, `_is_tracked_channel`/`_now` (Tasks 1-2).
- Produces: `archiver.live.apply_raw_delete(store: ShardStore, catalog_conn: sqlite3.Connection, channel_id: str, message_id) -> None` — sets `messages.deleted_utc` only if the message exists and isn't already marked deleted; logs an `events` row (`event_type='delete'`) only when the update actually matched a row (spec §6/§12: absence is never itself evidence of deletion — this function only ever records a delete when it was told about one explicitly).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live.py`:

```python
from archiver.live import apply_raw_delete


def test_apply_raw_delete_marks_deleted_utc(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_delete(store, catalog, "1", 100)

    row = shard_conn.execute("SELECT deleted_utc FROM messages WHERE id='100'").fetchone()
    assert row["deleted_utc"] is not None


def test_apply_raw_delete_logs_a_delete_event(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_delete(store, catalog, "1", 100)

    row = shard_conn.execute(
        "SELECT event_type FROM events WHERE message_id='100' AND event_type='delete'"
    ).fetchone()
    assert row is not None


def test_apply_raw_delete_on_message_never_archived_does_not_log_event(tmp_path):
    """spec §12: absence is never itself evidence of deletion -- this
    function must not fabricate a delete event for a message it never
    actually confirmed existed in the archive."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)

    apply_raw_delete(store, catalog, "1", 999)  # must not raise

    shard_conn = store.get_shard("1", CREATED)
    row = shard_conn.execute("SELECT 1 FROM events WHERE message_id='999'").fetchone()
    assert row is None


def test_apply_raw_delete_is_idempotent(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_delete(store, catalog, "1", 100)
    apply_raw_delete(store, catalog, "1", 100)  # a duplicate gateway delivery

    count = shard_conn.execute("SELECT COUNT(*) FROM events WHERE message_id='100'").fetchone()[0]
    assert count == 1  # second call is a no-op, not a duplicate event
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_raw_delete'`.

- [ ] **Step 3: Append `apply_raw_delete` to `archiver/live.py`**

```python
def apply_raw_delete(store: ShardStore, catalog_conn: sqlite3.Connection,
                      channel_id: str, message_id) -> None:
    """Mark a message deleted from an explicit gateway delete event.
    Never records a delete for a message this archive never confirmed
    it had (spec §12: absence is not proof of deletion)."""
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(message_id)
    created_at = discord.utils.snowflake_time(int(message_id))
    shard_conn = store.get_shard(channel_id, created_at)

    try:
        cursor = shard_conn.execute(
            "UPDATE messages SET deleted_utc=? WHERE id=? AND deleted_utc IS NULL",
            (_now(), message_id),
        )
        if cursor.rowcount > 0:
            shard_conn.execute(
                "INSERT INTO events (message_id, event_type, observed_utc, detail) "
                "VALUES (?, 'delete', ?, NULL)",
                (message_id, _now()),
            )
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: PASS (14 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/live.py tests/test_live.py
git commit -m "feat: delete handling from explicit raw delete events only"
```

---

### Task 4: Startup catch-up — missed messages since the live checkpoint

**Files:**
- Modify: `archiver/live.py` (append)
- Modify: `tests/discord_fakes.py` (extend `FakeHistoryChannel` usage — no new class needed, it already supports `.history(after=...)`)
- Modify: `tests/test_live.py` (append)

**Interfaces:**
- Consumes: `apply_live_message` (Task 1), `FakeHistoryChannel`/`FakeClient` (Stage 3's `tests/discord_fakes.py`).
- Produces: `archiver.live.catch_up_missed_messages(client, catalog_conn: sqlite3.Connection, store: ShardStore) -> None` — async. For every `coverage.status='complete'` channel, fetches everything created after `COALESCE(live_checkpoint, backfill_checkpoint, '0')` and applies each via `apply_live_message`. A channel that's vanished, forbidden, or has no `.history()` (a forum/media parent) is silently skipped, matching Stage 3's established pattern.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live.py`:

```python
from archiver.live import catch_up_missed_messages
from tests.discord_fakes import FakeClient, FakeHistoryChannel


async def test_catch_up_missed_messages_fetches_after_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    missed = FakeMessage(id=101, channel_id=1, author_id=2, content="missed while offline", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[missed])})

    await catch_up_missed_messages(client, catalog, store)

    row = catalog.execute("SELECT live_checkpoint, message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "101"
    assert row["message_count"] == 1


async def test_catch_up_missed_messages_falls_back_to_backfill_checkpoint_when_live_checkpoint_unset(tmp_path):
    """The first-ever live startup after a channel finishes backfill has
    no live_checkpoint yet -- must resume from backfill_checkpoint, not
    from the beginning of the channel."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', backfill_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    missed = FakeMessage(id=101, channel_id=1, author_id=2, content="missed", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[missed])})

    await catch_up_missed_messages(client, catalog, store)

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 1


async def test_catch_up_missed_messages_skips_channel_with_no_history_method(tmp_path):
    """Regression: same class of bug as Stage 3's forum-channel crash --
    a 'complete' coverage row can belong to a forum/media parent, which
    has no .history() at all."""
    class _FakeForumChannel:
        def __init__(self, id):
            self.id = id

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({1: _FakeForumChannel(id=1)})

    await catch_up_missed_messages(client, catalog, store)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'catch_up_missed_messages'`.

- [ ] **Step 3: Append `catch_up_missed_messages` to `archiver/live.py`**

```python
async def catch_up_missed_messages(client, catalog_conn: sqlite3.Connection, store: ShardStore) -> None:
    """For every 'complete' channel, fetch anything created after the
    live checkpoint (falling back to the backfill checkpoint on the
    first-ever live startup) while the bot was offline (spec §6)."""
    rows = catalog_conn.execute(
        "SELECT channel_id, COALESCE(live_checkpoint, backfill_checkpoint, '0') AS checkpoint "
        "FROM coverage WHERE status='complete'"
    ).fetchall()
    for row in rows:
        channel_id = row["channel_id"]
        checkpoint = row["checkpoint"]
        try:
            discord_channel = await client.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden):
            continue
        if not hasattr(discord_channel, "history"):
            continue
        async for message in discord_channel.history(
            after=discord.Object(id=int(checkpoint)), oldest_first=True, limit=None,
        ):
            apply_live_message(store, catalog_conn, message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: PASS (17 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/live.py tests/test_live.py
git commit -m "feat: startup catch-up for messages missed while offline"
```

---

### Task 5: Startup catch-up — recent-window rescan for missed edits

**Files:**
- Modify: `archiver/live.py` (append)
- Modify: `tests/test_live.py` (append)

**Interfaces:**
- Consumes: `map_message`, `write_message` (Stage 3), `FakeHistoryChannel`/`FakeClient`.
- Produces: `archiver.live.rescan_recent_window(client, catalog_conn: sqlite3.Connection, store: ShardStore, days: int = 3) -> None` — async. Re-fetches the last `days` of history for every `'complete'` channel and re-applies each message via the idempotent `write_message` directly (not `apply_live_message` — this must never touch `live_checkpoint`, since it's a supplementary re-sync pass, not the primary forward-progress signal).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live.py`:

```python
from archiver.live import rescan_recent_window


async def test_rescan_recent_window_reapplies_recent_messages(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    edited_elsewhere = FakeMessage(id=100, channel_id=1, author_id=2, content="edited via rescan", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[edited_elsewhere])})

    await rescan_recent_window(client, catalog, store, days=3)

    shard = store.get_shard("1", CREATED)
    row = shard.execute("SELECT content FROM messages WHERE id='100'").fetchone()
    assert row["content"] == "edited via rescan"


async def test_rescan_recent_window_never_touches_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute(
        "UPDATE coverage SET status='complete', live_checkpoint='50' WHERE channel_id='1'"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="x", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[msg])})

    await rescan_recent_window(client, catalog, store, days=3)

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "50"  # unchanged despite message id 100 > 50


async def test_rescan_recent_window_skips_channel_with_no_history_method(tmp_path):
    class _FakeForumChannel:
        def __init__(self, id):
            self.id = id

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({1: _FakeForumChannel(id=1)})

    await rescan_recent_window(client, catalog, store, days=3)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'rescan_recent_window'`.

- [ ] **Step 3: Append `rescan_recent_window` to `archiver/live.py`**

```python
async def rescan_recent_window(client, catalog_conn: sqlite3.Connection, store: ShardStore,
                                 days: int = 3) -> None:
    """Re-fetch the last `days` of history for every 'complete' channel
    and re-apply it via the idempotent writer directly, to catch
    edits/reactions missed during a reconnect gap (spec §6). Safe to
    re-run -- every write is an upsert. Deliberately does NOT advance
    live_checkpoint; this is a supplementary re-sync, not the primary
    forward-progress signal."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = catalog_conn.execute("SELECT channel_id FROM coverage WHERE status='complete'").fetchall()
    for row in rows:
        channel_id = row["channel_id"]
        if not _is_tracked_channel(catalog_conn, channel_id):
            continue
        try:
            discord_channel = await client.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden):
            continue
        if not hasattr(discord_channel, "history"):
            continue
        async for message in discord_channel.history(after=cutoff, oldest_first=True, limit=None):
            mapped = map_message(message)
            shard_conn = store.get_shard(channel_id, message.created_at)
            write_message(shard_conn, mapped)
            shard_conn.commit()
```

Note: `FakeHistoryChannel.history()` (Stage 3) ignores its `after` value's exact meaning beyond filtering by `after.id` — since this task passes a `datetime` cutoff (not a `discord.Object`), check `tests/discord_fakes.py`'s actual `FakeHistoryChannel.history` signature before running the tests above; if it only accepts a `discord.Object`-like `after` with an `.id` attribute, adapt either the fake (append a small branch handling a `datetime` by treating it as "include everything", since the tests above don't depend on real date filtering — they only check that messages in the fake's list get re-applied) or construct the test's expectations to match whatever the fake actually supports. Read the fake first; do not guess.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: PASS (20 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/live.py tests/test_live.py
git commit -m "feat: recent-window rescan for edits/reactions missed offline"
```

---

### Task 6: `live` CLI command — persistent gateway wiring

**Files:**
- Modify: `archiver/cli.py` (append)
- Test: none (live-connecting async glue, same documented pattern as `doctor`/`coverage --preflight`/`backfill` — not unit tested; verified manually against the real bot, see Task 7)

**Interfaces:**
- Consumes: `apply_live_message`, `apply_raw_edit`, `apply_raw_delete`, `catch_up_missed_messages`, `rescan_recent_window` (Tasks 1-5); `discover_guild` (Stage 2); `backfill_all_pending` (Stage 3).
- Produces: `archiver.cli._run_live(config) -> int` and a `live` subcommand on `main()`. Unlike every other command in this project, `_run_live` **never calls `client.close()`** in normal operation — it is the actual long-running background process, and is expected to run until externally stopped (Task Scheduler, Ctrl+C, or process kill — Stage 8's job to wire up supervision).

- [ ] **Step 1: Append to `archiver/cli.py`**

Add these imports at the top (alongside the existing ones):
```python
from archiver.live import apply_live_message, apply_raw_delete, apply_raw_edit, catch_up_missed_messages, rescan_recent_window
```

Append:

```python
async def _run_live(config) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    store = ShardStore(config.data_dir, catalog_conn)
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        guild = client.get_guild(int(config.guild_id))
        if guild is None:
            print(f"live capture failed: configured guild id {config.guild_id} not found")
            await client.close()
            return
        await discover_guild(guild, catalog_conn)
        await catch_up_missed_messages(client, catalog_conn, store)
        await rescan_recent_window(client, catalog_conn, store)
        asyncio.create_task(backfill_all_pending(client, catalog_conn, store))
        print(f"Live capture running as {client.user}.")

    @client.event
    async def on_message(message):
        apply_live_message(store, catalog_conn, message)

    @client.event
    async def on_raw_message_edit(payload):
        apply_raw_edit(store, catalog_conn, str(payload.channel_id), payload.data)

    @client.event
    async def on_raw_message_delete(payload):
        apply_raw_delete(store, catalog_conn, str(payload.channel_id), payload.message_id)

    token = load_token(HERE / ".env")
    try:
        await client.start(token)  # runs until externally stopped
    except Exception as e:
        print(f"live capture failed to connect: {e}")
        return 1
    return 0
```

Modify `main()`'s subparser setup to add, alongside `doctor`/`coverage`/`backfill`:
```python
    subparsers.add_parser("live")
```

And add a dispatch branch alongside the existing ones:
```python
    if args.command == "live":
        return asyncio.run(_run_live(config))
```

- [ ] **Step 2: Verify argparse wiring**

Run: `python -m archiver.cli live --bogus-flag`
Expected: argparse rejects `--bogus-flag` specifically (not `live` itself as an unrecognized subcommand) — confirms the subcommand exists.

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: all prior tests plus Tasks 1-5's still pass; nothing new fails (Task 6 adds no automated tests of its own).

- [ ] **Step 4: Commit**

```bash
git add archiver/cli.py
git commit -m "feat: add live CLI command (persistent gateway capture)"
```

---

### Task 7: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-6 passes, plus all prior stages' tests still pass (no regressions).

- [ ] **Step 2: Note what's NOT covered by the automated suite, and the live-run plan**

`_run_live` is not unit-tested by design (documented pattern, same as `doctor`/`coverage --preflight`/`backfill`). Before Stage 4 is considered actually done: run `python -m archiver.cli live` against the real bot in the background, post a test message in a tracked channel and confirm it appears in the correct shard within seconds, edit that message and confirm only the edited field changed (not a full-row overwrite), delete it and confirm `deleted_utc` gets set, then stop the process and restart it to confirm the startup catch-up picks up anything sent while it was down. This is real, hands-on live verification — the kind that already caught two production bugs in Stage 3 (a `Poll.question` API mismatch and a `ForumChannel.history()` gap) that no amount of fake-based unit testing surfaced on its own.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

If clean, no commit needed — Task 7 is verification only.

---

## Self-Review Notes

- **Spec coverage:** §6 partial-edit-never-nulls-absent-fields — Task 2, with a dedicated test. §6 delete-only-from-explicit-event, absence-not-evidence — Task 3, with a dedicated test proving no event is fabricated for a never-archived message. §6 checkpoint-only-advances-on-creation, never-on-edit/delete — Tasks 1-3's tests explicitly prove both directions (creation advances it, edit/delete don't touch it). §6 startup catch-up sequence (rediscover, missed-messages, recent-window, resume pending backfill) — Task 6 wires all four in `on_ready`, in that order, matching the spec's stated sequence. §2 forward-only checkpoint semantics with correct integer comparison — Task 1, directly addressing the recommendation Stage 2's final review flagged but didn't need yet.
- **Placeholder scan:** Task 2's Step 1 originally included an empty stub test (`test_apply_raw_edit_does_not_null_out_absent_fields` with no body) — the step's own text explicitly flags this and instructs removing it before running, since its assertion is already covered by the adjacent real test. This is the one placeholder-shaped thing in this plan, and it is explicitly called out and resolved within the same task, not left for the implementer to discover. No other placeholders found.
- **Type consistency:** `apply_live_message`/`apply_raw_edit`/`apply_raw_delete` all take `(store: ShardStore, catalog_conn: sqlite3.Connection, ...)` in the same first-two-argument order. `catch_up_missed_messages`/`rescan_recent_window` both take `(client, catalog_conn, store, ...)` matching the CLI's own call shape from Stage 3's `backfill_all_pending`. Every function that touches `coverage` checkpoints uses the exact column names Stage 1 defined (`live_checkpoint`, `backfill_checkpoint`, `message_count`) and the exact `CAST(... AS INTEGER)` comparison pattern established here for the first time but consistent with Stage 2's own flagged recommendation.

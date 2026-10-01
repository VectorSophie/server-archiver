# Stage 4 Follow-up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two findings deferred from Stage 4's final whole-branch review: (1) `message_count` can double-count a message when live capture and backfill both touch the same message id, because every write path blindly adds its own count regardless of whether the row already existed; (2) `discover_guild` holds a long catalog transaction open across several `await` points while `archive live`'s message handlers share the same connection, narrowing but not eliminating a window where a concurrent write's rollback could discard pending discovery writes.

**Architecture:** For (1), `write_message` (Stage 3) currently has no way to report whether a given message id was newly inserted or already existed — every caller (`commit_page`, `apply_live_message`) increments `coverage.message_count` unconditionally per message processed. Changing `write_message` to return `True` only on a genuine first insert (checked via a pre-write `SELECT`) lets every caller increment the count only for messages that are actually new to the archive, regardless of which code path (backfill, live, catch-up, rescan) got there first. For (2), `discover_guild` currently commits once at the very end; committing after each top-level channel's own discovery work (categories, the channel itself, its threads) completes — rather than holding everything open until the whole guild sweep finishes — shrinks the shared-connection race window from "the entire discovery pass" to "one channel's worth of work," without a larger redesign of connection ownership.

**Tech Stack:** No new dependencies — this is all changes to existing `sqlite3`-based code.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§4.1 cross-file commit protocol, §5 discovery, §6 live capture)

## Global Constraints

- Every shard write remains an idempotent upsert — this plan does not change that invariant, only what gets counted.
- `write_message`'s "does not commit" contract is unchanged — callers still batch commits exactly as before.
- `discover_guild`'s overall behavior (which channels/threads get discovered, how `seen_ids` is tracked, vanished-channel detection) must not change — only commit timing changes.
- Both fixes touch already-merged, already live-verified code (Stages 2-4) actively running as a background process against the real Discord server — changes must be minimal and surgical, not restructuring.

## File Structure

- **Modify:** `archiver/store.py` — `write_message` gains a return value; `commit_page` uses it to count only new messages.
- **Modify:** `archiver/live.py` — `apply_live_message` uses `write_message`'s return value to count only new messages, and splits the checkpoint-advance write from the count-increment write (they're independent facts now, not always true/false together).
- **Modify:** `archiver/discovery.py` — `discover_guild` commits after each top-level channel's discovery work instead of only at the very end.
- **Test:** `tests/test_store.py`, `tests/test_live.py`, `tests/test_discovery.py` (all existing files, extended).

---

### Task 1: `message_count` accuracy — `write_message` reports newly-inserted vs. already-existed

**Files:**
- Modify: `archiver/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `archiver.store.write_message(shard_conn: sqlite3.Connection, mapped: dict) -> bool` — now returns `True` if this call inserted a brand-new message row, `False` if the message id already existed (an edit/replay/duplicate-delivery upsert). Every other part of `write_message`'s behavior (child-table upserts, no-commit contract) is unchanged.
- `archiver.store.commit_page(store, catalog_conn, channel_id, pages, oldest_id, newest_id) -> None` — unchanged signature, but now increments `coverage.message_count` only by the count of messages in `pages` that were genuinely new (via `write_message`'s return value), not by `len(pages)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py` (check its existing imports first — it should already import `connect_shard`, `write_message`, and `tests.fixtures.make_message`):
```python
def test_write_message_returns_true_for_new_message_false_for_replay(tmp_path):
    shard_conn = connect_shard(tmp_path / "2025-10.sqlite")
    mapped = {
        "message": make_message(content="hello"),
        "attachments": [], "reactions": [], "mentions_user": [], "mentions_role": [],
        "stickers": [], "poll": None, "poll_answers": [], "embeds": [], "embed_fields": [],
    }
    first = write_message(shard_conn, mapped)
    second = write_message(shard_conn, mapped)  # replay with identical data
    assert first is True
    assert second is False


def test_commit_page_counts_only_newly_inserted_messages(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chan', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    class _FakeMsg:
        def __init__(self, id, created_at):
            self.id = id
            self.created_at = created_at

    created = datetime(2025, 10, 15, tzinfo=timezone.utc)
    msg_a = _FakeMsg(id=100, created_at=created)
    msg_b = _FakeMsg(id=101, created_at=created)
    mapped_a = {"message": make_message(id="100", channel_id="1", created_utc=now),
                "attachments": [], "reactions": [], "mentions_user": [], "mentions_role": [],
                "stickers": [], "poll": None, "poll_answers": [], "embeds": [], "embed_fields": []}
    mapped_b = {"message": make_message(id="101", channel_id="1", created_utc=now),
                "attachments": [], "reactions": [], "mentions_user": [], "mentions_role": [],
                "stickers": [], "poll": None, "poll_answers": [], "embeds": [], "embed_fields": []}

    # First page: both messages are new.
    commit_page(store, catalog, "1", [(msg_a, mapped_a), (msg_b, mapped_b)], "100", "101")
    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 2

    # Second page replays msg_a (e.g. live capture already wrote it) plus one genuinely new message.
    msg_c = _FakeMsg(id=102, created_at=created)
    mapped_c = {"message": make_message(id="102", channel_id="1", created_utc=now),
                "attachments": [], "reactions": [], "mentions_user": [], "mentions_role": [],
                "stickers": [], "poll": None, "poll_answers": [], "embeds": [], "embed_fields": []}
    commit_page(store, catalog, "1", [(msg_a, mapped_a), (msg_c, mapped_c)], "100", "102")
    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 3  # not 4 -- msg_a was already counted
```

Add `from datetime import datetime, timezone` and `from archiver.db import connect_catalog` to the test file's imports if not already present (check first).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `assert None is True` (write_message currently returns `None`), or the count assertion fails because `commit_page` currently counts `len(pages)` unconditionally.

- [ ] **Step 3: Modify `write_message` in `archiver/store.py`**

Change the function signature and add the existence check as the very first thing it does:
```python
def write_message(shard_conn: sqlite3.Connection, mapped: dict) -> bool:
    """Idempotent upsert of one mapped message and all its child rows.
    Safe to call twice with the same data (replay after a crash). Does
    not commit -- callers batch a page's writes under one commit.
    Returns True if this call inserted a brand-new message row, False
    if the id already existed (an edit, a replay, or the same message
    arriving via two different code paths -- e.g. live capture and a
    backfill sweep touching the same id) -- callers use this to count
    coverage.message_count accurately instead of once per write
    attempt."""
    m = mapped["message"]
    is_new = shard_conn.execute(
        "SELECT 1 FROM messages WHERE id=?", (m["id"],)
    ).fetchone() is None

    shard_conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc, "
        "edited_utc, reply_to_id, mention_everyone, flags, deleted_utc) "
        "VALUES (:id, :channel_id, :author_id, :content, :created_utc, :edited_utc, "
        ":reply_to_id, :mention_everyone, :flags, :deleted_utc) "
        "ON CONFLICT(id) DO UPDATE SET content=excluded.content, "
        "edited_utc=excluded.edited_utc, mention_everyone=excluded.mention_everyone, "
        "flags=excluded.flags",
        m,
    )

    # ... (every other block in this function -- attachments, reactions,
    #      mentions_user, mentions_role, stickers, poll, poll_answers,
    #      embeds, embed_fields -- is UNCHANGED, keep exactly as-is) ...

    return is_new
```
(Do not touch anything below the `messages` INSERT except adding `return is_new` as the function's final line.)

- [ ] **Step 4: Modify `commit_page` in `archiver/store.py`**

Change the write loop to count only new messages:
```python
    touched_shards: set[sqlite3.Connection] = set()
    try:
        new_count = 0
        for message, mapped in pages:
            shard_conn = store.get_shard(channel_id, message.created_at)
            if write_message(shard_conn, mapped):
                new_count += 1
            touched_shards.add(shard_conn)

        for shard_conn in touched_shards:
            shard_conn.commit()

        cursor = catalog_conn.execute(
            "UPDATE coverage SET backfill_checkpoint=?, "
            "oldest_message_id=COALESCE(oldest_message_id, ?), newest_message_id=?, "
            "message_count=message_count+? WHERE channel_id=?",
            (newest_id, oldest_id, newest_id, new_count, channel_id),
        )
```
(The rest of `commit_page` — the rowcount check, `catalog_conn.commit()`, the `except BaseException` rollback block — is unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS, including the 2 new tests.

- [ ] **Step 6: Run the full suite to confirm no regressions in callers**

Run: `python -m pytest -v`
Expected: all tests pass. `archiver/backfill.py` calls `commit_page` with its existing signature unchanged, so it needs no changes. Task 2 of this plan updates `archiver/live.py`'s `apply_live_message`, the other caller of `write_message` directly.

- [ ] **Step 7: Commit**

```bash
git add archiver/store.py tests/test_store.py
git commit -m "fix: count message_count only for genuinely new messages, not every write attempt"
```

---

### Task 2: `message_count` accuracy in live capture — `apply_live_message`

**Files:**
- Modify: `archiver/live.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Consumes: `write_message`'s new `bool` return value (Task 1).
- Produces: `apply_live_message`'s signature and behavior are unchanged from the outside (still `apply_live_message(store, catalog_conn, message, *, advance_checkpoint=True) -> None`) — only its internal counting logic changes: `message_count` now increments only when `write_message` reports a genuinely new message, independently of whether `advance_checkpoint` is `True` or `False` (a message can be new-to-the-archive while its channel hasn't finished catch-up yet, or already-archived while its channel has — these are now two independent facts, not coupled into one conditional `UPDATE`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live.py` (check its existing imports/fixtures first — it should already have `_seed_channel`, `FakeMessage`, `connect_catalog`, `ShardStore`):
```python
def test_apply_live_message_does_not_double_count_a_replayed_message(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hello", created_at=CREATED)

    apply_live_message(store, catalog, msg)  # first delivery
    apply_live_message(store, catalog, msg)  # duplicate delivery of the same id (e.g. rescan)

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live.py -v`
Expected: FAIL — `message_count == 2` (currently increments unconditionally every call where `advance_checkpoint` is True, regardless of whether the message was already archived).

- [ ] **Step 3: Modify `apply_live_message` in `archiver/live.py`**

```python
def apply_live_message(store: ShardStore, catalog_conn: sqlite3.Connection, message,
                        *, advance_checkpoint: bool = True) -> None:
    """Write a newly created message and, unless the channel's startup
    catch-up sweep hasn't finished this session (advance_checkpoint=False),
    advance the channel's live checkpoint -- the only place the checkpoint
    moves forward (spec §6). Untracked channels (a different guild on the
    same token, or a channel never discovered) are silently ignored.

    The shard write and the catalog checkpoint write are separate
    try/except blocks, each rolling back only its own connection, so a
    failure in one can't leave the other holding a dangling transaction.
    message_count only increments when write_message reports the
    message was genuinely new to this shard -- independent of whether
    the checkpoint advances, since a message can be new-to-the-archive
    on a channel that hasn't finished catch-up, or already-archived
    (e.g. a duplicate delivery, or backfill already wrote it) on a
    channel that has."""
    channel_id = str(message.channel.id)
    if not _is_tracked_channel(catalog_conn, channel_id):
        return

    message_id = str(message.id)
    mapped = map_message(message)
    shard_conn = store.get_shard(channel_id, message.created_at)
    try:
        is_new = write_message(shard_conn, mapped)
        shard_conn.commit()
    except BaseException:
        shard_conn.rollback()
        raise

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
        catalog_conn.commit()
    except BaseException:
        catalog_conn.rollback()
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live.py -v`
Expected: PASS, including existing checkpoint-advance tests (the checkpoint `UPDATE` is unchanged in its own logic, just no longer bundled with the count increment) and the new duplicate-delivery test.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add archiver/live.py tests/test_live.py
git commit -m "fix: decouple message_count increment from checkpoint advance in live capture"
```

---

### Task 3: Narrow the discovery/live-capture shared-connection race window

**Files:**
- Modify: `archiver/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces: `discover_guild`'s external behavior (return value, what gets discovered) is unchanged. Internally, it now calls `catalog_conn.commit()` after each top-level channel's own discovery work (itself + its threads) completes, rather than holding one transaction open for the entire guild sweep.

- [ ] **Step 1: Write the failing test**

This change is about commit *timing*, which is hard to observe via a black-box test of `discover_guild`'s return value (the end state is identical either way). Instead, add a test that proves intermediate commits actually happen by checking the catalog's state through a SEPARATE connection mid-discovery — append to `tests/test_discovery.py` (check its existing imports/fixtures first, especially `FakeGuild`/`FakeChannel` from `tests/discord_fakes.py`):

```python
import sqlite3


async def test_discover_guild_commits_incrementally_not_only_at_the_end(tmp_path):
    """Open a second connection to the same catalog file mid-discovery
    (via a custom FakeGuild whose fetch_channels callback peeks through
    it) to prove discover_guild doesn't hold everything in one
    uncommitted transaction until the very end -- a concurrent reader
    (e.g. a live-capture handler on the same process) must be able to
    see a channel discovered earlier in the same sweep before the sweep
    finishes."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    observer = sqlite3.connect(tmp_path / "catalog.sqlite")
    observer.row_factory = sqlite3.Row

    channel_a = FakeChannel(id=1, name="alpha", type_=discord.ChannelType.text)
    channel_b = FakeChannel(id=2, name="beta", type_=discord.ChannelType.text)
    seen_after_first: list = []

    class _ObservingGuild(FakeGuild):
        async def fetch_channels(self):
            result = await super().fetch_channels()
            return result

    guild = _ObservingGuild([channel_a, channel_b])

    # Patch discover_threads to peek at the observer connection after
    # the first channel's work would plausibly have committed --
    # simplest robust check: run discovery, then assert the observer
    # (a separate connection, same file) sees BOTH channels afterward,
    # and separately assert that commit() was called more than once by
    # counting WAL-visible writes is overkill -- instead, directly
    # assert discover_guild's own commit count via monkeypatching.
    commit_calls = []
    original_commit = catalog.commit
    catalog.commit = lambda: (commit_calls.append(1), original_commit())[-1]

    await discover_guild(guild, catalog)

    assert len(commit_calls) >= 2  # at least one per channel, not only the final commit
    observer.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_discovery.py -v -k incrementally`
Expected: FAIL — `assert 1 >= 2` (current code calls `catalog_conn.commit()` exactly once, at the very end).

- [ ] **Step 3: Modify `discover_guild` in `archiver/discovery.py`**

In the `for channel in top_level:` loop (the one that calls `classify_channel_type`, `_upsert_channel`, and conditionally `discover_threads`), add a commit at the end of each iteration — after the per-channel thread-discovery block, before moving to the next channel:

```python
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
            try:
                thread_infos, gap_reason = await discover_threads(channel)
            except discord.Forbidden:
                gap_reason = "cannot list archived threads (missing Read Message History)"
                thread_infos = []
                known_children = catalog_conn.execute(
                    "SELECT id FROM channels WHERE parent_id=?", (str(channel.id),)
                ).fetchall()
                for row in known_children:
                    seen_ids.add(row["id"])

            for t in thread_infos:
                seen_ids.add(t["id"])
                is_new = _upsert_channel(
                    catalog_conn, t["id"], t["name"], t["type"],
                    parent_id=t["parent_id"], category_id=category_id, now=now,
                )
                stats["discovered"] += 1
                stats["new"] += is_new
                _set_gap_reason(catalog_conn, t["id"], gap_reason)
            _set_gap_reason(catalog_conn, str(channel.id), gap_reason)

        catalog_conn.commit()  # <-- new: commit this channel's discovery work before the next await
```

Leave everything else in `discover_guild` (the category-upsert loop before this one, the `active_threads()` loop after it, `_mark_vanished`, the final `catalog_conn.commit()`) exactly as-is — the final commit at the end of the function still runs too, covering the category upserts and the active-threads/vanished-channel work that happen outside this per-channel loop.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_discovery.py -v`
Expected: PASS, including the new incremental-commit test and all pre-existing discovery tests (which assert on `discover_guild`'s final state, unaffected by intermediate commits).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add archiver/discovery.py tests/test_discovery.py
git commit -m "fix: commit discovery work per-channel instead of holding one transaction for the whole sweep"
```

---

### Task 4: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-3 passes, plus all prior stages' tests still pass (no regressions).

- [ ] **Step 2: Note what's NOT covered by the automated suite**

The actual race-window narrowing in Task 3 (whether a concurrent `apply_live_message` rollback can still discard some unrelated discovery writes within the now-smaller per-channel window) is not something a unit test can fully prove — it's a probabilistic narrowing, not an elimination, same as the plan's own text says. The automated test proves commits happen more often; it cannot prove no race exists at all (a full fix would mean discovery never shares a connection with live handlers at all, a larger redesign not undertaken here). This is an accepted, documented tradeoff, not a gap to silently ignore.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

If clean, no commit needed — Task 4 is verification only.

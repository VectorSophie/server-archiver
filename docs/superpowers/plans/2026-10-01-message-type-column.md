# Message Type Column Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture Discord's own `message.type` (e.g. pin notifications, member joins, boost announcements — distinct from normal chat messages) per message, and exclude non-default message types from report word/poster rankings (they're noise, not conversation), without excluding them from the archive or from total message counts.

**Architecture:** `discord.MessageType.default` (value `0`) is a normal chat message; every other value is a system-generated notification. Add a `message_type INTEGER NOT NULL DEFAULT 0` column to the shard schema (migration v3), capture `message.type.value` in `map_message`, store it in `write_message`'s existing upsert, and filter `message_type != 0` rows out of `gather_scope_stats`'s word/poster ranking Counters (same pattern already used for `excluded_author_ids` — total_messages still counts them).

**Tech Stack:** No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§8 Reports — "Bot/system messages are excludable... without being excluded from the archive itself")

## Global Constraints

- Pre-existing archived messages (written before this migration) get `message_type=0` by the column's `DEFAULT 0` — this is a reasonable assumption (most messages are normal chat), not a claim of certainty; this project's stated principle is that historical data from before a given capability existed can't be retroactively reconstructed with certainty, and this default is the least-surprising choice, not a correctness guarantee.
- `message_type` exclusion from rankings must behave exactly like `excluded_author_ids` already does: never touch `total_messages`, only the ranking Counters.
- `write_message`'s existing "does not commit" contract and upsert pattern are unchanged.

## File Structure

- **Modify:** `archiver/db.py` — `SHARD_SCHEMA_V3` (adds `message_type` column).
- **Modify:** `archiver/discord_message.py` — `map_message` captures `message.type.value`.
- **Modify:** `archiver/store.py` — `write_message`'s INSERT includes `message_type`.
- **Modify:** `archiver/reports.py` — `gather_scope_stats` excludes non-default message types from word/poster rankings.
- **Test:** `tests/test_shard_schema.py`, `tests/test_discord_message.py`, `tests/test_store.py`, `tests/test_reports.py` (all existing, extended).

---

### Task 1: Schema, mapping, and storage

**Files:**
- Modify: `archiver/db.py` (append `SHARD_SCHEMA_V3`)
- Modify: `archiver/discord_message.py`
- Modify: `archiver/store.py`
- Test: `tests/test_shard_schema.py`, `tests/test_discord_message.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `messages.message_type INTEGER NOT NULL DEFAULT 0` column. `map_message(message)`'s returned `"message"` dict gains a `"message_type"` key (`message.type.value`, an int). `write_message`'s INSERT includes it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shard_schema.py`:
```python
def test_messages_table_has_message_type_column_defaulting_to_zero(tmp_path):
    conn = connect_shard(tmp_path / "shard.sqlite")
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc, "
        "mention_everyone, flags) VALUES ('1', '1', '1', '', '2025-10-15T00:00:00Z', 0, 0)"
    )
    row = conn.execute("SELECT message_type FROM messages WHERE id='1'").fetchone()
    assert row["message_type"] == 0
```

Append to `tests/test_discord_message.py` (check its existing fake-message fixture style first):
```python
def test_map_message_captures_message_type():
    class _FakeType:
        value = 7  # discord.MessageType.user_premium_guild_subscription, as an example non-default type

    msg = make_fake_message(type_=_FakeType())  # adapt to this file's actual fake-construction helper/pattern
    mapped = map_message(msg)
    assert mapped["message"]["message_type"] == 7


def test_map_message_default_type_is_zero():
    class _FakeType:
        value = 0

    msg = make_fake_message(type_=_FakeType())
    mapped = map_message(msg)
    assert mapped["message"]["message_type"] == 0
```
(Adapt the fake-message construction to whatever this test file's actual existing pattern is — read the file first. If the project's shared `FakeMessage` in `tests/discord_fakes.py` doesn't currently expose a `.type` attribute, extend it minimally with a `type_` constructor parameter defaulting to a fake type object with `.value = 0`, following the established pattern of incrementally extending shared fakes.)

Append to `tests/test_store.py`:
```python
def test_write_message_stores_message_type(tmp_path):
    shard_conn = connect_shard(tmp_path / "2025-10.sqlite")
    mapped = {
        "message": make_message(content="hello", message_type=7),
        "attachments": [], "reactions": [], "mentions_user": [], "mentions_role": [],
        "stickers": [], "poll": None, "poll_answers": [], "embeds": [], "embed_fields": [],
    }
    write_message(shard_conn, mapped)
    row = shard_conn.execute("SELECT message_type FROM messages WHERE id=?", (mapped["message"]["id"],)).fetchone()
    assert row["message_type"] == 7
```
(`tests/fixtures.py`'s `make_message` needs a `message_type` key in its default dict — add it there too, defaulting to `0`, as part of this step; check the file first.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shard_schema.py tests/test_discord_message.py tests/test_store.py -v`
Expected: FAIL — column doesn't exist, `map_message` doesn't set `message_type`, `write_message` doesn't insert it.

- [ ] **Step 3: Append `SHARD_SCHEMA_V3` to `archiver/db.py`**

```python
SHARD_SCHEMA_V3 = """
ALTER TABLE messages ADD COLUMN message_type INTEGER NOT NULL DEFAULT 0;
"""

SHARD_MIGRATIONS: list[tuple[int, str]] = [
    (1, SHARD_SCHEMA_V1), (2, SHARD_SCHEMA_V2), (3, SHARD_SCHEMA_V3),
]
```
(Replace the existing `SHARD_MIGRATIONS` line.)

- [ ] **Step 4: Update `tests/fixtures.py`'s `make_message`**

Add `"message_type": 0` to its default dict (check the file first — it's a small dict literal with an `.update(overrides)` call, add the new key alongside the existing ones).

- [ ] **Step 5: Update `map_message` in `archiver/discord_message.py`**

Add one key to the `row` dict:
```python
    row = {
        "id": str(message.id),
        "channel_id": str(message.channel.id),
        "author_id": str(message.author.id),
        "content": message.content or "",
        "created_utc": _iso(message.created_at),
        "edited_utc": _iso(message.edited_at) if message.edited_at else None,
        "reply_to_id": reply_to_id,
        "mention_everyone": int(message.mention_everyone),
        "flags": message.flags.value,
        "message_type": message.type.value,
        "deleted_utc": None,
    }
```

- [ ] **Step 6: Update `write_message`'s INSERT in `archiver/store.py`**

```python
    shard_conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, content, created_utc, "
        "edited_utc, reply_to_id, mention_everyone, flags, message_type, deleted_utc) "
        "VALUES (:id, :channel_id, :author_id, :content, :created_utc, :edited_utc, "
        ":reply_to_id, :mention_everyone, :flags, :message_type, :deleted_utc) "
        "ON CONFLICT(id) DO UPDATE SET content=excluded.content, "
        "edited_utc=excluded.edited_utc, mention_everyone=excluded.mention_everyone, "
        "flags=excluded.flags",
        m,
    )
```
(Only the column list and VALUES placeholders change — the `ON CONFLICT DO UPDATE SET` clause is unchanged, since `message_type` can't change after creation, same reasoning as `id`/`channel_id`/`author_id`/`reply_to_id`/`created_utc` already not being in that SET clause.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_shard_schema.py tests/test_discord_message.py tests/test_store.py -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: all pass. Every existing caller of `map_message`/`write_message`/`make_message` picks up `message_type` automatically via the dict-based interface (no other call site needs editing) — confirm this is actually true by checking there's no OTHER place in the codebase that constructs a message dict by hand without going through `map_message`/`make_message` (if one exists, it needs the same key added).

- [ ] **Step 9: Commit**

```bash
git add archiver/db.py archiver/discord_message.py archiver/store.py tests/fixtures.py tests/test_shard_schema.py tests/test_discord_message.py tests/test_store.py tests/discord_fakes.py
git commit -m "feat: capture Discord's message type per message (pins, joins, boosts vs. normal chat)"
```

---

### Task 2: Exclude non-default message types from report rankings

**Files:**
- Modify: `archiver/reports.py`
- Test: `tests/test_reports.py`

**Interfaces:**
- Produces: `gather_scope_stats` excludes any message with `message_type != 0` from `top_authors`/`top_words` Counters (not from `total_messages` or `attachment_counts` — a system message can't have attachments anyway, but the exclusion is specifically scoped to the two ranking Counters that are about "what people said").

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reports.py`:
```python
def test_gather_scope_stats_excludes_system_messages_from_rankings_not_from_total(tmp_path):
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

    pin_notice = make_message(content="", author_id="alice", channel_id="1", message_type=6)  # channel_pinned_message
    real_msg = make_message(content="hello everyone", author_id="alice", channel_id="1", message_type=0)
    _write_shard_with_messages(shard_path, [pin_notice, real_msg])

    stats = gather_scope_stats(catalog, tmp_path, ["1"])
    assert stats.total_messages == 2  # both counted in the archive total
    assert dict(stats.top_authors)["alice"] == 1  # only the real message counted toward ranking
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_reports.py -v -k system_messages`
Expected: FAIL — `dict(stats.top_authors)["alice"] == 2` currently (both messages counted).

- [ ] **Step 3: Modify `gather_scope_stats` in `archiver/reports.py`**

Update the SELECT to include `message_type`, and gate the ranking updates on it:
```python
                    for row in shard_conn.execute(
                        "SELECT author_id, content, message_type FROM messages "
                        "WHERE channel_id=? AND deleted_utc IS NULL",
                        (channel_id,),
                    ).fetchall():
                        ch_total += 1
                        if row["message_type"] == 0 and row["author_id"] not in excluded_author_ids:
                            ch_authors[row["author_id"]] += 1
                            content_no_urls = _URL_RE.sub("", row["content"].lower())
                            for word in _WORD_RE.findall(content_no_urls):
                                if word not in _STOPWORDS:
                                    ch_words[word] += 1
```
(Only the `SELECT` columns and the `if` condition change — everything else in the loop body is unchanged.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_reports.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 6: Commit**

```bash
git add archiver/reports.py tests/test_reports.py
git commit -m "feat: exclude non-chat message types (pins, joins, boosts) from report rankings"
```

---

### Task 3: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test passes, no regressions from either task.

- [ ] **Step 2: Note what's NOT covered by the automated suite**

The real archive's ~341k already-archived messages will all get `message_type=0` by the migration's default — this is an assumption, not a verified fact (a handful of them are likely genuine pin/join notices that predate this column). No automated test can verify this against real historical data; it's a one-time, accepted imprecision consistent with this project's stated "can't reconstruct history from before a capability existed" principle. Going forward (any message archived after this migration lands), the type is captured accurately from the live Discord API data.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

# Stage 3: Historical Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull every extant message from every channel/thread the catalog knows about (`coverage.status` in `pending`/`crawling`/`failed`), oldest-first, into the correct monthly shard, with crash-safe checkpointing (spec §4.1) and per-channel failure isolation so one inaccessible channel never aborts the whole crawl.

**Architecture:** `archiver/discord_message.py` is a pure mapper: a discord.py `Message` (or a hand-built fake shaped like one) in, a dict of row-shaped data matching Stage 1's shard schema out — no I/O, no discord.py Client. `archiver/store.py` owns shard-file resolution (Asia/Seoul month bucketing, category-name-to-folder mapping with an `Uncategorized` sentinel, connection caching) and idempotent row writes, plus the cross-file commit protocol from spec §4.1: write every affected shard first, commit each, and only then advance the channel's catalog checkpoint in one transaction — so a crash between a shard commit and the checkpoint commit just means the next run replays that page, which is always safe because every write is `ON CONFLICT DO UPDATE`/`DO NOTHING`. `archiver/backfill.py` drives the oldest-first crawl per channel and the bounded-concurrency sweep across every pending channel. `archiver/cli.py` gains a `backfill` command.

**Tech Stack:** `discord.py` (already a dependency), stdlib `asyncio`/`sqlite3`/`re`/`zoneinfo`. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (see §4.1 cross-file commit protocol, §5 backfill — corrected 2026-09-30 to use `after=` not `before=` as the resume cursor, §11 Testing, §15 Stage 3 scope)

## Global Constraints

- Backfill resumes via `history(limit=100, after=discord.Object(id=int(checkpoint)), oldest_first=True)` — **never** `before=` as a resume cursor (spec §5, corrected). `coverage.backfill_checkpoint` holds the newest message ID successfully archived so far (a forward-moving high-water mark), not an "oldest reached" pointer.
- Cross-file commit order (spec §4.1): write and commit every affected shard **before** touching the catalog's checkpoint; the catalog checkpoint update is the last thing that happens for a page, in its own transaction. A crash before that point means the page is safely replayed (every write is idempotent).
- A full backfill page (up to 100 messages) is always materialized into a plain list **before** any database write begins, and no `await` happens between the first write of a page and that page's shard/catalog commits. This is what keeps concurrent channels' database writes from interleaving under `asyncio.gather` — preserve this ordering in every task that touches the write path.
- Each channel/thread's backfill is wrapped in its own try/except: a `discord.Forbidden` sets `status='inaccessible'`; any other `discord.HTTPException` sets `status='failed'`; either way the error text goes to `gap_reason` and the sweep moves to the next channel (spec §5, discrawl #27/#30).
- Discord IDs are `TEXT` everywhere, matching Stage 1/2.
- Category folder names are sanitized for Windows filesystem safety; a channel with no category uses the fixed sentinel category id `"uncategorized"` / folder name `"Uncategorized"` (closing the gap flagged in Stage 1's final review).
- No custom rate limiting — discord.py's own handling is trusted (ponytail: already-installed dependency).
- Test doubles for discord.py objects are hand-built (`tests/discord_fakes.py`, extended), never `unittest.mock.MagicMock`. Real `discord.Embed` and `discord.Object` instances are used directly in tests where their constructors don't require a live connection (confirmed: both work standalone).

---

## File Structure

- `archiver/discord_message.py` — `map_message(message) -> dict`.
- `archiver/store.py` — `month_bucket`, `sanitize_folder_name`, `ShardStore` (shard resolution + connection cache), `write_message`, `commit_page`.
- `archiver/backfill.py` — `backfill_channel`, `backfill_all_pending`.
- `archiver/cli.py` — append `backfill` command.
- `tests/discord_fakes.py` — extend with `FakeMessage`, `FakeAttachment`, `FakeReaction`, `FakeUserRef`, `FakeSticker`, `FakeMessageReference`, `FakePollMedia`, `FakePollAnswer`, `FakePoll`.
- `tests/test_discord_message.py`, `tests/test_store.py`, `tests/test_backfill.py`, `tests/test_cli.py` (append).

---

### Task 1: Message core fields + attachments mapping

**Files:**
- Create: `archiver/discord_message.py`
- Modify: `tests/discord_fakes.py` (append `FakeMessage`, `FakeAttachment`, `FakeUserRef`, `FakeChannelRef`, `FakeMessageFlags`, `FakeMessageReference`)
- Test: `tests/test_discord_message.py`

**Interfaces:**
- Produces: `archiver.discord_message.map_message(message) -> dict` — returns `{"message": {...}, "attachments": [...], "reactions": [], "mentions_user": [], "mentions_role": [], "stickers": [], "poll": None, "poll_answers": [], "embeds": [], "embed_fields": []}`. This task only populates `message` and `attachments`; the other keys are always present but empty/`None` until Task 2 fills them in for real inputs — every key must exist in the returned dict from this task onward so Task 2 doesn't need to change the dict's shape, only what's inside those already-present empty containers.

- [ ] **Step 1: Write the failing test**

```python
# tests/discord_fakes.py additions (append to the existing file)
class FakeChannelRef:
    def __init__(self, id):
        self.id = id


class FakeUserRef:
    def __init__(self, id):
        self.id = id


class FakeMessageFlags:
    def __init__(self, value=0):
        self.value = value


class FakeMessageReference:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeAttachment:
    def __init__(self, id, filename, content_type=None, size=0,
                 width=None, height=None, duration=None, description=None):
        self.id = id
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.width = width
        self.height = height
        self.duration = duration
        self.description = description


class FakeMessage:
    def __init__(self, id, channel_id, author_id, content="", created_at=None,
                 edited_at=None, reference=None, mention_everyone=False,
                 flags_value=0, attachments=None, reactions=None,
                 mentions=None, role_mentions=None, stickers=None,
                 poll=None, embeds=None):
        self.id = id
        self.channel = FakeChannelRef(channel_id)
        self.author = FakeUserRef(author_id)
        self.content = content
        self.created_at = created_at
        self.edited_at = edited_at
        self.reference = reference
        self.mention_everyone = mention_everyone
        self.flags = FakeMessageFlags(flags_value)
        self.attachments = attachments or []
        self.reactions = reactions or []
        self.mentions = mentions or []
        self.role_mentions = role_mentions or []
        self.stickers = stickers or []
        self.poll = poll
        self.embeds = embeds or []
```

```python
# tests/test_discord_message.py
from datetime import datetime, timezone

from archiver.discord_message import map_message
from tests.discord_fakes import FakeAttachment, FakeMessage, FakeMessageReference

CREATED = datetime(2025, 10, 15, 3, 0, 0, tzinfo=timezone.utc)


def test_map_message_core_fields():
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hello", created_at=CREATED)
    mapped = map_message(msg)
    assert mapped["message"] == {
        "id": "100", "channel_id": "1", "author_id": "2", "content": "hello",
        "created_utc": "2025-10-15T03:00:00Z", "edited_utc": None,
        "reply_to_id": None, "mention_everyone": 0, "flags": 0, "deleted_utc": None,
    }


def test_map_message_empty_content_with_attachment_is_still_a_message():
    """spec: 'An empty-text message with an attachment or embed is still
    a message and must be stored.'"""
    msg = FakeMessage(
        id=101, channel_id=1, author_id=2, content="", created_at=CREATED,
        attachments=[FakeAttachment(id=500, filename="photo.png", content_type="image/png", size=1234)],
    )
    mapped = map_message(msg)
    assert mapped["message"]["content"] == ""
    assert mapped["attachments"] == [{
        "id": "500", "message_id": "101", "filename": "photo.png",
        "content_type": "image/png", "size": 1234, "width": None, "height": None,
        "duration_secs": None, "description": None,
    }]


def test_map_message_edited_and_reply():
    edited = datetime(2025, 10, 15, 3, 5, 0, tzinfo=timezone.utc)
    msg = FakeMessage(
        id=102, channel_id=1, author_id=2, content="fixed", created_at=CREATED,
        edited_at=edited, reference=FakeMessageReference(message_id=100),
    )
    mapped = map_message(msg)
    assert mapped["message"]["edited_utc"] == "2025-10-15T03:05:00Z"
    assert mapped["message"]["reply_to_id"] == "100"


def test_map_message_mention_everyone_and_flags():
    msg = FakeMessage(
        id=103, channel_id=1, author_id=2, content="@everyone hi", created_at=CREATED,
        mention_everyone=True, flags_value=64,
    )
    mapped = map_message(msg)
    assert mapped["message"]["mention_everyone"] == 1
    assert mapped["message"]["flags"] == 64


def test_map_message_always_has_all_child_keys_even_when_empty():
    msg = FakeMessage(id=104, channel_id=1, author_id=2, content="plain", created_at=CREATED)
    mapped = map_message(msg)
    assert set(mapped.keys()) == {
        "message", "attachments", "reactions", "mentions_user", "mentions_role",
        "stickers", "poll", "poll_answers", "embeds", "embed_fields",
    }
    assert mapped["attachments"] == []
    assert mapped["reactions"] == []
    assert mapped["poll"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_discord_message.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.discord_message'`.

- [ ] **Step 3: Write `archiver/discord_message.py`**

```python
# archiver/discord_message.py
"""Pure mapping from a discord.py Message (or a fake shaped like one)
into row dicts matching the Stage 1 shard schema. No I/O, no discord.py
Client -- fully unit-testable with hand-built fakes."""
from datetime import datetime


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def map_message(message) -> dict:
    reply_to_id = None
    if message.reference is not None and message.reference.message_id is not None:
        reply_to_id = str(message.reference.message_id)

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
        "deleted_utc": None,
    }

    attachments = [
        {
            "id": str(a.id), "message_id": row["id"], "filename": a.filename,
            "content_type": a.content_type, "size": a.size,
            "width": a.width, "height": a.height,
            "duration_secs": a.duration, "description": a.description,
        }
        for a in message.attachments
    ]

    return {
        "message": row, "attachments": attachments, "reactions": [],
        "mentions_user": [], "mentions_role": [], "stickers": [],
        "poll": None, "poll_answers": [], "embeds": [], "embed_fields": [],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_discord_message.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/discord_message.py tests/discord_fakes.py tests/test_discord_message.py
git commit -m "feat: map discord.py messages to shard row dicts (core fields, attachments)"
```

---

### Task 2: Reactions, mentions, stickers, polls, embeds mapping

**Files:**
- Modify: `archiver/discord_message.py` (rewrite `map_message`'s return to populate the remaining keys)
- Modify: `tests/discord_fakes.py` (append `FakeReaction`, `FakeSticker`, `FakePollMedia`, `FakePollAnswer`, `FakePoll`)
- Modify: `tests/test_discord_message.py` (append)

**Interfaces:**
- Consumes: nothing new from other tasks.
- Produces: `map_message` now fully populates `reactions`, `mentions_user`, `mentions_role`, `stickers`, `poll`, `poll_answers`, `embeds`, `embed_fields`. Real `discord.Embed` instances (constructed directly, not faked) are used for embed input in tests — confirmed `discord.Embed(title=..., description=..., url=...)` plus `.add_field(name=..., value=..., inline=...)` work standalone with no live connection.

- [ ] **Step 1: Write the failing test**

Append to `tests/discord_fakes.py`:

```python
class FakeReaction:
    def __init__(self, emoji, count, custom=False, animated=False):
        self.emoji = emoji
        self.count = count
        self._custom = custom
        if custom:
            self.emoji = _FakeCustomEmoji(str(emoji), animated)

    def is_custom_emoji(self) -> bool:
        return self._custom


class _FakeCustomEmoji:
    def __init__(self, text, animated):
        self._text = text
        self.animated = animated

    def __str__(self):
        return self._text


class FakeSticker:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class FakePollMedia:
    def __init__(self, text):
        self.text = text


class FakePollAnswer:
    def __init__(self, id, text, vote_count):
        self.id = id
        self.text = text
        self.vote_count = vote_count


class FakePoll:
    def __init__(self, question, multiple=False, expires_at=None, answers=None):
        self.question = FakePollMedia(question)
        self.multiple = multiple
        self.expires_at = expires_at
        self.answers = answers or []
```

Append to `tests/test_discord_message.py`:

```python
import discord

from tests.discord_fakes import FakePoll, FakePollAnswer, FakeReaction, FakeSticker


def test_map_message_reactions_unicode_and_custom():
    msg = FakeMessage(
        id=200, channel_id=1, author_id=2, content="funny", created_at=CREATED,
        reactions=[
            FakeReaction(emoji="\U0001F602", count=3),
            FakeReaction(emoji="<:pog:999>", count=1, custom=True, animated=True),
        ],
    )
    mapped = map_message(msg)
    assert mapped["reactions"] == [
        {"message_id": "200", "emoji": "\U0001F602", "is_custom": 0, "animated": 0, "count": 3},
        {"message_id": "200", "emoji": "<:pog:999>", "is_custom": 1, "animated": 1, "count": 1},
    ]


def test_map_message_mentions_and_stickers():
    from tests.discord_fakes import FakeUserRef
    role = type("FakeRole", (), {"id": 77})()
    msg = FakeMessage(
        id=201, channel_id=1, author_id=2, content="hi @you", created_at=CREATED,
        mentions=[FakeUserRef(id=42)], role_mentions=[role],
        stickers=[FakeSticker(id=900, name="PogChamp")],
    )
    mapped = map_message(msg)
    assert mapped["mentions_user"] == [{"message_id": "201", "user_id": "42"}]
    assert mapped["mentions_role"] == [{"message_id": "201", "role_id": "77"}]
    assert mapped["stickers"] == [{"message_id": "201", "sticker_id": "900", "name": "PogChamp"}]


def test_map_message_poll():
    expires = datetime(2025, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
    poll = FakePoll(
        question="Best game?", multiple=False, expires_at=expires,
        answers=[FakePollAnswer(id=1, text="Chess", vote_count=3), FakePollAnswer(id=2, text="Go", vote_count=5)],
    )
    msg = FakeMessage(id=202, channel_id=1, author_id=2, content="", created_at=CREATED, poll=poll)
    mapped = map_message(msg)
    assert mapped["poll"] == {
        "message_id": "202", "question": "Best game?", "multiselect": 0,
        "expires_utc": "2025-10-20T00:00:00Z",
    }
    assert mapped["poll_answers"] == [
        {"message_id": "202", "answer_id": 1, "text": "Chess", "vote_count": 3},
        {"message_id": "202", "answer_id": 2, "text": "Go", "vote_count": 5},
    ]


def test_map_message_embeds_with_fields():
    embed = discord.Embed(title="A link preview", description="desc", url="https://example.com")
    embed.add_field(name="Field One", value="Value One", inline=True)
    msg = FakeMessage(id=203, channel_id=1, author_id=2, content="check this", created_at=CREATED, embeds=[embed])
    mapped = map_message(msg)
    assert mapped["embeds"] == [{
        "message_id": "203", "position": 0, "title": "A link preview",
        "description": "desc", "url": "https://example.com", "embed_type": "rich",
    }]
    assert mapped["embed_fields"] == [{
        "message_id": "203", "embed_position": 0, "position": 0,
        "name": "Field One", "value": "Value One", "inline": 1,
    }]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_discord_message.py -v`
Expected: FAIL — new tests assert on empty lists (`reactions == []` etc.) that don't match the populated expectations.

- [ ] **Step 3: Rewrite `map_message`'s return in `archiver/discord_message.py`**

Replace the final `return {...}` block with:

```python
    reactions = [
        {
            "message_id": row["id"], "emoji": str(r.emoji),
            "is_custom": int(r.is_custom_emoji()),
            "animated": int(getattr(r.emoji, "animated", False)),
            "count": r.count,
        }
        for r in message.reactions
    ]

    mentions_user = [{"message_id": row["id"], "user_id": str(u.id)} for u in message.mentions]
    mentions_role = [{"message_id": row["id"], "role_id": str(r.id)} for r in message.role_mentions]
    stickers = [
        {"message_id": row["id"], "sticker_id": str(s.id), "name": s.name}
        for s in message.stickers
    ]

    poll = None
    poll_answers = []
    if message.poll is not None:
        poll = {
            "message_id": row["id"],
            "question": message.poll.question.text,
            "multiselect": int(message.poll.multiple),
            "expires_utc": _iso(message.poll.expires_at) if message.poll.expires_at else None,
        }
        poll_answers = [
            {"message_id": row["id"], "answer_id": a.id, "text": a.text, "vote_count": a.vote_count}
            for a in message.poll.answers
        ]

    embeds = []
    embed_fields = []
    for position, e in enumerate(message.embeds):
        embeds.append({
            "message_id": row["id"], "position": position,
            "title": e.title, "description": e.description,
            "url": e.url, "embed_type": e.type,
        })
        for field_position, f in enumerate(e.fields):
            embed_fields.append({
                "message_id": row["id"], "embed_position": position,
                "position": field_position, "name": f.name, "value": f.value,
                "inline": int(f.inline),
            })

    return {
        "message": row, "attachments": attachments, "reactions": reactions,
        "mentions_user": mentions_user, "mentions_role": mentions_role,
        "stickers": stickers, "poll": poll, "poll_answers": poll_answers,
        "embeds": embeds, "embed_fields": embed_fields,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_discord_message.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/discord_message.py tests/discord_fakes.py tests/test_discord_message.py
git commit -m "feat: map reactions, mentions, stickers, polls, and embeds"
```

---

### Task 3: Shard resolution, connection caching, and idempotent row writing

**Files:**
- Create: `archiver/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `connect_shard` from Stage 1's `archiver.db`.
- Produces:
  - `archiver.store.month_bucket(created_utc: datetime) -> str` — Asia/Seoul `YYYY-MM`.
  - `archiver.store.sanitize_folder_name(name: str) -> str`.
  - `archiver.store.UNCATEGORIZED_ID = "uncategorized"`, `archiver.store.UNCATEGORIZED_NAME = "Uncategorized"`.
  - `archiver.store.ShardStore(data_dir: Path, catalog_conn: sqlite3.Connection)` — `.get_shard(channel_id: str, created_utc: datetime) -> sqlite3.Connection` (caches connections; on first use for a `(channel_id, yyyymm)` pair, resolves and records the shard path in `catalog.channel_month_shard`, using the channel's *current* category at that moment — a later category rename never moves already-written months, per spec §4). `.close_all()`.
  - `archiver.store.write_message(shard_conn: sqlite3.Connection, mapped: dict) -> None` — idempotent upsert of one mapped message and all its child rows into the given shard connection. Does not commit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
from datetime import datetime, timezone
from pathlib import Path

from archiver.db import connect_catalog
from archiver.discord_message import map_message
from archiver.store import ShardStore, UNCATEGORIZED_NAME, month_bucket, sanitize_folder_name, write_message
from tests.discord_fakes import FakeMessage

SEOUL_MIDNIGHT_UTC = datetime(2025, 10, 15, 15, 0, 0, tzinfo=timezone.utc)  # 2025-10-16 00:00 KST


def test_month_bucket_uses_seoul_timezone():
    assert month_bucket(SEOUL_MIDNIGHT_UTC) == "2025-10"
    just_before = datetime(2025, 10, 15, 14, 59, 59, tzinfo=timezone.utc)  # 2025-10-15 23:59:59 KST
    assert month_bucket(just_before) == "2025-10"
    just_after = datetime(2025, 10, 15, 15, 0, 1, tzinfo=timezone.utc)  # 2025-10-16 00:00:01 KST
    assert month_bucket(just_after) == "2025-10"


def test_month_bucket_crosses_into_next_month():
    end_of_oct_kst = datetime(2025, 10, 31, 15, 0, 1, tzinfo=timezone.utc)  # 2025-11-01 00:00:01 KST
    assert month_bucket(end_of_oct_kst) == "2025-11"


def test_sanitize_folder_name_strips_invalid_windows_chars():
    assert sanitize_folder_name("normal name") == "normal name"
    assert sanitize_folder_name('weird:name/with*bad<chars>') == "weird_name_with_bad_chars_"
    assert sanitize_folder_name("") == UNCATEGORIZED_NAME


def test_shard_store_resolves_category_folder(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.commit()

    store = ShardStore(tmp_path, catalog)
    shard_conn = store.get_shard("1", SEOUL_MIDNIGHT_UTC)

    row = shard_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    assert row is not None
    assert (tmp_path / "Friends" / "2025-10.sqlite").exists()

    catalog_row = catalog.execute(
        "SELECT shard_path, category_id FROM channel_month_shard WHERE channel_id='1' AND yyyymm='2025-10'"
    ).fetchone()
    assert catalog_row["shard_path"] == "Friends/2025-10.sqlite"
    assert catalog_row["category_id"] == "10"


def test_shard_store_uses_uncategorized_sentinel_for_channel_with_no_category(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('2','loose-channel','text',NULL,NULL,0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.commit()

    store = ShardStore(tmp_path, catalog)
    store.get_shard("2", SEOUL_MIDNIGHT_UTC)

    assert (tmp_path / "Uncategorized" / "2025-10.sqlite").exists()
    catalog_row = catalog.execute(
        "SELECT category_id FROM channel_month_shard WHERE channel_id='2'"
    ).fetchone()
    assert catalog_row["category_id"] == "uncategorized"


def test_shard_store_reuses_cached_connection(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    conn_a = store.get_shard("1", SEOUL_MIDNIGHT_UTC)
    conn_b = store.get_shard("1", SEOUL_MIDNIGHT_UTC)

    assert conn_a is conn_b


def test_shard_store_keeps_old_months_path_after_category_rename(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    store.get_shard("1", SEOUL_MIDNIGHT_UTC)

    catalog.execute(
        "UPDATE category_names SET name='Renamed' WHERE category_id='10'"
    )
    catalog.commit()
    store2 = ShardStore(tmp_path, catalog)
    store2.get_shard("1", SEOUL_MIDNIGHT_UTC)  # same channel, same month, after rename

    assert (tmp_path / "Friends" / "2025-10.sqlite").exists()
    assert not (tmp_path / "Renamed" / "2025-10.sqlite").exists()


def test_write_message_is_idempotent(tmp_path: Path):
    from archiver.db import connect_shard
    shard_conn = connect_shard(tmp_path / "2025-10.sqlite")
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=SEOUL_MIDNIGHT_UTC)
    mapped = map_message(msg)

    write_message(shard_conn, mapped)
    write_message(shard_conn, mapped)  # replay must not duplicate
    shard_conn.commit()

    count = shard_conn.execute("SELECT COUNT(*) FROM messages WHERE id='100'").fetchone()[0]
    assert count == 1


def test_write_message_upserts_all_child_tables(tmp_path: Path):
    from archiver.db import connect_shard
    from tests.discord_fakes import FakePoll, FakePollAnswer
    shard_conn = connect_shard(tmp_path / "2025-10.sqlite")
    poll = FakePoll(question="Best?", answers=[FakePollAnswer(id=1, text="A", vote_count=1)])
    msg = FakeMessage(id=101, channel_id=1, author_id=2, content="", created_at=SEOUL_MIDNIGHT_UTC, poll=poll)
    mapped = map_message(msg)

    write_message(shard_conn, mapped)
    shard_conn.commit()

    assert shard_conn.execute("SELECT 1 FROM polls WHERE message_id='101'").fetchone() is not None
    assert shard_conn.execute(
        "SELECT vote_count FROM poll_answers WHERE message_id='101' AND answer_id=1"
    ).fetchone()["vote_count"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.store'`.

- [ ] **Step 3: Write `archiver/store.py`**

```python
# archiver/store.py
"""Shard resolution, connection caching, and idempotent message writes.
One ShardStore per backfill/live-capture run holds open shard
connections so repeated writes to the same month don't re-open the
file every time."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from archiver.db import connect_shard

SEOUL = ZoneInfo("Asia/Seoul")
UNCATEGORIZED_ID = "uncategorized"
UNCATEGORIZED_NAME = "Uncategorized"
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')


def month_bucket(created_utc: datetime) -> str:
    if created_utc.tzinfo is None:
        created_utc = created_utc.replace(tzinfo=timezone.utc)
    return created_utc.astimezone(SEOUL).strftime("%Y-%m")


def sanitize_folder_name(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or UNCATEGORIZED_NAME


class ShardStore:
    def __init__(self, data_dir: Path, catalog_conn: sqlite3.Connection):
        self.data_dir = data_dir
        self.catalog_conn = catalog_conn
        self._shards: dict[str, sqlite3.Connection] = {}

    def get_shard(self, channel_id: str, created_utc: datetime) -> sqlite3.Connection:
        yyyymm = month_bucket(created_utc)
        row = self.catalog_conn.execute(
            "SELECT shard_path FROM channel_month_shard WHERE channel_id=? AND yyyymm=?",
            (channel_id, yyyymm),
        ).fetchone()
        if row is not None:
            relative_path = row["shard_path"]
        else:
            category_id, category_name = self._resolve_category(channel_id)
            folder = sanitize_folder_name(category_name)
            relative_path = f"{folder}/{yyyymm}.sqlite"
            self.catalog_conn.execute(
                "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, yyyymm, category_id, relative_path),
            )
            self.catalog_conn.commit()

        if relative_path not in self._shards:
            self._shards[relative_path] = connect_shard(self.data_dir / relative_path)
        return self._shards[relative_path]

    def _resolve_category(self, channel_id: str) -> tuple[str, str]:
        row = self.catalog_conn.execute(
            "SELECT category_id FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        category_id = row["category_id"] if row and row["category_id"] else None
        if category_id is None:
            return UNCATEGORIZED_ID, UNCATEGORIZED_NAME
        name_row = self.catalog_conn.execute(
            "SELECT name FROM category_names WHERE category_id=?", (category_id,)
        ).fetchone()
        name = name_row["name"] if name_row else category_id
        return category_id, name

    def close_all(self) -> None:
        for conn in self._shards.values():
            conn.close()
        self._shards.clear()


def write_message(shard_conn: sqlite3.Connection, mapped: dict) -> None:
    """Idempotent upsert of one mapped message and all its child rows.
    Safe to call twice with the same data (replay after a crash). Does
    not commit -- callers batch a page's writes under one commit."""
    m = mapped["message"]
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

    for a in mapped["attachments"]:
        shard_conn.execute(
            "INSERT INTO attachments (id, message_id, filename, content_type, size, "
            "width, height, duration_secs, description) VALUES "
            "(:id, :message_id, :filename, :content_type, :size, :width, :height, "
            ":duration_secs, :description) ON CONFLICT(id) DO UPDATE SET "
            "filename=excluded.filename",
            a,
        )

    for r in mapped["reactions"]:
        shard_conn.execute(
            "INSERT INTO reactions (message_id, emoji, is_custom, animated, count) "
            "VALUES (:message_id, :emoji, :is_custom, :animated, :count) "
            "ON CONFLICT(message_id, emoji) DO UPDATE SET count=excluded.count",
            r,
        )

    for mu in mapped["mentions_user"]:
        shard_conn.execute(
            "INSERT INTO mentions_user (message_id, user_id) VALUES (:message_id, :user_id) "
            "ON CONFLICT(message_id, user_id) DO NOTHING",
            mu,
        )

    for mr in mapped["mentions_role"]:
        shard_conn.execute(
            "INSERT INTO mentions_role (message_id, role_id) VALUES (:message_id, :role_id) "
            "ON CONFLICT(message_id, role_id) DO NOTHING",
            mr,
        )

    for s in mapped["stickers"]:
        shard_conn.execute(
            "INSERT INTO stickers (message_id, sticker_id, name) "
            "VALUES (:message_id, :sticker_id, :name) "
            "ON CONFLICT(message_id, sticker_id) DO NOTHING",
            s,
        )

    if mapped["poll"] is not None:
        shard_conn.execute(
            "INSERT INTO polls (message_id, question, multiselect, expires_utc) "
            "VALUES (:message_id, :question, :multiselect, :expires_utc) "
            "ON CONFLICT(message_id) DO UPDATE SET question=excluded.question",
            mapped["poll"],
        )
    for pa in mapped["poll_answers"]:
        shard_conn.execute(
            "INSERT INTO poll_answers (message_id, answer_id, text, vote_count) "
            "VALUES (:message_id, :answer_id, :text, :vote_count) "
            "ON CONFLICT(message_id, answer_id) DO UPDATE SET vote_count=excluded.vote_count",
            pa,
        )

    for e in mapped["embeds"]:
        shard_conn.execute(
            "INSERT INTO embeds (message_id, position, title, description, url, embed_type) "
            "VALUES (:message_id, :position, :title, :description, :url, :embed_type) "
            "ON CONFLICT(message_id, position) DO UPDATE SET title=excluded.title, "
            "description=excluded.description",
            e,
        )
    for ef in mapped["embed_fields"]:
        shard_conn.execute(
            "INSERT INTO embed_fields (message_id, embed_position, position, name, value, inline) "
            "VALUES (:message_id, :embed_position, :position, :name, :value, :inline) "
            "ON CONFLICT(message_id, embed_position, position) DO UPDATE SET value=excluded.value",
            ef,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/store.py tests/test_store.py
git commit -m "feat: shard resolution, connection caching, idempotent message writes"
```

---

### Task 4: Cross-file commit protocol (spec §4.1)

**Files:**
- Modify: `archiver/store.py` (append `commit_page`)
- Modify: `tests/test_store.py` (append)

**Interfaces:**
- Consumes: `ShardStore.get_shard`, `write_message` from Task 3.
- Produces: `archiver.store.commit_page(store: ShardStore, catalog_conn: sqlite3.Connection, channel_id: str, pages: list[tuple[Any, dict]], oldest_id: str, newest_id: str) -> None` — `pages` is a list of `(discord_message_or_fake, mapped_dict)` tuples for one backfill page. Writes every affected shard, commits each, then advances `coverage.backfill_checkpoint`/`oldest_message_id`/`newest_message_id`/`message_count` in one catalog transaction — the checkpoint only ever moves after every shard write for that page has committed.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
def test_commit_page_advances_checkpoint_and_span(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    from archiver.store import commit_page
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    m2 = FakeMessage(id=101, channel_id=1, author_id=2, content="b", created_at=SEOUL_MIDNIGHT_UTC)
    pages = [(m1, map_message(m1)), (m2, map_message(m2))]

    commit_page(store, catalog, "1", pages, oldest_id="100", newest_id="101")

    row = catalog.execute(
        "SELECT backfill_checkpoint, oldest_message_id, newest_message_id, message_count "
        "FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["backfill_checkpoint"] == "101"
    assert row["oldest_message_id"] == "100"
    assert row["newest_message_id"] == "101"
    assert row["message_count"] == 2


def test_commit_page_oldest_message_id_not_overwritten_by_later_pages(tmp_path: Path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    from archiver.store import commit_page
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    commit_page(store, catalog, "1", [(m1, map_message(m1))], oldest_id="100", newest_id="100")

    m2 = FakeMessage(id=200, channel_id=1, author_id=2, content="b", created_at=SEOUL_MIDNIGHT_UTC)
    commit_page(store, catalog, "1", [(m2, map_message(m2))], oldest_id="200", newest_id="200")

    row = catalog.execute(
        "SELECT oldest_message_id, newest_message_id FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["oldest_message_id"] == "100"  # first page's oldest, never overwritten
    assert row["newest_message_id"] == "200"  # advances with each page


def test_commit_page_crash_between_shard_and_checkpoint_is_replay_safe(tmp_path: Path):
    """spec §4.1: a crash between a shard's commit and the catalog
    checkpoint commit must be safely replayable -- no duplicate rows,
    checkpoint advances exactly once when the page is finally committed
    in full."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        "('1','general','text','10','10',0,'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        "('10','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute("INSERT INTO coverage (channel_id, status) VALUES ('1', 'crawling')")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    from archiver.store import commit_page, write_message
    m1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=SEOUL_MIDNIGHT_UTC)
    m2 = FakeMessage(id=101, channel_id=1, author_id=2, content="b", created_at=SEOUL_MIDNIGHT_UTC)

    # Simulate a crash: write and commit the shard directly (as commit_page
    # would), but never advance the catalog checkpoint.
    shard_conn = store.get_shard("1", SEOUL_MIDNIGHT_UTC)
    write_message(shard_conn, map_message(m1))
    write_message(shard_conn, map_message(m2))
    shard_conn.commit()
    # (crash here -- catalog checkpoint was never touched)

    checkpoint_before = catalog.execute(
        "SELECT backfill_checkpoint FROM coverage WHERE channel_id='1'"
    ).fetchone()["backfill_checkpoint"]
    assert checkpoint_before is None  # confirms the "crash" left no checkpoint

    # Replay: the real backfill loop would re-fetch and re-commit the same page.
    pages = [(m1, map_message(m1)), (m2, map_message(m2))]
    commit_page(store, catalog, "1", pages, oldest_id="100", newest_id="101")

    count = shard_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 2  # no duplicates from the replay
    row = catalog.execute(
        "SELECT backfill_checkpoint, message_count FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["backfill_checkpoint"] == "101"
    assert row["message_count"] == 2  # counted once, not twice
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'commit_page'`.

- [ ] **Step 3: Append `commit_page` to `archiver/store.py`**

```python
def commit_page(store: ShardStore, catalog_conn: sqlite3.Connection, channel_id: str,
                 pages: list, oldest_id: str, newest_id: str) -> None:
    """Write one backfill page's messages to their shard(s), then advance
    the channel's coverage row (checkpoint, message span, count) in a
    single catalog transaction -- only after every affected shard has
    committed (spec §4.1). If the process crashes between a shard's
    commit and this catalog commit, the checkpoint still points at the
    previous page on restart; replaying the page is safe because every
    write here is an idempotent upsert.

    `pages` is a list of (discord_message, mapped_dict) tuples."""
    touched_shards: set[sqlite3.Connection] = set()
    for message, mapped in pages:
        shard_conn = store.get_shard(channel_id, message.created_at)
        write_message(shard_conn, mapped)
        touched_shards.add(shard_conn)

    for shard_conn in touched_shards:
        shard_conn.commit()

    catalog_conn.execute(
        "UPDATE coverage SET backfill_checkpoint=?, "
        "oldest_message_id=COALESCE(oldest_message_id, ?), newest_message_id=?, "
        "message_count=message_count+? WHERE channel_id=?",
        (newest_id, oldest_id, newest_id, len(pages), channel_id),
    )
    catalog_conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/store.py tests/test_store.py
git commit -m "feat: cross-file commit protocol for backfill pages (spec §4.1)"
```

---

### Task 5: Per-channel oldest-first backfill with failure isolation

**Files:**
- Create: `archiver/backfill.py`
- Modify: `tests/discord_fakes.py` (append a `history()` method's worth of support to `FakeChannel` — see below)
- Test: `tests/test_backfill.py`

**Interfaces:**
- Consumes: `map_message` (Task 1-2), `ShardStore`, `commit_page` (Task 3-4).
- Produces: `archiver.backfill.backfill_channel(discord_channel, catalog_conn: sqlite3.Connection, store: ShardStore) -> None` — async. Resumes from `coverage.backfill_checkpoint` (defaulting to `"0"`), pages via `discord_channel.history(limit=100, after=discord.Object(id=int(checkpoint)), oldest_first=True)`, calls `commit_page` per page, sets `status='crawling'` while running and `status='complete'` when the channel is exhausted (a page returns fewer than 100 messages). On `discord.Forbidden`, sets `status='inaccessible'`; on any other `discord.HTTPException`, sets `status='failed'` — either way with the error text as `gap_reason`, and returns normally rather than raising (so a caller iterating multiple channels never aborts on one failure).

- [ ] **Step 1: Write the failing test**

Append to `tests/discord_fakes.py` (`FakeChannel` needs a `history()` async generator alongside its existing `archived_threads()`; this reuses the same `_forbidden_all`-style pattern already on the class):

```python
class FakeHistoryChannel:
    """A minimal channel double for backfill tests -- separate from
    FakeChannel (which models discovery's archived_threads surface) since
    backfill only needs .id and .history(), not thread pagination."""
    def __init__(self, id, messages, forbidden=False, http_error=False):
        self.id = id
        self._messages = sorted(messages, key=lambda m: m.id)
        self._forbidden = forbidden
        self._http_error = http_error

    async def history(self, *, limit=100, after=None, oldest_first=True):
        if self._forbidden:
            raise discord.Forbidden(FakeResponse(), "Missing Permissions")
        if self._http_error:
            raise discord.HTTPException(FakeResponse(), "Internal Server Error")
        after_id = after.id if after is not None else 0
        remaining = [m for m in self._messages if m.id > after_id]
        for m in remaining[:limit]:
            yield m
```

```python
# tests/test_backfill.py
from datetime import datetime, timezone

from archiver.backfill import backfill_channel
from archiver.db import connect_catalog
from archiver.store import ShardStore
from tests.discord_fakes import FakeHistoryChannel, FakeMessage

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
    catalog.execute(f"INSERT INTO coverage (channel_id, status) VALUES ('{channel_id}', 'pending')")
    catalog.commit()


async def test_backfill_channel_archives_all_messages_and_marks_complete(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    messages = [
        FakeMessage(id=100 + i, channel_id=1, author_id=2, content=f"msg{i}", created_at=CREATED)
        for i in range(5)
    ]
    channel = FakeHistoryChannel(id=1, messages=messages)

    await backfill_channel(channel, catalog, store)

    row = catalog.execute("SELECT status, message_count, backfill_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "complete"
    assert row["message_count"] == 5
    assert row["backfill_checkpoint"] == "104"


async def test_backfill_channel_resumes_from_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET backfill_checkpoint='101' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    messages = [
        FakeMessage(id=100 + i, channel_id=1, author_id=2, content=f"msg{i}", created_at=CREATED)
        for i in range(5)
    ]
    channel = FakeHistoryChannel(id=1, messages=messages)

    await backfill_channel(channel, catalog, store)

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 3  # only ids 102, 103, 104 -- 100, 101 already archived


async def test_backfill_channel_forbidden_marks_inaccessible_and_does_not_raise(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    channel = FakeHistoryChannel(id=1, messages=[], forbidden=True)

    await backfill_channel(channel, catalog, store)  # must not raise

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "inaccessible"
    assert row["gap_reason"] is not None


async def test_backfill_channel_other_http_error_marks_failed(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    channel = FakeHistoryChannel(id=1, messages=[], http_error=True)

    await backfill_channel(channel, catalog, store)  # must not raise

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "failed"
    assert row["gap_reason"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.backfill'`.

- [ ] **Step 3: Write `archiver/backfill.py`**

```python
# archiver/backfill.py
"""Historical backfill: oldest-first crawl per channel with per-channel
failure isolation, and (Task 6) bounded-concurrency orchestration across
every pending channel in the catalog."""
import sqlite3
from datetime import datetime, timezone

import discord

from archiver.discord_message import map_message
from archiver.store import ShardStore, commit_page

PAGE_SIZE = 100


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def backfill_channel(discord_channel, catalog_conn: sqlite3.Connection, store: ShardStore) -> None:
    """Backfill one channel/thread from its current checkpoint to the
    present, oldest-first, committing and checkpointing every page (spec
    §4.1). Never raises for an in-scope API failure -- records it as
    status='inaccessible'/'failed' with the error as gap_reason and
    returns, so the caller can move on to the next channel (spec §5,
    discrawl #27/#30)."""
    channel_id = str(discord_channel.id)
    row = catalog_conn.execute(
        "SELECT backfill_checkpoint FROM coverage WHERE channel_id=?", (channel_id,)
    ).fetchone()
    checkpoint = row["backfill_checkpoint"] if row and row["backfill_checkpoint"] else "0"

    catalog_conn.execute(
        "UPDATE coverage SET status='crawling' WHERE channel_id=? AND status != 'complete'",
        (channel_id,),
    )
    catalog_conn.commit()

    try:
        while True:
            messages = [
                m async for m in discord_channel.history(
                    limit=PAGE_SIZE, after=discord.Object(id=int(checkpoint)), oldest_first=True,
                )
            ]
            if not messages:
                break

            pages = [(m, map_message(m)) for m in messages]
            oldest_id = str(messages[0].id)
            newest_id = str(messages[-1].id)
            commit_page(store, catalog_conn, channel_id, pages, oldest_id, newest_id)
            checkpoint = newest_id

            if len(messages) < PAGE_SIZE:
                break

        catalog_conn.execute(
            "UPDATE coverage SET status='complete', last_checked_utc=? WHERE channel_id=?",
            (_now(), channel_id),
        )
        catalog_conn.commit()
    except discord.Forbidden as e:
        catalog_conn.execute(
            "UPDATE coverage SET status='inaccessible', gap_reason=?, last_checked_utc=? "
            "WHERE channel_id=?",
            (f"backfill failed: {e}", _now(), channel_id),
        )
        catalog_conn.commit()
    except discord.HTTPException as e:
        catalog_conn.execute(
            "UPDATE coverage SET status='failed', gap_reason=?, last_checked_utc=? "
            "WHERE channel_id=?",
            (f"backfill failed: {e}", _now(), channel_id),
        )
        catalog_conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backfill.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/backfill.py tests/discord_fakes.py tests/test_backfill.py
git commit -m "feat: per-channel oldest-first backfill with failure isolation"
```

---

### Task 6: Bounded-concurrency orchestration across all pending channels

**Files:**
- Modify: `archiver/backfill.py` (append `backfill_all_pending`)
- Modify: `tests/test_backfill.py` (append)

**Interfaces:**
- Consumes: `backfill_channel` from Task 5.
- Produces: `archiver.backfill.backfill_all_pending(client, catalog_conn: sqlite3.Connection, store: ShardStore, concurrency: int = 3) -> None` — async. Reads every `coverage.channel_id` whose `status` is `pending`, `crawling`, or `failed` (never `complete` or `inaccessible` — those are settled states this function doesn't retry), fetches each via `client.fetch_channel(int(channel_id))`, and runs `backfill_channel` for each, bounded by an `asyncio.Semaphore(concurrency)`. A channel that's vanished or gone inaccessible since discovery (`discord.NotFound`/`discord.Forbidden` from `fetch_channel` itself) is marked `inaccessible` directly, without ever calling `backfill_channel`.

- [ ] **Step 1: Write the failing test**

Append to `tests/discord_fakes.py`:

```python
class FakeClient:
    """A minimal Client double for backfill_all_pending: maps channel id
    -> channel object (or an exception to raise) for fetch_channel."""
    def __init__(self, channels: dict, errors: dict | None = None):
        self._channels = channels
        self._errors = errors or {}

    async def fetch_channel(self, channel_id: int):
        if channel_id in self._errors:
            raise self._errors[channel_id]
        return self._channels[channel_id]
```

Append to `tests/test_backfill.py`:

```python
from archiver.backfill import backfill_all_pending
from tests.discord_fakes import FakeClient


async def test_backfill_all_pending_runs_every_pending_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    _seed_channel(catalog, channel_id="2", category_id="10")
    store = ShardStore(tmp_path, catalog)
    msg1 = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=CREATED)
    msg2 = FakeMessage(id=200, channel_id=2, author_id=2, content="b", created_at=CREATED)
    client = FakeClient({
        1: FakeHistoryChannel(id=1, messages=[msg1]),
        2: FakeHistoryChannel(id=2, messages=[msg2]),
    })

    await backfill_all_pending(client, catalog, store, concurrency=2)

    statuses = {
        row["channel_id"]: row["status"]
        for row in catalog.execute("SELECT channel_id, status FROM coverage").fetchall()
    }
    assert statuses == {"1": "complete", "2": "complete"}


async def test_backfill_all_pending_skips_complete_and_inaccessible(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    _seed_channel(catalog, channel_id="2", category_id="10")
    catalog.execute("UPDATE coverage SET status='inaccessible' WHERE channel_id='2'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({})  # would KeyError if either channel were fetched

    await backfill_all_pending(client, catalog, store)  # must not raise / not touch either

    statuses = {
        row["channel_id"]: row["status"]
        for row in catalog.execute("SELECT channel_id, status FROM coverage").fetchall()
    }
    assert statuses == {"1": "complete", "2": "inaccessible"}


async def test_backfill_all_pending_marks_gone_channel_inaccessible_without_calling_backfill(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({}, errors={1: discord.NotFound(FakeResponse(), "Unknown Channel")})

    await backfill_all_pending(client, catalog, store)

    row = catalog.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='1'").fetchone()
    assert row["status"] == "inaccessible"
    assert row["gap_reason"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backfill.py -v`
Expected: FAIL — `ImportError: cannot import name 'backfill_all_pending'`.

- [ ] **Step 3: Append `backfill_all_pending` to `archiver/backfill.py`**

Add `import asyncio` to the top of the file, then append:

```python
async def backfill_all_pending(client, catalog_conn: sqlite3.Connection, store: ShardStore,
                                 concurrency: int = 3) -> None:
    """Run backfill_channel for every channel/thread whose coverage
    status is 'pending', 'crawling', or 'failed', bounded by a semaphore
    so backfill never starves the gateway heartbeat or the rate-limit
    bucket shared with other bots on the same token (spec §2)."""
    rows = catalog_conn.execute(
        "SELECT channel_id FROM coverage WHERE status IN ('pending', 'crawling', 'failed')"
    ).fetchall()
    channel_ids = [row["channel_id"] for row in rows]
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(channel_id: str) -> None:
        async with semaphore:
            try:
                discord_channel = await client.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden) as e:
                catalog_conn.execute(
                    "UPDATE coverage SET status='inaccessible', gap_reason=?, last_checked_utc=? "
                    "WHERE channel_id=?",
                    (f"backfill failed: {e}", _now(), channel_id),
                )
                catalog_conn.commit()
                return
            await backfill_channel(discord_channel, catalog_conn, store)

    await asyncio.gather(*(_run_one(cid) for cid in channel_ids))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backfill.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/backfill.py tests/discord_fakes.py tests/test_backfill.py
git commit -m "feat: bounded-concurrency orchestration across all pending channels"
```

---

### Task 7: `backfill` CLI command

**Files:**
- Modify: `archiver/cli.py` (append)
- Modify: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `backfill_all_pending` (Task 6), `ShardStore` (Task 3), `connect_catalog` (Stage 1).
- Produces: `archiver.cli._run_backfill(config) -> int` (async, live-connecting, not unit tested — same documented pattern as `_run_doctor`/`_run_coverage_preflight`). `main()` gains a `backfill` subcommand.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_main_backfill_subcommand_is_registered(monkeypatch):
    """Argparse routing only -- the live connection itself is verified
    manually, same as doctor/coverage --preflight."""
    import pytest
    monkeypatch.setattr("sys.argv", ["archive", "backfill", "--bogus-flag"])
    with pytest.raises(SystemExit):
        main()  # unrecognized flag must still fail argparse, proving the subcommand exists
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — argparse currently has no `backfill` subcommand, so `"backfill"` itself is rejected as an invalid choice with a different error than the intended `--bogus-flag` one; the test still passes today only by accident (SystemExit fires either way) — this is fine since the assertion is only that SOME SystemExit occurs, but confirm by running it that it fails for the RIGHT reason once the command exists in Step 4's re-check. Proceed to Step 3.

- [ ] **Step 3: Append to `archiver/cli.py`**

Add these imports at the top (alongside the existing ones):
```python
from archiver.backfill import backfill_all_pending
from archiver.store import ShardStore
```

Append:

```python
async def _run_backfill(config) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    store = ShardStore(config.data_dir, catalog_conn)
    intents = discord.Intents.default()
    intents.message_content = True  # backfill reads actual message content
    client = discord.Client(intents=intents)
    error: Exception | None = None

    @client.event
    async def on_ready():
        nonlocal error
        try:
            guild = client.get_guild(int(config.guild_id))
            if guild is None:
                raise RuntimeError(f"configured guild id {config.guild_id} not found")
            await backfill_all_pending(client, catalog_conn, store)
        except Exception as e:
            error = e
        finally:
            store.close_all()
            await client.close()

    token = load_token(HERE / ".env")
    try:
        await client.start(token)
    except Exception as e:
        print(f"backfill failed to connect: {e}")
        return 1

    if error is not None:
        print(f"backfill failed: {error}")
        return 1

    print("Backfill pass complete.")
    return 0
```

Modify `main()`'s subparser setup to add, alongside `doctor` and `coverage`:
```python
    subparsers.add_parser("backfill")
```

And add a dispatch branch alongside the existing `doctor`/`coverage` branches:
```python
    if args.command == "backfill":
        return asyncio.run(_run_backfill(config))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS. Confirm the new test now fails specifically on the unrecognized `--bogus-flag` (not on `backfill` itself being an invalid subcommand) by running `python -m archiver.cli backfill --bogus-flag` manually and reading argparse's error message — it should complain about `--bogus-flag`, not about `backfill`.

- [ ] **Step 5: Commit**

```bash
git add archiver/cli.py tests/test_cli.py
git commit -m "feat: add backfill CLI command"
```

---

### Task 8: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-7 passes, plus all of Stage 1/2's existing tests still pass (no regressions).

- [ ] **Step 2: Note what's NOT covered by the automated suite**

`_run_backfill` opens a real `discord.Client` and is not exercised by `pytest` — intentional, same pattern as `doctor`/`coverage --preflight`. Before Stage 3 is considered actually done (not just test-green), a controller/human must run `python -m archiver.cli backfill` against the real bot and real `config.json`, watch it complete without raising, and spot-check the resulting shard files. Given the real server has 265 channels/threads (discovered in Stage 2), a full first backfill pass will take a meaningful amount of wall-clock time — run it in the background and check on it rather than blocking on it synchronously.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

If clean, no commit needed — Task 8 is verification only.

---

## Self-Review Notes

- **Spec coverage:** §4.1 cross-file commit protocol — Task 4, with the required crash-between-shard-and-checkpoint test. §5 backfill (corrected `after=` cursor, per-channel failure isolation) — Tasks 5-6. §2 bounded concurrency (`asyncio.Semaphore`) — Task 6. Message/attachment/reaction/mention/sticker/poll/embed capture per spec's "Preserve complete available text..." requirement — Tasks 1-2. Empty-text-with-attachment-is-still-a-message — Task 1's dedicated test. Category rename never moves already-written months — Task 3's dedicated test. Uncategorized-channel sentinel (Stage 1's flagged gap) — Task 3.
- **Placeholder scan:** none found — every step has real code and real assertions. Task 7's Step 2 has an intentionally soft "Expected" note (the test technically passes for either reason before the command exists) but Step 4 explicitly requires confirming the *right* reason once the code lands, so no placeholder survives past the task's own cycle.
- **Type consistency:** `map_message` always returns all ten dict keys from Task 1 onward, so Task 2 only changes values, never the shape `write_message`/`commit_page` depend on. `ShardStore.get_shard(channel_id: str, created_utc: datetime) -> sqlite3.Connection` is the one signature every later task (`commit_page`, `backfill_channel`) calls identically. `commit_page`'s `pages: list[tuple[message, mapped]]` shape matches exactly what `backfill_channel` builds (`[(m, map_message(m)) for m in messages]`) and what Task 4's own tests construct by hand.

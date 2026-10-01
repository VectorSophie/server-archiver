# Stage 5: Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CLI `find` command (plus a reusable `archiver/search.py` module) that searches archived messages with a `from:`/`in:`/`during:`/`after:`/`before:`/`has:`/`file:`/`ext:` filter grammar, substring content matching (trigram FTS or LIKE fallback per spec §7.1), and `--full`/`--context N`/`--json` output modes, plus a no-args interactive loop.

**Architecture:** A pure tokenizer turns the raw query string into filter tokens + free text (no DB). A resolver turns filter tokens into concrete channel/author id lists and UTC date bounds (needs `catalog_conn` for name lookups). Candidate shard files are narrowed via the catalog's `channel_month_shard` table (channel + month range) before any shard file is opened — the same "filter first on indexed columns, only then touch content" principle spec §7.1 requires. Each candidate shard is queried with a SQL WHERE clause combining indexed predicates and EXISTS subqueries for `has:`/`file:`/`ext:`, plus either an FTS5 trigram `MATCH` or a `LIKE` fallback for the free-text portion, chosen by query length exactly as spec §7.1 specifies. Results are merged, sorted by message id, and formatted for terminal or JSON output.

**Tech Stack:** Python stdlib only (`shlex`, `sqlite3`, `json`, `zoneinfo`) — no parser or CLI framework beyond the existing `argparse` setup in `archiver/cli.py`.

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§7 Search, §7.1 Substring matching)

## Global Constraints

- Never download attachment bytes — this stage only ever reads already-archived metadata/content already in the shard/catalog databases.
- Substring matching routes on query length, not just trigram availability: length >= 3 AND trigram available -> FTS `MATCH`; anything else -> `LIKE '%fragment%'` fallback, scoped to already-filtered rows (spec §7.1). A trigram tokenizer cannot match under 3 characters by construction.
- Content matching must work correctly on a fragment from the middle of a Korean word and a fragment from the middle of an English word, under whichever backend is active.
- `--context N` must correctly cross month-shard boundaries (a message near the start/end of a month has its context messages in the adjacent month's shard file).
- No SQL/web UI — CLI only, per spec §7.
- Never print the bot token (this stage doesn't touch Discord at all — no `discord.Client`, no token load — so this is automatically satisfied, but don't add one).
- Follow this project's established idioms: `sqlite3.Row` row factory (already set by `open_db`/`connect_shard`/`connect_catalog`), pure formatting functions kept separate from I/O so they're unit-testable without a live DB fixture (see `archiver/cli.py`'s existing `format_doctor_report`/`format_coverage_rows` for the pattern), hand-built fixtures in tests (`tests/fixtures.py`) rather than mocks.

## File Structure

- **Create:** `archiver/search.py` — query tokenizing, filter resolution, candidate-shard selection, per-shard SQL construction, the `search()` orchestrator, context lookup, and result formatting. One cohesive module for this stage, matching the size of this project's other single-purpose modules (~150-220 lines).
- **Modify:** `archiver/cli.py` — add the `find` subcommand (`_run_find`, `_run_find_interactive`) and argparse wiring. Synchronous, no `discord.Client` involved.
- **Test:** `tests/test_search.py` (new) — covers the pure tokenizer, the resolver, candidate-shard selection, SQL/content matching (including the Korean/English fragment requirement), and context lookup, using `tests/fixtures.py`'s existing `seed_shard`/`make_message` helpers plus small additions where a filter needs data `seed_shard` doesn't already have.

---

### Task 1: Query tokenizer (pure, no DB)

**Files:**
- Create: `archiver/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Produces: `archiver.search.ParsedQuery` (dataclass: `text: str`, `filters: dict[str, list[str]]`) and `archiver.search.tokenize_query(raw: str) -> ParsedQuery`. Recognizes `key:value` tokens for keys in `{"from", "in", "during", "after", "before", "has", "file", "ext"}` (case-insensitive key); everything else is free text, joined with a single space into `text` (quoted phrases via `shlex.split` are already merged into one token before this joining happens, so a quoted phrase stays contiguous). A colon inside a value that isn't a recognized filter key (e.g. a URL-looking word) falls through to free text unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_search.py`:
```python
from archiver.search import ParsedQuery, tokenize_query


def test_tokenize_plain_text():
    result = tokenize_query("hello world")
    assert result.text == "hello world"
    assert result.filters == {}


def test_tokenize_quoted_phrase_stays_together():
    result = tokenize_query('"hello world" foo')
    assert result.text == "hello world foo"


def test_tokenize_single_filter():
    result = tokenize_query("hello from:alice")
    assert result.text == "hello"
    assert result.filters == {"from": ["alice"]}


def test_tokenize_multiple_filters_and_text():
    result = tokenize_query("hello in:general has:image from:Bob world")
    assert result.text == "hello world"
    assert result.filters == {"in": ["general"], "has": ["image"], "from": ["Bob"]}


def test_tokenize_filter_key_is_case_insensitive():
    result = tokenize_query("FROM:alice")
    assert result.filters == {"from": ["alice"]}


def test_tokenize_unknown_key_colon_is_free_text():
    result = tokenize_query("see https://example.com/path")
    assert result.text == "see https://example.com/path"
    assert result.filters == {}


def test_tokenize_repeated_filter_key_accumulates():
    result = tokenize_query("in:general in:random")
    assert result.filters == {"in": ["general", "random"]}


def test_tokenize_empty_query():
    result = tokenize_query("")
    assert result.text == ""
    assert result.filters == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.search'`.

- [ ] **Step 3: Write `archiver/search.py`**

```python
"""Search: query tokenizing, filter resolution, candidate-shard
selection, and result formatting for the `archive find` CLI command.
No discord.py import in this module -- search only ever reads
already-archived data (spec §7)."""
import shlex
from dataclasses import dataclass, field

_FILTER_KEYS = {"from", "in", "during", "after", "before", "has", "file", "ext"}


@dataclass
class ParsedQuery:
    text: str
    filters: dict[str, list[str]] = field(default_factory=dict)


def tokenize_query(raw: str) -> ParsedQuery:
    tokens = shlex.split(raw) if raw.strip() else []
    text_words: list[str] = []
    filters: dict[str, list[str]] = {}
    for token in tokens:
        key, sep, value = token.partition(":")
        if sep and value and key.lower() in _FILTER_KEYS:
            filters.setdefault(key.lower(), []).append(value)
        else:
            text_words.append(token)
    return ParsedQuery(text=" ".join(text_words), filters=filters)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/search.py tests/test_search.py
git commit -m "feat: search query tokenizer (filter grammar parsing)"
```

---

### Task 2: Filter resolution (needs catalog)

**Files:**
- Modify: `archiver/search.py` (append)
- Modify: `tests/test_search.py` (append)

**Interfaces:**
- Consumes: `ParsedQuery` (Task 1); `archiver.db.connect_catalog` (existing); the `channels`, `users`, `user_nicknames` catalog tables (existing schema, `archiver/db.py`).
- Produces: `archiver.search.ResolvedQuery` (dataclass: `text: str`, `channel_ids: list[str] | None`, `author_ids: list[str] | None`, `after_utc: str | None`, `before_utc: str | None`, `has: set[str]`, `file_substr: str | None`, `ext: str | None`) and `archiver.search.resolve_query(catalog_conn, parsed: ParsedQuery) -> ResolvedQuery`. `channel_ids`/`author_ids` of `None` means "no restriction"; an empty list `[]` means "the filter matched nobody" (a real, distinct state from "no filter given" — callers must treat `[]` as "return zero results", not "no restriction").

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py`:
```python
from datetime import datetime, timezone

from archiver.db import connect_catalog
from archiver.search import resolve_query


def _seed_catalog(catalog):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'random-chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('20', 'alice', ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO user_nicknames (user_id, nickname, observed_utc) VALUES ('20', 'ally', ?)",
        (now,),
    )
    catalog.commit()


def test_resolve_in_filter_matches_channel_by_name_substring(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("in:general"))
    assert resolved.channel_ids == ["10"]


def test_resolve_in_filter_by_numeric_id_bypasses_name_lookup(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("in:10"))
    assert resolved.channel_ids == ["10"]


def test_resolve_in_filter_no_match_gives_empty_list_not_none(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("in:nonexistent"))
    assert resolved.channel_ids == []


def test_resolve_no_in_filter_gives_none(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("hello"))
    assert resolved.channel_ids is None


def test_resolve_from_filter_matches_username_or_nickname(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    by_username = resolve_query(catalog, tokenize_query("from:alice"))
    by_nickname = resolve_query(catalog, tokenize_query("from:ally"))
    assert by_username.author_ids == ["20"]
    assert by_nickname.author_ids == ["20"]


def test_resolve_during_month_gives_seoul_month_bounds_in_utc(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("during:2025-10"))
    # 2025-10-01T00:00:00 KST == 2025-09-30T15:00:00 UTC
    assert resolved.after_utc == "2025-09-30T15:00:00Z"
    assert resolved.before_utc == "2025-10-31T15:00:00Z"


def test_resolve_during_day_gives_seoul_day_bounds_in_utc(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("during:2025-10-15"))
    assert resolved.after_utc == "2025-10-14T15:00:00Z"
    assert resolved.before_utc == "2025-10-15T15:00:00Z"


def test_resolve_has_filter_keeps_only_known_values(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("has:image has:bogus"))
    assert resolved.has == {"image"}


def test_resolve_file_and_ext_filters(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("file:report ext:.PDF"))
    assert resolved.file_substr == "report"
    assert resolved.ext == "pdf"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_query'`.

- [ ] **Step 3: Append to `archiver/search.py`**

```python
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
_HAS_VALUES = {"image", "video", "audio", "embed", "poll", "sticker", "reaction", "attachment"}
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class ResolvedQuery:
    text: str
    channel_ids: list | None
    author_ids: list | None
    after_utc: str | None
    before_utc: str | None
    has: set
    file_substr: str | None
    ext: str | None


def _day_or_month_bounds_utc(date_str: str) -> tuple[str, str]:
    """One Asia/Seoul calendar day (YYYY-MM-DD) or whole month (YYYY-MM)
    as a [start, end) UTC instant pair, ISO-formatted to match
    created_utc's own format."""
    if len(date_str) == 7:
        start_local = datetime.strptime(date_str, "%Y-%m").replace(tzinfo=SEOUL)
        end_local = (
            start_local.replace(year=start_local.year + 1, month=1)
            if start_local.month == 12
            else start_local.replace(month=start_local.month + 1)
        )
    else:
        start_local = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=SEOUL)
        end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).strftime(_ISO_FMT),
        end_local.astimezone(timezone.utc).strftime(_ISO_FMT),
    )


def _resolve_channel_ids(catalog_conn, values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value.isdigit():
            ids.append(value)
        else:
            rows = catalog_conn.execute(
                "SELECT id FROM channels WHERE LOWER(name) LIKE ?", (f"%{value.lower()}%",)
            ).fetchall()
            ids.extend(r["id"] for r in rows)
    return ids


def _resolve_author_ids(catalog_conn, values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value.isdigit():
            ids.append(value)
        else:
            pattern = f"%{value.lower()}%"
            rows = catalog_conn.execute(
                "SELECT id FROM users WHERE LOWER(username) LIKE ? "
                "UNION SELECT user_id AS id FROM user_nicknames WHERE LOWER(nickname) LIKE ?",
                (pattern, pattern),
            ).fetchall()
            ids.extend(r["id"] for r in rows)
    return ids


def resolve_query(catalog_conn, parsed: ParsedQuery) -> ResolvedQuery:
    filters = parsed.filters

    channel_ids = _resolve_channel_ids(catalog_conn, filters["in"]) if "in" in filters else None
    author_ids = _resolve_author_ids(catalog_conn, filters["from"]) if "from" in filters else None

    after_utc = before_utc = None
    if "during" in filters:
        after_utc, before_utc = _day_or_month_bounds_utc(filters["during"][-1])
    if "after" in filters:
        after_utc, _ = _day_or_month_bounds_utc(filters["after"][-1])
    if "before" in filters:
        _, before_utc = _day_or_month_bounds_utc(filters["before"][-1])

    has = {v.lower() for v in filters.get("has", []) if v.lower() in _HAS_VALUES}

    return ResolvedQuery(
        text=parsed.text,
        channel_ids=channel_ids,
        author_ids=author_ids,
        after_utc=after_utc,
        before_utc=before_utc,
        has=has,
        file_substr=filters["file"][-1] if "file" in filters else None,
        ext=filters["ext"][-1].lstrip(".").lower() if "ext" in filters else None,
    )
```

Add `from archiver.search import tokenize_query, resolve_query` (extend the existing import line) at the top of `tests/test_search.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS (17 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/search.py tests/test_search.py
git commit -m "feat: resolve search filters against the catalog (channels, authors, dates)"
```

---

### Task 3: Candidate shard selection

**Files:**
- Modify: `archiver/search.py` (append)
- Modify: `tests/test_search.py` (append)

**Interfaces:**
- Consumes: `ResolvedQuery` (Task 2); `archiver.store.month_bucket` (existing, Stage 3); the `channel_month_shard` catalog table (existing schema).
- Produces: `archiver.search.candidate_shards(catalog_conn, resolved: ResolvedQuery) -> list[tuple[str, str]]` — list of `(channel_id, shard_path)` pairs, narrowed by `channel_ids` (if restricted) and by month range (if `after_utc`/`before_utc` given), without opening any shard file.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py`:
```python
from archiver.search import ResolvedQuery, candidate_shards


def _seed_shard_rows(catalog):
    now = "2025-10-15T00:00:00Z"
    for cid in ("10", "11"):
        catalog.execute(
            "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
            "first_seen_utc, last_seen_utc) VALUES (?, 'chan', 'text', NULL, NULL, 0, ?, ?)",
            (cid, now, now),
        )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-09', 'uncategorized', 'a/2025-09.sqlite')"
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'a/2025-10.sqlite')"
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('11', '2025-10', 'uncategorized', 'b/2025-10.sqlite')"
    )
    catalog.commit()


def _empty_resolved(**overrides) -> ResolvedQuery:
    base = dict(text="", channel_ids=None, author_ids=None, after_utc=None,
                before_utc=None, has=set(), file_substr=None, ext=None)
    base.update(overrides)
    return ResolvedQuery(**base)


def test_candidate_shards_no_filters_returns_everything(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    result = candidate_shards(catalog, _empty_resolved())
    assert sorted(result) == [
        ("10", "a/2025-09.sqlite"), ("10", "a/2025-10.sqlite"), ("11", "b/2025-10.sqlite"),
    ]


def test_candidate_shards_restricted_to_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    result = candidate_shards(catalog, _empty_resolved(channel_ids=["11"]))
    assert result == [("11", "b/2025-10.sqlite")]


def test_candidate_shards_empty_channel_list_returns_nothing(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    result = candidate_shards(catalog, _empty_resolved(channel_ids=[]))
    assert result == []


def test_candidate_shards_restricted_by_month_range(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    resolved = _empty_resolved(after_utc="2025-10-01T00:00:00Z", before_utc="2025-10-31T23:00:00Z")
    result = candidate_shards(catalog, resolved)
    assert sorted(result) == [("10", "a/2025-10.sqlite"), ("11", "b/2025-10.sqlite")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'candidate_shards'`.

- [ ] **Step 3: Append to `archiver/search.py`**

```python
from archiver.store import month_bucket


def candidate_shards(catalog_conn, resolved: ResolvedQuery) -> list[tuple[str, str]]:
    """(channel_id, shard_path) pairs to search, narrowed by channel and
    month-range filters via the indexed channel_month_shard table --
    before any shard file is opened (spec §7.1: filter on indexed
    columns first, only then touch content)."""
    if resolved.channel_ids == []:
        return []

    sql = "SELECT channel_id, yyyymm, shard_path FROM channel_month_shard"
    params: list = []
    if resolved.channel_ids is not None:
        sql += f" WHERE channel_id IN ({','.join('?' for _ in resolved.channel_ids)})"
        params.extend(resolved.channel_ids)
    rows = catalog_conn.execute(sql, params).fetchall()

    after_month = (
        month_bucket(datetime.strptime(resolved.after_utc, _ISO_FMT).replace(tzinfo=timezone.utc))
        if resolved.after_utc else None
    )
    before_month = (
        month_bucket(
            datetime.strptime(resolved.before_utc, _ISO_FMT).replace(tzinfo=timezone.utc)
            - timedelta(seconds=1)
        )
        if resolved.before_utc else None
    )

    result = []
    for row in rows:
        if after_month and row["yyyymm"] < after_month:
            continue
        if before_month and row["yyyymm"] > before_month:
            continue
        result.append((row["channel_id"], row["shard_path"]))
    return result
```

Add `from archiver.db import connect_catalog` is already imported; add `candidate_shards, ResolvedQuery` to the existing `from archiver.search import ...` line in the test file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS (21 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/search.py tests/test_search.py
git commit -m "feat: narrow search to candidate shards via channel_month_shard"
```

---

### Task 4: Per-shard SQL construction and the `search()` orchestrator

**Files:**
- Modify: `archiver/search.py` (append)
- Modify: `tests/test_search.py` (append)

**Interfaces:**
- Consumes: `ResolvedQuery` (Task 2), `candidate_shards` (Task 3), `archiver.db.connect_shard` (existing), `archiver.fts.ensure_messages_fts` (existing, Stage 1 — must be called per opened shard here too, since `search()` opens shards directly rather than through `ShardStore`).
- Produces: `archiver.search.build_message_sql(resolved: ResolvedQuery, channel_id: str, has_fts: bool) -> tuple[str, list]` and `archiver.search.search(catalog_conn, data_dir: Path, raw_query: str) -> list[dict]`. Each result dict is a full `messages` row (as a plain dict, via `dict(row)`) plus a `shard_path` key.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py`:
```python
from pathlib import Path

from archiver.db import connect_shard
from archiver.search import build_message_sql, search
from tests.fixtures import make_message, make_attachment, _insert


def _write_shard(path: Path, messages_and_extras=()) -> None:
    conn = connect_shard(path)
    for msg, extra in messages_and_extras:
        _insert(conn, "messages", msg)
        for table, row in extra:
            _insert(conn, table, row)
    conn.commit()
    conn.close()


def test_build_message_sql_matches_korean_fragment_via_fts():
    resolved = ResolvedQuery(text="반가워", channel_ids=None, author_ids=None,
                              after_utc=None, before_utc=None, has=set(),
                              file_substr=None, ext=None)
    sql, params = build_message_sql(resolved, "10", has_fts=True)
    assert "messages_fts" in sql
    assert "MATCH" in sql


def test_build_message_sql_short_query_uses_like_even_with_fts():
    resolved = ResolvedQuery(text="ab", channel_ids=None, author_ids=None,
                              after_utc=None, before_utc=None, has=set(),
                              file_substr=None, ext=None)
    sql, params = build_message_sql(resolved, "10", has_fts=True)
    assert "LIKE" in sql
    assert "messages_fts" not in sql


def test_search_end_to_end_english_fragment(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="the quick brown fox", channel_id="10")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "brown")
    assert len(results) == 1
    assert results[0]["id"] == msg["id"]


def test_search_end_to_end_korean_fragment_mid_word(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="안녕하세요 반갑습니다", channel_id="10")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "갑습")  # mid-word fragment, 3 chars
    assert len(results) == 1
    assert results[0]["id"] == msg["id"]


def test_search_has_image_filter(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    with_img = make_message(content="", channel_id="10")
    without_img = make_message(content="", channel_id="10")
    _write_shard(shard_path, [
        (with_img, [("attachments", make_attachment(with_img["id"]))]),
        (without_img, []),
    ])

    results = search(catalog, tmp_path, "has:image")
    assert [r["id"] for r in results] == [with_img["id"]]


def test_search_from_filter_with_no_matching_author_returns_nothing(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    _write_shard(shard_path, [(make_message(channel_id="10"), [])])

    results = search(catalog, tmp_path, "from:nobody")
    assert results == []


def test_search_excludes_deleted_messages(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    deleted = make_message(content="gone fragment", channel_id="10", deleted_utc=now)
    _write_shard(shard_path, [(deleted, [])])

    results = search(catalog, tmp_path, "fragment")
    assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_message_sql'`.

- [ ] **Step 3: Append to `archiver/search.py`**

```python
from pathlib import Path

from archiver.db import connect_shard
from archiver.fts import ensure_messages_fts


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


_HAS_TABLE_EXISTS = {"attachment": "attachments", "embed": "embeds",
                      "poll": "polls", "sticker": "stickers", "reaction": "reactions"}
_HAS_CONTENT_TYPE = {"image", "video", "audio"}


def build_message_sql(resolved: ResolvedQuery, channel_id: str, has_fts: bool) -> tuple[str, list]:
    where = ["m.channel_id = ?", "m.deleted_utc IS NULL"]
    params: list = [channel_id]

    if resolved.author_ids is not None:
        if not resolved.author_ids:
            return "SELECT m.* FROM messages m WHERE 0", []
        where.append(f"m.author_id IN ({','.join('?' for _ in resolved.author_ids)})")
        params.extend(resolved.author_ids)

    if resolved.after_utc:
        where.append("m.created_utc >= ?")
        params.append(resolved.after_utc)
    if resolved.before_utc:
        where.append("m.created_utc < ?")
        params.append(resolved.before_utc)

    for value in resolved.has:
        if value in _HAS_CONTENT_TYPE:
            where.append("EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id "
                          "AND a.content_type LIKE ?)")
            params.append(f"{value}/%")
        else:
            table = _HAS_TABLE_EXISTS[value]
            where.append(f"EXISTS (SELECT 1 FROM {table} t WHERE t.message_id = m.id)")

    if resolved.file_substr:
        where.append("EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id "
                      "AND a.filename LIKE ? ESCAPE '\\')")
        params.append(f"%{_escape_like(resolved.file_substr)}%")
    if resolved.ext:
        where.append("EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id "
                      "AND a.filename LIKE ? ESCAPE '\\')")
        params.append(f"%.{_escape_like(resolved.ext)}")

    text = resolved.text.strip()
    if text:
        if has_fts and len(text) >= 3:
            where.append("m.id IN (SELECT message_id FROM messages_fts WHERE messages_fts MATCH ?)")
            params.append(_fts_phrase(text))
        else:
            where.append("m.content LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(text)}%")

    sql = f"SELECT m.* FROM messages m WHERE {' AND '.join(where)} ORDER BY m.id"
    return sql, params


def search(catalog_conn, data_dir: Path, raw_query: str) -> list[dict]:
    parsed = tokenize_query(raw_query)
    resolved = resolve_query(catalog_conn, parsed)
    if resolved.channel_ids == [] or resolved.author_ids == []:
        return []

    results = []
    for channel_id, shard_path in candidate_shards(catalog_conn, resolved):
        shard_conn = connect_shard(data_dir / shard_path)
        try:
            has_fts = ensure_messages_fts(shard_conn)
            sql, params = build_message_sql(resolved, channel_id, has_fts)
            for row in shard_conn.execute(sql, params).fetchall():
                entry = dict(row)
                entry["shard_path"] = shard_path
                results.append(entry)
        finally:
            shard_conn.close()

    results.sort(key=lambda r: int(r["id"]))
    return results
```

Add `build_message_sql, search` (and `from tests.fixtures import make_message, make_attachment, _insert`, `from pathlib import Path`) to the test file's imports as shown in Step 1.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS (27 tests). If the Korean-fragment test fails specifically because this Python build's SQLite lacks the trigram tokenizer, that's expected per spec §7.1 (the LIKE fallback still must match it correctly — investigate a failure here as a real bug, not an environment quirk, since LIKE has no such limitation).

- [ ] **Step 5: Commit**

```bash
git add archiver/search.py tests/test_search.py
git commit -m "feat: per-shard search query and end-to-end search() orchestrator"
```

---

### Task 5: Context lookup and result formatting

**Files:**
- Modify: `archiver/search.py` (append)
- Modify: `tests/test_search.py` (append)

**Interfaces:**
- Consumes: `search()` result rows (Task 4); `channel_month_shard` (existing schema).
- Produces: `archiver.search.get_context(catalog_conn, data_dir: Path, channel_id: str, message_id: str, n: int) -> tuple[list[dict], list[dict]]` (before, after — each a list of full message-row dicts, id-ordered, crossing month-shard boundaries) and `archiver.search.format_result(catalog_conn, row: dict, *, full: bool = False) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py`:
```python
from archiver.search import format_result, get_context


def test_get_context_crosses_month_boundary(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-09', 'uncategorized', 'uncategorized/general/2025-09.sqlite')"
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()

    sep_path = tmp_path / "uncategorized" / "general" / "2025-09.sqlite"
    oct_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    sep_path.parent.mkdir(parents=True)

    before1 = make_message(id="1001", content="before two", channel_id="10")
    before2 = make_message(id="1002", content="before one", channel_id="10")
    target = make_message(id="1003", content="target message", channel_id="10")
    after1 = make_message(id="1004", content="after one", channel_id="10")
    _write_shard(sep_path, [(before1, []), (before2, [])])
    _write_shard(oct_path, [(target, []), (after1, [])])

    before, after = get_context(catalog, tmp_path, "10", "1003", 2)
    assert [m["id"] for m in before] == ["1001", "1002"]
    assert [m["id"] for m in after] == ["1004"]


def test_format_result_truncates_by_default(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    row = make_message(content="x" * 300, channel_id="10", created_utc=now)
    line = format_result(catalog, row)
    assert len(line) < 300
    assert "general" in line

    full_line = format_result(catalog, row, full=True)
    assert "x" * 300 in full_line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_context'`.

- [ ] **Step 3: Append to `archiver/search.py`**

```python
def get_context(catalog_conn, data_dir: Path, channel_id: str, message_id: str,
                 n: int) -> tuple[list[dict], list[dict]]:
    """Messages immediately before/after `message_id` in `channel_id`,
    by id order, crossing month-shard boundaries as needed."""
    shard_rows = catalog_conn.execute(
        "SELECT yyyymm, shard_path FROM channel_month_shard WHERE channel_id=? ORDER BY yyyymm",
        (channel_id,),
    ).fetchall()

    id_index: list[tuple[str, str]] = []  # (message_id, shard_path), sorted by id
    for row in shard_rows:
        conn = connect_shard(data_dir / row["shard_path"])
        try:
            ids = conn.execute(
                "SELECT id FROM messages WHERE channel_id=? AND deleted_utc IS NULL ORDER BY id",
                (channel_id,),
            ).fetchall()
        finally:
            conn.close()
        id_index.extend((r["id"], row["shard_path"]) for r in ids)
    id_index.sort(key=lambda t: int(t[0]))

    idx = next((i for i, (mid, _) in enumerate(id_index) if mid == message_id), None)
    if idx is None:
        return [], []

    def _fetch(refs: list[tuple[str, str]]) -> list[dict]:
        by_shard: dict[str, list[str]] = {}
        for mid, shard_path in refs:
            by_shard.setdefault(shard_path, []).append(mid)
        out: list[dict] = []
        for shard_path, ids in by_shard.items():
            conn = connect_shard(data_dir / shard_path)
            try:
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({placeholders})", ids
                ).fetchall()
                out.extend(dict(r) for r in rows)
            finally:
                conn.close()
        out.sort(key=lambda r: int(r["id"]))
        return out

    before = _fetch(id_index[max(0, idx - n):idx])
    after = _fetch(id_index[idx + 1:idx + 1 + n])
    return before, after


def format_result(catalog_conn, row: dict, *, full: bool = False) -> str:
    channel = catalog_conn.execute(
        "SELECT name FROM channels WHERE id=?", (row["channel_id"],)
    ).fetchone()
    channel_name = channel["name"] if channel else row["channel_id"]
    author = catalog_conn.execute(
        "SELECT username FROM users WHERE id=?", (row["author_id"],)
    ).fetchone()
    author_name = author["username"] if author else row["author_id"]
    content = row["content"]
    if not full and len(content) > 200:
        content = content[:200] + "..."
    return f"[{row['created_utc']}] #{channel_name} {author_name}: {content}"
```

Add `format_result, get_context` to the test file's `from archiver.search import ...` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS (29 tests).

- [ ] **Step 5: Commit**

```bash
git add archiver/search.py tests/test_search.py
git commit -m "feat: cross-shard context lookup and terminal result formatting"
```

---

### Task 6: `find` CLI command (with interactive loop)

**Files:**
- Modify: `archiver/cli.py` (append)
- Test: none (thin CLI glue over the already-tested `archiver/search.py` functions — same documented pattern as this project's other CLI-wiring tasks; the argparse wiring itself is checked by a bad-flag smoke test, not full pytest coverage)

**Interfaces:**
- Consumes: `tokenize_query`, `resolve_query`, `search`, `get_context`, `format_result` (Tasks 1-5, `archiver.search`); `connect_catalog` (existing).
- Produces: `archiver.cli._run_find(config, raw_query: str, *, full: bool = False, context: int = 0, json_output: bool = False) -> int`, `archiver.cli._run_find_interactive(config) -> int`, and a `find` subcommand on `main()`. Synchronous — no `discord.Client`, no `asyncio.run` needed for this command.

- [ ] **Step 1: Append to `archiver/cli.py`**

Add these imports at the top (alongside the existing ones):
```python
import json
import shlex

from archiver.search import format_result, get_context, search
```

Append (after `_run_live`, before `main()`):

```python
def _run_find(config, raw_query: str, *, full: bool = False, context: int = 0,
               json_output: bool = False) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    try:
        results = search(catalog_conn, config.data_dir, raw_query)
    except ValueError as e:
        print(f"invalid query: {e}")
        return 1

    if json_output:
        payload = []
        for row in results:
            entry = dict(row)
            if context:
                before, after = get_context(catalog_conn, config.data_dir,
                                             row["channel_id"], row["id"], context)
                entry["context_before"] = before
                entry["context_after"] = after
            payload.append(entry)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if not results:
        print("No results.")
        return 0
    for row in results:
        print(format_result(catalog_conn, row, full=full))
        if context:
            before, after = get_context(catalog_conn, config.data_dir,
                                         row["channel_id"], row["id"], context)
            for b in before:
                print(f"    {format_result(catalog_conn, b, full=full)}")
            print("    --- match ---")
            for a in after:
                print(f"    {format_result(catalog_conn, a, full=full)}")
    return 0


def _run_find_interactive(config) -> int:
    print("Interactive search. Enter a query, or leave blank / Ctrl-D to exit.")
    while True:
        try:
            raw = input("search> ")
        except EOFError:
            print()
            return 0
        if not raw.strip():
            return 0
        _run_find(config, raw)
```

Modify `main()`'s subparser setup to add, alongside `doctor`/`coverage`/`backfill`/`live`:
```python
    find_parser = subparsers.add_parser("find")
    find_parser.add_argument("query", nargs="*")
    find_parser.add_argument("--full", action="store_true")
    find_parser.add_argument("--context", type=int, default=0)
    find_parser.add_argument("--json", action="store_true", dest="json_output")
```

And add a dispatch branch alongside the existing ones (note: this one does NOT go through `asyncio.run` — `_run_find`/`_run_find_interactive` are plain sync functions):
```python
    if args.command == "find":
        if not args.query:
            return _run_find_interactive(config)
        raw_query = shlex.join(args.query)
        return _run_find(config, raw_query, full=args.full, context=args.context,
                          json_output=args.json_output)
```

- [ ] **Step 2: Verify argparse wiring**

Run: `python -m archiver.cli find --bogus-flag`
Expected: argparse rejects `--bogus-flag` specifically (not `find` itself as an unrecognized subcommand) — confirms the subcommand exists.

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: all prior tests plus Tasks 1-5's still pass; nothing new fails (Task 6 adds no automated tests of its own).

- [ ] **Step 4: Commit**

```bash
git add archiver/cli.py
git commit -m "feat: add find CLI command (search with filters, context, JSON, interactive mode)"
```

---

### Task 7: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-6 passes, plus all prior stages' tests still pass (no regressions).

- [ ] **Step 2: Note what's NOT covered by the automated suite**

`_run_find_interactive`'s `input()` loop is not unit-tested by design (interactive stdin glue, same documented-gap pattern as this project's other CLI entry points). Before Stage 5 is considered fully proven: run `python -m archiver.cli find` against the real archived data (`config.json` already points at the real `data_dir`) with a handful of real queries — a plain substring, a `from:`/`in:`/`during:` combination, a `has:image` filter, and `--context 2` on a hit near a month boundary — and eyeball that the results look right. This is real, hands-on verification of the kind that already caught production bugs earlier in this project that no unit test surfaced on its own.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

If clean, no commit needed — Task 7 is verification only.

---

## Self-Review Notes

- Spec §7's filter grammar (`from:`, `in:`, `during:`, `after:`, `before:`, `has:`, `file:`, `ext:`, quoted phrases via `shlex`) is fully covered across Tasks 1-2.
- Spec §7.1's length-based routing (trigram MATCH for length >= 3 with trigram available, LIKE fallback otherwise) is implemented in Task 4's `build_message_sql` and directly tested by the Korean/English fragment tests in Task 4 and the length-based routing test in the same task.
- Spec §7.1's requirement that filters narrow the candidate set via indexed columns before any content scan is satisfied structurally: `candidate_shards` (Task 3) never opens a shard file, and `build_message_sql` (Task 4) puts the channel/author/date/has predicates in the same WHERE clause as the content predicate, so SQLite's own query planner narrows on the indexed/EXISTS predicates before evaluating LIKE — the fallback's documented O(n) cost is over the already-filtered result set, not the whole shard.
- `--context N` crossing month-shard boundaries is covered by Task 5's dedicated cross-boundary test.
- `--full`/`--json` are both wired in Task 6; `--json`'s per-result `context_before`/`context_after` keys are included when `--context` is passed alongside `--json`.
- The interactive `archive find` loop (no query given) is in Task 6.
- Deliberately out of scope for this plan (not in spec §7, not requested): typo tolerance/fuzzy matching (spec explicitly says if built, it must stay a separate opt-in — not building it now, nothing here blocks adding it later), ranking/relevance scoring (results are chronological by message id, matching how every other list in this project is ordered), pagination beyond `--full` (a `head`/`less`-style pipe covers this for a CLI tool; no need to reinvent it).

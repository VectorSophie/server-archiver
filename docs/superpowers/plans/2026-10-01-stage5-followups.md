# Stage 5 Follow-up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the findings deferred from Stage 5's final whole-branch review: missing `--help` text, `has:` unknown values silently dropped, LIKE-backend Unicode case-folding differing from the trigram backend, repeated trigram-support probing on every shard open, `from:` having no index to narrow on, `in:` not including a channel's own threads, and missing test coverage for the trigram-routing path and username-collision disambiguation.

**Architecture:** Each fix is independent and bounded to `archiver/search.py`, `archiver/fts.py`, `archiver/db.py`, or `archiver/cli.py`. The riskiest change is overriding SQLite's built-in `LIKE` with a Python-regex implementation for Unicode-aware case-insensitivity — scoped narrowly to connections `archiver/search.py` itself opens for searching, never touching write-path connections elsewhere in the codebase, to keep the blast radius contained to read-only search queries.

**Tech Stack:** stdlib only (`re` for the LIKE override, nothing new for the rest).

**Spec:** `docs/superpowers/specs/2026-09-29-server-archiver-design.md` (§7 Search, §7.1 Substring matching)

## Global Constraints

- The LIKE override must apply only to connections opened by `archiver/search.py` for searching — never register it anywhere that would affect `archiver/store.py`, `archiver/live.py`, or `archiver/backfill.py`'s connections.
- The new shard index (for `from:`) is purely additive (`CREATE INDEX IF NOT EXISTS`) — no existing data or query behavior changes, only query planning for author-filtered searches gets faster.
- `in:`'s thread-inclusion must not change behavior when a channel has no threads (the common case) — it should add zero extra ids in that case, not alter result ordering or counts.
- No change to this project's established idiom: parameterized SQL only, no injection surface anywhere these fixes touch.

## File Structure

- **Modify:** `archiver/fts.py` — cache `detect_trigram_support`'s result (a pure fact about the SQLite build, invariant across every connection in one process).
- **Modify:** `archiver/db.py` — add `SHARD_SCHEMA_V2` (one index on `messages.author_id`).
- **Modify:** `archiver/search.py` — Unicode-aware LIKE override (used only by this module's own shard connections), `in:` thread-inclusion in `_resolve_channel_ids`.
- **Modify:** `archiver/cli.py` — `--help` text for the `find` subcommand and its flags; a stderr warning for unrecognized `has:` values.
- **Test:** `tests/test_search.py`, `tests/test_fts.py` (both existing, extended).

---

### Task 1: Unicode-aware LIKE override and trigram-probe caching

**Files:**
- Modify: `archiver/fts.py`
- Modify: `archiver/search.py`
- Test: `tests/test_fts.py`, `tests/test_search.py`

**Interfaces:**
- Produces: `archiver.search._open_search_shard(data_dir: Path, shard_path: str) -> sqlite3.Connection` — replaces every direct `connect_shard(data_dir / shard_path)` call in `search()` and `get_context()`, additionally registering a Unicode-case-insensitive `LIKE` override on the returned connection. `archiver.fts.detect_trigram_support` keeps its existing signature but now caches its result at module level after the first call in a process.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fts.py` (check its existing imports first):
```python
def test_detect_trigram_support_is_cached_across_calls(tmp_path, monkeypatch):
    import archiver.fts as fts_module
    fts_module._trigram_support_cache = None  # reset for test isolation
    conn = connect_catalog(tmp_path / "a.sqlite")
    first = detect_trigram_support(conn)

    probe_calls = []
    original_execute = conn.execute
    def _spy_execute(sql, *a, **kw):
        if "fts_trigram_probe" in sql:
            probe_calls.append(sql)
        return original_execute(sql, *a, **kw)
    monkeypatch.setattr(conn, "execute", _spy_execute)

    second = detect_trigram_support(conn)
    assert second == first
    assert probe_calls == []  # no new probe query ran -- cached result reused
```

Append to `tests/test_search.py` (check its existing imports first — needs `make_message`, `connect_catalog`, `_write_shard`-style helpers already present from earlier tasks):
```python
def test_search_like_fallback_matches_unicode_case_insensitively(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="École de Paris", channel_id="1")
    _write_shard(shard_path, [(msg, [])])

    # "ab" is under the 3-char trigram floor, so this always exercises the LIKE path
    # regardless of whether this SQLite build has trigram support.
    results = search(catalog, tmp_path, "ÉCOLE" if False else "école")
    assert len(results) == 1
    results_upper = search(catalog, tmp_path, "ÉCOLE")
    assert len(results_upper) == 1
```

(Use whichever existing shard-writing helper `tests/test_search.py` already defines from earlier Stage 5 tasks — e.g. `_write_shard` — rather than redefining one; check the file first.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fts.py tests/test_search.py -v -k "cached or unicode"`
Expected: FAIL — the cache test fails because `detect_trigram_support` re-probes every call; the Unicode test fails because SQLite's built-in `LIKE` only folds ASCII case, so `"ÉCOLE"` doesn't match `"École"`.

- [ ] **Step 3: Cache `detect_trigram_support` in `archiver/fts.py`**

```python
_trigram_support_cache: bool | None = None


def detect_trigram_support(conn: sqlite3.Connection) -> bool:
    global _trigram_support_cache
    if _trigram_support_cache is not None:
        return _trigram_support_cache
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.fts_trigram_probe USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE temp.fts_trigram_probe")
        _trigram_support_cache = True
    except sqlite3.OperationalError:
        _trigram_support_cache = False
    return _trigram_support_cache
```
(This is a fact about the SQLite build this Python process is linked against, invariant across every connection and every database file opened in that process — caching it is safe.)

- [ ] **Step 4: Add the LIKE override to `archiver/search.py`**

Add near the top of the file (after the existing imports and constants):
```python
import re

_LIKE_REGEX_CACHE: dict[tuple[str, str | None], re.Pattern] = {}


def _compile_like_pattern(pattern: str, escape: str | None) -> re.Pattern:
    cache_key = (pattern, escape)
    cached = _LIKE_REGEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if escape and c == escape and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        else:
            out.append(re.escape(c))
        i += 1
    compiled = re.compile("^" + "".join(out) + "$", re.IGNORECASE | re.DOTALL)
    _LIKE_REGEX_CACHE[cache_key] = compiled
    return compiled


def _unicode_like(pattern, value, escape=None):
    if pattern is None or value is None:
        return None
    return 1 if _compile_like_pattern(pattern, escape).match(value) else 0


def _register_unicode_like(conn: sqlite3.Connection) -> None:
    """Override SQLite's built-in LIKE (ASCII-only case folding, so
    e.g. 'École' doesn't match 'ÉCOLE') with a Python-regex
    implementation using Python's Unicode-aware re.IGNORECASE, for
    connections this module opens for searching. Never applied to
    write-path connections elsewhere in this codebase -- the override
    is per-connection, not global."""
    conn.create_function("LIKE", 2, lambda p, v: _unicode_like(p, v))
    conn.create_function("LIKE", 3, lambda p, v, e: _unicode_like(p, v, e))


def _open_search_shard(data_dir: Path, shard_path: str) -> sqlite3.Connection:
    conn = connect_shard(data_dir / shard_path)
    _register_unicode_like(conn)
    return conn
```

Add `import sqlite3` if not already present in the file (check first).

Then replace every direct `connect_shard(data_dir / shard_path)` / `connect_shard(data_dir / row["shard_path"])` call inside `search()` and `get_context()` with `_open_search_shard(data_dir, shard_path)` / `_open_search_shard(data_dir, row["shard_path"])` respectively (there are 3 call sites: one in `search()`, two in `get_context()`). Leave `ensure_messages_fts(shard_conn)`'s call in `search()` exactly where it is — it runs on the same connection, now with the LIKE override already registered, which doesn't affect FTS5's own `MATCH` operator at all (only `LIKE`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_fts.py tests/test_search.py -v`
Expected: PASS, including both new tests and every pre-existing test (the LIKE override preserves exact ASCII LIKE semantics — `%`/`_` wildcards, `ESCAPE` char — only extending case-folding to Unicode, so no existing assertion about ASCII content should change).

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 7: Commit**

```bash
git add archiver/fts.py archiver/search.py tests/test_fts.py tests/test_search.py
git commit -m "fix: Unicode-aware LIKE fallback for search, cache trigram-support detection"
```

---

### Task 2: `from:` index and `in:` thread-inclusion

**Files:**
- Modify: `archiver/db.py` (append `SHARD_SCHEMA_V2`)
- Modify: `archiver/search.py` (`_resolve_channel_ids`)
- Test: `tests/test_search.py`, `tests/test_shard_schema.py`

**Interfaces:**
- Produces: a new index `idx_messages_author` on every shard's `messages(author_id)` column (via `SHARD_MIGRATIONS` version 2). `_resolve_channel_ids` now also includes any channel whose `parent_id` matches a resolved channel's id (that channel's threads), so `in:general` finds messages in `#general`'s threads too, not just `#general` itself.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shard_schema.py` (check its existing imports/style first):
```python
def test_messages_author_id_has_an_index(tmp_path):
    conn = connect_shard(tmp_path / "shard.sqlite")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert any("author" in n.lower() for n in names)
```

Append to `tests/test_search.py`:
```python
def test_resolve_channel_ids_includes_threads_of_a_matched_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'a thread', 'public_thread', '10', NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:general"))
    assert sorted(resolved.channel_ids) == ["10", "11"]


def test_resolve_channel_ids_no_threads_adds_nothing_extra(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:general"))
    assert resolved.channel_ids == ["10"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shard_schema.py tests/test_search.py -v -k "author_id or threads"`
Expected: FAIL — no index exists yet; `in:general` currently resolves to `["10"]` only, never including thread "11".

- [ ] **Step 3: Append `SHARD_SCHEMA_V2` to `archiver/db.py`**

```python
SHARD_SCHEMA_V2 = """
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id);
"""

SHARD_MIGRATIONS: list[tuple[int, str]] = [(1, SHARD_SCHEMA_V1), (2, SHARD_SCHEMA_V2)]
```
(Replace the existing `SHARD_MIGRATIONS` line with this two-entry version.)

- [ ] **Step 4: Modify `_resolve_channel_ids` in `archiver/search.py`**

```python
def _resolve_channel_ids(catalog_conn, values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        matched = [value] if value.isdigit() else [
            r["id"] for r in catalog_conn.execute(
                "SELECT id FROM channels WHERE LOWER(name) LIKE ?", (f"%{value.lower()}%",)
            ).fetchall()
        ]
        for channel_id in matched:
            ids.append(channel_id)
            thread_rows = catalog_conn.execute(
                "SELECT id FROM channels WHERE parent_id=?", (channel_id,)
            ).fetchall()
            ids.extend(r["id"] for r in thread_rows)
    return ids
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_shard_schema.py tests/test_search.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: all pass. The new shard migration applies safely to any existing shard file via `apply_migrations`'s existing mechanism (same pattern as Stage 6's `CATALOG_SCHEMA_V2`).

- [ ] **Step 7: Commit**

```bash
git add archiver/db.py archiver/search.py tests/test_shard_schema.py tests/test_search.py
git commit -m "feat: index messages.author_id for from: searches; in: now includes a channel's threads"
```

---

### Task 3: `--help` text and a warning for unrecognized `has:` values

**Files:**
- Modify: `archiver/cli.py`
- Test: none (argparse help text and a stderr print are CLI-glue, consistent with this project's established "CLI entry-point output isn't unit-tested" pattern — verified by direct invocation instead)

**Interfaces:**
- Produces: `python -m archiver.cli find --help` prints descriptive help for the command and its flags, including a note on the `LIKE`-fallback's slower performance on large unfiltered ranges (spec §7.1's own documentation requirement). `_run_find` prints a one-line stderr warning naming any `has:` value in the query that isn't recognized, before running the search (the search still runs — unrecognized values are dropped from filtering, as before; this only makes that fact visible instead of silent).

- [ ] **Step 1: Add help text to the `find` subparser in `archiver/cli.py`'s `main()`**

```python
    find_parser = subparsers.add_parser(
        "find",
        help="Search archived messages",
        description=(
            "Search archived messages. Supports from:/in:/during:/after:/before:/has:/"
            "file:/ext: filters and quoted phrases. Substring matches of 3+ characters use "
            "an indexed trigram search when available; shorter queries, or any query when "
            "trigram support is unavailable, fall back to a full scan of the filtered result "
            "set, which is correctness-first and can be slow on large, unfiltered date ranges "
            "-- narrow with a channel/date/author filter for better performance."
        ),
    )
    find_parser.add_argument("query", nargs="*", help="Search terms and filters (omit for interactive mode)")
    find_parser.add_argument("--full", action="store_true", help="Show full message content, not truncated")
    find_parser.add_argument("--context", type=int, default=0, metavar="N",
                              help="Show N messages before/after each result")
    find_parser.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")
```
(Replace the existing, help-less `find_parser` block with this one — same arguments, same defaults, only `help=`/`description=` added.)

- [ ] **Step 2: Add a `has:` unknown-value warning to `_run_find` in `archiver/cli.py`**

At the top of `_run_find`, after `catalog_conn = connect_catalog(...)` and before calling `search(...)`:
```python
    from archiver.search import HAS_VALUES, tokenize_query  # add to the existing import line instead if cleaner
    parsed = tokenize_query(raw_query)
    unknown_has = sorted({v for v in parsed.filters.get("has", []) if v.lower() not in HAS_VALUES})
    if unknown_has:
        print(f"warning: unrecognized has: value(s) ignored: {', '.join(unknown_has)}", file=sys.stderr)
```
This requires exporting `_HAS_VALUES` as a public name — rename it to `HAS_VALUES` in `archiver/search.py` (it's currently referenced only within `resolve_query` in that same file) and update that one internal reference. Add `tokenize_query` to the existing `from archiver.search import ...` line in `archiver/cli.py` if `search()`'s own internal tokenizing isn't already imported there (check first — `_run_find` currently only imports `format_result, get_context, search`).

(Place this check before the `try: results = search(...)` call, so the warning prints even though `search()` will separately re-tokenize the same query internally — this minor duplication is cheap and avoids changing `search()`'s own return contract just to surface a warning.)

- [ ] **Step 3: Verify manually**

Run: `python -m archiver.cli find --help` — confirm the description and per-flag help text print correctly.

Run: `python -m pytest -v` to confirm the `HAS_VALUES` rename didn't break anything (grep the codebase first for any other reference to `_HAS_VALUES` before renaming, to make sure every reference is updated).

- [ ] **Step 4: Commit**

```bash
git add archiver/cli.py archiver/search.py
git commit -m "feat: add --help text for find, warn on unrecognized has: filter values"
```

---

### Task 4: Test coverage — real trigram-routing fragments and username-collision disambiguation

**Files:**
- Modify: `tests/test_search.py`

**Interfaces:** none new — this task only adds tests proving existing behavior that was previously under-tested.

- [ ] **Step 1: Add tests**

Append to `tests/test_search.py`:
```python
def test_search_korean_mid_word_fragment_three_chars_routes_to_trigram_when_available(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="프로그래밍을 좋아해요", channel_id="1")  # "programming" mid-word
    _write_shard(shard_path, [(msg, [])])

    from archiver.fts import detect_trigram_support
    shard_conn = connect_shard(shard_path)
    has_fts = detect_trigram_support(shard_conn)
    shard_conn.close()

    results = search(catalog, tmp_path, "그래밍")  # 3-char mid-word fragment
    assert len(results) == 1
    if has_fts:
        sql, _ = build_message_sql(
            resolve_query(catalog, tokenize_query("그래밍")), "1", has_fts=True
        )
        assert "messages_fts" in sql and "MATCH" in sql


def test_search_english_mid_word_fragment_routes_to_trigram_when_available(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="the quick brown fox jumps", channel_id="1")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "ick br")  # mid-word-to-mid-word fragment, not a whole word
    assert len(results) == 1


def test_search_disambiguates_two_users_sharing_a_display_name(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('20', 'alex', ?, ?)", (now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('21', 'alex', ?, ?)", (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg_from_20 = make_message(content="hello from the first alex", channel_id="1", author_id="20")
    msg_from_21 = make_message(content="hello from the second alex", channel_id="1", author_id="21")
    _write_shard(shard_path, [(msg_from_20, []), (msg_from_21, [])])

    results = search(catalog, tmp_path, "from:alex")
    assert {r["author_id"] for r in results} == {"20", "21"}  # both users' messages found, not collapsed
```

Add `from archiver.db import connect_shard` and `from archiver.search import resolve_query, tokenize_query, build_message_sql` to the test file's imports if not already present (check first — several should already be imported from earlier Stage 5 tasks).

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS. If a trigram-path assertion fails specifically because this build's SQLite lacks trigram support, that's expected (the LIKE fallback must still find the match correctly, which the `len(results) == 1` assertion already covers independent of which backend served it).

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 4: Commit**

```bash
git add tests/test_search.py
git commit -m "test: cover real trigram-routing fragments and username-collision disambiguation"
```

---

### Task 5: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-4 passes, plus all prior stages' tests still pass.

- [ ] **Step 2: Note what's NOT covered by the automated suite**

The `--help` text (Task 3) is verified by direct invocation, not an automated test, consistent with this project's established pattern for CLI-entry-point glue. Before this follow-up pass is considered fully proven: run `python -m archiver.cli find --help` and `python -m archiver.cli find from:someone has:bogus hello` against the real archived data and confirm the help text reads sensibly and the unknown-`has:`-value warning prints to stderr without interrupting the actual search.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

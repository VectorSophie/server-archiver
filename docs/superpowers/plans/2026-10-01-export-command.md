# Export Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `archive export <query...>` CLI command that reuses `find`'s exact filter grammar (`from:`/`in:`/`during:`/`after:`/`before:`/`has:`/`file:`/`ext:`, now also `category:`) but writes ALL matching results (no 500-result cap) to a file, in `txt`, `md`, or `json` format — for bulk data extraction rather than interactive search.

**Architecture:** `archiver.export.render_text`/`render_markdown` build on the already-existing `search()`/`format_result()` from `archiver/search.py` — `export` is a thin, uncapped wrapper around search, not a parallel implementation. A new `category:` filter key is added to `search.py`'s existing filter-resolution machinery (benefiting `find` too, not just `export`), resolving to every channel in a named (or numeric-id, or `uncategorized`) category, unioned with any `in:` filter's channel ids in the same query.

**Tech Stack:** stdlib only (`json` already used elsewhere in `cli.py`).

**Spec:** this extends spec §14's "Later" idea #2 (`archive export <channel> --format json|md`) — generalized from one channel to the full query filter grammar, per explicit user request.

## Global Constraints

- `export` must reuse `search()`/`format_result()` exactly as `find` does — no duplicated filter/matching logic.
- No new CLI-level cap: `export` calls `search()` with an effectively unlimited `limit` (the existing `limit` parameter already supports this — no change to `search()` itself needed).
- Output never goes to stdout by default (unlike `find`) — always written to a file, with a sensible auto-generated filename when `--output` isn't given.
- `category:` filter values resolve case-insensitive-substring against `category_names.name`, a numeric value as a literal `category_id`, and the literal value `uncategorized` as every channel with `category_id IS NULL` — matching the existing `in:`/`from:` filters' own numeric-id-or-substring convention.

## File Structure

- **Modify:** `archiver/search.py` — add `category:` to `_FILTER_KEYS`, a `_resolve_category_channel_ids` helper, and wire it into `resolve_query` (unioned with `in:`'s channel ids).
- **Create:** `archiver/export.py` — `render_text`, `render_markdown` (both take already-fetched result rows, matching `render_report`'s existing "pure rendering function, no I/O" convention in `archiver/reports.py`).
- **Modify:** `archiver/cli.py` — `_run_export`, `export` subcommand.
- **Test:** `tests/test_search.py`, `tests/test_export.py` (new).

---

### Task 1: `category:` filter

**Files:**
- Modify: `archiver/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Produces: `archiver.search._resolve_category_channel_ids(catalog_conn, values: list[str]) -> list[str]`. `resolve_query` now also consumes a `category` filter key, unioning its resolved channel ids into `channel_ids` alongside any `in:` results (so `in:general category:Gaming` finds messages in `#general` OR any channel under the `Gaming` category).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_search.py` (check its existing imports/fixtures first):
```python
def test_resolve_category_filter_matches_by_category_name_substring(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'Gaming Chat', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'games', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'other', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("category:gaming"))
    assert resolved.channel_ids == ["10"]


def test_resolve_category_filter_uncategorized_matches_null_category(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'other', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("category:uncategorized"))
    assert resolved.channel_ids == ["11"]


def test_resolve_in_and_category_filters_union_their_channel_ids(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'Gaming', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'games', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('20', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:general category:gaming"))
    assert sorted(resolved.channel_ids) == ["10", "20"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -v -k category_filter or in_and_category`
Expected: FAIL — `category` isn't a recognized filter key yet (falls into free text), `resolved.channel_ids` stays `None`.

- [ ] **Step 3: Modify `archiver/search.py`**

Add `"category"` to `_FILTER_KEYS`:
```python
_FILTER_KEYS = {"from", "in", "during", "after", "before", "has", "file", "ext", "category"}
```

Add `_resolve_category_channel_ids`, right after `_resolve_channel_ids`:
```python
def _resolve_category_channel_ids(catalog_conn, values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value.lower() == "uncategorized":
            rows = catalog_conn.execute("SELECT id FROM channels WHERE category_id IS NULL").fetchall()
            ids.extend(r["id"] for r in rows)
            continue
        category_ids = [value] if value.isdigit() else [
            r["category_id"] for r in catalog_conn.execute(
                "SELECT category_id FROM category_names WHERE LOWER(name) LIKE ? ESCAPE '\\'",
                (f"%{_escape_like(value.lower())}%",),
            ).fetchall()
        ]
        for category_id in category_ids:
            rows = catalog_conn.execute(
                "SELECT id FROM channels WHERE category_id=?", (category_id,)
            ).fetchall()
            ids.extend(r["id"] for r in rows)
    return ids
```

Modify `resolve_query`'s channel-id resolution:
```python
    channel_ids = None
    if "in" in filters or "category" in filters:
        channel_ids = []
        if "in" in filters:
            channel_ids.extend(_resolve_channel_ids(catalog_conn, filters["in"]))
        if "category" in filters:
            channel_ids.extend(_resolve_category_channel_ids(catalog_conn, filters["category"]))
```
(Replace the existing single-line `channel_ids = _resolve_channel_ids(...) if "in" in filters else None` with this block. Leave everything else in `resolve_query` unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS, including all pre-existing `in:`/`channel_ids` tests (confirm `in:` alone, with no `category:`, still behaves exactly as before).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 6: Commit**

```bash
git add archiver/search.py tests/test_search.py
git commit -m "feat: add category: search filter, resolving to every channel in a category"
```

---

### Task 2: Export rendering (txt, markdown)

**Files:**
- Create: `archiver/export.py`
- Test: `tests/test_export.py` (new)

**Interfaces:**
- Produces: `archiver.export.render_text(catalog_conn, results: list[dict]) -> str` (one `format_result(..., full=True)` line per message, chronological — reuses `archiver.search.format_result` exactly). `archiver.export.render_markdown(catalog_conn, results: list[dict]) -> str` (grouped under `## YYYY-MM-DD` Asia/Seoul-date headers, one list entry per message).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_export.py`:
```python
from archiver.db import connect_catalog
from archiver.export import render_text, render_markdown


def _seed(catalog):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) VALUES ('2', 'alice', ?, ?)",
        (now, now),
    )
    catalog.commit()


def _row(id, created_utc, content):
    return {
        "id": id, "channel_id": "1", "author_id": "2", "content": content,
        "created_utc": created_utc, "edited_utc": None, "reply_to_id": None,
        "mention_everyone": 0, "flags": 0, "message_type": 0, "deleted_utc": None,
    }


def test_render_text_one_line_per_message_in_order(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog)
    results = [
        _row("100", "2025-10-15T00:00:00Z", "hello"),
        _row("101", "2025-10-15T00:01:00Z", "world"),
    ]
    text = render_text(catalog, results)
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "hello" in lines[0]
    assert "world" in lines[1]
    assert "alice" in lines[0]


def test_render_markdown_groups_by_seoul_date(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog)
    results = [
        _row("100", "2025-10-14T16:00:00Z", "late night"),  # 2025-10-15 01:00 KST
        _row("101", "2025-10-15T01:00:00Z", "next day"),     # 2025-10-15 10:00 KST
    ]
    text = render_markdown(catalog, results)
    assert text.count("## 2025-10-15") == 1  # both messages fall on the same KST date -> one header
    assert "late night" in text
    assert "next day" in text
```
- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archiver.export'`.

- [ ] **Step 3: Write `archiver/export.py`**

```python
"""Bulk export rendering for the `archive export` CLI command. Thin
wrapper over archiver/search.py's search()/format_result() -- export
reuses the exact same filter grammar and matching logic as `find`,
just uncapped and written to a file instead of stdout."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from archiver.search import _ISO_FMT, format_result

SEOUL = ZoneInfo("Asia/Seoul")


def render_text(catalog_conn, results: list[dict]) -> str:
    return "\n".join(format_result(catalog_conn, row, full=True) for row in results)


def render_markdown(catalog_conn, results: list[dict]) -> str:
    lines: list[str] = []
    current_date = None
    for row in results:
        local = (
            datetime.strptime(row["created_utc"], _ISO_FMT)
            .replace(tzinfo=timezone.utc)
            .astimezone(SEOUL)
        )
        date_str = local.strftime("%Y-%m-%d")
        if date_str != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"## {date_str}")
            lines.append("")
            current_date = date_str
        lines.append(f"- {format_result(catalog_conn, row, full=True)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_export.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 6: Commit**

```bash
git add archiver/export.py tests/test_export.py
git commit -m "feat: export rendering (plain text and markdown, grouped by Seoul date)"
```

---

### Task 3: `export` CLI command

**Files:**
- Modify: `archiver/cli.py`
- Test: none (thin CLI glue over already-tested `search()`/`render_text`/`render_markdown`, same documented pattern as `find`'s own CLI wiring)

**Interfaces:**
- Produces: `archiver.cli._run_export(config, raw_query: str, *, fmt: str = "txt", output: str | None = None) -> int` and an `export` subcommand on `main()`. Synchronous, no `discord.Client`, same as `find`.

- [ ] **Step 1: Append to `archiver/cli.py`**

Add to the existing `from archiver.search import ...` line: `search` (if not already imported — check first, `find`'s own wiring likely already imports it). Add a new import line:
```python
from archiver.export import render_markdown, render_text
```

Append (after `_run_report`, before `main()`):
```python
def _run_export(config, raw_query: str, *, fmt: str = "txt", output: str | None = None) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    try:
        results = search(catalog_conn, config.data_dir, raw_query, limit=10**9)
        if not results:
            print("No results.")
            return 0

        if fmt == "json":
            text = json.dumps(results, ensure_ascii=False, indent=2, default=str)
        elif fmt == "md":
            text = render_markdown(catalog_conn, results)
        else:
            text = render_text(catalog_conn, results)

        if output:
            out_path = Path(output)
        else:
            safe_query = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw_query).strip("_") or "export"
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_path = Path(f"export-{safe_query}-{timestamp}.{fmt}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Exported {len(results)} message(s) to {out_path}")
        return 0
    finally:
        catalog_conn.close()
```
(Add `from datetime import datetime` to the existing imports if not already present — check first, `_run_report` likely doesn't need it but another part of the file might.)

Modify `main()`'s subparser setup to add, alongside `find`/`report`:
```python
    export_parser = subparsers.add_parser(
        "export",
        help="Export search results to a file (no result cap, unlike find)",
        description=(
            "Export every message matching a query (same filter grammar as find: "
            "from:/in:/category:/during:/after:/before:/has:/file:/ext:) to a file. "
            "Unlike find, there is no result cap -- this is for bulk extraction, not "
            "interactive search."
        ),
    )
    export_parser.add_argument("query", nargs="+", help="Search terms and filters")
    export_parser.add_argument("--format", choices=["txt", "md", "json"], default="txt",
                                dest="export_format", help="Output format (default: txt)")
    export_parser.add_argument("--output", default=None, help="Output file path (default: auto-named)")
```

And add a dispatch branch alongside the existing ones:
```python
    if args.command == "export":
        raw_query = shlex.join(args.query)
        return _run_export(config, raw_query, fmt=args.export_format, output=args.output)
```

- [ ] **Step 2: Verify argparse wiring**

Run: `python -m archiver.cli export --bogus-flag`
Expected: argparse rejects `--bogus-flag` specifically.

Run: `python -m archiver.cli export`
Expected: argparse rejects the missing required `query` argument (unlike `find`, `export`'s query is required — no interactive mode for a bulk-export command).

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `python -m pytest -v`

- [ ] **Step 4: Commit**

```bash
git add archiver/cli.py
git commit -m "feat: add export CLI command (uncapped query results to txt/md/json file)"
```

---

### Task 4: Full-suite sanity check

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -v`
Expected: every test from Tasks 1-3 passes, plus all prior tests still pass.

- [ ] **Step 2: Note what's NOT covered by the automated suite**

`_run_export`'s own file-writing logic isn't unit-tested, consistent with this project's established pattern for CLI-entry-point glue (`_run_find`, `_run_report` are handled the same way). Before this feature is considered fully proven: run `python -m archiver.cli export from:someone --format txt --output somefile.txt` against the real archive and confirm the file is written with the right content and count.

- [ ] **Step 3: Commit if anything changed**

```bash
git status
```

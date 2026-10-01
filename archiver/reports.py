"""Scope stats aggregation (opens shard files) and markdown rendering
for the `archive report` CLI command. Coverage gathering here is
catalog-only; poster/word/attachment aggregation (added in a later
task in this same file) opens shard files, same as archiver/search.py
does for its own per-shard queries."""
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from archiver.db import connect_shard

UNCATEGORIZED_SCOPE = "category:uncategorized"
_CONTENT_TYPE_PREFIXES = ("image", "video", "audio")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def gather_coverage(catalog_conn: sqlite3.Connection) -> list[dict]:
    rows = catalog_conn.execute(
        "SELECT c.id AS channel_id, c.name, c.type, c.category_id, cat.name AS category_name, "
        "cov.status, cov.gap_reason, cov.oldest_message_id, cov.newest_message_id, cov.message_count "
        "FROM channels c "
        "JOIN coverage cov ON cov.channel_id = c.id "
        "LEFT JOIN category_names cat ON cat.category_id = c.category_id "
        "ORDER BY c.category_id, c.name"
    ).fetchall()
    return [dict(r) for r in rows]


def channels_by_scope(catalog_conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = catalog_conn.execute("SELECT id, category_id FROM channels").fetchall()
    scopes: dict[str, list[str]] = {"server": []}
    for row in rows:
        scopes["server"].append(row["id"])
        scope_key = f"category:{row['category_id']}" if row["category_id"] else UNCATEGORIZED_SCOPE
        scopes.setdefault(scope_key, []).append(row["id"])
    return scopes


def classify_attachment(content_type: str | None) -> str:
    if content_type:
        for prefix in _CONTENT_TYPE_PREFIXES:
            if content_type.startswith(f"{prefix}/"):
                return prefix
    return "file"


@dataclass
class ScopeStats:
    total_messages: int
    top_authors: list[tuple[str, int]]
    top_words: list[tuple[str, int]]
    attachment_counts: dict[str, int]


def gather_scope_stats(catalog_conn: sqlite3.Connection, data_dir: Path, channel_ids: list[str],
                        *, excluded_author_ids: frozenset = frozenset(), top_n: int = 20) -> ScopeStats:
    author_counts: Counter = Counter()
    word_counts: Counter = Counter()
    attachment_counts: Counter = Counter()
    total_messages = 0

    for channel_id in channel_ids:
        shard_rows = catalog_conn.execute(
            "SELECT DISTINCT shard_path FROM channel_month_shard WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
        for shard_row in shard_rows:
            shard_conn = connect_shard(data_dir / shard_row["shard_path"])
            try:
                for row in shard_conn.execute(
                    "SELECT author_id, content FROM messages WHERE channel_id=? AND deleted_utc IS NULL",
                    (channel_id,),
                ).fetchall():
                    total_messages += 1
                    if row["author_id"] in excluded_author_ids:
                        continue
                    author_counts[row["author_id"]] += 1
                    for word in _WORD_RE.findall(row["content"].lower()):
                        word_counts[word] += 1

                for att_row in shard_conn.execute(
                    "SELECT a.content_type FROM attachments a JOIN messages m ON a.message_id = m.id "
                    "WHERE m.channel_id=? AND m.deleted_utc IS NULL",
                    (channel_id,),
                ).fetchall():
                    attachment_counts[classify_attachment(att_row["content_type"])] += 1
            finally:
                shard_conn.close()

    return ScopeStats(
        total_messages=total_messages,
        top_authors=author_counts.most_common(top_n),
        top_words=word_counts.most_common(top_n),
        attachment_counts=dict(attachment_counts),
    )


def render_report(scope_label: str, coverage_rows: list[dict], stats: ScopeStats,
                   newly_inaccessible: list[dict]) -> str:
    lines = [f"# {scope_label} report", ""]

    if newly_inaccessible:
        lines.append("## Newly inaccessible since the last report")
        lines.append("")
        for row in newly_inaccessible:
            lines.append(f"- **{row.get('name', row['channel_id'])}** (id {row['channel_id']})")
        lines.append("")

    lines.append("## Coverage")
    lines.append("")
    lines.append("| Channel | Type | Status | Messages | Gap reason |")
    lines.append("|---|---|---|---|---|")
    for row in coverage_rows:
        gap = row.get("gap_reason") or ""
        lines.append(
            f"| {row['name']} | {row['type']} | {row['status']} | "
            f"{row['message_count']} | {gap} |"
        )
    lines.append("")

    lines.append("## Activity")
    lines.append("")
    lines.append(f"Total messages: {stats.total_messages}")
    lines.append("")
    lines.append(
        "Note on counts: the Coverage section's \"Messages\" column is a running counter "
        "that is never decremented when a message is later deleted, and includes every "
        "author regardless of ranking exclusions, so it can differ from this section's "
        "\"Total messages\" (a live count of currently non-deleted shard rows) once any "
        "deletion has occurred. Top posters and Top words below also silently omit any "
        "author configured as excluded from rankings, so their counts are not expected "
        "to sum to the total here."
    )
    lines.append("")

    lines.append("## Top posters")
    lines.append("")
    if stats.top_authors:
        lines.append("| Author ID | Messages |")
        lines.append("|---|---|")
        for author_id, count in stats.top_authors:
            lines.append(f"| {author_id} | {count} |")
    else:
        lines.append("No data.")
    lines.append("")

    lines.append("## Top words")
    lines.append("")
    lines.append(
        "Tokenized by simple whitespace/punctuation splitting (Korean and English alike) -- "
        "no linguistic segmentation is applied, so this is a rough frequency count, not a "
        "morphologically-aware word list."
    )
    lines.append("")
    if stats.top_words:
        lines.append("| Word | Count |")
        lines.append("|---|---|")
        for word, count in stats.top_words:
            lines.append(f"| {word} | {count} |")
    else:
        lines.append("No data.")
    lines.append("")

    lines.append("## Attachments")
    lines.append("")
    lines.append(
        "Counted by attachment `content_type`: **image** = content_type starting `image/`, "
        "**video** = starting `video/`, **audio** = starting `audio/`, **file** = anything else "
        "(including no content_type). These categories can overlap in casual usage but not in "
        "this count -- each attachment is counted in exactly one category."
    )
    lines.append("")
    if stats.attachment_counts:
        lines.append("| Category | Count |")
        lines.append("|---|---|")
        for category in ("image", "video", "audio", "file"):
            if category in stats.attachment_counts:
                lines.append(f"| {category} | {stats.attachment_counts[category]} |")
    else:
        lines.append("No data.")
    lines.append("")

    return "\n".join(lines)

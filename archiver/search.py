"""Search: query tokenizing, filter resolution, candidate-shard
selection, and result formatting for the `archive find` CLI command.
No discord.py import in this module -- search only ever reads
already-archived data (spec §7)."""
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from archiver.db import connect_shard
from archiver.fts import ensure_messages_fts
from archiver.store import month_bucket

_FILTER_KEYS = {"from", "in", "during", "after", "before", "has", "file", "ext"}
SEOUL = ZoneInfo("Asia/Seoul")
_HAS_VALUES = {"image", "video", "audio", "embed", "poll", "sticker", "reaction", "attachment"}
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


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


def search(catalog_conn, data_dir: Path, raw_query: str, *, limit: int = 500) -> list[dict]:
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
    return results[:limit]


def get_context(catalog_conn, data_dir: Path, channel_id: str, message_id: str,
                 n: int, id_index_cache: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Messages immediately before/after `message_id` in `channel_id`,
    by id order, crossing month-shard boundaries as needed. Pass a dict
    via `id_index_cache` to reuse one channel's id index across multiple
    calls in the same search (e.g. many hits in the same busy channel)
    instead of rebuilding it from scratch every time."""
    if id_index_cache is not None and channel_id in id_index_cache:
        id_index = id_index_cache[channel_id]
    else:
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
        if id_index_cache is not None:
            id_index_cache[channel_id] = id_index

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
    created_local = (
        datetime.strptime(row["created_utc"], _ISO_FMT)
        .replace(tzinfo=timezone.utc)
        .astimezone(SEOUL)
        .strftime("%Y-%m-%d %H:%M:%S KST")
    )
    return f"[{created_local}] #{channel_name} {author_name}: {content}"

"""Bulk export rendering for the `archive export` CLI command. Thin
wrapper over archiver/search.py's search()/format_result() -- export
reuses the exact same filter grammar and matching logic as `find`,
just uncapped and written to a file instead of stdout."""
from datetime import datetime, timezone

from archiver.search import SEOUL, _ISO_FMT, format_result


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

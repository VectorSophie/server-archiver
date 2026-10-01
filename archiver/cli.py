"""Command-line entry points: `doctor` and `coverage --preflight`. Never
prints the token. The async orchestration functions (_run_doctor,
_run_coverage_preflight) open a live discord.Client and are verified
manually against the real bot (spec's own testing directive), not by
the automated suite — everything they don't have to touch a live
connection for is factored into the pure `format_*` functions below,
which the suite does cover."""
import argparse
import asyncio
import getpass
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord

from archiver.backfill import backfill_all_pending
from archiver.config import load_config
from archiver.credentials import add_user
from archiver.db import connect_catalog
from archiver.discovery import discover_guild
from archiver.discord_io import load_token
from archiver.export import render_markdown, render_text
from archiver.live import apply_live_message, apply_live_reaction_change, apply_live_thread_create, apply_raw_delete, apply_raw_edit, catch_up_missed_messages, rescan_recent_window, run_periodic_rediscovery_once
from archiver.users import backfill_missing_users
from archiver.report_state import (
    compute_scope_fingerprint, diff_newly_inaccessible, is_scope_dirty, mark_scope_generated,
    save_coverage_snapshot, get_previous_filename, all_known_scopes, forget_scope,
)
from archiver.reports import channels_by_scope, gather_coverage, gather_scope_stats, render_report
from archiver.search import HAS_VALUES, format_result, get_context, search, tokenize_query
from archiver.snapshot import create_snapshot, is_snapshot_stale, list_snapshot_scopes, restore_snapshot, verify_snapshot
from archiver.store import ShardStore
from archiver.sync import SYNC_STATE_FILENAME, changed_files, load_sync_state, save_sync_state, sync_to_remote

HERE = Path(__file__).parent.parent


def _setup_background_logging() -> None:
    """pythonw.exe (Task Scheduler runs `archive live` through it so no
    console window appears) gives sys.stdout/sys.stderr as None -- the
    very first print() would crash with AttributeError. Redirect both
    to a UTF-8 log file before anything else touches them. One file per
    calendar day, kept next to config.json so it's always writable even
    if config.json itself fails to load."""
    log_dir = HERE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"live-{datetime.now().strftime('%Y%m%d')}.log"
    log_file = open(log_path, "a", buffering=1, encoding="utf-8")
    sys.stdout = log_file
    sys.stderr = log_file


def _acquire_single_instance_lock(data_dir: Path):
    """Prevents two `archive live` processes from writing to the same
    shards at once -- e.g. Task Scheduler firing while a manual run is
    still going. Returns the open lock file handle on success (keep it
    referenced for the process lifetime; closing it releases the lock),
    or None if another process already holds it. Windows-only, like the
    rest of this project's Stage 8 deployment story."""
    import msvcrt
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".live.lock"
    lock_file = open(lock_path, "a+")
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def format_doctor_report(username, user_id, message_content_intent,
                          guild_found, guild_name, guild_id, data_dir,
                          data_folder_writable=True) -> list[str]:
    lines = [f"Bot: {username} (id {user_id})"]
    lines.append(
        f"Message Content Intent: {'enabled' if message_content_intent else 'NOT enabled'}"
    )
    if guild_found:
        lines.append(f"Configured guild: {guild_name} (id {guild_id}) - accessible")
    else:
        lines.append(f"Configured guild id {guild_id} - NOT found among this bot's guilds")
    lines.append(
        f"Data folder writable: {data_dir.as_posix()}" if data_folder_writable
        else f"Data folder NOT writable: {data_dir.as_posix()}"
    )
    return lines


async def _run_doctor(config) -> int:
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    result: dict = {}
    error: Exception | None = None

    @client.event
    async def on_ready():
        nonlocal error
        try:
            app_info = await client.application_info()
            guild = client.get_guild(int(config.guild_id))
            result["username"] = str(client.user)
            result["user_id"] = client.user.id
            result["message_content_intent"] = (
                app_info.flags.gateway_message_content
                or app_info.flags.gateway_message_content_limited
            )
            result["guild_found"] = guild is not None
            result["guild_name"] = guild.name if guild else None
        except Exception as e:
            error = e
        finally:
            await client.close()

    token = load_token(HERE / ".env")
    try:
        await client.start(token)
    except Exception as e:
        print(f"doctor failed to connect: {e}")
        return 1

    if error is not None:
        print(f"doctor failed after connecting: {error}")
        return 1

    data_folder_writable = True
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        probe = config.data_dir / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        data_folder_writable = False

    for line in format_doctor_report(
        result["username"], result["user_id"], result["message_content_intent"],
        result["guild_found"], result["guild_name"], config.guild_id, config.data_dir,
        data_folder_writable,
    ):
        print(line)

    return 0 if (
        result["message_content_intent"] and result["guild_found"] and data_folder_writable
    ) else 1


def format_coverage_rows(rows) -> list[str]:
    lines = []
    for row in rows:
        gap = f" ({row['gap_reason']})" if row["gap_reason"] else ""
        lines.append(f"  [{row['status']:>13}] {row['type']:<15} {row['name']}{gap}")
    return lines


async def _run_coverage_preflight(config) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    stats: dict = {}
    error: Exception | None = None

    @client.event
    async def on_ready():
        nonlocal error
        try:
            guild = client.get_guild(int(config.guild_id))
            if guild is None:
                raise RuntimeError(
                    f"configured guild id {config.guild_id} not found among this bot's guilds"
                )
            stats.update(await discover_guild(guild, catalog_conn))
        except Exception as e:
            error = e
        finally:
            await client.close()

    token = load_token(HERE / ".env")
    try:
        await client.start(token)
    except Exception as e:
        print(f"coverage --preflight failed to connect: {e}")
        return 1

    if error is not None:
        print(f"coverage --preflight failed after connecting: {error}")
        return 1

    print(
        f"Discovered {stats['discovered']} channel(s)/thread(s), {stats['new']} new, "
        f"{stats['inaccessible']} now inaccessible."
    )
    rows = catalog_conn.execute(
        "SELECT c.name, c.type, cov.status, cov.gap_reason "
        "FROM channels c JOIN coverage cov ON cov.channel_id = c.id "
        "ORDER BY c.type, c.name"
    ).fetchall()
    for line in format_coverage_rows(rows):
        print(line)
    return 0


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
            user_stats = await backfill_missing_users(client, catalog_conn, config.data_dir)
            if user_stats["missing"]:
                print(f"User catch-up: fetched {user_stats['fetched']}/{user_stats['missing']} "
                      f"missing usernames ({user_stats['failed']} unreachable).")
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


async def _run_live(config) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    store = ShardStore(config.data_dir, catalog_conn)
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    caught_up_channels: set[str] = set()
    backfill_task: asyncio.Task | None = None
    rediscovery_task: asyncio.Task | None = None

    async def _periodic_rediscovery_loop(interval_seconds: int = 1800) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await run_periodic_rediscovery_once(client, config.guild_id, catalog_conn)

    @client.event
    async def on_ready():
        nonlocal backfill_task, rediscovery_task
        guild = client.get_guild(int(config.guild_id))
        if guild is None:
            print(f"live capture failed: configured guild id {config.guild_id} not found")
            await client.close()
            return
        try:
            await discover_guild(guild, catalog_conn)
            await catch_up_missed_messages(client, catalog_conn, store, caught_up_channels)
            await rescan_recent_window(client, catalog_conn, store)
            if backfill_task is None or backfill_task.done():
                backfill_task = asyncio.create_task(backfill_all_pending(client, catalog_conn, store))
            if rediscovery_task is None or rediscovery_task.done():
                rediscovery_task = asyncio.create_task(_periodic_rediscovery_loop())
            # After, not before, kicking off backfill: a network error in the
            # user-catch-up sweep must not delay backfill starting until the
            # next reconnect (reviewer-found scenario -- the sweep only
            # isolates per-user NotFound/Forbidden/HTTPException, not every
            # possible failure, e.g. a transport error).
            user_stats = await backfill_missing_users(client, catalog_conn, config.data_dir)
            if user_stats["missing"]:
                print(f"User catch-up: fetched {user_stats['fetched']}/{user_stats['missing']} "
                      f"missing usernames ({user_stats['failed']} unreachable).")
            print(f"Live capture running as {client.user}.")
        except Exception as e:
            print(f"live capture startup sequence failed: {e}. "
                  f"Live message capture continues, but discovery/catch-up/backfill may be incomplete.")

    @client.event
    async def on_message(message):
        channel_id = str(message.channel.id)
        row = catalog_conn.execute(
            "SELECT status FROM coverage WHERE channel_id=?", (channel_id,)
        ).fetchone()
        advance = row is None or row["status"] != "complete" or channel_id in caught_up_channels
        apply_live_message(store, catalog_conn, message, advance_checkpoint=advance)

    @client.event
    async def on_thread_create(thread):
        apply_live_thread_create(catalog_conn, thread)

    @client.event
    async def on_raw_message_edit(payload):
        apply_raw_edit(store, catalog_conn, str(payload.channel_id), payload.data)

    @client.event
    async def on_raw_message_delete(payload):
        apply_raw_delete(store, catalog_conn, str(payload.channel_id), payload.message_id)

    @client.event
    async def on_raw_bulk_message_delete(payload):
        for message_id in payload.message_ids:
            apply_raw_delete(store, catalog_conn, str(payload.channel_id), message_id)

    @client.event
    async def on_raw_reaction_add(payload):
        await apply_live_reaction_change(client, store, catalog_conn, payload.channel_id, payload.message_id)

    @client.event
    async def on_raw_reaction_remove(payload):
        await apply_live_reaction_change(client, store, catalog_conn, payload.channel_id, payload.message_id)

    token = load_token(HERE / ".env")
    try:
        await client.start(token)  # runs until externally stopped
    except Exception as e:
        print(f"live capture failed to connect: {e}")
        return 1
    return 0


def _run_find(config, raw_query: str, *, full: bool = False, context: int = 0,
              json_output: bool = False) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    parsed = tokenize_query(raw_query)
    unknown_has = sorted({v for v in parsed.filters.get("has", []) if v.lower() not in HAS_VALUES})
    if unknown_has:
        print(f"warning: unrecognized has: value(s) ignored: {', '.join(unknown_has)}", file=sys.stderr)
    try:
        try:
            results = search(catalog_conn, config.data_dir, raw_query)
        except ValueError as e:
            print(f"invalid query: {e}")
            return 1

        if len(results) == 500:
            print("(showing first 500 results — narrow your search with a channel, date, "
                  "or author filter for more)", file=sys.stderr)

        id_index_cache: dict = {}

        if json_output:
            payload = []
            for row in results:
                entry = dict(row)
                if context:
                    before, after = get_context(catalog_conn, config.data_dir,
                                                 row["channel_id"], row["id"], context,
                                                 id_index_cache=id_index_cache)
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
                                             row["channel_id"], row["id"], context,
                                             id_index_cache=id_index_cache)
                for b in before:
                    print(f"    {format_result(catalog_conn, b, full=full)}")
                print("    --- match ---")
                for a in after:
                    print(f"    {format_result(catalog_conn, a, full=full)}")
        return 0
    finally:
        catalog_conn.close()


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


def _run_report(config, *, force: bool = False) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    try:
        coverage = gather_coverage(catalog_conn)
        coverage_by_channel = {row["channel_id"]: row for row in coverage}
        scopes = channels_by_scope(catalog_conn)
        newly_inaccessible = diff_newly_inaccessible(catalog_conn, coverage)

        reports_dir = config.data_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        excluded = frozenset(config.excluded_ranking_author_ids)

        generated, skipped = [], []
        channel_stats_cache: dict = {}
        for scope, channel_ids in scopes.items():
            scope_rows = [coverage_by_channel[cid] for cid in channel_ids if cid in coverage_by_channel]

            if scope == "server":
                label, filename = "Server", "server.md"
            else:
                category_id = scope.split(":", 1)[1]
                label = next(
                    (r["category_name"] for r in scope_rows if r.get("category_name")),
                    "Uncategorized" if category_id == "uncategorized" else category_id,
                )
                safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in label).strip() or category_id
                filename = f"{safe_name}-{category_id[-6:]}.md"

            # Computed unconditionally (not gated behind is_scope_dirty) so a
            # category rename is detected and regenerated under its new
            # filename even when nothing else about the scope's content
            # changed -- the fingerprint never incorporates category_name,
            # so a rename alone would otherwise never be seen as "dirty".
            previous_filename = get_previous_filename(catalog_conn, scope)
            renamed = previous_filename is not None and previous_filename != filename

            current_fingerprint = compute_scope_fingerprint(catalog_conn, config.data_dir, channel_ids)
            if not force and not renamed and not is_scope_dirty(catalog_conn, scope, current_fingerprint):
                skipped.append(scope)
                continue

            stats = gather_scope_stats(catalog_conn, config.data_dir, channel_ids,
                                        excluded_author_ids=excluded,
                                        channel_stats_cache=channel_stats_cache)
            scope_newly_inaccessible = [r for r in newly_inaccessible if r["channel_id"] in channel_ids]

            author_ids = [author_id for author_id, _ in stats.top_authors]
            usernames = {}
            if author_ids:
                placeholders = ",".join("?" for _ in author_ids)
                usernames = {
                    row["id"]: row["username"] for row in catalog_conn.execute(
                        f"SELECT id, username FROM users WHERE id IN ({placeholders})", author_ids
                    ).fetchall()
                }
            text = render_report(label, scope_rows, stats, scope_newly_inaccessible, usernames)
            (reports_dir / filename).write_text(text, encoding="utf-8")
            if renamed and previous_filename.lower() != filename.lower():
                # Windows filesystems are case-insensitive, so a case-only
                # rename (e.g. "Yuri" -> "YURI") writes to the SAME file the
                # write_text() above just wrote -- unlinking it here would
                # delete the report that was just generated. Skip the delete
                # entirely in that case; the write_text() already updated the
                # file's actual casing on disk.
                stale_path = reports_dir / previous_filename
                if stale_path.exists():
                    stale_path.unlink()
            mark_scope_generated(catalog_conn, scope, current_fingerprint, filename)
            generated.append(filename)

        removed = []
        for old_scope, old_filename in all_known_scopes(catalog_conn).items():
            if old_scope not in scopes:
                # old_filename can be '' for a report_fingerprint row that
                # predates this column (backfilled by CATALOG_SCHEMA_V3) and
                # was never regenerated since -- reports_dir / '' resolves to
                # reports_dir itself, so unlinking it would delete the whole
                # reports directory. Only unlink when there's a real filename.
                if old_filename:
                    stale_path = reports_dir / old_filename
                    if stale_path.exists():
                        stale_path.unlink()
                forget_scope(catalog_conn, old_scope)
                if old_filename:
                    removed.append(old_filename)

        save_coverage_snapshot(catalog_conn, coverage)

        if generated:
            print(f"Generated: {', '.join(generated)}")
        if skipped:
            print(f"Skipped (unchanged): {len(skipped)} scope(s)")
        if removed:
            print(f"Removed (category gone): {', '.join(removed)}")
        if not generated and not skipped:
            print("No channels discovered yet -- nothing to report.")
        return 0
    finally:
        catalog_conn.close()


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


def _run_snapshot(config, *, force: bool = False) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    try:
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        snapshots_dir = config.data_dir / "snapshots"
        created, skipped = [], []
        for category_id, yyyymm in list_snapshot_scopes(catalog_conn):
            if not force and not is_snapshot_stale(catalog_conn, config.data_dir, category_id, yyyymm):
                skipped.append(f"{category_id}/{yyyymm}")
                continue
            create_snapshot(catalog_conn, config.data_dir, category_id, yyyymm, snapshots_dir,
                             is_current_month=(yyyymm == current_month))
            created.append(f"{category_id}/{yyyymm}")

        if created:
            print(f"Snapshotted: {', '.join(created)}")
        if skipped:
            print(f"Skipped (unchanged): {len(skipped)} scope(s)")
        if not created and not skipped:
            print("No shards discovered yet -- nothing to snapshot.")
        return 0
    finally:
        catalog_conn.close()


def _run_verify(config, category: str, yyyymm: str) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    try:
        row = catalog_conn.execute(
            "SELECT tarball_path FROM snapshots WHERE category=? AND yyyymm=?", (category, yyyymm)
        ).fetchone()
        if row is None:
            print(f"No snapshot found for {category}/{yyyymm}.")
            return 1
        result = verify_snapshot(config.data_dir, row["tarball_path"])
        print(f"sha256: {'OK' if result['sha256_ok'] else 'MISMATCH'}")
        print(f"integrity: {'OK' if result['integrity_ok'] else 'FAILED'}")
        for name, ok in result["shard_results"].items():
            print(f"  {name}: {'ok' if ok else 'CORRUPT'}")
        return 0 if (result["sha256_ok"] and result["integrity_ok"]) else 1
    finally:
        catalog_conn.close()


def _run_restore(config, category: str, yyyymm: str, *, target: str | None = None, force: bool = False) -> int:
    catalog_conn = connect_catalog(config.data_dir / "catalog.sqlite")
    try:
        row = catalog_conn.execute(
            "SELECT tarball_path FROM snapshots WHERE category=? AND yyyymm=?", (category, yyyymm)
        ).fetchone()
        if row is None:
            print(f"No snapshot found for {category}/{yyyymm}.")
            return 1
        target_dir = Path(target) if target else None
        try:
            result = restore_snapshot(config.data_dir, row["tarball_path"], target_dir=target_dir, force=force)
        except (ValueError, FileExistsError) as exc:
            print(f"Restore refused: {exc}")
            return 1
        print(f"Restored to {result['restored_to']}")
        return 0
    finally:
        catalog_conn.close()


def _run_sync(config) -> int:
    missing = [k for k, v in (
        ("sync_remote_host", config.sync_remote_host),
        ("sync_remote_user", config.sync_remote_user),
        ("sync_remote_data_dir", config.sync_remote_data_dir),
        ("sync_ssh_key_path", config.sync_ssh_key_path),
    ) if not v]
    if missing:
        print(f"sync not configured -- missing config.json key(s): {', '.join(missing)}")
        return 1

    state_path = config.data_dir / SYNC_STATE_FILENAME
    previous_state = load_sync_state(state_path)
    relpaths = changed_files(config.data_dir, previous_state)
    if not relpaths:
        print("Nothing changed since last sync.")
        return 0

    sync_to_remote(
        config.data_dir, relpaths,
        ssh_key=Path(config.sync_ssh_key_path),
        remote_user=config.sync_remote_user,
        remote_host=config.sync_remote_host,
        remote_data_dir=config.sync_remote_data_dir,
    )

    new_state = dict(previous_state)
    for rel in relpaths:
        st = (config.data_dir / rel).stat()
        new_state[rel] = [st.st_mtime_ns, st.st_size]
    save_sync_state(state_path, new_state)

    print(f"Synced {len(relpaths)} file(s).")
    return 0


def _run_serve(config, *, host: str = "127.0.0.1", port: int = 8000) -> int:
    from archiver.webapp import create_app
    app = create_app(config)
    app.run(host=host, port=port, debug=False)
    return 0


def _run_useradd(config, codename: str) -> int:
    password = getpass.getpass(f"Password for '{codename}': ")
    if not password:
        print("password cannot be empty")
        return 1
    credentials_path = HERE / "credentials.json"
    add_user(codename, password, credentials_path)
    print(f"Added/updated credentials for '{codename}'.")
    return 0


def main() -> int:
    if sys.stdout is None:
        _setup_background_logging()
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backfill")
    subparsers.add_parser("doctor")
    coverage_parser = subparsers.add_parser("coverage")
    coverage_parser.add_argument("--preflight", action="store_true")
    find_parser = subparsers.add_parser(
        "find",
        help="Search archived messages",
        description=(
            "Search archived messages. Supports from:/in:/during:/after:/before:/has:/"
            "file:/ext: filters and quoted phrases. Substring matches of 3+ characters use "
            "an indexed trigram search when available; shorter queries, or any query when "
            "trigram support is unavailable, fall back to a full scan of the filtered result "
            "set, which is correctness-first and can be slow on large, unfiltered date ranges "
            "-- narrow with a channel/date/author filter for better performance. "
            "Note: after:/before: are inclusive of the given day (Asia/Seoul calendar day) "
            "or month, unlike Discord's own search operators of the same name, which are "
            "exclusive."
        ),
    )
    find_parser.add_argument("query", nargs="*", help="Search terms and filters (omit for interactive mode)")
    find_parser.add_argument("--full", action="store_true", help="Show full message content, not truncated")
    find_parser.add_argument("--context", type=int, default=0, metavar="N",
                              help="Show N messages before/after each result")
    find_parser.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")
    subparsers.add_parser("live")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--force", action="store_true")
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
    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Create point-in-time snapshots of shard files that changed since their last snapshot",
    )
    snapshot_parser.add_argument("--force", action="store_true", help="Re-snapshot every scope, even unchanged ones")
    verify_parser = subparsers.add_parser("verify", help="Verify a snapshot's integrity (sha256 + PRAGMA integrity_check)")
    verify_parser.add_argument("category", help="Category id")
    verify_parser.add_argument("yyyymm", help="Month, e.g. 2025-10")
    restore_parser = subparsers.add_parser("restore", help="Restore a snapshot's shard files to a directory")
    restore_parser.add_argument("category", help="Category id")
    restore_parser.add_argument("yyyymm", help="Month, e.g. 2025-10")
    restore_parser.add_argument("--target", default=None, help="Destination directory (default: <data_dir>/restored/<category>/<yyyymm>)")
    restore_parser.add_argument("--force", action="store_true", help="Overwrite existing files at the destination")
    serve_parser = subparsers.add_parser("serve", help="Run the private search web app")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    useradd_parser = subparsers.add_parser("useradd", help="Add or reset a league member's web login")
    useradd_parser.add_argument("codename")
    subparsers.add_parser("sync", help="Push changed archive data to the deployed remote mirror")

    args = parser.parse_args()

    if args.command == "coverage" and not args.preflight:
        parser.error("coverage requires --preflight in this stage")

    config = load_config(HERE / "config.json")

    if args.command == "backfill":
        return asyncio.run(_run_backfill(config))
    if args.command == "doctor":
        return asyncio.run(_run_doctor(config))
    if args.command == "coverage":
        return asyncio.run(_run_coverage_preflight(config))
    if args.command == "find":
        if not args.query:
            return _run_find_interactive(config)
        raw_query = shlex.join(args.query)
        return _run_find(config, raw_query, full=args.full, context=args.context,
                         json_output=args.json_output)
    if args.command == "live":
        lock = _acquire_single_instance_lock(config.data_dir)
        if lock is None:
            print("archive live is already running (lock held) -- exiting.")
            return 1
        try:
            return asyncio.run(_run_live(config))
        finally:
            lock.close()
    if args.command == "report":
        return _run_report(config, force=args.force)
    if args.command == "export":
        raw_query = shlex.join(args.query)
        return _run_export(config, raw_query, fmt=args.export_format, output=args.output)
    if args.command == "snapshot":
        return _run_snapshot(config, force=args.force)
    if args.command == "verify":
        return _run_verify(config, args.category, args.yyyymm)
    if args.command == "restore":
        return _run_restore(config, args.category, args.yyyymm, target=args.target, force=args.force)
    if args.command == "serve":
        return _run_serve(config, host=args.host, port=args.port)
    if args.command == "useradd":
        return _run_useradd(config, args.codename)
    if args.command == "sync":
        return _run_sync(config)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

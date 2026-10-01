"""Command-line entry points: `doctor` and `coverage --preflight`. Never
prints the token. The async orchestration functions (_run_doctor,
_run_coverage_preflight) open a live discord.Client and are verified
manually against the real bot (spec's own testing directive), not by
the automated suite — everything they don't have to touch a live
connection for is factored into the pure `format_*` functions below,
which the suite does cover."""
import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path

import discord

from archiver.backfill import backfill_all_pending
from archiver.config import load_config
from archiver.db import connect_catalog
from archiver.discovery import discover_guild
from archiver.discord_io import load_token
from archiver.live import apply_live_message, apply_raw_delete, apply_raw_edit, catch_up_missed_messages, rescan_recent_window
from archiver.search import format_result, get_context, search
from archiver.store import ShardStore

HERE = Path(__file__).parent.parent


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

    @client.event
    async def on_ready():
        nonlocal backfill_task
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
    async def on_raw_message_edit(payload):
        apply_raw_edit(store, catalog_conn, str(payload.channel_id), payload.data)

    @client.event
    async def on_raw_message_delete(payload):
        apply_raw_delete(store, catalog_conn, str(payload.channel_id), payload.message_id)

    @client.event
    async def on_raw_bulk_message_delete(payload):
        for message_id in payload.message_ids:
            apply_raw_delete(store, catalog_conn, str(payload.channel_id), message_id)

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


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backfill")
    subparsers.add_parser("doctor")
    coverage_parser = subparsers.add_parser("coverage")
    coverage_parser.add_argument("--preflight", action="store_true")
    find_parser = subparsers.add_parser("find")
    find_parser.add_argument("query", nargs="*")
    find_parser.add_argument("--full", action="store_true")
    find_parser.add_argument("--context", type=int, default=0)
    find_parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers.add_parser("live")

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
        return asyncio.run(_run_live(config))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

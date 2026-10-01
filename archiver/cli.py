"""Command-line entry points: `doctor` and `coverage --preflight`. Never
prints the token. The async orchestration functions (_run_doctor,
_run_coverage_preflight) open a live discord.Client and are verified
manually against the real bot (spec's own testing directive), not by
the automated suite — everything they don't have to touch a live
connection for is factored into the pure `format_*` functions below,
which the suite does cover."""
import argparse
import asyncio
from pathlib import Path

import discord

from archiver.backfill import backfill_all_pending
from archiver.config import load_config
from archiver.db import connect_catalog
from archiver.discovery import discover_guild
from archiver.discord_io import load_token
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backfill")
    subparsers.add_parser("doctor")
    coverage_parser = subparsers.add_parser("coverage")
    coverage_parser.add_argument("--preflight", action="store_true")

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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

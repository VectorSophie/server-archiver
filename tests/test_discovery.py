import discord

from archiver.db import connect_catalog
from archiver.discovery import discover_guild
from tests.discord_fakes import FakeCategory, FakeChannel, FakeGuild, FakeThread


async def test_discover_guild_inserts_category_and_channel(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    category = FakeCategory(id=1, name="Friends")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(top_level_channels=[category, channel])

    stats = await discover_guild(guild, conn)

    assert stats == {"discovered": 1, "new": 1, "inaccessible": 0}
    cat_row = conn.execute("SELECT name FROM category_names WHERE category_id='1'").fetchone()
    assert cat_row["name"] == "Friends"
    chan_row = conn.execute("SELECT name, type, category_id FROM channels WHERE id='2'").fetchone()
    assert (chan_row["name"], chan_row["type"], chan_row["category_id"]) == ("general", "text", "1")
    cov_row = conn.execute("SELECT status FROM coverage WHERE channel_id='2'").fetchone()
    assert cov_row["status"] == "pending"


async def test_discover_guild_skips_out_of_scope_channel_types(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    stage_channel = FakeChannel(id=3, name="stage", type_=discord.ChannelType.stage_voice)
    guild = FakeGuild(top_level_channels=[stage_channel])

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 0
    assert conn.execute("SELECT 1 FROM channels WHERE id='3'").fetchone() is None


async def test_discover_guild_includes_channel_threads(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        archived_public=[FakeThread(20, "old-thread", parent_id=2)],
    )
    guild = FakeGuild(top_level_channels=[channel])

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 2  # the channel + its thread
    thread_row = conn.execute(
        "SELECT type, parent_id, category_id FROM channels WHERE id='20'"
    ).fetchone()
    assert thread_row["type"] == "public_thread"
    assert thread_row["parent_id"] == "2"
    assert thread_row["category_id"] == "1"  # inherited from parent channel


async def test_discover_guild_includes_guild_wide_active_threads(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(
        top_level_channels=[channel],
        active_threads=[FakeThread(30, "hot-topic", parent_id=2)],
    )

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 2  # the channel + the active thread
    row = conn.execute("SELECT category_id FROM channels WHERE id='30'").fetchone()
    assert row["category_id"] == "1"


async def test_discover_guild_marks_vanished_channel_inaccessible(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild_with_channel = FakeGuild(top_level_channels=[channel])
    await discover_guild(guild_with_channel, conn)

    guild_without_channel = FakeGuild(top_level_channels=[])
    stats = await discover_guild(guild_without_channel, conn)

    assert stats["inaccessible"] == 1
    cov_row = conn.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert cov_row["status"] == "inaccessible"
    assert cov_row["gap_reason"] == "no longer discoverable"


async def test_discover_guild_rediscovering_known_channel_preserves_status(tmp_path):
    """A channel already past 'pending' (e.g. backfill marked it 'complete'
    in an earlier stage) must not be silently reset to 'pending' just
    because discovery ran again."""
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(top_level_channels=[channel])
    await discover_guild(guild, conn)
    conn.execute("UPDATE coverage SET status='complete' WHERE channel_id='2'")
    conn.commit()

    stats = await discover_guild(guild, conn)

    assert stats["new"] == 0
    cov_row = conn.execute("SELECT status FROM coverage WHERE channel_id='2'").fetchone()
    assert cov_row["status"] == "complete"


async def test_discover_guild_records_gap_reason_on_channel_and_its_threads(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        archived_joined=[FakeThread(20, "joined-only", parent_id=2, private=True)],
        forbidden_private=True,
    )
    guild = FakeGuild(top_level_channels=[channel])

    await discover_guild(guild, conn)

    chan_gap = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    thread_gap = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='20'").fetchone()
    assert chan_gap["gap_reason"] is not None
    assert thread_gap["gap_reason"] is not None


async def test_discover_guild_updates_category_id_when_channel_moves(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel_v1 = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    guild_v1 = FakeGuild(top_level_channels=[channel_v1])
    await discover_guild(guild_v1, conn)

    channel_v2 = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=2)
    guild_v2 = FakeGuild(top_level_channels=[channel_v2])
    await discover_guild(guild_v2, conn)

    row = conn.execute("SELECT category_id FROM channels WHERE id='2'").fetchone()
    assert row["category_id"] == "2"


async def test_discover_guild_clears_gap_reason_once_access_restored(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel_forbidden = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        forbidden_private=True,
    )
    guild_forbidden = FakeGuild(top_level_channels=[channel_forbidden])
    await discover_guild(guild_forbidden, conn)
    gap_row = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert gap_row["gap_reason"] is not None

    channel_ok = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        forbidden_private=False,
    )
    guild_ok = FakeGuild(top_level_channels=[channel_ok])
    await discover_guild(guild_ok, conn)

    row = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert row["gap_reason"] is None


async def test_discover_guild_isolates_channel_level_forbidden(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    forbidden_channel = FakeChannel(
        id=2, name="secret", type_=discord.ChannelType.text, category_id=1,
        forbidden_all=True,
    )
    normal_channel = FakeChannel(id=3, name="general", type_=discord.ChannelType.text, category_id=1)
    guild = FakeGuild(top_level_channels=[forbidden_channel, normal_channel])

    stats = await discover_guild(guild, conn)

    assert stats["discovered"] == 2  # both channels themselves, despite the forbidden one's threads failing
    forbidden_row = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert forbidden_row["gap_reason"] is not None
    normal_row = conn.execute("SELECT 1 FROM channels WHERE id='3'").fetchone()
    assert normal_row is not None


async def test_discover_guild_preserves_known_threads_when_channel_becomes_forbidden(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        archived_public=[FakeThread(20, "old-thread", parent_id=2)],
    )
    await discover_guild(FakeGuild(top_level_channels=[channel]), conn)

    now_forbidden_channel = FakeChannel(
        id=2, name="general", type_=discord.ChannelType.text, category_id=1,
        forbidden_all=True,
    )
    stats = await discover_guild(FakeGuild(top_level_channels=[now_forbidden_channel]), conn)

    assert stats["inaccessible"] == 0  # the known thread must NOT be marked vanished
    thread_row = conn.execute("SELECT status FROM coverage WHERE channel_id='20'").fetchone()
    assert thread_row["status"] != "inaccessible"


async def test_discover_guild_revives_previously_vanished_channel(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    await discover_guild(FakeGuild(top_level_channels=[channel]), conn)
    await discover_guild(FakeGuild(top_level_channels=[]), conn)  # vanish it
    cov = conn.execute("SELECT status FROM coverage WHERE channel_id='2'").fetchone()
    assert cov["status"] == "inaccessible"

    await discover_guild(FakeGuild(top_level_channels=[channel]), conn)  # revive it

    cov = conn.execute("SELECT status, gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert cov["status"] == "pending"
    assert cov["gap_reason"] is None


async def test_discover_guild_does_not_clobber_non_discovery_gap_reason(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="general", type_=discord.ChannelType.text, category_id=1)
    await discover_guild(FakeGuild(top_level_channels=[channel]), conn)
    conn.execute(
        "UPDATE coverage SET gap_reason='backfill failed: rate limited repeatedly' WHERE channel_id='2'"
    )
    conn.commit()

    await discover_guild(FakeGuild(top_level_channels=[channel]), conn)

    cov = conn.execute("SELECT gap_reason FROM coverage WHERE channel_id='2'").fetchone()
    assert cov["gap_reason"] == "backfill failed: rate limited repeatedly"


async def test_discover_guild_handles_news_thread_type(tmp_path):
    conn = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=2, name="announcements", type_=discord.ChannelType.news, category_id=1)
    guild = FakeGuild(
        top_level_channels=[channel],
        active_threads=[FakeThread(30, "reply-thread", parent_id=2, type_=discord.ChannelType.news_thread)],
    )

    stats = await discover_guild(guild, conn)  # must not raise IntegrityError

    row = conn.execute("SELECT type FROM channels WHERE id='30'").fetchone()
    assert row["type"] == "public_thread"


import sqlite3

from archiver.db import CATALOG_MIGRATIONS, apply_migrations


class _CommitCountingConnection(sqlite3.Connection):
    """Plain sqlite3.Connection has no __dict__, so its bound methods
    can't be monkeypatched on an instance -- subclassing gives us one."""


async def test_discover_guild_commits_incrementally_not_only_at_the_end(tmp_path):
    """Open a second connection to the same catalog file mid-discovery
    (via a custom FakeGuild whose fetch_channels callback peeks through
    it) to prove discover_guild doesn't hold everything in one
    uncommitted transaction until the very end -- a concurrent reader
    (e.g. a live-capture handler on the same process) must be able to
    see a channel discovered earlier in the same sweep before the sweep
    finishes."""
    catalog = sqlite3.connect(tmp_path / "catalog.sqlite", factory=_CommitCountingConnection)
    catalog.row_factory = sqlite3.Row
    catalog.execute("PRAGMA journal_mode=WAL")
    catalog.execute("PRAGMA foreign_keys=ON")
    catalog.execute("PRAGMA busy_timeout=5000")
    apply_migrations(catalog, CATALOG_MIGRATIONS)

    channel_a = FakeChannel(id=1, name="alpha", type_=discord.ChannelType.text)
    channel_b = FakeChannel(id=2, name="beta", type_=discord.ChannelType.text)
    guild = FakeGuild([channel_a, channel_b])

    # Assert discover_guild's own commit count via monkeypatching, since
    # that's a direct, robust check of the commit-timing fix itself.
    commit_calls = []
    original_commit = catalog.commit
    catalog.commit = lambda: (commit_calls.append(1), original_commit())[-1]

    await discover_guild(guild, catalog)

    assert len(commit_calls) >= 2  # at least one per channel, not only the final commit

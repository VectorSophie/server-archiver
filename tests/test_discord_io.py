import discord

from archiver.discord_io import classify_channel_type, resolve_category_id


class _Obj:
    """Minimal stand-in carrying only the attributes classify/resolve read."""
    def __init__(self, type_, category_id=None):
        self.type = type_
        self.category_id = category_id


def test_classify_text_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.text)) == "text"


def test_classify_announcement_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.news)) == "announcement"


def test_classify_forum_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.forum)) == "forum"


def test_classify_media_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.media)) == "media"


def test_classify_voice_channel():
    assert classify_channel_type(_Obj(discord.ChannelType.voice)) == "voice"


def test_classify_public_thread():
    assert classify_channel_type(_Obj(discord.ChannelType.public_thread)) == "public_thread"


def test_classify_private_thread():
    assert classify_channel_type(_Obj(discord.ChannelType.private_thread)) == "private_thread"


def test_classify_out_of_scope_type_returns_none():
    assert classify_channel_type(_Obj(discord.ChannelType.category)) is None
    assert classify_channel_type(_Obj(discord.ChannelType.stage_voice)) is None


def test_resolve_category_id_present():
    assert resolve_category_id(_Obj(discord.ChannelType.text, category_id=555)) == "555"


def test_resolve_category_id_absent():
    assert resolve_category_id(_Obj(discord.ChannelType.text, category_id=None)) is None


from pathlib import Path

import pytest

from archiver.discord_io import discover_threads, load_token
from tests.discord_fakes import FakeChannel, FakeThread


async def test_discover_threads_collects_public_and_private_archived():
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=[FakeThread(10, "old-topic", parent_id=1)],
        archived_private=[FakeThread(11, "secret-topic", parent_id=1, private=True)],
    )
    threads, gap_reason = await discover_threads(channel)
    ids = {t["id"] for t in threads}
    assert ids == {"10", "11"}
    assert gap_reason is None


async def test_discover_threads_falls_back_when_private_forbidden():
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=[],
        archived_joined=[FakeThread(12, "joined-only", parent_id=1, private=True)],
        forbidden_private=True,
    )
    threads, gap_reason = await discover_threads(channel)
    ids = {t["id"] for t in threads}
    assert ids == {"12"}
    assert gap_reason is not None
    assert "Manage Threads" in gap_reason


async def test_discover_threads_thread_info_fields():
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=[FakeThread(10, "old-topic", parent_id=1)],
    )
    threads, _ = await discover_threads(channel)
    assert threads[0] == {
        "id": "10", "name": "old-topic", "type": "public_thread",
        "parent_id": "1", "is_archived": True,
    }


def test_load_token_reads_discord_token(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text('DISCORD_TOKEN=abc123.def456\n', encoding="utf-8")
    assert load_token(env_path) == "abc123.def456"


def test_load_token_strips_quotes(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text('DISCORD_TOKEN="abc123.def456"\n', encoding="utf-8")
    assert load_token(env_path) == "abc123.def456"


def test_load_token_missing_raises(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text('SOME_OTHER_VAR=x\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        load_token(env_path)


async def test_discover_threads_forum_channel_never_passes_private_kwarg():
    channel = FakeChannel(
        id=1, name="game-ideas", type_=discord.ChannelType.forum,
        archived_public=[FakeThread(10, "post one", parent_id=1)],
    )
    threads, gap_reason = await discover_threads(channel)
    assert {t["id"] for t in threads} == {"10"}
    assert gap_reason is None


async def test_discover_threads_media_channel_never_passes_private_kwarg():
    channel = FakeChannel(
        id=1, name="clips", type_=discord.ChannelType.media,
        archived_public=[FakeThread(10, "clip one", parent_id=1)],
    )
    threads, gap_reason = await discover_threads(channel)
    assert {t["id"] for t in threads} == {"10"}


async def test_discover_threads_paginates_past_default_limit_of_100():
    many_threads = [FakeThread(100 + i, f"thread-{i}", parent_id=1) for i in range(150)]
    channel = FakeChannel(
        id=1, name="general", type_=discord.ChannelType.text,
        archived_public=many_threads,
    )
    threads, _ = await discover_threads(channel)
    assert len(threads) == 150


def test_classify_news_thread_maps_to_public_thread():
    assert classify_channel_type(_Obj(discord.ChannelType.news_thread)) == "public_thread"

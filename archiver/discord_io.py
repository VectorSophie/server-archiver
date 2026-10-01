"""discord.py-facing glue: channel-type classification, per-channel
thread discovery, and token loading. Functions here take discord.py
objects (or hand-built fakes shaped like them) and don't hold a live
connection themselves — archiver/cli.py owns the actual Client."""
import discord

CHANNEL_TYPE_MAP = {
    discord.ChannelType.text: "text",
    discord.ChannelType.news: "announcement",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.media: "media",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.public_thread: "public_thread",
    discord.ChannelType.private_thread: "private_thread",
    discord.ChannelType.news_thread: "public_thread",
}


def classify_channel_type(channel) -> str | None:
    return CHANNEL_TYPE_MAP.get(channel.type)


def resolve_category_id(channel) -> str | None:
    category_id = getattr(channel, "category_id", None)
    return str(category_id) if category_id is not None else None


def _thread_info(thread) -> dict:
    return {
        "id": str(thread.id),
        "name": thread.name,
        "type": classify_channel_type(thread),
        "parent_id": str(thread.parent_id),
        "is_archived": True,
    }


async def discover_threads(channel) -> tuple[list[dict], str | None]:
    """Discover archived threads under one parent channel (public,
    paginated to exhaustion by discord.py's own async iterator; private,
    same, unless Manage Threads isn't granted, in which case fall back to
    joined-only and record why). Forum/media channels have no private
    threads and their archived_threads() has no `private` parameter at
    all, so they take a separate, simpler path. Active threads are
    discovered separately at the guild level (see archiver/discovery.py)
    — they're not repeated here."""
    threads: list[dict] = []
    gap_reason: str | None = None
    kind = classify_channel_type(channel)

    if kind in ("forum", "media"):
        async for thread in channel.archived_threads(limit=None):
            threads.append(_thread_info(thread))
        return threads, gap_reason

    async for thread in channel.archived_threads(private=False, limit=None):
        threads.append(_thread_info(thread))

    try:
        async for thread in channel.archived_threads(private=True, limit=None):
            threads.append(_thread_info(thread))
    except discord.Forbidden:
        gap_reason = (
            "private archived threads: Manage Threads not granted, "
            "showing joined-only"
        )
        async for thread in channel.archived_threads(private=True, joined=True, limit=None):
            threads.append(_thread_info(thread))

    return threads, gap_reason


def load_token(env_path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DISCORD_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DISCORD_TOKEN not found in .env")

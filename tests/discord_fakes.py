"""Hand-built fakes for the discord.py async channel/thread surface used
by discovery tests — deliberately not unittest.mock.MagicMock, so a fake
missing an attribute the code reads fails loudly instead of silently
returning a Mock."""
import discord


class FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeThread:
    def __init__(self, id, name, parent_id, private=False, type_=None):
        self.id = id
        self.name = name
        self.parent_id = parent_id
        self.type = type_ or (
            discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
        )


class FakeChannel:
    def __init__(self, id, name, type_, category_id=None,
                 archived_public=None, archived_private=None,
                 archived_joined=None, forbidden_private=False,
                 forbidden_all=False):
        self.id = id
        self.name = name
        self.type = type_
        self.category_id = category_id
        self._archived_public = archived_public or []
        self._archived_private = archived_private or []
        self._archived_joined = archived_joined or []
        self._forbidden_private = forbidden_private
        self._forbidden_all = forbidden_all

    async def archived_threads(self, private=False, joined=False, limit=100, before=None):
        if self.type in (discord.ChannelType.forum, discord.ChannelType.media):
            if private or joined:
                raise TypeError(
                    "archived_threads() got an unexpected keyword argument 'private'"
                )
            source = self._archived_public
        elif private and joined:
            source = self._archived_joined
        elif private:
            if self._forbidden_private:
                raise discord.Forbidden(FakeResponse(), "Missing Permissions")
            source = self._archived_private
        else:
            if self._forbidden_all:
                raise discord.Forbidden(FakeResponse(), "Missing Permissions")
            source = self._archived_public

        count = 0
        for t in source:
            if limit is not None and count >= limit:
                return
            yield t
            count += 1


class FakeCategory:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.type = discord.ChannelType.category


class FakeGuild:
    def __init__(self, top_level_channels, active_threads=None):
        self._top_level = top_level_channels
        self._active_threads = active_threads or []

    async def fetch_channels(self):
        return self._top_level

    async def active_threads(self):
        return self._active_threads


class FakeChannelRef:
    def __init__(self, id):
        self.id = id


class FakeUserRef:
    def __init__(self, id):
        self.id = id


class FakeMessageFlags:
    def __init__(self, value=0):
        self.value = value


class FakeMessageReference:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeAttachment:
    def __init__(self, id, filename, content_type=None, size=0,
                 width=None, height=None, duration=None, description=None):
        self.id = id
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.width = width
        self.height = height
        self.duration = duration
        self.description = description


class FakeMessage:
    def __init__(self, id, channel_id, author_id, content="", created_at=None,
                 edited_at=None, reference=None, mention_everyone=False,
                 flags_value=0, attachments=None, reactions=None,
                 mentions=None, role_mentions=None, stickers=None,
                 poll=None, embeds=None):
        self.id = id
        self.channel = FakeChannelRef(channel_id)
        self.author = FakeUserRef(author_id)
        self.content = content
        self.created_at = created_at
        self.edited_at = edited_at
        self.reference = reference
        self.mention_everyone = mention_everyone
        self.flags = FakeMessageFlags(flags_value)
        self.attachments = attachments or []
        self.reactions = reactions or []
        self.mentions = mentions or []
        self.role_mentions = role_mentions or []
        self.stickers = stickers or []
        self.poll = poll
        self.embeds = embeds or []


class FakeReaction:
    def __init__(self, emoji, count, custom=False, animated=False):
        self.emoji = emoji
        self.count = count
        self._custom = custom
        if custom:
            self.emoji = _FakeCustomEmoji(str(emoji), animated)

    def is_custom_emoji(self) -> bool:
        return self._custom


class _FakeCustomEmoji:
    def __init__(self, text, animated):
        self._text = text
        self.animated = animated

    def __str__(self):
        return self._text


class FakeSticker:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class FakePollAnswer:
    def __init__(self, id, text, vote_count):
        self.id = id
        self.text = text
        self.vote_count = vote_count


class FakePoll:
    def __init__(self, question, multiple=False, expires_at=None, answers=None):
        # Real discord.py: Poll.question is a str property (self._question_media.text),
        # not an object with .text -- confirmed via inspect.getsource after this
        # mismatch caused a live crash ('str' object has no attribute 'text').
        self.question = question
        self.multiple = multiple
        self.expires_at = expires_at
        self.answers = answers or []


class FakeHistoryChannel:
    """A minimal channel double for backfill tests -- separate from
    FakeChannel (which models discovery's archived_threads surface) since
    backfill only needs .id and .history(), not thread pagination."""
    def __init__(self, id, messages, forbidden=False, http_error=False):
        self.id = id
        self._messages = sorted(messages, key=lambda m: m.id)
        self._forbidden = forbidden
        self._http_error = http_error

    async def history(self, *, limit=100, after=None, oldest_first=True):
        if self._forbidden:
            raise discord.Forbidden(FakeResponse(), "Missing Permissions")
        if self._http_error:
            raise discord.HTTPException(FakeResponse(), "Internal Server Error")
        after_id = after.id if after is not None else 0
        remaining = [m for m in self._messages if m.id > after_id]
        for m in remaining[:limit]:
            yield m


class FakeClient:
    """A minimal Client double for backfill_all_pending: maps channel id
    -> channel object (or an exception to raise) for fetch_channel."""
    def __init__(self, channels: dict, errors: dict | None = None):
        self._channels = channels
        self._errors = errors or {}

    async def fetch_channel(self, channel_id: int):
        if channel_id in self._errors:
            raise self._errors[channel_id]
        return self._channels[channel_id]

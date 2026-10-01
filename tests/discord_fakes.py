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

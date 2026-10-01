from datetime import datetime, timezone

from archiver.discord_message import map_message
from tests.discord_fakes import FakeAttachment, FakeMessage, FakeMessageReference

CREATED = datetime(2025, 10, 15, 3, 0, 0, tzinfo=timezone.utc)


def test_map_message_core_fields():
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hello", created_at=CREATED)
    mapped = map_message(msg)
    assert mapped["message"] == {
        "id": "100", "channel_id": "1", "author_id": "2", "content": "hello",
        "created_utc": "2025-10-15T03:00:00Z", "edited_utc": None,
        "reply_to_id": None, "mention_everyone": 0, "flags": 0, "deleted_utc": None,
    }


def test_map_message_empty_content_with_attachment_is_still_a_message():
    """spec: 'An empty-text message with an attachment or embed is still
    a message and must be stored.'"""
    msg = FakeMessage(
        id=101, channel_id=1, author_id=2, content="", created_at=CREATED,
        attachments=[FakeAttachment(id=500, filename="photo.png", content_type="image/png", size=1234)],
    )
    mapped = map_message(msg)
    assert mapped["message"]["content"] == ""
    assert mapped["attachments"] == [{
        "id": "500", "message_id": "101", "filename": "photo.png",
        "content_type": "image/png", "size": 1234, "width": None, "height": None,
        "duration_secs": None, "description": None,
    }]


def test_map_message_edited_and_reply():
    edited = datetime(2025, 10, 15, 3, 5, 0, tzinfo=timezone.utc)
    msg = FakeMessage(
        id=102, channel_id=1, author_id=2, content="fixed", created_at=CREATED,
        edited_at=edited, reference=FakeMessageReference(message_id=100),
    )
    mapped = map_message(msg)
    assert mapped["message"]["edited_utc"] == "2025-10-15T03:05:00Z"
    assert mapped["message"]["reply_to_id"] == "100"


def test_map_message_mention_everyone_and_flags():
    msg = FakeMessage(
        id=103, channel_id=1, author_id=2, content="@everyone hi", created_at=CREATED,
        mention_everyone=True, flags_value=64,
    )
    mapped = map_message(msg)
    assert mapped["message"]["mention_everyone"] == 1
    assert mapped["message"]["flags"] == 64


def test_map_message_always_has_all_child_keys_even_when_empty():
    msg = FakeMessage(id=104, channel_id=1, author_id=2, content="plain", created_at=CREATED)
    mapped = map_message(msg)
    assert set(mapped.keys()) == {
        "message", "attachments", "reactions", "mentions_user", "mentions_role",
        "stickers", "poll", "poll_answers", "embeds", "embed_fields",
    }
    assert mapped["attachments"] == []
    assert mapped["reactions"] == []
    assert mapped["poll"] is None


import discord

from tests.discord_fakes import FakePoll, FakePollAnswer, FakeReaction, FakeSticker


def test_map_message_reactions_unicode_and_custom():
    msg = FakeMessage(
        id=200, channel_id=1, author_id=2, content="funny", created_at=CREATED,
        reactions=[
            FakeReaction(emoji="\U0001F602", count=3),
            FakeReaction(emoji="<:pog:999>", count=1, custom=True, animated=True),
        ],
    )
    mapped = map_message(msg)
    assert mapped["reactions"] == [
        {"message_id": "200", "emoji": "\U0001F602", "is_custom": 0, "animated": 0, "count": 3},
        {"message_id": "200", "emoji": "<:pog:999>", "is_custom": 1, "animated": 1, "count": 1},
    ]


def test_map_message_mentions_and_stickers():
    from tests.discord_fakes import FakeUserRef
    role = type("FakeRole", (), {"id": 77})()
    msg = FakeMessage(
        id=201, channel_id=1, author_id=2, content="hi @you", created_at=CREATED,
        mentions=[FakeUserRef(id=42)], role_mentions=[role],
        stickers=[FakeSticker(id=900, name="PogChamp")],
    )
    mapped = map_message(msg)
    assert mapped["mentions_user"] == [{"message_id": "201", "user_id": "42"}]
    assert mapped["mentions_role"] == [{"message_id": "201", "role_id": "77"}]
    assert mapped["stickers"] == [{"message_id": "201", "sticker_id": "900", "name": "PogChamp"}]


def test_map_message_poll():
    expires = datetime(2025, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
    poll = FakePoll(
        question="Best game?", multiple=False, expires_at=expires,
        answers=[FakePollAnswer(id=1, text="Chess", vote_count=3), FakePollAnswer(id=2, text="Go", vote_count=5)],
    )
    msg = FakeMessage(id=202, channel_id=1, author_id=2, content="", created_at=CREATED, poll=poll)
    mapped = map_message(msg)
    assert mapped["poll"] == {
        "message_id": "202", "question": "Best game?", "multiselect": 0,
        "expires_utc": "2025-10-20T00:00:00Z",
    }
    assert mapped["poll_answers"] == [
        {"message_id": "202", "answer_id": 1, "text": "Chess", "vote_count": 3},
        {"message_id": "202", "answer_id": 2, "text": "Go", "vote_count": 5},
    ]


def test_map_message_embeds_with_fields():
    embed = discord.Embed(title="A link preview", description="desc", url="https://example.com")
    embed.add_field(name="Field One", value="Value One", inline=True)
    msg = FakeMessage(id=203, channel_id=1, author_id=2, content="check this", created_at=CREATED, embeds=[embed])
    mapped = map_message(msg)
    assert mapped["embeds"] == [{
        "message_id": "203", "position": 0, "title": "A link preview",
        "description": "desc", "url": "https://example.com", "embed_type": "rich",
    }]
    assert mapped["embed_fields"] == [{
        "message_id": "203", "embed_position": 0, "position": 0,
        "name": "Field One", "value": "Value One", "inline": 1,
    }]


def test_map_message_poll_against_real_discord_poll_object():
    """Regression: discord.Poll.question is a str property
    (self._question_media.text internally), NOT an object with its own
    .text attribute. The hand-built FakePoll originally wrapped question
    in a FakePollMedia(text=...), which let map_message's buggy
    `message.poll.question.text` pass all unit tests while crashing on
    the real server with "'str' object has no attribute 'text'" the
    first time it hit an actual poll. This test uses a real
    discord.Poll (directly constructible, no live connection needed) so
    a reintroduced mismatch is caught here, not in production."""
    poll = discord.Poll(question="Best game?", duration=discord.utils.MISSING)
    msg = FakeMessage(id=204, channel_id=1, author_id=2, content="", created_at=CREATED)
    msg.poll = poll

    mapped = map_message(msg)

    assert mapped["poll"]["question"] == "Best game?"

"""Pure mapping from a discord.py Message (or a fake shaped like one)
into row dicts matching the Stage 1 shard schema. No I/O, no discord.py
Client -- fully unit-testable with hand-built fakes."""
from datetime import datetime


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def map_message(message) -> dict:
    reply_to_id = None
    if message.reference is not None and message.reference.message_id is not None:
        reply_to_id = str(message.reference.message_id)

    row = {
        "id": str(message.id),
        "channel_id": str(message.channel.id),
        "author_id": str(message.author.id),
        "content": message.content or "",
        "created_utc": _iso(message.created_at),
        "edited_utc": _iso(message.edited_at) if message.edited_at else None,
        "reply_to_id": reply_to_id,
        "mention_everyone": int(message.mention_everyone),
        "flags": message.flags.value,
        "deleted_utc": None,
    }

    attachments = [
        {
            "id": str(a.id), "message_id": row["id"], "filename": a.filename,
            "content_type": a.content_type, "size": a.size,
            "width": a.width, "height": a.height,
            "duration_secs": a.duration, "description": a.description,
        }
        for a in message.attachments
    ]

    reactions = [
        {
            "message_id": row["id"], "emoji": str(r.emoji),
            "is_custom": int(r.is_custom_emoji()),
            "animated": int(getattr(r.emoji, "animated", False)),
            "count": r.count,
        }
        for r in message.reactions
    ]

    mentions_user = [{"message_id": row["id"], "user_id": str(u.id)} for u in message.mentions]
    mentions_role = [{"message_id": row["id"], "role_id": str(r.id)} for r in message.role_mentions]
    stickers = [
        {"message_id": row["id"], "sticker_id": str(s.id), "name": s.name}
        for s in message.stickers
    ]

    poll = None
    poll_answers = []
    if message.poll is not None:
        poll = {
            "message_id": row["id"],
            "question": message.poll.question,
            "multiselect": int(message.poll.multiple),
            "expires_utc": _iso(message.poll.expires_at) if message.poll.expires_at else None,
        }
        poll_answers = [
            {"message_id": row["id"], "answer_id": a.id, "text": a.text, "vote_count": a.vote_count}
            for a in message.poll.answers
        ]

    embeds = []
    embed_fields = []
    for position, e in enumerate(message.embeds):
        embeds.append({
            "message_id": row["id"], "position": position,
            "title": e.title, "description": e.description,
            "url": e.url, "embed_type": e.type,
        })
        for field_position, f in enumerate(e.fields):
            embed_fields.append({
                "message_id": row["id"], "embed_position": position,
                "position": field_position, "name": f.name, "value": f.value,
                "inline": int(f.inline),
            })

    return {
        "message": row, "attachments": attachments, "reactions": reactions,
        "mentions_user": mentions_user, "mentions_role": mentions_role,
        "stickers": stickers, "poll": poll, "poll_answers": poll_answers,
        "embeds": embeds, "embed_fields": embed_fields,
    }

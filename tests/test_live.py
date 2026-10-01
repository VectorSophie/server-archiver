from datetime import datetime, timezone

import discord

from archiver.db import connect_catalog
from archiver.live import apply_live_message
from archiver.store import ShardStore
from tests.discord_fakes import FakeMessage

CREATED = datetime(2025, 10, 15, 3, 0, 0, tzinfo=timezone.utc)


def _seed_channel(catalog, channel_id="1", category_id="10"):
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES "
        f"('{channel_id}','general','text','{category_id}','{category_id}',0,"
        "'2025-10-15T00:00:00Z','2025-10-15T00:00:00Z')"
    )
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES "
        f"('{category_id}','Friends','2025-10-15T00:00:00Z')"
    )
    catalog.execute(f"INSERT INTO coverage (channel_id, status) VALUES ('{channel_id}', 'complete')")
    catalog.commit()


def test_apply_live_message_writes_and_advances_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED)

    apply_live_message(store, catalog, msg)

    row = catalog.execute(
        "SELECT live_checkpoint, message_count FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["live_checkpoint"] == "100"
    assert row["message_count"] == 1
    shard = store.get_shard("1", CREATED)
    assert shard.execute("SELECT content FROM messages WHERE id='100'").fetchone()["content"] == "hi"


def test_apply_live_message_never_moves_checkpoint_backward(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    newer = FakeMessage(id=200, channel_id=1, author_id=2, content="b", created_at=CREATED)
    older = FakeMessage(id=100, channel_id=1, author_id=2, content="a", created_at=CREATED)

    apply_live_message(store, catalog, newer)
    apply_live_message(store, catalog, older)  # arrives out of order

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "200"  # stays at the newer id, not overwritten by the older one


def test_apply_live_message_checkpoint_compares_as_integer_not_text(tmp_path):
    """A larger-digit-count snowflake must compare correctly against a
    smaller one -- TEXT '>' would get this backwards (e.g. '9' > '10')."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    small_digit_count = FakeMessage(id=9, channel_id=1, author_id=2, content="a", created_at=CREATED)
    large_digit_count = FakeMessage(id=10, channel_id=1, author_id=2, content="b", created_at=CREATED)

    apply_live_message(store, catalog, small_digit_count)
    apply_live_message(store, catalog, large_digit_count)

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "10"


def test_apply_live_message_writes_but_skips_checkpoint_when_advance_checkpoint_false(tmp_path):
    """Finding #1: a channel still mid-catch-up must have its message
    written (so nothing is lost) without the checkpoint jumping past
    the still-unfinished gap. message_count still increments -- the
    message is genuinely new to the archive even though the channel
    hasn't finished catch-up; checkpoint-advance and new-message
    counting are independent facts (see apply_live_message)."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED)

    apply_live_message(store, catalog, msg, advance_checkpoint=False)

    row = catalog.execute(
        "SELECT live_checkpoint, message_count FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["live_checkpoint"] is None
    assert row["message_count"] == 1
    shard = store.get_shard("1", CREATED)
    assert shard.execute("SELECT content FROM messages WHERE id='100'").fetchone()["content"] == "hi"


def test_apply_live_message_populates_users_table(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED,
                       author_name="alice")

    apply_live_message(store, catalog, msg)

    row = catalog.execute("SELECT username FROM users WHERE id='2'").fetchone()
    assert row is not None
    assert row["username"] == "alice"


def test_apply_live_message_ignores_untracked_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    store = ShardStore(tmp_path, catalog)  # no channel seeded at all
    msg = FakeMessage(id=100, channel_id=999, author_id=2, content="hi", created_at=CREATED)

    apply_live_message(store, catalog, msg)  # must not raise

    row = catalog.execute("SELECT 1 FROM coverage WHERE channel_id='999'").fetchone()
    assert row is None


def test_apply_live_message_does_not_double_count_a_replayed_message(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hello", created_at=CREATED)

    apply_live_message(store, catalog, msg)  # first delivery
    apply_live_message(store, catalog, msg)  # duplicate delivery of the same id (e.g. rescan)

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 1


def test_apply_live_message_does_not_count_a_message_backfill_already_archived(tmp_path):
    """The previous test (same id delivered twice via apply_live_message)
    doesn't actually discriminate correct from buggy code, since the
    live_checkpoint WHERE clause alone already blocks a same-id replay
    even under the old, coupled counting logic. This test seeds the
    shard directly (simulating a prior backfill write) with the
    catalog's message_count/live_checkpoint still untouched, then
    delivers the same id live -- this is the scenario the bug fix
    actually targets: a message already archived by a different
    operation must not be double-counted when live capture also
    touches it."""
    from archiver.discord_message import map_message
    from archiver.store import write_message

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hello", created_at=CREATED)

    shard_conn = store.get_shard("1", CREATED)
    write_message(shard_conn, map_message(msg))
    shard_conn.commit()

    apply_live_message(store, catalog, msg)  # live delivery of the already-archived id

    row = catalog.execute(
        "SELECT message_count, live_checkpoint FROM coverage WHERE channel_id='1'"
    ).fetchone()
    assert row["message_count"] == 0  # not double-counted
    assert row["live_checkpoint"] == "100"  # checkpoint still advances correctly


from archiver.discord_message import map_message
from archiver.live import apply_raw_edit
from archiver.store import write_message


def _seed_message(store, catalog, channel_id, message_id, content="original", flags=0):
    msg = FakeMessage(id=message_id, channel_id=int(channel_id), author_id=2, content=content,
                       created_at=CREATED, flags_value=flags)
    # Shard placement must match apply_raw_edit's own lookup, which derives
    # created_at from the message id via discord.utils.snowflake_time --
    # for a real Discord id this coincides with CREATED, but a synthetic
    # test id (e.g. 100) decodes to the Discord epoch (2015-01-01), not
    # CREATED (2025-10-15), landing in a different shard file. Seed at the
    # id-derived date so the fixture matches what apply_raw_edit will find.
    shard_conn = store.get_shard(channel_id, discord.utils.snowflake_time(message_id))
    write_message(shard_conn, map_message(msg))
    shard_conn.commit()
    return shard_conn


def test_apply_raw_edit_updates_only_present_fields(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100, content="original", flags=0)

    apply_raw_edit(store, catalog, "1", {"id": "100", "content": "edited content"})

    row = shard_conn.execute("SELECT content, flags, edited_utc FROM messages WHERE id='100'").fetchone()
    assert row["content"] == "edited content"
    assert row["flags"] == 0  # untouched -- absent from the raw payload
    assert row["edited_utc"] is None  # untouched -- absent from the raw payload


def test_apply_raw_edit_sets_edited_utc_when_present(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_edit(store, catalog, "1", {
        "id": "100", "content": "edited", "edited_timestamp": "2025-10-15T04:00:00.000000+00:00",
    })

    row = shard_conn.execute("SELECT edited_utc FROM messages WHERE id='100'").fetchone()
    assert row["edited_utc"] == "2025-10-15T04:00:00Z"


def test_apply_raw_edit_never_advances_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    _seed_message(store, catalog, "1", 100)

    apply_raw_edit(store, catalog, "1", {"id": "100", "content": "edited"})

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] is None  # writing a message did NOT go through apply_live_message


def test_apply_raw_edit_on_message_never_archived_is_a_safe_no_op(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)

    apply_raw_edit(store, catalog, "1", {"id": "999", "content": "edited"})  # must not raise

    shard_conn = store.get_shard("1", CREATED)
    assert shard_conn.execute("SELECT 1 FROM messages WHERE id='999'").fetchone() is None


def test_apply_raw_edit_logs_an_edit_event(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_edit(store, catalog, "1", {"id": "100", "content": "edited"})

    row = shard_conn.execute(
        "SELECT event_type FROM events WHERE message_id='100' AND event_type='edit'"
    ).fetchone()
    assert row is not None


from archiver.live import apply_raw_delete


def test_apply_raw_delete_marks_deleted_utc(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_delete(store, catalog, "1", 100)

    row = shard_conn.execute("SELECT deleted_utc FROM messages WHERE id='100'").fetchone()
    assert row["deleted_utc"] is not None


def test_apply_raw_delete_logs_a_delete_event(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_delete(store, catalog, "1", 100)

    row = shard_conn.execute(
        "SELECT event_type FROM events WHERE message_id='100' AND event_type='delete'"
    ).fetchone()
    assert row is not None


def test_apply_raw_delete_on_message_never_archived_does_not_log_event(tmp_path):
    """spec §12: absence is never itself evidence of deletion -- this
    function must not fabricate a delete event for a message it never
    actually confirmed existed in the archive."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)

    apply_raw_delete(store, catalog, "1", 999)  # must not raise

    shard_conn = store.get_shard("1", CREATED)
    row = shard_conn.execute("SELECT 1 FROM events WHERE message_id='999'").fetchone()
    assert row is None


def test_apply_raw_delete_is_idempotent(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    store = ShardStore(tmp_path, catalog)
    shard_conn = _seed_message(store, catalog, "1", 100)

    apply_raw_delete(store, catalog, "1", 100)
    apply_raw_delete(store, catalog, "1", 100)  # a duplicate gateway delivery

    count = shard_conn.execute("SELECT COUNT(*) FROM events WHERE message_id='100'").fetchone()[0]
    assert count == 1  # second call is a no-op, not a duplicate event


from archiver.live import catch_up_missed_messages
from tests.discord_fakes import FakeClient, FakeHistoryChannel, FakeResponse


async def test_catch_up_missed_messages_fetches_after_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    missed = FakeMessage(id=101, channel_id=1, author_id=2, content="missed while offline", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[missed])})

    await catch_up_missed_messages(client, catalog, store)

    row = catalog.execute("SELECT live_checkpoint, message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "101"
    assert row["message_count"] == 1


async def test_catch_up_missed_messages_falls_back_to_backfill_checkpoint_when_live_checkpoint_unset(tmp_path):
    """The first-ever live startup after a channel finishes backfill has
    no live_checkpoint yet -- must resume from backfill_checkpoint, not
    from the beginning of the channel."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', backfill_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    missed = FakeMessage(id=101, channel_id=1, author_id=2, content="missed", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[missed])})

    await catch_up_missed_messages(client, catalog, store)

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='1'").fetchone()
    assert row["message_count"] == 1


async def test_catch_up_missed_messages_skips_channel_with_no_history_method(tmp_path):
    """Regression: same class of bug as Stage 3's forum-channel crash --
    a 'complete' coverage row can belong to a forum/media parent, which
    has no .history() at all."""
    class _FakeForumChannel:
        def __init__(self, id):
            self.id = id

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({1: _FakeForumChannel(id=1)})

    await catch_up_missed_messages(client, catalog, store)  # must not raise


async def test_catch_up_missed_messages_isolates_one_channels_http_error_from_the_rest(tmp_path):
    """A transient discord.HTTPException mid-.history() for one channel
    must not abort the sweep for the other channels (same isolation
    pattern as fetch_channel's NotFound/Forbidden guard, and the same
    class of bug already fixed for Stage 2's on_ready hang and Stage 3's
    per-channel backfill isolation)."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1", category_id="10")
    _seed_channel(catalog, channel_id="2", category_id="20")
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='2'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    ok_message = FakeMessage(id=101, channel_id=2, author_id=2, content="still applied", created_at=CREATED)
    client = FakeClient({
        1: FakeHistoryChannel(id=1, messages=[], http_error=True),
        2: FakeHistoryChannel(id=2, messages=[ok_message]),
    })

    await catch_up_missed_messages(client, catalog, store)  # must not raise

    row = catalog.execute("SELECT message_count FROM coverage WHERE channel_id='2'").fetchone()
    assert row["message_count"] == 1


async def test_catch_up_missed_messages_adds_channel_to_caught_up_channels_on_success(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    missed = FakeMessage(id=101, channel_id=1, author_id=2, content="missed", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[missed])})
    caught_up_channels: set = set()

    await catch_up_missed_messages(client, catalog, store, caught_up_channels)

    assert caught_up_channels == {"1"}


async def test_catch_up_missed_messages_does_not_add_channel_on_http_error(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[], http_error=True)})
    caught_up_channels: set = set()

    await catch_up_missed_messages(client, catalog, store, caught_up_channels)  # must not raise

    assert caught_up_channels == set()


async def test_catch_up_missed_messages_does_not_add_channel_on_forbidden_fetch(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete', live_checkpoint='100' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({}, errors={1: discord.Forbidden(FakeResponse(), "Missing Permissions")})
    caught_up_channels: set = set()

    await catch_up_missed_messages(client, catalog, store, caught_up_channels)  # must not raise

    assert caught_up_channels == set()


from archiver.live import rescan_recent_window


async def test_rescan_recent_window_reapplies_recent_messages(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    edited_elsewhere = FakeMessage(id=100, channel_id=1, author_id=2, content="edited via rescan", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[edited_elsewhere])})

    await rescan_recent_window(client, catalog, store, days=3)

    shard = store.get_shard("1", CREATED)
    row = shard.execute("SELECT content FROM messages WHERE id='100'").fetchone()
    assert row["content"] == "edited via rescan"


async def test_rescan_recent_window_never_touches_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute(
        "UPDATE coverage SET status='complete', live_checkpoint='50' WHERE channel_id='1'"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="x", created_at=CREATED)
    client = FakeClient({1: FakeHistoryChannel(id=1, messages=[msg])})

    await rescan_recent_window(client, catalog, store, days=3)

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "50"  # unchanged despite message id 100 > 50


async def test_rescan_recent_window_skips_channel_with_no_history_method(tmp_path):
    class _FakeForumChannel:
        def __init__(self, id):
            self.id = id

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    client = FakeClient({1: _FakeForumChannel(id=1)})

    await rescan_recent_window(client, catalog, store, days=3)  # must not raise


async def test_rescan_recent_window_isolates_one_channels_http_error_from_the_rest(tmp_path):
    """A transient discord.HTTPException mid-.history() for one channel
    must not abort the rescan for the other channels (same isolation
    pattern as catch_up_missed_messages)."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1", category_id="10")
    _seed_channel(catalog, channel_id="2", category_id="20")
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='2'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)
    ok_message = FakeMessage(id=101, channel_id=2, author_id=2, content="still reapplied", created_at=CREATED)
    client = FakeClient({
        1: FakeHistoryChannel(id=1, messages=[], http_error=True),
        2: FakeHistoryChannel(id=2, messages=[ok_message]),
    })

    await rescan_recent_window(client, catalog, store, days=3)  # must not raise

    shard = store.get_shard("2", CREATED)
    row = shard.execute("SELECT content FROM messages WHERE id='101'").fetchone()
    assert row["content"] == "still reapplied"


from archiver.live import apply_live_thread_create
from tests.discord_fakes import FakeThread


def test_apply_live_thread_create_adds_channel_and_pending_coverage_row(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)  # seeds channel "1" as the thread's parent
    thread = FakeThread(id=999, name="new thread", parent_id=1)

    apply_live_thread_create(catalog, thread)

    channel_row = catalog.execute("SELECT name, type, parent_id FROM channels WHERE id='999'").fetchone()
    assert channel_row["name"] == "new thread"
    assert channel_row["type"] == "public_thread"
    assert channel_row["parent_id"] == "1"
    coverage_row = catalog.execute("SELECT status FROM coverage WHERE channel_id='999'").fetchone()
    assert coverage_row["status"] == "pending"


def test_apply_live_thread_create_inherits_parent_category(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog, channel_id="1", category_id="cat1")
    thread = FakeThread(id=999, name="new thread", parent_id=1)

    apply_live_thread_create(catalog, thread)

    row = catalog.execute("SELECT category_id FROM channels WHERE id='999'").fetchone()
    assert row["category_id"] == "cat1"


def test_apply_live_thread_create_is_idempotent_on_replay(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    thread = FakeThread(id=999, name="new thread", parent_id=1)

    apply_live_thread_create(catalog, thread)
    apply_live_thread_create(catalog, thread)  # e.g. a duplicate gateway event

    rows = catalog.execute("SELECT 1 FROM coverage WHERE channel_id='999'").fetchall()
    assert len(rows) == 1  # not duplicated, and status stays 'pending' not reset


from archiver.live import apply_live_reaction_change
from tests.discord_fakes import FakeHistoryChannel, FakeClient, FakeReaction


async def test_apply_live_reaction_change_refetches_and_updates_reaction_count(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED,
                       reactions=[FakeReaction("\N{THUMBS UP SIGN}", count=3)])
    channel = FakeHistoryChannel(id=1, messages=[msg])
    client = FakeClient(channels={1: channel})

    await apply_live_reaction_change(client, store, catalog, "1", 100)

    shard = store.get_shard("1", CREATED)
    row = shard.execute("SELECT count FROM reactions WHERE message_id='100'").fetchone()
    assert row["count"] == 3


async def test_apply_live_reaction_change_never_touches_live_checkpoint(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute(
        "UPDATE coverage SET status='complete', live_checkpoint='50' WHERE channel_id='1'"
    )
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    msg = FakeMessage(id=100, channel_id=1, author_id=2, content="hi", created_at=CREATED)
    channel = FakeHistoryChannel(id=1, messages=[msg])
    client = FakeClient(channels={1: channel})

    await apply_live_reaction_change(client, store, catalog, "1", 100)

    row = catalog.execute("SELECT live_checkpoint FROM coverage WHERE channel_id='1'").fetchone()
    assert row["live_checkpoint"] == "50"  # unchanged despite message id 100 > 50


async def test_apply_live_reaction_change_isolates_a_message_fetch_failure(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_channel(catalog)
    catalog.execute("UPDATE coverage SET status='complete' WHERE channel_id='1'")
    catalog.commit()
    store = ShardStore(tmp_path, catalog)

    channel = FakeHistoryChannel(id=1, messages=[])  # message 100 doesn't exist -> fetch_message raises NotFound
    client = FakeClient(channels={1: channel})

    await apply_live_reaction_change(client, store, catalog, "1", 100)  # must not raise


from archiver.discovery import discover_guild
from archiver.live import run_periodic_rediscovery_once
from tests.discord_fakes import FakeChannel, FakeGuild


async def test_run_periodic_rediscovery_once_runs_discover_guild(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    channel = FakeChannel(id=1, name="general", type_=discord.ChannelType.text)
    guild = FakeGuild([channel])
    client = FakeClient(guild=guild)

    await run_periodic_rediscovery_once(client, "999", catalog)

    row = catalog.execute("SELECT 1 FROM channels WHERE id='1'").fetchone()
    assert row is not None


async def test_run_periodic_rediscovery_once_no_guild_does_not_raise(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    client = FakeClient(guild=None)

    await run_periodic_rediscovery_once(client, "999", catalog)  # must not raise


async def test_run_periodic_rediscovery_once_isolates_a_discover_guild_failure(tmp_path, monkeypatch):
    import archiver.live as live_module

    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    guild = FakeGuild([])
    client = FakeClient(guild=guild)

    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(live_module, "discover_guild", _raise)

    await run_periodic_rediscovery_once(client, "999", catalog)  # must not raise

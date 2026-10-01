from pathlib import Path

from archiver.db import connect_catalog, connect_shard
from archiver.reports import ScopeStats, gather_coverage, gather_scope_stats, channels_by_scope, classify_attachment, render_report
from tests.fixtures import make_message, make_attachment, _insert


def _seed(catalog):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'General', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('2', 'misc', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO coverage (channel_id, status, oldest_message_id, newest_message_id, "
        "message_count, gap_reason) VALUES ('1', 'complete', '10', '99', 5, NULL)"
    )
    catalog.execute(
        "INSERT INTO coverage (channel_id, status, oldest_message_id, newest_message_id, "
        "message_count, gap_reason) VALUES ('2', 'inaccessible', NULL, NULL, 0, 'no longer discoverable')"
    )
    catalog.commit()


def test_gather_coverage_includes_category_name_and_status(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog)
    rows = gather_coverage(catalog)
    by_id = {r["channel_id"]: r for r in rows}
    assert by_id["1"]["category_name"] == "General"
    assert by_id["1"]["status"] == "complete"
    assert by_id["1"]["message_count"] == 5
    assert by_id["2"]["category_name"] is None
    assert by_id["2"]["gap_reason"] == "no longer discoverable"


def test_channels_by_scope_groups_server_and_categories(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed(catalog)
    scopes = channels_by_scope(catalog)
    assert sorted(scopes["server"]) == ["1", "2"]
    assert scopes["category:cat1"] == ["1"]
    assert scopes["category:uncategorized"] == ["2"]


def test_classify_attachment_by_content_type_prefix():
    assert classify_attachment("image/png") == "image"
    assert classify_attachment("video/mp4") == "video"
    assert classify_attachment("audio/mpeg") == "audio"
    assert classify_attachment("application/pdf") == "file"
    assert classify_attachment(None) == "file"


def _write_shard_with_messages(path: Path, messages, attachments_by_msg=None):
    conn = connect_shard(path)
    for msg in messages:
        _insert(conn, "messages", msg)
    for msg_id, atts in (attachments_by_msg or {}).items():
        for att in atts:
            _insert(conn, "attachments", att)
    conn.commit()
    conn.close()


def test_gather_scope_stats_counts_authors_words_and_attachments(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chat" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)

    m1 = make_message(content="hello world", author_id="alice", channel_id="1")
    m2 = make_message(content="hello there", author_id="alice", channel_id="1")
    m3 = make_message(content="world peace", author_id="bob", channel_id="1")
    _write_shard_with_messages(shard_path, [m1, m2, m3], {
        m1["id"]: [make_attachment(m1["id"], content_type="image/png")],
    })

    stats = gather_scope_stats(catalog, tmp_path, ["1"])
    assert stats.total_messages == 3
    assert dict(stats.top_authors)["alice"] == 2
    assert dict(stats.top_authors)["bob"] == 1
    assert dict(stats.top_words)["hello"] == 2
    assert dict(stats.top_words)["world"] == 2
    assert stats.attachment_counts["image"] == 1


def test_gather_scope_stats_excludes_configured_authors_from_word_and_poster_ranking(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chat" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)

    bot_msg = make_message(content="automated spam words", author_id="bot1", channel_id="1")
    human_msg = make_message(content="real conversation", author_id="alice", channel_id="1")
    _write_shard_with_messages(shard_path, [bot_msg, human_msg])

    stats = gather_scope_stats(catalog, tmp_path, ["1"], excluded_author_ids={"bot1"})
    assert stats.total_messages == 2  # bot message still counted in the archive/total
    assert "bot1" not in dict(stats.top_authors)
    assert "automated" not in dict(stats.top_words)
    assert "real" in dict(stats.top_words)


def test_render_report_states_counting_rules_explicitly():
    stats = ScopeStats(total_messages=10, top_authors=[("alice", 6), ("bob", 4)],
                        top_words=[("hello", 3)], attachment_counts={"image": 2, "file": 1})
    coverage_rows = [
        {"channel_id": "1", "name": "chat", "type": "text", "status": "complete",
         "gap_reason": None, "message_count": 10},
    ]
    text = render_report("Server", coverage_rows, stats, newly_inaccessible=[])
    assert "image" in text.lower()
    assert "content_type" in text.lower() or "starting" in text.lower()  # counting rule stated
    assert "whitespace" in text.lower() or "punctuation" in text.lower()  # word-tokenization rule stated
    assert "alice" in text
    assert "chat" in text
    assert "never decremented" in text.lower()
    assert "excluded from rankings" in text.lower()


def test_render_report_resolves_known_usernames_falls_back_to_id_for_unknown():
    stats = ScopeStats(total_messages=10, top_authors=[("20", 6), ("21", 4)],
                        top_words=[], attachment_counts={})
    coverage_rows = [
        {"channel_id": "1", "name": "chat", "type": "text", "status": "complete",
         "gap_reason": None, "message_count": 10},
    ]
    text = render_report("Server", coverage_rows, stats, newly_inaccessible=[],
                          usernames={"20": "alice"})
    assert "alice" in text
    assert "21" in text  # no users row for "21" -- falls back to the raw id


def test_render_report_surfaces_newly_inaccessible_channels():
    stats = ScopeStats(total_messages=0, top_authors=[], top_words=[], attachment_counts={})
    coverage_rows = [
        {"channel_id": "2", "name": "gone", "type": "text", "status": "inaccessible",
         "gap_reason": "no longer discoverable", "message_count": 0},
    ]
    newly_inaccessible = [{"channel_id": "2", "name": "gone", "status": "inaccessible"}]
    text = render_report("Server", coverage_rows, stats, newly_inaccessible)
    assert "gone" in text
    assert "newly" in text.lower() or "just" in text.lower() or "since the last report" in text.lower()


def test_gather_scope_stats_reuses_cache_across_calls_for_the_same_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chat" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    _write_shard_with_messages(shard_path, [make_message(content="hello", channel_id="1")])

    cache: dict = {}
    first = gather_scope_stats(catalog, tmp_path, ["1"], channel_stats_cache=cache)
    assert "1" in cache

    # Delete the shard file entirely -- if gather_scope_stats re-scans
    # channel "1" instead of using the cache, this would now raise or
    # return zero messages instead of the cached count.
    shard_path.unlink()
    second = gather_scope_stats(catalog, tmp_path, ["1"], channel_stats_cache=cache)
    assert second.total_messages == first.total_messages == 1


def test_gather_scope_stats_skips_a_missing_shard_file_without_creating_it(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    # Note: the shard file/directory is never created on disk.

    stats = gather_scope_stats(catalog, tmp_path, ["1"])
    assert stats.total_messages == 0
    assert not (tmp_path / "uncategorized" / "chat" / "2025-10.sqlite").exists()


def test_gather_scope_stats_strips_urls_and_stopwords_from_word_rankings(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chat" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)

    msg = make_message(
        content="i think the signal is at https://example.com/path?x=1 check it out",
        author_id="alice", channel_id="1",
    )
    _write_shard_with_messages(shard_path, [msg])

    stats = gather_scope_stats(catalog, tmp_path, ["1"])
    words = dict(stats.top_words)
    assert "signal" in words and "check" in words  # real content words survive
    for stopword in ("i", "the", "is", "at", "it"):
        assert stopword not in words
    for url_fragment in ("https", "example", "com", "path", "x"):
        assert url_fragment not in words


def test_render_report_escapes_pipe_characters_in_table_cells():
    stats = ScopeStats(total_messages=1, top_authors=[("alice|bob", 1)],
                        top_words=[("a|b", 1)], attachment_counts={})
    coverage_rows = [
        {"channel_id": "1", "name": "chat|room", "type": "text", "status": "complete",
         "gap_reason": "weird|reason", "message_count": 1},
    ]
    text = render_report("Server", coverage_rows, stats, newly_inaccessible=[])
    # A literal "|" from data must appear escaped, not as a raw column separator.
    assert "chat\\|room" in text
    assert "weird\\|reason" in text
    assert "alice\\|bob" in text
    assert "a\\|b" in text


def test_gather_scope_stats_excludes_system_messages_from_rankings_not_from_total(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/chat/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "chat" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)

    pin_notice = make_message(content="", author_id="alice", channel_id="1", message_type=6)  # channel_pinned_message
    real_msg = make_message(content="hello everyone", author_id="alice", channel_id="1", message_type=0)
    _write_shard_with_messages(shard_path, [pin_notice, real_msg])

    stats = gather_scope_stats(catalog, tmp_path, ["1"])
    assert stats.total_messages == 2  # both counted in the archive total
    assert dict(stats.top_authors)["alice"] == 1  # only the real message counted toward ranking

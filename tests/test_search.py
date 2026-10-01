from datetime import datetime, timezone
from pathlib import Path

from archiver.db import connect_catalog, connect_shard
from archiver.search import (
    ParsedQuery,
    ResolvedQuery,
    build_message_sql,
    candidate_shards,
    format_result,
    get_context,
    resolve_query,
    search,
    tokenize_query,
)
from tests.fixtures import make_message, make_attachment, _insert


def test_tokenize_plain_text():
    result = tokenize_query("hello world")
    assert result.text == "hello world"
    assert result.filters == {}


def test_tokenize_quoted_phrase_stays_together():
    result = tokenize_query('"hello world" foo')
    assert result.text == "hello world foo"


def test_tokenize_single_filter():
    result = tokenize_query("hello from:alice")
    assert result.text == "hello"
    assert result.filters == {"from": ["alice"]}


def test_tokenize_multiple_filters_and_text():
    result = tokenize_query("hello in:general has:image from:Bob world")
    assert result.text == "hello world"
    assert result.filters == {"in": ["general"], "has": ["image"], "from": ["Bob"]}


def test_tokenize_filter_key_is_case_insensitive():
    result = tokenize_query("FROM:alice")
    assert result.filters == {"from": ["alice"]}


def test_tokenize_unknown_key_colon_is_free_text():
    result = tokenize_query("see https://example.com/path")
    assert result.text == "see https://example.com/path"
    assert result.filters == {}


def test_tokenize_repeated_filter_key_accumulates():
    result = tokenize_query("in:general in:random")
    assert result.filters == {"in": ["general", "random"]}


def test_tokenize_empty_query():
    result = tokenize_query("")
    assert result.text == ""
    assert result.filters == {}


def _seed_catalog(catalog):
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'random-chat', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('20', 'alice', ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO user_nicknames (user_id, nickname, observed_utc) VALUES ('20', 'ally', ?)",
        (now,),
    )
    catalog.commit()


def test_resolve_in_filter_matches_channel_by_name_substring(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("in:general"))
    assert resolved.channel_ids == ["10"]


def test_resolve_in_filter_by_numeric_id_bypasses_name_lookup(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("in:10"))
    assert resolved.channel_ids == ["10"]


def test_resolve_in_filter_no_match_gives_empty_list_not_none(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("in:nonexistent"))
    assert resolved.channel_ids == []


def test_resolve_no_in_filter_gives_none(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("hello"))
    assert resolved.channel_ids is None


def test_resolve_from_filter_matches_username_or_nickname(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    by_username = resolve_query(catalog, tokenize_query("from:alice"))
    by_nickname = resolve_query(catalog, tokenize_query("from:ally"))
    assert by_username.author_ids == ["20"]
    assert by_nickname.author_ids == ["20"]


def test_resolve_from_filter_does_not_treat_underscore_as_a_wildcard(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('30', 'axb', ?, ?)", (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("from:a_b"))
    assert resolved.author_ids == []  # "a_b" must not match "axb" via LIKE's _ wildcard


def test_resolve_in_filter_does_not_treat_underscore_as_a_wildcard(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('30', 'genxral', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:gen_ral"))
    assert resolved.channel_ids == []  # "gen_ral" must not match "genxral" via LIKE's _ wildcard


def test_resolve_during_month_gives_seoul_month_bounds_in_utc(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("during:2025-10"))
    # 2025-10-01T00:00:00 KST == 2025-09-30T15:00:00 UTC
    assert resolved.after_utc == "2025-09-30T15:00:00Z"
    assert resolved.before_utc == "2025-10-31T15:00:00Z"


def test_resolve_during_day_gives_seoul_day_bounds_in_utc(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("during:2025-10-15"))
    assert resolved.after_utc == "2025-10-14T15:00:00Z"
    assert resolved.before_utc == "2025-10-15T15:00:00Z"


def test_resolve_has_filter_keeps_only_known_values(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("has:image has:bogus"))
    assert resolved.has == {"image"}


def test_resolve_file_and_ext_filters(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_catalog(catalog)
    resolved = resolve_query(catalog, tokenize_query("file:report ext:.PDF"))
    assert resolved.file_substr == "report"
    assert resolved.ext == "pdf"


def _seed_shard_rows(catalog):
    now = "2025-10-15T00:00:00Z"
    for cid in ("10", "11"):
        catalog.execute(
            "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
            "first_seen_utc, last_seen_utc) VALUES (?, 'chan', 'text', NULL, NULL, 0, ?, ?)",
            (cid, now, now),
        )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-09', 'uncategorized', 'a/2025-09.sqlite')"
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'a/2025-10.sqlite')"
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('11', '2025-10', 'uncategorized', 'b/2025-10.sqlite')"
    )
    catalog.commit()


def _empty_resolved(**overrides) -> ResolvedQuery:
    base = dict(text="", channel_ids=None, author_ids=None, after_utc=None,
                before_utc=None, has=set(), file_substr=None, ext=None)
    base.update(overrides)
    return ResolvedQuery(**base)


def test_candidate_shards_no_filters_returns_everything(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    result = candidate_shards(catalog, _empty_resolved())
    assert sorted(result) == [
        ("10", "a/2025-09.sqlite"), ("10", "a/2025-10.sqlite"), ("11", "b/2025-10.sqlite"),
    ]


def test_candidate_shards_restricted_to_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    result = candidate_shards(catalog, _empty_resolved(channel_ids=["11"]))
    assert result == [("11", "b/2025-10.sqlite")]


def test_candidate_shards_empty_channel_list_returns_nothing(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    result = candidate_shards(catalog, _empty_resolved(channel_ids=[]))
    assert result == []


def test_candidate_shards_restricted_by_month_range(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    _seed_shard_rows(catalog)
    resolved = _empty_resolved(after_utc="2025-10-01T00:00:00Z", before_utc="2025-10-31T23:00:00Z")
    result = candidate_shards(catalog, resolved)
    assert sorted(result) == [("10", "a/2025-10.sqlite"), ("11", "b/2025-10.sqlite")]


def _write_shard(path: Path, messages_and_extras=()) -> None:
    conn = connect_shard(path)
    for msg, extra in messages_and_extras:
        _insert(conn, "messages", msg)
        for table, row in extra:
            _insert(conn, table, row)
    conn.commit()
    conn.close()


def test_build_message_sql_matches_korean_fragment_via_fts():
    resolved = ResolvedQuery(text="반가워", channel_ids=None, author_ids=None,
                              after_utc=None, before_utc=None, has=set(),
                              file_substr=None, ext=None)
    sql, params = build_message_sql(resolved, "10", has_fts=True)
    assert "messages_fts" in sql
    assert "MATCH" in sql


def test_build_message_sql_short_query_uses_like_even_with_fts():
    resolved = ResolvedQuery(text="ab", channel_ids=None, author_ids=None,
                              after_utc=None, before_utc=None, has=set(),
                              file_substr=None, ext=None)
    sql, params = build_message_sql(resolved, "10", has_fts=True)
    assert "LIKE" in sql
    assert "messages_fts" not in sql


def test_search_end_to_end_english_fragment(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="the quick brown fox", channel_id="10")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "brown")
    assert len(results) == 1
    assert results[0]["id"] == msg["id"]


def test_search_end_to_end_korean_fragment_mid_word(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="안녕하세요 반갑습니다", channel_id="10")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "갑습")  # mid-word fragment, 3 chars
    assert len(results) == 1
    assert results[0]["id"] == msg["id"]


def test_search_has_image_filter(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    with_img = make_message(content="", channel_id="10")
    without_img = make_message(content="", channel_id="10")
    _write_shard(shard_path, [
        (with_img, [("attachments", make_attachment(with_img["id"]))]),
        (without_img, []),
    ])

    results = search(catalog, tmp_path, "has:image")
    assert [r["id"] for r in results] == [with_img["id"]]


def test_search_from_filter_with_no_matching_author_returns_nothing(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    _write_shard(shard_path, [(make_message(channel_id="10"), [])])

    results = search(catalog, tmp_path, "from:nobody")
    assert results == []


def test_search_excludes_deleted_messages(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    deleted = make_message(content="gone fragment", channel_id="10", deleted_utc=now)
    _write_shard(shard_path, [(deleted, [])])

    results = search(catalog, tmp_path, "fragment")
    assert results == []


def test_get_context_crosses_month_boundary(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-09', 'uncategorized', 'uncategorized/general/2025-09.sqlite')"
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()

    sep_path = tmp_path / "uncategorized" / "general" / "2025-09.sqlite"
    oct_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    sep_path.parent.mkdir(parents=True)

    before1 = make_message(id="1001", content="before two", channel_id="10")
    before2 = make_message(id="1002", content="before one", channel_id="10")
    target = make_message(id="1003", content="target message", channel_id="10")
    after1 = make_message(id="1004", content="after one", channel_id="10")
    _write_shard(sep_path, [(before1, []), (before2, [])])
    _write_shard(oct_path, [(target, []), (after1, [])])

    before, after = get_context(catalog, tmp_path, "10", "1003", 2)
    assert [m["id"] for m in before] == ["1001", "1002"]
    assert [m["id"] for m in after] == ["1004"]


def test_format_result_truncates_by_default(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    row = make_message(content="x" * 300, channel_id="10", created_utc=now)
    line = format_result(catalog, row)
    assert len(line) < 300
    assert "general" in line

    full_line = format_result(catalog, row, full=True)
    assert "x" * 300 in full_line


def test_format_result_converts_created_utc_to_seoul(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    # 2025-10-31T15:30:00Z UTC == 2025-11-01T00:30:00 KST -- crosses the calendar-day boundary
    row = make_message(content="hi", channel_id="10", created_utc="2025-10-31T15:30:00Z")
    line = format_result(catalog, row)
    assert "2025-11-01" in line
    assert "2025-10-31" not in line


def test_get_context_reuses_id_index_cache_across_calls(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)

    m1 = make_message(id="1001", content="one", channel_id="10")
    m2 = make_message(id="1002", content="two", channel_id="10")
    m3 = make_message(id="1003", content="three", channel_id="10")
    _write_shard(shard_path, [(m1, []), (m2, []), (m3, [])])

    cache: dict = {}
    before1, after1 = get_context(catalog, tmp_path, "10", "1001", 1, id_index_cache=cache)
    before2, after2 = get_context(catalog, tmp_path, "10", "1003", 1, id_index_cache=cache)

    assert before1 == []
    assert [m["id"] for m in after1] == ["1002"]
    assert [m["id"] for m in before2] == ["1002"]
    assert after2 == []
    assert len(cache) == 1


def test_search_like_fallback_matches_unicode_case_insensitively(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="École de Paris", channel_id="1")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "école")
    assert len(results) == 1
    results_upper = search(catalog, tmp_path, "ÉCOLE")
    assert len(results_upper) == 1


def test_search_like_fallback_forces_unicode_case_fold_under_trigram_floor(tmp_path):
    """Query length under 3 chars always routes to LIKE (spec §7.1),
    regardless of trigram availability -- unlike the test above, which
    can be satisfied by a trigram-enabled build's own case-folding and
    so doesn't actually exercise this module's LIKE override. Content
    is stored lowercase and searched uppercase so only the accented
    character's own case fold is exercised, not an incidental ASCII
    letter's."""
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="école de paris", channel_id="1")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "ÉC")  # 2 chars, under the trigram floor -> always LIKE
    assert len(results) == 1


def test_search_truncates_to_limit(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('10', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    messages = [(make_message(id=str(1000 + i), content="hello", channel_id="10"), [])
                for i in range(5)]
    _write_shard(shard_path, messages)

    limited = search(catalog, tmp_path, "hello", limit=3)
    assert len(limited) == 3

    unlimited = search(catalog, tmp_path, "hello", limit=500)
    assert len(unlimited) == 5


def test_resolve_channel_ids_includes_threads_of_a_matched_channel(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'a thread', 'public_thread', '10', NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:general"))
    assert sorted(resolved.channel_ids) == ["10", "11"]


def test_resolve_channel_ids_no_threads_adds_nothing_extra(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:general"))
    assert resolved.channel_ids == ["10"]


def test_search_korean_mid_word_fragment_three_chars_routes_to_trigram_when_available(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="프로그래밍을 좋아해요", channel_id="1")  # "programming" mid-word
    _write_shard(shard_path, [(msg, [])])

    from archiver.fts import detect_trigram_support
    shard_conn = connect_shard(shard_path)
    has_fts = detect_trigram_support(shard_conn)
    shard_conn.close()

    results = search(catalog, tmp_path, "그래밍")  # 3-char mid-word fragment
    assert len(results) == 1
    if has_fts:
        sql, _ = build_message_sql(
            resolve_query(catalog, tokenize_query("그래밍")), "1", has_fts=True
        )
        assert "messages_fts" in sql and "MATCH" in sql


def test_search_english_mid_word_fragment_routes_to_trigram_when_available(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg = make_message(content="the quick brown fox jumps", channel_id="1")
    _write_shard(shard_path, [(msg, [])])

    results = search(catalog, tmp_path, "ick br")  # mid-word-to-mid-word fragment, not a whole word
    assert len(results) == 1


def test_search_disambiguates_two_users_sharing_a_display_name(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('20', 'alex', ?, ?)", (now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) "
        "VALUES ('21', 'alex', ?, ?)", (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('1', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES ('1', '2025-10', 'uncategorized', 'uncategorized/general/2025-10.sqlite')"
    )
    catalog.commit()
    shard_path = tmp_path / "uncategorized" / "general" / "2025-10.sqlite"
    shard_path.parent.mkdir(parents=True)
    msg_from_20 = make_message(content="hello from the first alex", channel_id="1", author_id="20")
    msg_from_21 = make_message(content="hello from the second alex", channel_id="1", author_id="21")
    _write_shard(shard_path, [(msg_from_20, []), (msg_from_21, [])])

    results = search(catalog, tmp_path, "from:alex")
    assert {r["author_id"] for r in results} == {"20", "21"}  # both users' messages found, not collapsed


def test_resolve_category_filter_matches_by_category_name_substring(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'Gaming Chat', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'games', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'other', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("category:gaming"))
    assert resolved.channel_ids == ["10"]


def test_resolve_category_filter_uncategorized_matches_null_category(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('11', 'other', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("category:uncategorized"))
    assert resolved.channel_ids == ["11"]


def test_resolve_in_and_category_filters_union_their_channel_ids(tmp_path):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO category_names (category_id, name, updated_utc) VALUES ('cat1', 'Gaming', ?)",
        (now,),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('10', 'games', 'text', NULL, 'cat1', 0, ?, ?)",
        (now, now),
    )
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES ('20', 'general', 'text', NULL, NULL, 0, ?, ?)",
        (now, now),
    )
    catalog.commit()
    resolved = resolve_query(catalog, tokenize_query("in:general category:gaming"))
    assert sorted(resolved.channel_ids) == ["10", "20"]


def test_query_stats_empty_results():
    from archiver.search import query_stats
    stats = query_stats([])
    assert stats["count"] == 0
    assert stats["date_span"] == "--"
    assert stats["frequency"] == "--"
    assert stats["top_words"] == []


def test_query_stats_counts_and_date_span():
    from archiver.search import query_stats
    results = [
        {"created_utc": "2025-09-14T21:02:00Z", "content": "pizza night again"},
        {"created_utc": "2025-09-21T12:11:00Z", "content": "pizza again? we just had it"},
    ]
    stats = query_stats(results)
    assert stats["count"] == 2
    assert stats["date_span"] == "2025-09-14 - 2025-09-21"


def test_query_stats_single_day_span_shows_one_date():
    from archiver.search import query_stats
    results = [
        {"created_utc": "2025-09-14T21:02:00Z", "content": "a"},
        {"created_utc": "2025-09-14T21:05:00Z", "content": "b"},
    ]
    stats = query_stats(results)
    assert stats["date_span"] == "2025-09-14"


def test_query_stats_top_words_excludes_stopwords_and_urls():
    from archiver.search import query_stats
    results = [
        {"created_utc": "2025-09-14T00:00:00Z", "content": "the pizza is the best pizza"},
        {"created_utc": "2025-09-14T00:00:00Z", "content": "check https://example.com/pizza for pizza"},
    ]
    stats = query_stats(results)
    words = dict(stats["top_words"])
    assert words["pizza"] == 3  # the embedded "pizza" inside the URL is stripped along with it
    assert "the" not in words
    assert "https" not in words
    assert "example" not in words  # URL stripped entirely, not just the scheme


def test_query_stats_frequency_is_rate_per_day_over_the_span():
    from archiver.search import query_stats
    results = [
        {"created_utc": f"2025-09-{day:02d}T00:00:00Z", "content": "hi"}
        for day in range(1, 8)  # 7 messages across a 7-day span (6-day span: 1st to 7th)
    ]
    stats = query_stats(results)
    assert stats["count"] == 7
    assert "/day" in stats["frequency"]

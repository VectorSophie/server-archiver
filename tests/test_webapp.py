import pytest

from archiver.config import Config
from archiver.credentials import add_user
from archiver.db import connect_catalog, connect_shard
from archiver.webapp import create_app
from tests.fixtures import make_message, _insert


def _seed_one_message(tmp_path, *, channel_id="1", content="hello world", author_id="user1"):
    catalog = connect_catalog(tmp_path / "catalog.sqlite")
    now = "2025-10-15T00:00:00Z"
    catalog.execute(
        "INSERT INTO channels (id, name, type, parent_id, category_id, is_archived, "
        "first_seen_utc, last_seen_utc) VALUES (?, 'general', 'text', NULL, NULL, 0, ?, ?)",
        (channel_id, now, now),
    )
    catalog.execute(
        "INSERT INTO users (id, username, first_seen_utc, last_seen_utc) VALUES (?, 'milkika', ?, ?)",
        (author_id, now, now),
    )
    shard_path = "Uncategorized/general/2025-10.sqlite"
    catalog.execute(
        "INSERT INTO channel_month_shard (channel_id, yyyymm, category_id, shard_path) "
        "VALUES (?, '2025-10', 'uncategorized', ?)",
        (channel_id, shard_path),
    )
    catalog.commit()
    catalog.close()

    full_path = tmp_path / shard_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_shard(full_path)
    _insert(conn, "messages", make_message(content=content, channel_id=channel_id, author_id=author_id))
    conn.commit()
    conn.close()


@pytest.fixture
def app_and_creds(tmp_path):
    credentials_path = tmp_path / "credentials.json"
    add_user("shoe", "correct-horse", credentials_path)
    config = Config(guild_id="1", data_dir=tmp_path, secure_cookies=False)
    app = create_app(config, credentials_path=credentials_path)
    app.testing = True
    return app, credentials_path


def test_login_wrong_credentials_redirects_back_without_session(app_and_creds):
    app, _ = app_and_creds
    client = app.test_client()
    resp = client.post("/login", data={"codename": "shoe", "password": "wrong"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert "codename" not in sess


def test_login_correct_credentials_redirects_home_and_sets_session(app_and_creds):
    app, _ = app_and_creds
    client = app.test_client()
    resp = client.post("/login", data={"codename": "shoe", "password": "correct-horse"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    with client.session_transaction() as sess:
        assert sess["codename"] == "shoe"


def test_index_without_session_redirects_to_login(app_and_creds):
    app, _ = app_and_creds
    client = app.test_client()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"


def test_index_with_session_serves_search_page(app_and_creds):
    app, _ = app_and_creds
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["codename"] = "shoe"
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"search" in resp.data.lower()


def test_api_search_without_session_returns_401(app_and_creds):
    app, _ = app_and_creds
    client = app.test_client()
    resp = client.post("/api/search", json={"query": "hello"})
    assert resp.status_code == 401


def test_api_search_with_session_returns_expected_shape(app_and_creds, tmp_path):
    app, _ = app_and_creds
    _seed_one_message(tmp_path, content="pizza tonight")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["codename"] = "shoe"
    resp = client.post("/api/search", json={"query": "pizza"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    assert data["results"][0]["content"] == "pizza tonight"
    assert data["results"][0]["channel"] == "general"
    assert data["results"][0]["author"] == "milkika"


def test_api_search_malformed_query_returns_400_not_a_crash(app_and_creds):
    app, _ = app_and_creds
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["codename"] = "shoe"
    resp = client.post("/api/search", json={"query": 'unterminated "quote'})
    assert resp.status_code == 400
    assert "error" in resp.get_json()

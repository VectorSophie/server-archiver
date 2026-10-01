from archiver.credentials import add_user, load_secret_key, verify_login


def test_add_user_then_verify_login_correct_password_succeeds(tmp_path):
    path = tmp_path / "credentials.json"
    add_user("shoe", "correct-horse", path)
    assert verify_login("shoe", "correct-horse", path) is True


def test_verify_login_wrong_password_fails(tmp_path):
    path = tmp_path / "credentials.json"
    add_user("shoe", "correct-horse", path)
    assert verify_login("shoe", "wrong-password", path) is False


def test_verify_login_unknown_codename_fails(tmp_path):
    path = tmp_path / "credentials.json"
    add_user("shoe", "correct-horse", path)
    assert verify_login("nope", "correct-horse", path) is False


def test_verify_login_missing_file_fails_not_raises(tmp_path):
    path = tmp_path / "does-not-exist.json"
    assert verify_login("shoe", "correct-horse", path) is False


def test_add_user_overwrites_existing_codename_password(tmp_path):
    path = tmp_path / "credentials.json"
    add_user("shoe", "first-password", path)
    add_user("shoe", "second-password", path)
    assert verify_login("shoe", "first-password", path) is False
    assert verify_login("shoe", "second-password", path) is True


def test_load_secret_key_is_stable_across_calls(tmp_path):
    path = tmp_path / "credentials.json"
    key1 = load_secret_key(path)
    key2 = load_secret_key(path)
    assert key1 == key2


def test_load_secret_key_before_any_useradd_still_works(tmp_path):
    path = tmp_path / "credentials.json"
    key = load_secret_key(path)
    assert len(key) == 64  # secrets.token_hex(32)

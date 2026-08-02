"""Password hashing, verification, and role reading."""

from nvr import auth


def test_hash_is_salted_and_verifies():
    a = auth.hash_password("hunter2")
    b = auth.hash_password("hunter2")
    assert a != b  # random salt each time
    assert a.startswith("scrypt$")
    assert auth.verify_password("hunter2", a)
    assert auth.verify_password("hunter2", b)


def test_verify_rejects_wrong_password():
    encoded = auth.hash_password("correct horse")
    assert not auth.verify_password("battery staple", encoded)


def test_verify_rejects_malformed_hash():
    assert not auth.verify_password("x", "not-a-real-hash")
    assert not auth.verify_password("x", "")
    assert not auth.verify_password("x", "bcrypt$1$2$3$4")  # wrong scheme


def test_new_token_is_unique():
    assert auth.new_token() != auth.new_token()


class _Row(dict):
    """Stand-in for a sqlite3.Row supporting ['role']."""


def test_is_admin():
    assert auth.is_admin(_Row(role="admin"))
    assert not auth.is_admin(_Row(role="viewer"))
    assert not auth.is_admin(None)
    # A pre-roles account (role NULL) reads as admin — matches the migration.
    assert auth.is_admin(_Row(role=None))

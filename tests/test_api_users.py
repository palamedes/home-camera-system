"""User-management API and its guardrails (admin only)."""

from conftest import make_user


def test_admin_creates_viewer(admin_client, db):
    r = admin_client.post("/api/users", json={
        "username": "newbie", "password": "password123", "role": "viewer",
    })
    assert r.status_code == 200
    assert db.user_by_name("newbie")["role"] == "viewer"


def test_create_user_validation(admin_client):
    assert admin_client.post("/api/users", json={
        "username": "ab", "password": "password123", "role": "viewer"}).status_code == 400
    assert admin_client.post("/api/users", json={
        "username": "okname", "password": "short", "role": "viewer"}).status_code == 400
    assert admin_client.post("/api/users", json={
        "username": "okname", "password": "password123", "role": "wizard"}).status_code == 400


def test_duplicate_username_rejected(admin_client, db):
    make_user_via_api = admin_client.post("/api/users", json={
        "username": "dup", "password": "password123", "role": "viewer"})
    assert make_user_via_api.status_code == 200
    again = admin_client.post("/api/users", json={
        "username": "dup", "password": "password123", "role": "viewer"})
    assert again.status_code == 400


def test_cannot_delete_own_account(admin_client, db):
    me = db.user_by_name("admin")
    r = admin_client.delete(f"/api/users/{me['id']}")
    assert r.status_code == 400
    assert db.user_by_id(me["id"]) is not None


def test_cannot_delete_last_admin(admin_client, app_module, db):
    # A second (viewer) account exists, but the admin being deleted is the only
    # admin — and it's someone else, to get past the self-delete guard first.
    admin_client.post("/api/users", json={
        "username": "second", "password": "password123", "role": "admin"})
    # Now demote the *other* admin; last-admin guard should block leaving zero.
    second = db.user_by_name("second")
    # Delete "second" is fine (two admins). Delete works:
    assert admin_client.delete(f"/api/users/{second['id']}").status_code == 200
    # Only the original admin remains; deleting themselves is blocked by self
    # guard, and demoting is blocked by the last-admin guard.
    me = db.user_by_name("admin")
    r = admin_client.patch(f"/api/users/{me['id']}", json={"role": "viewer"})
    assert r.status_code == 400
    assert db.admin_count() == 1


def test_update_role_and_password(admin_client, db):
    r = admin_client.post("/api/users", json={
        "username": "user1", "password": "password123", "role": "viewer"})
    assert r.status_code == 200
    u = db.user_by_name("user1")
    assert admin_client.patch(f"/api/users/{u['id']}", json={"role": "admin"}).status_code == 200
    assert db.user_by_id(u["id"])["role"] == "admin"
    # Too-short password rejected.
    assert admin_client.patch(f"/api/users/{u['id']}", json={"password": "x"}).status_code == 400

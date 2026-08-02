"""Auth middleware: setup gate, login/logout, default-deny, role enforcement."""

from conftest import add_camera, login, make_user


def test_setup_required_when_no_users(client):
    # With an empty user table, everything funnels to /setup.
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_creates_first_admin_and_logs_in(client, db):
    r = client.post(
        "/setup",
        data={"username": "boss", "password": "password123", "confirm": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.user_count() == 1
    assert db.user_by_name("boss")["role"] == "admin"
    # Session cookie was issued.
    assert client.cookies.get("nvr_session")


def test_setup_closed_once_a_user_exists(client, app_module, db):
    make_user(app_module, db, "admin")
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_rejects_bad_password(client, app_module, db):
    make_user(app_module, db, "admin", "password123")
    r = client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # re-renders the form, no cookie
    assert not client.cookies.get("nvr_session")


def test_protected_page_redirects_anonymous_to_login(client, app_module, db):
    make_user(app_module, db, "admin")  # so we're past /setup
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_protected_api_returns_401_for_anonymous(client, app_module, db):
    make_user(app_module, db, "admin")
    r = client.get("/api/status")
    assert r.status_code == 401


def test_logout_clears_session(admin_client, db):
    assert admin_client.get("/api/status").status_code == 200
    admin_client.post("/logout", follow_redirects=False)
    assert admin_client.get("/api/status").status_code == 401


def test_static_and_health_are_public(client):
    assert client.get("/health").status_code == 200


# --- role enforcement (the security-critical part) --------------------------

def test_viewer_cannot_list_users(viewer_client):
    assert viewer_client.get("/api/users").status_code == 403


def test_viewer_cannot_create_camera(viewer_client):
    r = viewer_client.post("/api/cameras", json={"name": "x", "host": "1.2.3.4"})
    assert r.status_code == 403


def test_viewer_cannot_run_discovery(viewer_client):
    assert viewer_client.post("/api/discover").status_code == 403


def test_viewer_can_read_a_camera_but_not_mutate(viewer_client, db):
    add_camera(db, "cam1")
    assert viewer_client.get("/api/cameras").status_code == 200
    assert viewer_client.patch("/api/cameras/cam1", json={"name": "z"}).status_code == 403
    assert viewer_client.delete("/api/cameras/cam1").status_code == 403


def test_admin_can_mutate_camera(admin_client, db):
    add_camera(db, "cam1", "Cam One")
    r = admin_client.patch("/api/cameras/cam1", json={"name": "Renamed"})
    assert r.status_code == 200
    assert db.camera("cam1")["name"] == "Renamed"

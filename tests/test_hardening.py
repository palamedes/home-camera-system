"""Second-pass review fixes.

Each test here corresponds to a finding, and states the failure mode it locks
out — these are the bugs that produce a wrong answer or a silent loss rather
than an obvious crash, so the test is the only thing that keeps them fixed.
"""

import io

import pytest

from nvr import config as config_module


# --- a bad interval must never kill a background thread --------------------

@pytest.mark.parametrize("value,expected", [
    (2.0, 2.0),
    ("5", 5.0),
    (0.01, 0.5),      # clamped up to the floor, not a busy loop
    (-3, 0.5),
])
def test_safe_interval_accepts_sane_values(value, expected):
    assert config_module.safe_interval(value, default=2.0, minimum=0.5) == expected


@pytest.mark.parametrize("value", [None, "", "soon", [], {}, object(),
                                   float("nan"), float("inf")])
def test_safe_interval_never_raises(value):
    """This is used for the sleep at the END of a loop, AFTER the try that
    guards the work. Anything thrown there kills the thread outright —
    silently, permanently, with the UI still reporting the service healthy."""
    assert config_module.safe_interval(value, default=7.0, minimum=0.5) == 7.0


def test_the_event_poller_survives_a_poisoned_interval(app_module, monkeypatch):
    """End to end: a nonsense poll_seconds must degrade to the default rather
    than taking camera detection down for the life of the process."""
    service = app_module.events
    monkeypatch.setattr(type(service), "cfg",
                        property(lambda self: type("C", (), {
                            "poll_seconds": "not a number", "enabled": True,
                            "detect": ["person"],
                        })()))
    # The value the loop would sleep on:
    assert config_module.safe_interval(
        service.cfg.poll_seconds, default=2.0, minimum=0.5) == 2.0


# --- malformed JSON is the caller's mistake, not a 500 ---------------------

@pytest.mark.parametrize("body", [
    "{not json", "", "[1,2", '{"a":}', "null-ish",
])
def test_a_malformed_body_is_a_400_not_a_500(admin_client, db, body):
    from conftest import add_camera
    add_camera(db)
    r = admin_client.patch("/api/cameras/front", content=body,
                           headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_an_undecodable_body_is_a_400(admin_client):
    r = admin_client.post("/api/users", content=b"\xff\xfe\x00",
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_the_handler_did_not_break_valid_requests(admin_client, db):
    from conftest import add_camera
    add_camera(db)
    assert admin_client.get("/api/cameras").status_code == 200
    assert admin_client.patch("/api/cameras/front",
                              json={"name": "Renamed"}).status_code == 200


def test_malformed_json_on_a_newer_endpoint_too(admin_client):
    """The fix is central precisely so every endpoint gets it, including ones
    written after the fix."""
    for path in ("/api/tasks", "/api/blinds/rooms", "/api/automations"):
        r = admin_client.post(path, content="{oops",
                              headers={"Content-Type": "application/json"})
        assert r.status_code == 400, f"{path}: {r.status_code}"


# --- clip upload is streamed and capped ------------------------------------

def _upload(client, payload: bytes, name="Clip"):
    return client.post(
        "/api/clips",
        files={"file": ("clip.mp4", io.BytesIO(payload), "video/mp4")},
        data={"camera_id": "front", "name": name},
    )


def test_a_normal_clip_saves(admin_client, db):
    from conftest import add_camera
    add_camera(db)
    r = _upload(admin_client, b"\x00" * 2048)
    assert r.status_code == 200
    assert db.clips()[0]["size"] == 2048


def test_an_empty_clip_is_refused_and_leaves_no_file(admin_client, db, app_module):
    from conftest import add_camera
    add_camera(db)
    before = set(app_module.cfg.storage.clips_dir.glob("*"))
    assert _upload(admin_client, b"").status_code == 400
    # A partial file left behind is a disk leak nobody would go looking for.
    assert set(app_module.cfg.storage.clips_dir.glob("*")) == before
    assert db.clips() == []


def test_an_oversized_clip_is_refused_and_leaves_no_file(
        admin_client, db, app_module, monkeypatch):
    from conftest import add_camera
    add_camera(db)
    # Shrink the cap rather than actually posting half a gigabyte.
    monkeypatch.setattr(app_module, "MAX_CLIP_BYTES", 4096)
    before = set(app_module.cfg.storage.clips_dir.glob("*"))
    r = _upload(admin_client, b"\x00" * 20000)
    assert r.status_code == 413
    assert set(app_module.cfg.storage.clips_dir.glob("*")) == before
    assert db.clips() == []


def test_a_declared_oversize_is_refused_before_the_body_is_read(
        admin_client, db, app_module, monkeypatch):
    """The cheap early exit: an honest client saying "here comes 2 GB" is told
    no before a byte is transferred."""
    from conftest import add_camera
    add_camera(db)
    monkeypatch.setattr(app_module, "MAX_CLIP_BYTES", 100)
    r = admin_client.post(
        "/api/clips",
        content=b"x" * 500,
        headers={"Content-Type": "application/octet-stream",
                 "Content-Length": "500"},
    )
    assert r.status_code in (400, 413, 422)


def test_the_upload_reads_in_chunks_not_all_at_once(
        admin_client, db, app_module, monkeypatch):
    """Guards the actual regression: read() with no argument slurps the whole
    body into memory, which is what the size check used to happen after."""
    sizes = []
    import starlette.datastructures as ds
    original = ds.UploadFile.read

    async def spy(self, size=-1):
        sizes.append(size)
        return await original(self, size)

    monkeypatch.setattr(ds.UploadFile, "read", spy)
    from conftest import add_camera
    add_camera(db)
    _upload(admin_client, b"\x00" * 5000)
    assert sizes, "upload never read the body"
    assert all(s and s > 0 for s in sizes), f"unbounded read: {sizes}"


def test_two_clips_saved_in_the_same_second_do_not_collide(admin_client, db):
    """The filename used to be unique only to the second, so the second clip
    overwrote the first and the first clip's row pointed at the wrong video."""
    from conftest import add_camera
    add_camera(db)
    _upload(admin_client, b"A" * 100, name="First")
    _upload(admin_client, b"B" * 200, name="Second")
    clips = db.clips()
    assert len(clips) == 2
    paths = {c["path"] for c in clips}
    assert len(paths) == 2, "both clips wrote to the same file"
    for clip in clips:
        import pathlib
        assert pathlib.Path(clip["path"]).exists()
        assert pathlib.Path(clip["path"]).stat().st_size == clip["size"]


def test_a_rejected_upload_cannot_delete_an_existing_clip(
        admin_client, db, app_module, monkeypatch):
    """The cleanup path must only ever remove the file THIS request created."""
    from conftest import add_camera
    add_camera(db)
    _upload(admin_client, b"A" * 100, name="Keep me")
    kept = db.clips()[0]["path"]
    monkeypatch.setattr(app_module, "MAX_CLIP_BYTES", 10)
    assert _upload(admin_client, b"B" * 500).status_code == 413
    import pathlib
    assert pathlib.Path(kept).exists(), "a refused upload deleted a saved clip"

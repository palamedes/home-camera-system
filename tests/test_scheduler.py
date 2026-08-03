"""Camera schedules: window logic, db CRUD, the scheduler's record enforcement,
edge-triggered light/nightvision, and the HTTP routes."""

from conftest import add_camera
from nvr import scheduler as sched_mod
from nvr.scheduler import SchedulerService, in_window, day_enabled

# Weekday index: 0=Mon .. 6=Sun (matches Python's tm_wday / date.weekday()).
MON, TUE, WED, SAT, SUN = 0, 1, 2, 5, 6
ALL_DAYS = 0b1111111  # 127


# --------------------------------------------------------------------------
# Pure window logic
# --------------------------------------------------------------------------

def test_day_enabled_bitmask():
    assert day_enabled(0b0000001, MON)      # bit0 = Monday
    assert not day_enabled(0b0000001, TUE)
    assert day_enabled(0b1000000, SUN)      # bit6 = Sunday
    assert day_enabled(ALL_DAYS, WED)


def test_same_day_window_inclusive_start_exclusive_end():
    # 09:00 (540) .. 17:00 (1020) on all days.
    assert in_window(ALL_DAYS, 540, 1020, MON, 540)       # at start -> in
    assert in_window(ALL_DAYS, 540, 1020, MON, 800)       # middle -> in
    assert not in_window(ALL_DAYS, 540, 1020, MON, 1020)  # at end -> out
    assert not in_window(ALL_DAYS, 540, 1020, MON, 539)   # before -> out


def test_same_day_window_respects_day_mask():
    only_wed = 1 << WED
    assert in_window(only_wed, 540, 1020, WED, 600)
    assert not in_window(only_wed, 540, 1020, MON, 600)


def test_wrap_around_past_midnight():
    # 22:00 (1320) .. 06:00 (360), enabled on Monday only.
    only_mon = 1 << MON
    # Monday evening, inside the pre-midnight leg.
    assert in_window(only_mon, 1320, 360, MON, 1350)      # Mon 22:30 -> in
    assert in_window(only_mon, 1320, 360, MON, 1320)      # Mon 22:00 -> in
    # Tuesday small hours belong to Monday's window (attributed to start day).
    assert in_window(only_mon, 1320, 360, TUE, 120)       # Tue 02:00 -> in
    assert not in_window(only_mon, 1320, 360, TUE, 360)   # Tue 06:00 -> out (end excl)
    # A Tuesday-only rule must NOT light up Tuesday small hours (that tail is
    # Monday's), only Tuesday evening.
    only_tue = 1 << TUE
    assert not in_window(only_tue, 1320, 360, TUE, 120)   # Tue 02:00 -> out
    assert in_window(only_tue, 1320, 360, TUE, 1400)      # Tue 23:20 -> in
    assert in_window(only_tue, 1320, 360, WED, 120)       # Wed 02:00 -> Tue's tail


def test_wrap_around_daytime_gap_is_out():
    only_mon = 1 << MON
    assert not in_window(only_mon, 1320, 360, MON, 720)   # Mon noon -> out


def test_empty_window_never_active():
    assert not in_window(ALL_DAYS, 600, 600, MON, 600)


# --------------------------------------------------------------------------
# DB CRUD
# --------------------------------------------------------------------------

def test_schedule_crud(db):
    add_camera(db, "cam1")
    sid = db.add_schedule("cam1", "record", ALL_DAYS, 540, 1020)
    rows = db.schedules_for("cam1")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "record" and row["start_min"] == 540
    assert row["value"] == "on" and row["enabled"] == 1

    db.set_schedule_enabled(sid, False)
    assert db.schedules_for("cam1")[0]["enabled"] == 0

    db.delete_schedule(sid)
    assert db.schedules_for("cam1") == []


def test_schedules_global_and_delete_cascade(db):
    add_camera(db, "cam1")
    add_camera(db, "cam2")
    db.add_schedule("cam1", "record", ALL_DAYS, 0, 60)
    db.add_schedule("cam2", "light", 1 << MON, 1320, 360)
    assert len(db.schedules()) == 2
    # Deleting a camera sweeps its schedules.
    db.delete_camera("cam1")
    remaining = db.schedules()
    assert len(remaining) == 1 and remaining[0]["camera_id"] == "cam2"


def test_nightvision_schedule_stores_mode(db):
    add_camera(db, "cam1")
    db.add_schedule("cam1", "nightvision", ALL_DAYS, 1200, 300, value="bw")
    assert db.schedules_for("cam1")[0]["value"] == "bw"


# --------------------------------------------------------------------------
# Scheduler record enforcement
# --------------------------------------------------------------------------

class _FakeRecording:
    def __init__(self):
        self.syncs = 0

    def sync(self):
        self.syncs += 1


def test_record_turns_on_inside_window(app_module, db):
    add_camera(db, "cam1", record=0)
    db.add_schedule("cam1", "record", ALL_DAYS, 540, 1020)  # 09:00-17:00
    rec = _FakeRecording()
    svc = SchedulerService(app_module.cfg, db, rec)

    svc.apply(MON, 600)  # 10:00 -> inside
    assert db.camera("cam1")["record"] == 1
    assert rec.syncs == 1

    # Idempotent: a second tick inside the window doesn't re-sync.
    svc.apply(MON, 601)
    assert rec.syncs == 1


def test_record_turns_off_outside_window(app_module, db):
    add_camera(db, "cam1", record=1)
    db.add_schedule("cam1", "record", ALL_DAYS, 540, 1020)
    rec = _FakeRecording()
    svc = SchedulerService(app_module.cfg, db, rec)

    svc.apply(MON, 1200)  # 20:00 -> outside -> off
    assert db.camera("cam1")["record"] == 0
    assert rec.syncs == 1


def test_record_leaves_unscheduled_cameras_alone(app_module, db):
    # cam1 has a record schedule; cam2 has none and must never be touched.
    add_camera(db, "cam1", record=0)
    add_camera(db, "cam2", record=1)
    db.add_schedule("cam1", "record", ALL_DAYS, 540, 1020)
    svc = SchedulerService(app_module.cfg, db, _FakeRecording())

    svc.apply(MON, 600)
    assert db.camera("cam1")["record"] == 1
    assert db.camera("cam2")["record"] == 1  # untouched (no record schedule)


def test_record_wrap_window_active(app_module, db):
    add_camera(db, "cam1", record=0)
    db.add_schedule("cam1", "record", 1 << MON, 1320, 360)  # Mon 22:00 -> 06:00
    svc = SchedulerService(app_module.cfg, db, _FakeRecording())
    svc.apply(TUE, 120)  # Tue 02:00 belongs to Monday's window
    assert db.camera("cam1")["record"] == 1


def test_disabled_schedule_is_ignored(app_module, db):
    add_camera(db, "cam1", record=1)
    sid = db.add_schedule("cam1", "record", ALL_DAYS, 540, 1020)
    db.set_schedule_enabled(sid, False)
    svc = SchedulerService(app_module.cfg, db, _FakeRecording())
    svc.apply(MON, 1200)  # outside, but the only schedule is disabled
    assert db.camera("cam1")["record"] == 1  # unchanged: camera has no *active* rule


# --------------------------------------------------------------------------
# Edge-triggered light / nightvision via the soft-imported camera_control
# --------------------------------------------------------------------------

class _FakeControl:
    def __init__(self):
        self.light_calls = []
        self.nv_calls = []

    def set_light(self, camera_row, on):
        self.light_calls.append((camera_row["id"], on))

    def set_night_vision(self, camera_row, *, mode=None):
        self.nv_calls.append((camera_row["id"], mode))


def test_light_edge_triggered(app_module, db, monkeypatch):
    add_camera(db, "cam1")
    db.add_schedule("cam1", "light", 1 << MON, 1320, 360)  # 22:00-06:00
    fake = _FakeControl()
    monkeypatch.setattr(sched_mod, "camera_control", fake)
    svc = SchedulerService(app_module.cfg, db, None)

    svc.apply(MON, 1350)  # inside -> turn on (edge)
    svc.apply(MON, 1351)  # still inside -> no repeat
    assert fake.light_calls == [("cam1", True)]

    svc.apply(TUE, 720)   # Tue noon -> outside -> turn off (edge)
    svc.apply(TUE, 721)   # still outside -> no repeat
    assert fake.light_calls == [("cam1", True), ("cam1", False)]


def test_nightvision_applies_on_window_open(app_module, db, monkeypatch):
    add_camera(db, "cam1")
    db.add_schedule("cam1", "nightvision", 1 << MON, 1200, 300, value="bw")
    fake = _FakeControl()
    monkeypatch.setattr(sched_mod, "camera_control", fake)
    svc = SchedulerService(app_module.cfg, db, None)

    svc.apply(MON, 1150)  # before window -> nothing
    svc.apply(MON, 1200)  # window opens -> apply mode once
    svc.apply(MON, 1260)  # still inside -> don't hammer
    assert fake.nv_calls == [("cam1", "bw")]


def test_missing_camera_control_does_not_crash(app_module, db, monkeypatch):
    add_camera(db, "cam1")
    db.add_schedule("cam1", "light", ALL_DAYS, 0, 1439)
    monkeypatch.setattr(sched_mod, "camera_control", None)
    svc = SchedulerService(app_module.cfg, db, None)
    svc.apply(MON, 600)  # must not raise even though the module is absent


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------

def test_route_create_list_delete(admin_client, db):
    add_camera(db, "cam1")
    r = admin_client.post("/api/cameras/cam1/schedules", json={
        "action": "record", "days": ALL_DAYS, "start_min": 540, "end_min": 1020,
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    listing = admin_client.get("/api/cameras/cam1/schedules").json()
    assert len(listing) == 1 and listing[0]["action"] == "record"

    patched = admin_client.patch(f"/api/cameras/cam1/schedules/{sid}",
                                 json={"enabled": False})
    assert patched.status_code == 200
    assert db.schedules_for("cam1")[0]["enabled"] == 0

    assert admin_client.delete(f"/api/cameras/cam1/schedules/{sid}").status_code == 200
    assert db.schedules_for("cam1") == []


def test_route_validation(admin_client, db):
    add_camera(db, "cam1")
    base = {"action": "record", "days": ALL_DAYS, "start_min": 0, "end_min": 60}
    assert admin_client.post("/api/cameras/cam1/schedules",
                             json={**base, "action": "bogus"}).status_code == 400
    assert admin_client.post("/api/cameras/cam1/schedules",
                             json={**base, "days": 999}).status_code == 400
    assert admin_client.post("/api/cameras/cam1/schedules",
                             json={**base, "days": 0}).status_code == 400
    assert admin_client.post("/api/cameras/cam1/schedules",
                             json={**base, "start_min": 5000}).status_code == 400
    assert admin_client.post("/api/cameras/cam1/schedules",
                             json={**base, "end_min": 0}).status_code == 400  # equals start
    assert admin_client.post(
        "/api/cameras/cam1/schedules",
        json={"action": "nightvision", "days": ALL_DAYS, "start_min": 0,
              "end_min": 60, "value": "rainbow"}).status_code == 400


def test_route_unknown_camera_404(admin_client):
    assert admin_client.get("/api/cameras/ghost/schedules").status_code == 404
    assert admin_client.post("/api/cameras/ghost/schedules",
                             json={"action": "record", "days": 1,
                                   "start_min": 0, "end_min": 60}).status_code == 404


def test_route_viewer_forbidden(viewer_client, db):
    add_camera(db, "cam1")
    sid = db.add_schedule("cam1", "record", ALL_DAYS, 0, 60)
    # Viewers may GET (read) but not mutate.
    assert viewer_client.get("/api/cameras/cam1/schedules").status_code == 200
    assert viewer_client.post("/api/cameras/cam1/schedules",
                              json={"action": "record", "days": 1,
                                    "start_min": 0, "end_min": 60}).status_code == 403
    assert viewer_client.delete(f"/api/cameras/cam1/schedules/{sid}").status_code == 403

"""Every server-rendered page returns 200 (template smoke tests).

These exist because a template can be syntactically broken (e.g. an unsupported
Jinja operator in the schedules table) and nothing else catches it until a human
hits the page and gets a 500. Each test seeds enough data that the template's
loops actually run — a schedule row, a virtual camera, a recorded segment — so
the day-bit rendering, vcam rows, and history timeline are all exercised.
"""

import time

import pytest

from conftest import add_camera


def _seed(db):
    add_camera(db, "cam1", "Front Door", fisheye=1)
    # A virtual camera so dashboard/cameras/settings render their vcam rows.
    db.add_virtual_camera("cam1", "Door View", 0.0, 0.0, 1.5708, "{}")
    # A schedule spanning several days so settings.html exercises the per-day
    # bit rendering that previously 500'd on an unsupported Jinja '&'.
    db.add_schedule("cam1", "record", 0b1011011, 8 * 60, 18 * 60, "on")
    db.add_schedule("cam1", "nightvision", 0b1111111, 20 * 60, 6 * 60, "bw")
    # A recorded segment so the history page has bounds and draws the timeline.
    now = time.time()
    db.add_segment("cam1", "/tmp/x/a.mp4", now - 120, 60.0, 1000, "h264")


ADMIN_PAGES = [
    "/",
    "/cameras",
    "/wall",
    "/clips",
    "/settings",
    "/discover",
    "/cameras/cam1",
    "/cameras/cam1/history",
]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_page_renders(admin_client, db, path):
    _seed(db)
    r = admin_client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}\n{r.text[:800]}"


def test_admin_virtual_camera_live_and_history(admin_client, db):
    _seed(db)
    vid = db.virtual_cameras()[0]["id"]
    assert admin_client.get(f"/cameras/cam1?vcam={vid}").status_code == 200
    assert admin_client.get(f"/cameras/cam1/history?vcam={vid}").status_code == 200


# Pages a viewer is allowed to see must also render (no admin-only context leaks
# breaking the template for the lower-privilege path).
VIEWER_PAGES = ["/", "/cameras", "/wall", "/clips", "/cameras/cam1", "/cameras/cam1/history"]


@pytest.mark.parametrize("path", VIEWER_PAGES)
def test_viewer_page_renders(viewer_client, db, path):
    _seed(db)  # cam1 is viewer_visible by default
    r = viewer_client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}\n{r.text[:800]}"


def test_history_page_without_footage_renders(admin_client, db):
    # No segments — the "no recorded footage yet" branch must still render.
    add_camera(db, "empty", "Empty Cam")
    assert admin_client.get("/cameras/empty/history").status_code == 200

"""Household calendar: shared family calendar plus private per-person ones.

The interesting rules are visibility and write access — a shared calendar is
deliberately open to everyone signed in, and a private one is not.
"""

import time

import pytest

DAY = 86400


@pytest.fixture
def now():
    return time.time()


def _event(client, calendar_id, start, end, **over):
    body = {"calendar_id": calendar_id, "title": "Dentist",
            "start": start, "end": end}
    body.update(over)
    return client.post("/api/calendar/events", json=body)


# --- calendars -------------------------------------------------------------

def test_a_shared_family_calendar_exists_by_default(admin_client):
    """There is always somewhere to put an event."""
    cals = admin_client.get("/api/calendar/calendars").json()
    assert any(c["shared"] for c in cals)


def test_everyone_sees_the_shared_calendar(viewer_client, admin_client):
    admin_client.get("/api/calendar/calendars")       # ensure it exists
    cals = viewer_client.get("/api/calendar/calendars").json()
    assert any(c["shared"] for c in cals)


def test_a_private_calendar_is_invisible_to_others(admin_client, viewer_client):
    admin_client.post("/api/calendar/calendars", json={"name": "Work"})
    assert not any(c["name"] == "Work"
                   for c in viewer_client.get("/api/calendar/calendars").json())
    assert any(c["name"] == "Work"
               for c in admin_client.get("/api/calendar/calendars").json())


def test_a_viewer_can_make_their_own_calendar(viewer_client):
    r = viewer_client.post("/api/calendar/calendars", json={"name": "Soccer"})
    assert r.status_code == 200
    mine = viewer_client.get("/api/calendar/calendars").json()
    assert any(c["name"] == "Soccer" and c["mine"] and not c["shared"] for c in mine)


def test_only_an_admin_can_add_a_shared_calendar(viewer_client):
    r = viewer_client.post("/api/calendar/calendars",
                           json={"name": "Chores", "shared": True})
    assert r.status_code == 403


def test_a_calendar_needs_a_name(admin_client):
    assert admin_client.post("/api/calendar/calendars", json={"name": " "}).status_code == 400


def test_deleting_a_calendar_takes_its_events(admin_client, db, now):
    cal = admin_client.post("/api/calendar/calendars", json={"name": "Trip"}).json()["id"]
    _event(admin_client, cal, now, now + 3600)
    assert db.calendar_events(0, 1e12, [cal])

    admin_client.delete(f"/api/calendar/calendars/{cal}")
    assert db.calendar_events(0, 1e12, [cal]) == []


def test_a_viewer_cannot_delete_the_shared_calendar(viewer_client, admin_client):
    admin_client.get("/api/calendar/calendars")
    assert viewer_client.delete("/api/calendar/calendars/family").status_code == 403


# --- events ----------------------------------------------------------------

def test_add_and_read_back_an_event(admin_client, now):
    r = _event(admin_client, "family", now, now + 3600)
    assert r.status_code == 200
    assert r.json()["title"] == "Dentist"

    got = admin_client.get(
        f"/api/calendar/events?start={now - DAY}&end={now + DAY}").json()
    assert [e["title"] for e in got] == ["Dentist"]


def test_anyone_can_add_to_the_shared_calendar(viewer_client, admin_client, now):
    admin_client.get("/api/calendar/calendars")
    r = _event(viewer_client, "family", now, now + 3600, title="Bin day")
    assert r.status_code == 200
    # ...and the admin sees it: that is the point of a family calendar.
    seen = admin_client.get(
        f"/api/calendar/events?start={now - DAY}&end={now + DAY}").json()
    assert any(e["title"] == "Bin day" for e in seen)


def test_events_on_a_private_calendar_stay_private(admin_client, viewer_client, now):
    cal = admin_client.post("/api/calendar/calendars", json={"name": "Work"}).json()["id"]
    _event(admin_client, cal, now, now + 3600, title="Appraisal")
    seen = viewer_client.get(
        f"/api/calendar/events?start={now - DAY}&end={now + DAY}").json()
    assert not any(e["title"] == "Appraisal" for e in seen)


def test_a_viewer_cannot_write_to_someone_elses_calendar(admin_client, viewer_client, now):
    cal = admin_client.post("/api/calendar/calendars", json={"name": "Work"}).json()["id"]
    assert _event(viewer_client, cal, now, now + 3600).status_code == 404


def test_only_overlapping_events_come_back(admin_client, now):
    _event(admin_client, "family", now, now + 3600, title="Today")
    _event(admin_client, "family", now + 30 * DAY, now + 30 * DAY + 3600, title="Later")
    got = admin_client.get(
        f"/api/calendar/events?start={now - DAY}&end={now + DAY}").json()
    assert [e["title"] for e in got] == ["Today"]


def test_a_multi_day_event_shows_across_its_whole_span(admin_client, now):
    """A week-long trip must appear on a window in the middle of it."""
    _event(admin_client, "family", now, now + 7 * DAY, title="Holiday", all_day=True)
    mid = now + 3 * DAY
    got = admin_client.get(
        f"/api/calendar/events?start={mid}&end={mid + 3600}").json()
    assert [e["title"] for e in got] == ["Holiday"]


def test_editing_an_event(admin_client, now):
    eid = _event(admin_client, "family", now, now + 3600).json()["id"]
    r = admin_client.patch(f"/api/calendar/events/{eid}", json={"title": "Moved"})
    assert r.status_code == 200 and r.json()["title"] == "Moved"


def test_a_partial_edit_keeps_the_rest(admin_client, now):
    eid = _event(admin_client, "family", now, now + 3600,
                 location="Main St").json()["id"]
    admin_client.patch(f"/api/calendar/events/{eid}", json={"title": "Renamed"})
    got = admin_client.get(
        f"/api/calendar/events?start={now - DAY}&end={now + DAY}").json()[0]
    assert got["location"] == "Main St"


def test_deleting_an_event(admin_client, now):
    eid = _event(admin_client, "family", now, now + 3600).json()["id"]
    assert admin_client.delete(f"/api/calendar/events/{eid}").status_code == 200
    assert admin_client.get(
        f"/api/calendar/events?start={now - DAY}&end={now + DAY}").json() == []


@pytest.mark.parametrize("body,reason", [
    ({"title": "  ", "start": 0, "end": 1}, "no title"),
    ({"title": "x", "start": "soon", "end": 1}, "start not a number"),
    ({"title": "x", "start": 0}, "end missing"),
])
def test_invalid_events_are_rejected(admin_client, body, reason):
    payload = {"calendar_id": "family", **body}
    assert admin_client.post("/api/calendar/events",
                             json=payload).status_code == 400, reason


def test_an_event_must_end_after_it_starts(admin_client, now):
    assert _event(admin_client, "family", now + 3600, now).status_code == 400


def test_an_unknown_calendar_is_rejected(admin_client, now):
    assert _event(admin_client, "ghost", now, now + 3600).status_code == 404


def test_a_bad_window_is_a_400(admin_client, now):
    assert admin_client.get(
        f"/api/calendar/events?start={now}&end={now - 10}").status_code == 400
    assert admin_client.get(
        "/api/calendar/events?start=NaN&end=NaN").status_code == 400


def test_the_calendar_page_renders(admin_client):
    r = admin_client.get("/calendar")
    assert r.status_code == 200
    assert "calendar.js" in r.text


def test_anonymous_users_get_nothing(client, app_module, db):
    from conftest import make_user
    make_user(app_module, db, "admin", "password123")
    assert client.get("/api/calendar/calendars",
                      follow_redirects=False).status_code == 401


def test_a_non_finite_time_is_rejected(admin_client):
    """NaN is not legal JSON, but Python's parser accepts it, so a raw body can
    still carry one — and an event at NaN would break the window query and the
    JSON response on the way back out."""
    admin_client.get("/api/calendar/calendars")     # ensure the shared calendar
    r = admin_client.post(
        "/api/calendar/events",
        content='{"calendar_id":"family","title":"x","start":0,"end":NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400

"""The household to-do board.

Lists are categories (House, Boat, Car), not workflow stages, and "done" is a
flag rather than a column. The rules worth pinning down are that everyone can
edit everything (it is a family list), that deleting a category never deletes
somebody's work, and that a due date reaches the calendar without being
duplicated into it.
"""

import time

import pytest

DAY = 86400


@pytest.fixture
def lists(admin_client):
    """Touching the page seeds House / Boat / Car."""
    return {l["name"]: l["id"] for l in admin_client.get("/api/tasks").json()["lists"]}


def _add(client, title="Change the oil", **over):
    body = {"title": title}
    body.update(over)
    return client.post("/api/tasks", json=body)


# --- lists -----------------------------------------------------------------

def test_the_obvious_categories_are_seeded(admin_client):
    names = [l["name"] for l in admin_client.get("/api/tasks").json()["lists"]]
    assert names == ["House", "Boat", "Car"]


def test_seeding_happens_once(admin_client, db):
    admin_client.get("/api/tasks")
    admin_client.get("/api/tasks")
    assert len(db.task_lists()) == 3


def test_seeding_does_not_resurrect_deleted_lists(admin_client, db, lists):
    """Someone who deletes Boat should not find it back on the next page load."""
    admin_client.delete(f"/api/tasks/lists/{lists['Boat']}")
    admin_client.get("/api/tasks")
    assert "Boat" not in [l["name"] for l in db.task_lists()]


def test_an_admin_can_add_a_list(admin_client):
    r = admin_client.post("/api/tasks/lists", json={"name": "Dock"})
    assert r.status_code == 200 and r.json()["name"] == "Dock"


def test_a_list_needs_a_name(admin_client):
    assert admin_client.post("/api/tasks/lists", json={"name": "  "}).status_code == 400


def test_a_viewer_cannot_manage_lists(viewer_client, lists):
    assert viewer_client.post("/api/tasks/lists", json={"name": "X"}).status_code == 403
    assert viewer_client.delete(f"/api/tasks/lists/{lists['Car']}").status_code == 403


def test_deleting_a_list_keeps_its_tasks(admin_client, db, lists):
    """The category is a label; the work is the thing. Cascading would quietly
    delete somebody's jobs because a heading got tidied up."""
    _add(admin_client, "Wax the hull", list_id=lists["Boat"])
    admin_client.delete(f"/api/tasks/lists/{lists['Boat']}")
    remaining = db.tasks()
    assert [t["title"] for t in remaining] == ["Wax the hull"]
    assert remaining[0]["list_id"] is None


def test_renaming_a_list(admin_client, lists):
    r = admin_client.patch(f"/api/tasks/lists/{lists['Car']}", json={"name": "Truck"})
    assert r.status_code == 200 and r.json()["name"] == "Truck"


# --- tasks -----------------------------------------------------------------

def test_add_and_read_back(admin_client, lists):
    r = _add(admin_client, list_id=lists["Car"])
    assert r.status_code == 200
    assert r.json()["title"] == "Change the oil"
    assert admin_client.get("/api/tasks").json()["tasks"][0]["list_id"] == lists["Car"]


def test_a_task_needs_a_title(admin_client):
    assert _add(admin_client, title="   ").status_code == 400


def test_a_task_can_have_no_list(admin_client):
    r = _add(admin_client, list_id=None)
    assert r.status_code == 200 and r.json()["list_id"] is None


def test_an_unknown_list_is_refused(admin_client):
    assert _add(admin_client, list_id=9999).status_code == 404


def test_assigning_a_task_to_someone(admin_client, login_as, db):
    login_as("dana", role="viewer")
    dana = db.user_by_name("dana")["id"]
    r = _add(admin_client, assignee_id=dana)
    assert r.status_code == 200
    assert r.json()["assignee"] == "dana"


def test_an_unknown_assignee_is_refused(admin_client):
    assert _add(admin_client, assignee_id=9999).status_code == 404


def test_a_viewer_can_add_and_complete_a_task(viewer_client, admin_client, lists):
    """A family list where you cannot tick off a job somebody else wrote down
    is not a household feature."""
    theirs = _add(admin_client, "Bins out").json()["id"]
    assert _add(viewer_client, "Buy milk").status_code == 200
    r = viewer_client.patch(f"/api/tasks/{theirs}", json={"done": True})
    assert r.status_code == 200 and r.json()["done"] is True


def test_completing_records_when(admin_client, db):
    tid = _add(admin_client).json()["id"]
    admin_client.patch(f"/api/tasks/{tid}", json={"done": True})
    assert db.task(tid)["done_utc"] is not None


def test_reopening_clears_the_completion_time(admin_client, db):
    tid = _add(admin_client).json()["id"]
    admin_client.patch(f"/api/tasks/{tid}", json={"done": True})
    admin_client.patch(f"/api/tasks/{tid}", json={"done": False})
    row = db.task(tid)
    assert row["done"] == 0 and row["done_utc"] is None


def test_editing_leaves_untouched_fields_alone(admin_client, lists, db):
    tid = _add(admin_client, list_id=lists["House"], notes="under the sink").json()["id"]
    admin_client.patch(f"/api/tasks/{tid}", json={"title": "Fix the tap"})
    row = db.task(tid)
    assert row["title"] == "Fix the tap"
    assert row["notes"] == "under the sink"
    assert row["list_id"] == lists["House"]


def test_a_due_date_can_be_cleared(admin_client, db):
    tid = _add(admin_client, due=time.time() + DAY).json()["id"]
    admin_client.patch(f"/api/tasks/{tid}", json={"due": None})
    assert db.task(tid)["due_utc"] is None


@pytest.mark.parametrize("due", ["soon", float("nan"), float("inf")])
def test_a_nonsense_due_date_is_refused(admin_client, due):
    import json as _json
    r = admin_client.post(
        "/api/tasks",
        content=_json.dumps({"title": "x", "due": due}) if isinstance(due, str)
        else f'{{"title":"x","due":{due}}}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_deleting_a_task(admin_client, db):
    tid = _add(admin_client).json()["id"]
    assert admin_client.delete(f"/api/tasks/{tid}").status_code == 200
    assert db.tasks() == []


def test_deleting_a_user_keeps_their_tasks_unassigned(admin_client, login_as, db):
    """Somebody still has to do the thing."""
    login_as("dana", role="viewer")
    dana = db.user_by_name("dana")["id"]
    tid = _add(admin_client, "Mow the lawn", assignee_id=dana).json()["id"]
    db.delete_user(dana)
    row = db.task(tid)
    assert row is not None and row["assignee_id"] is None


def test_undone_tasks_sort_before_done_ones(admin_client, db):
    first = _add(admin_client, "A").json()["id"]
    _add(admin_client, "B")
    admin_client.patch(f"/api/tasks/{first}", json={"done": True})
    assert [t["title"] for t in db.tasks()] == ["B", "A"]


def test_the_soonest_due_comes_first(admin_client, db):
    now = time.time()
    _add(admin_client, "later", due=now + 5 * DAY)
    _add(admin_client, "sooner", due=now + DAY)
    _add(admin_client, "whenever")
    assert [t["title"] for t in db.tasks()] == ["sooner", "later", "whenever"]


def test_the_page_renders(admin_client):
    r = admin_client.get("/tasks")
    assert r.status_code == 200 and "tasks.js" in r.text


def test_anonymous_users_get_nothing(client, app_module, db):
    from conftest import make_user
    make_user(app_module, db)
    assert client.get("/api/tasks", follow_redirects=False).status_code == 401


# --- the calendar bridge ---------------------------------------------------

def _events(client, around):
    return client.get(
        f"/api/calendar/events?start={around - DAY}&end={around + DAY}").json()


def test_a_due_task_appears_on_the_calendar(admin_client, lists):
    due = time.time() + 3600
    _add(admin_client, "Renew the registration", due=due, list_id=lists["Car"])
    found = [e for e in _events(admin_client, due) if e["kind"] == "task"]
    assert len(found) == 1
    assert "Renew the registration" in found[0]["title"]
    assert found[0]["all_day"] is True


def test_a_task_with_no_due_date_stays_off_the_calendar(admin_client):
    _add(admin_client, "Someday")
    assert [e for e in _events(admin_client, time.time()) if e["kind"] == "task"] == []


def test_a_completed_task_leaves_the_calendar(admin_client):
    """The calendar answers "what is coming up", and a finished chore is not."""
    due = time.time() + 3600
    tid = _add(admin_client, "Bins out", due=due).json()["id"]
    assert [e for e in _events(admin_client, due) if e["kind"] == "task"]
    admin_client.patch(f"/api/tasks/{tid}", json={"done": True})
    assert [e for e in _events(admin_client, due) if e["kind"] == "task"] == []


def test_a_due_task_is_never_copied_into_the_events_table(admin_client, db):
    """Synthesised per request, so there is one copy of the truth and ticking a
    task off cannot leave a ghost event behind."""
    _add(admin_client, "Service the boat", due=time.time() + 3600)
    assert db.calendar_events(0, 1e12, None) == []


def test_the_task_carries_its_assignee_onto_the_calendar(admin_client, login_as, db):
    login_as("dana", role="viewer")
    dana = db.user_by_name("dana")["id"]
    due = time.time() + 3600
    _add(admin_client, "Take the car in", due=due, assignee_id=dana)
    found = [e for e in _events(admin_client, due) if e["kind"] == "task"][0]
    assert "dana" in found["title"]


def test_task_ids_cannot_collide_with_event_ids(admin_client):
    """Both share one feed; a bare integer id would let the calendar delete the
    wrong thing."""
    due = time.time() + 3600
    admin_client.post("/api/calendar/events", json={
        "calendar_id": "family", "title": "Dentist", "start": due, "end": due + 3600,
    })
    _add(admin_client, "Bins out", due=due)
    ids = [e["id"] for e in _events(admin_client, due)]
    assert len(set(ids)) == len(ids)
    assert any(isinstance(i, str) and i.startswith("task-") for i in ids)


def test_real_events_are_still_marked_as_events(admin_client):
    due = time.time() + 3600
    admin_client.post("/api/calendar/events", json={
        "calendar_id": "family", "title": "Dentist", "start": due, "end": due + 3600,
    })
    kinds = {e["kind"] for e in _events(admin_client, due)}
    assert kinds == {"event"}

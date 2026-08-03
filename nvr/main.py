"""Application entry point: routes, wiring, lifecycle."""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
    Response, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config as config_module, discovery, playback, proxy, streams
from .db import Database
from .recorder import RecordingService
from .retention import RetentionService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("nvr")

HERE = Path(__file__).resolve().parent

cfg = config_module.load()
db = Database(cfg.db_path)
go2rtc = streams.Go2rtcManager(cfg, db)
recording = RecordingService(cfg, db, go2rtc)
retention = RetentionService(cfg, db)

templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["human_size"] = config_module.human_size


def _asset_url(path: str) -> str:
    """Cache-busting URL for a static file: /static/<path>?v=<mtime>.

    Without this, browsers serve stale JS/CSS after an edit — which silently
    masked more than one fix during development (a "still broken" that was
    really the old file still cached).
    """
    try:
        version = int((HERE / "static" / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


templates.env.globals["asset"] = _asset_url


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.purge_expired_sessions()
    go2rtc.start()
    # Let go2rtc's RTSP port come up before recorders reach for it, or the
    # first recording attempt hits connection-refused, backs off, and leaves a
    # spurious restart in the status panel. It self-heals either way.
    if not go2rtc.wait_ready(timeout=15.0):
        log.warning("go2rtc did not report ready within 15s; starting recorders anyway")
    recording.start()
    retention.start()
    log.info("NVR ready on http://%s:%s", cfg.server.host, cfg.server.port)
    try:
        yield
    finally:
        retention.stop()
        recording.stop()
        go2rtc.stop()


app = FastAPI(title="Sentry NVR", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.add_middleware(auth.AuthMiddleware, db=db, config=cfg)


def render(
    request: Request, template: str, status_code: int = 200, **context: Any
) -> HTMLResponse:
    user = auth.current_user(request)
    return templates.TemplateResponse(
        request,
        template,
        {"user": user, "is_admin": auth.is_admin(user), **context},
        status_code=status_code,
    )


def detect_fisheye(model: str | None, width: int | None, height: int | None) -> bool:
    """Guess whether a camera is a 360/fisheye.

    A single-sensor fisheye renders a circular image on a square frame, so a
    ~1:1 aspect ratio is the strong signal; the model name (Reolink's "FE"
    series, or any 'fisheye'/'360'/'panoramic') corroborates it. The admin can
    override either way with the per-camera checkbox.
    """
    text = (model or "").lower()
    if any(k in text for k in ("fisheye", "360", "panoram", "fe-", "fe ")):
        return True
    if width and height:
        ratio = width / height
        if 0.9 <= ratio <= 1.1:
            return True
    return False


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "camera"


def unique_camera_id(name: str) -> str:
    base = slugify(name)
    candidate, suffix = base, 2
    while db.camera(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@app.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    error = None
    if len(username.strip()) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm:
        error = "Passwords do not match."
    if error:
        return render(request, "setup.html", error=error, username=username)

    # The first account is always the admin.
    user_id = db.create_user(username.strip(), auth.hash_password(password), role="admin")
    token = auth.new_token()
    db.create_session(token, user_id, cfg.server.session_days * 86400)
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(
        response, token, days=cfg.server.session_days, secure=cfg.server.secure_cookies
    )
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", next=next)


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    user = db.user_by_name(username.strip())
    if not user or not auth.verify_password(password, user["password_hash"]):
        # Deliberately vague: distinguishing "no such user" from "wrong
        # password" tells an attacker which usernames are real.
        time.sleep(0.5)
        return render(
            request, "login.html", error="Incorrect username or password.",
            username=username, next=next,
        )
    token = auth.new_token()
    db.create_session(token, user["id"], cfg.server.session_days * 86400)
    target = next if next.startswith("/") else "/"
    response = RedirectResponse(target, status_code=303)
    auth.set_session_cookie(
        response, token, days=cfg.server.session_days, secure=cfg.server.secure_cookies
    )
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        db.delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response)
    return response


@app.post("/account/password")
async def change_password(request: Request):
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    payload = await request.json()
    current = payload.get("current_password") or ""
    new = payload.get("new_password") or ""
    confirm = payload.get("confirm") or ""

    if not auth.verify_password(current, user["password_hash"]):
        # Same deliberate delay as the login path, so a wrong current password
        # cannot be probed quickly by a hijacked session.
        time.sleep(0.5)
        return JSONResponse(
            {"error": "Current password is incorrect."}, status_code=400
        )
    if len(new) < 8:
        return JSONResponse(
            {"error": "New password must be at least 8 characters."}, status_code=400
        )
    if new != confirm:
        return JSONResponse({"error": "New passwords do not match."}, status_code=400)
    if new == current:
        return JSONResponse(
            {"error": "New password must differ from the current one."}, status_code=400
        )

    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (auth.hash_password(new), user["id"]),
    )
    # Keep this browser signed in, but drop every other session — changing a
    # password should log out anywhere the old one might still be active.
    token = request.cookies.get(auth.COOKIE_NAME)
    db.execute(
        "DELETE FROM sessions WHERE user_id = ? AND token != ?",
        (user["id"], token),
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API — users (admin only; enforced in AuthMiddleware)
# ---------------------------------------------------------------------------

VALID_ROLES = {"admin", "viewer"}


def _user_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"] if "role" in row.keys() else "admin",
        "created_at": row["created_at"],
    }


@app.get("/api/users")
def api_list_users():
    return JSONResponse([_user_dict(u) for u in db.users()])


@app.post("/api/users")
async def api_create_user(request: Request):
    payload = await request.json()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    role = payload.get("role") or "viewer"

    if len(username) < 3:
        return JSONResponse({"error": "Username must be at least 3 characters."}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters."}, status_code=400)
    if role not in VALID_ROLES:
        return JSONResponse({"error": "Invalid role."}, status_code=400)
    if db.user_by_name(username):
        return JSONResponse({"error": "That username is taken."}, status_code=400)

    user_id = db.create_user(username, auth.hash_password(password), role=role)
    return JSONResponse({"id": user_id, "username": username, "role": role})


@app.patch("/api/users/{user_id}")
async def api_update_user(user_id: int, request: Request):
    target = db.user_by_id(user_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()

    if "role" in payload:
        role = payload["role"]
        if role not in VALID_ROLES:
            return JSONResponse({"error": "Invalid role."}, status_code=400)
        # Never let the last admin demote themselves out of existence.
        if role != "admin" and target["role"] == "admin" and db.admin_count() <= 1:
            return JSONResponse(
                {"error": "This is the only admin — promote someone else first."},
                status_code=400,
            )
        db.set_user_role(user_id, role)

    if payload.get("password"):
        if len(payload["password"]) < 8:
            return JSONResponse(
                {"error": "Password must be at least 8 characters."}, status_code=400
            )
        db.set_user_password(user_id, auth.hash_password(payload["password"]))

    return JSONResponse({"ok": True})


@app.delete("/api/users/{user_id}")
def api_delete_user(user_id: int, request: Request):
    target = db.user_by_id(user_id)
    if not target:
        return JSONResponse({"error": "not found"}, status_code=404)
    me = auth.current_user(request)
    if me and me["id"] == user_id:
        return JSONResponse({"error": "You cannot delete your own account."}, status_code=400)
    if target["role"] == "admin" and db.admin_count() <= 1:
        return JSONResponse({"error": "Cannot delete the only admin."}, status_code=400)
    db.delete_user(user_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def can_view(request: Request, camera: Any) -> bool:
    """Whether the current user may see a given camera.

    Admins see everything; viewers see only cameras flagged viewer_visible.
    """
    if auth.is_admin(auth.current_user(request)):
        return True
    return bool(camera["viewer_visible"])


def camera_view_models(request: Request) -> list[dict[str, Any]]:
    """Camera rows decorated with live status, for the dashboard and grid.

    Filtered to what the current user is allowed to see.
    """
    cameras = [dict(row) for row in db.cameras() if can_view(request, row)]
    status = go2rtc.stream_status()
    recorder_status = recording.status()
    for camera in cameras:
        info = status.get(streams.main_stream_name(camera["id"]))
        camera["online"] = streams.stream_online(info)
        camera["recorder"] = recorder_status.get(camera["id"], {})
        camera["stats"] = db.camera_stats(camera["id"])
    return cameras


def virtual_view_models(request: Request) -> list[dict[str, Any]]:
    """Virtual cameras the user may see, with their parent's stream and online
    state, ready to be dewarped in the browser."""
    import json as _json

    status = go2rtc.stream_status()
    result = []
    admin = auth.is_admin(auth.current_user(request))
    for v in db.virtual_cameras():
        parent = db.camera(v["parent_id"])
        if not parent or not can_view(request, parent):
            continue
        if not admin and not v["viewer_visible"]:
            continue
        stream = (
            streams.sub_stream_name(parent["id"])
            if parent["sub_url"] else streams.main_stream_name(parent["id"])
        )
        info = status.get(streams.main_stream_name(parent["id"]))
        try:
            calib = _json.loads(v["calib"]) if v["calib"] else {}
        except (ValueError, TypeError):
            calib = {}
        result.append({
            "id": v["id"],
            "name": v["name"],
            "parent_id": v["parent_id"],
            "parent_name": parent["name"],
            "stream": stream,
            "view": {"yaw": v["yaw"], "pitch": v["pitch"], "fov": v["fov"]},
            "calib": calib,
            "viewer_visible": bool(v["viewer_visible"]),
            "online": streams.stream_online(info),
        })
    return result


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cameras = camera_view_models(request)
    grid = [c for c in cameras if c["show_on_grid"]]
    return render(
        request, "dashboard.html",
        cameras=cameras,   # all viewable, for the System recording controls
        grid=grid,         # only those shown as tiles
        virtuals=virtual_view_models(request),
        total=len(cameras),
        online=sum(1 for c in cameras if c["online"]),
        recording_count=sum(1 for c in cameras if c["record"]),
        storage=retention.estimate(),
    )


@app.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request):
    cameras = camera_view_models(request)
    return render(
        request, "cameras.html",
        cameras=[c for c in cameras if c["show_on_grid"]],
        total=len(cameras),
        virtuals=virtual_view_models(request),
        storage=retention.estimate(),
    )


@app.get("/wall", response_class=HTMLResponse)
def wall(request: Request):
    """Chromeless video wall: every camera (and virtual camera) tiled to fill
    the viewport."""
    cameras = [c for c in camera_view_models(request) if c["show_on_grid"]]
    return render(
        request, "wall.html",
        cameras=cameras,
        virtuals=virtual_view_models(request),
    )


@app.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_page(request: Request, camera_id: str):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return render(request, "404.html", status_code=404)
    return render(
        request, "camera.html",
        camera=dict(camera),
        stats=db.camera_stats(camera_id),
        recorder=recording.status().get(camera_id, {}),
        stream_name=streams.main_stream_name(camera_id),
        sub_stream_name=streams.sub_stream_name(camera_id),
    )


@app.get("/cameras/{camera_id}/history", response_class=HTMLResponse)
def history_page(request: Request, camera_id: str, vcam: int | None = None):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return render(request, "404.html", status_code=404)

    # A virtual camera's history is this (parent) recording, dewarped in the
    # browser to the virtual camera's saved angle — we don't record virtuals
    # separately.
    vcam_ctx = None
    if vcam is not None:
        v = db.virtual_camera(vcam)
        if v and v["parent_id"] == camera_id:
            import json as _json

            try:
                calib = _json.loads(v["calib"]) if v["calib"] else {}
            except (ValueError, TypeError):
                calib = {}
            vcam_ctx = {
                "id": v["id"], "name": v["name"],
                "view": {"yaw": v["yaw"], "pitch": v["pitch"], "fov": v["fov"]},
                "calib": calib,
            }

    bounds = db.segment_bounds(camera_id)
    return render(
        request, "history.html",
        camera=dict(camera),
        vcam=vcam_ctx,
        bounds={"start": bounds[0], "end": bounds[1]} if bounds else None,
        stats=db.camera_stats(camera_id),
    )


@app.get("/discover", response_class=HTMLResponse)
def discover_page(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return RedirectResponse("/", status_code=303)
    return render(request, "discover.html")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    me = auth.current_user(request)
    if not auth.is_admin(me):
        return RedirectResponse("/", status_code=303)
    return render(
        request, "settings.html",
        cameras=[dict(row) for row in db.cameras()],
        virtuals=virtual_view_models(request),
        storage=retention.estimate(),
        config=cfg,
        users=[_user_dict(u) for u in db.users()] if auth.is_admin(me) else [],
        me_id=me["id"] if me else None,
    )


# ---------------------------------------------------------------------------
# API — cameras
# ---------------------------------------------------------------------------


# Never sent to the browser: the RTSP URLs embed the camera password, and the
# credentials themselves have no client use — go2rtc holds them server-side.
_CAMERA_SECRET_FIELDS = ("password", "username", "main_url", "sub_url")


@app.get("/api/cameras")
def api_cameras(request: Request):
    status = go2rtc.stream_status()
    result = []
    for row in db.cameras():
        if not can_view(request, row):
            continue
        camera = dict(row)
        for field in _CAMERA_SECRET_FIELDS:
            camera.pop(field, None)
        camera["has_sub"] = bool(row["sub_url"])
        info = status.get(streams.main_stream_name(camera["id"]))
        camera["online"] = streams.stream_online(info)
        camera["stats"] = db.camera_stats(camera["id"])
        result.append(camera)
    return JSONResponse(result)


@app.post("/api/discover")
def api_discover():
    known = {row["host"]: row["id"] for row in db.cameras()}
    found = discovery.discover(
        subnets=cfg.discovery.subnets or None,
        timeout=cfg.discovery.timeout,
        onvif_wait=cfg.discovery.onvif_wait,
        known_hosts=known,
    )
    return JSONResponse([candidate.to_dict() for candidate in found])


@app.post("/api/cameras/inspect")
async def api_inspect(request: Request):
    payload = await request.json()
    host = (payload.get("host") or "").strip()
    if not host:
        return JSONResponse({"error": "host is required"}, status_code=400)
    result = discovery.inspect(
        host=host,
        username=payload.get("username") or "",
        password=payload.get("password") or "",
        brand=payload.get("brand"),
        onvif_url=payload.get("onvif_url"),
    )
    return JSONResponse(result)


@app.post("/api/cameras")
async def api_add_camera(request: Request):
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    main_url = (payload.get("main_url") or "").strip()
    if not (name and host and main_url):
        return JSONResponse(
            {"error": "name, host and main_url are required"}, status_code=400
        )

    # Verify before persisting: a camera that fails here would otherwise sit in
    # the list looking healthy while producing nothing.
    check = streams.probe_rtsp(main_url)
    if not check.get("ok"):
        return JSONResponse(
            {"error": f"Could not open the main stream: {check.get('error')}"},
            status_code=400,
        )

    # Auto-flag 360 cameras from the probed frame shape and the model name.
    # The admin can override this later with the per-camera checkbox.
    fisheye = payload.get("fisheye")
    if fisheye is None:
        fisheye = detect_fisheye(
            payload.get("model"), check.get("width"), check.get("height")
        )

    camera_id = unique_camera_id(name)
    db.add_camera(
        id=camera_id,
        name=name,
        host=host,
        port=int(payload.get("port") or 80),
        brand=payload.get("brand"),
        model=payload.get("model"),
        serial=payload.get("serial"),
        mac=payload.get("mac"),
        username=payload.get("username"),
        password=payload.get("password"),
        main_url=main_url,
        sub_url=(payload.get("sub_url") or "").strip() or None,
        record=1 if payload.get("record", True) else 0,
        record_stream=payload.get("record_stream") or "main",
        fisheye=1 if fisheye else 0,
        enabled=1,
        last_seen=int(time.time()),
    )
    go2rtc.reload()
    recording.sync()
    return JSONResponse(
        {"id": camera_id, "probe": check, "redirect": f"/cameras/{camera_id}"}
    )


# Bounded recording windows, in seconds, offered by the dashboard control.
RECORD_WINDOWS = {
    "1h": 3600,
    "24h": 86400,
    "3d": 3 * 86400,
    "5d": 5 * 86400,
}


@app.patch("/api/cameras/{camera_id}")
async def api_update_camera(camera_id: str, request: Request):
    if not db.camera(camera_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()

    # A record_mode is sugar the UI sends instead of hand-setting record and
    # record_until: "on"/"off" for continuous, or a window key for a bounded
    # capture that auto-stops.
    mode = payload.get("record_mode")
    if mode == "on":
        payload["record"], payload["record_until"] = 1, None
    elif mode == "off":
        payload["record"], payload["record_until"] = 0, None
    elif mode in RECORD_WINDOWS:
        payload["record"] = 1
        payload["record_until"] = time.time() + RECORD_WINDOWS[mode]
    elif mode is not None:
        return JSONResponse({"error": f"unknown record_mode {mode!r}"}, status_code=400)

    allowed = {"name", "record", "record_stream", "enabled", "main_url", "sub_url",
               "username", "password", "record_until", "retention_seconds",
               "fisheye", "viewer_visible", "show_on_grid"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    for flag in ("record", "enabled", "fisheye", "viewer_visible", "show_on_grid"):
        if flag in fields:
            fields[flag] = 1 if fields[flag] else 0
    db.update_camera(camera_id, **fields)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True})


@app.delete("/api/cameras/{camera_id}")
def api_delete_camera(camera_id: str, purge: bool = False):
    if not db.camera(camera_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    if purge:
        import shutil

        shutil.rmtree(cfg.storage.recordings_dir / camera_id, ignore_errors=True)
    db.delete_camera(camera_id)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True, "purged": purge})


# ---------------------------------------------------------------------------
# API — virtual cameras (fixed dewarp views of a fisheye parent; admin only)
# ---------------------------------------------------------------------------


@app.post("/api/cameras/{parent_id}/virtual")
async def api_create_virtual(parent_id: str, request: Request):
    parent = db.camera(parent_id)
    if not parent:
        return JSONResponse({"error": "parent not found"}, status_code=404)
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "A name is required."}, status_code=400)
    import json as _json

    vid = db.add_virtual_camera(
        parent_id=parent_id,
        name=name,
        yaw=float(payload.get("yaw") or 0.0),
        pitch=float(payload.get("pitch") or 0.0),
        fov=float(payload.get("fov") or 1.5708),
        calib=_json.dumps(payload.get("calib") or {}),
    )
    return JSONResponse({"id": vid, "name": name})


@app.get("/api/virtual/{vid}")
def api_get_virtual(vid: int, request: Request):
    v = db.virtual_camera(vid)
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    parent = db.camera(v["parent_id"])
    if not parent or not can_view(request, parent):
        return JSONResponse({"error": "not found"}, status_code=404)
    import json as _json

    try:
        calib = _json.loads(v["calib"]) if v["calib"] else {}
    except (ValueError, TypeError):
        calib = {}
    return JSONResponse({
        "id": v["id"], "name": v["name"], "parent_id": v["parent_id"],
        "yaw": v["yaw"], "pitch": v["pitch"], "fov": v["fov"], "calib": calib,
    })


@app.put("/api/virtual/{vid}")
async def api_update_virtual(vid: int, request: Request):
    if not db.virtual_camera(vid):
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    import json as _json

    fields: dict[str, Any] = {}
    if "name" in payload and (payload.get("name") or "").strip():
        fields["name"] = payload["name"].strip()
    for key in ("yaw", "pitch", "fov"):
        if key in payload:
            fields[key] = float(payload[key])
    if "calib" in payload:
        fields["calib"] = _json.dumps(payload["calib"] or {})
    if "viewer_visible" in payload:
        fields["viewer_visible"] = 1 if payload["viewer_visible"] else 0
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    assigns = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE virtual_cameras SET {assigns} WHERE id = ?",
        (*fields.values(), vid),
    )
    return JSONResponse({"ok": True})


@app.delete("/api/virtual/{vid}")
def api_delete_virtual(vid: int):
    if not db.virtual_camera(vid):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_virtual_camera(vid)
    return JSONResponse({"ok": True})


@app.get("/api/cameras/{camera_id}/snapshot.jpg")
def api_snapshot(request: Request, camera_id: str):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return Response("not found", status_code=404)
    frame = go2rtc.snapshot(camera_id)
    if not frame:
        return Response("no frame available", status_code=503)
    return Response(
        frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ---------------------------------------------------------------------------
# API — playback
# ---------------------------------------------------------------------------


@app.get("/api/cameras/{camera_id}/timeline")
def api_timeline(request: Request, camera_id: str, start: float, end: float):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return JSONResponse({"error": "not found"}, status_code=404)
    if end <= start:
        return JSONResponse({"error": "end must be after start"}, status_code=400)
    ranges = playback.coverage(db, camera_id, start, end)
    bounds = db.segment_bounds(camera_id)
    return JSONResponse({
        "camera_id": camera_id,
        "start": start,
        "end": end,
        "ranges": [r.to_dict() for r in ranges],
        "bounds": {"start": bounds[0], "end": bounds[1]} if bounds else None,
    })


@app.get("/api/cameras/{camera_id}/playback.mp4")
def api_playback(request: Request, camera_id: str, start: float, duration: float = 300.0):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return Response("not found", status_code=404)
    duration = max(1.0, min(duration, 3600.0))
    try:
        chunks = playback.stream_window(db, cfg, camera_id, start, duration)
    except FileNotFoundError:
        return Response("no footage for that time", status_code=404)
    return StreamingResponse(
        chunks,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
    )


@app.get("/api/cameras/{camera_id}/clip.mp4")
def api_clip(request: Request, camera_id: str, start: float, duration: float = 60.0):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return Response("not found", status_code=404)
    duration = max(1.0, min(duration, 7200.0))  # cap exports at 2 hours
    try:
        path = playback.export_clip(db, cfg, camera_id, start, duration)
    except FileNotFoundError:
        return Response("no footage for that time", status_code=404)
    except RuntimeError as exc:
        return Response(f"export failed: {exc}", status_code=500)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{camera_id}-{stamp}.mp4",
        background=_cleanup(path),
    )


def _cleanup(path: Path):
    """Delete an exported clip once it has been sent."""
    from starlette.background import BackgroundTask
    import shutil

    return BackgroundTask(lambda: shutil.rmtree(path.parent, ignore_errors=True))


# ---------------------------------------------------------------------------
# API — status
# ---------------------------------------------------------------------------


@app.get("/api/status")
def api_status():
    return JSONResponse({
        "storage": retention.estimate(),
        "recorders": recording.status(),
        "streams": {
            name: {"producers": len(info.get("producers") or [])}
            for name, info in go2rtc.stream_status().items()
        },
        "go2rtc_running": go2rtc.process is not None and go2rtc.process.poll() is None,
    })


# ---------------------------------------------------------------------------
# API — presence (who's watching a camera in single live view)
# ---------------------------------------------------------------------------

# target -> { session_token: (last_seen, listening) }. A target is a camera id,
# or "vcam:<id>" for a virtual camera. Grid tiles do not ping, so this counts
# only people actually on a camera's single live view. In-memory and
# best-effort: entries simply expire.
_presence: dict[str, dict[str, tuple[float, bool]]] = {}
_presence_lock = threading.Lock()
PRESENCE_TTL = 20.0


def _presence_counts() -> dict[str, dict[str, int]]:
    now = time.time()
    out: dict[str, dict[str, int]] = {}
    with _presence_lock:
        for target in list(_presence):
            live = {
                tok: v for tok, v in _presence[target].items()
                if now - v[0] < PRESENCE_TTL
            }
            if live:
                _presence[target] = live
                out[target] = {
                    "watching": len(live),
                    "listening": sum(1 for v in live.values() if v[1]),
                }
            else:
                _presence.pop(target, None)
    return out


@app.post("/api/presence/ping")
async def api_presence_ping(request: Request):
    payload = await request.json()
    target = (payload.get("target") or "").strip()
    if not target:
        return JSONResponse({"error": "target required"}, status_code=400)
    token = request.cookies.get(auth.COOKIE_NAME) or "anon"
    with _presence_lock:
        if payload.get("leave"):
            if target in _presence:
                _presence[target].pop(token, None)
        else:
            _presence.setdefault(target, {})[token] = (time.time(), bool(payload.get("listening")))
    return JSONResponse({"ok": True})


@app.get("/api/presence")
def api_presence():
    return JSONResponse(_presence_counts())


@app.get("/api/presence/stream")
async def api_presence_stream(request: Request):
    """Server-Sent Events: push viewer counts the instant they change, so the
    dashboard and cameras page update live without polling."""
    import asyncio
    import json as _json

    async def gen():
        last = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            payload = _json.dumps(_presence_counts(), sort_keys=True)
            if payload != last:
                last = payload
                idle = 0
                yield f"data: {payload}\n\n"
            else:
                idle += 1
                if idle >= 30:      # ~15s comment keeps the connection warm
                    idle = 0
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# API — saved clips (kept permanently on the box; captured dewarped in-browser)
# ---------------------------------------------------------------------------


def _clip_allowed(request: Request, clip: Any) -> bool:
    """Whether the user may see a clip: they can view its source camera, or
    (for a clip whose camera was since deleted) they are an admin."""
    camera = db.camera(clip["camera_id"])
    if camera is not None:
        return can_view(request, camera)
    return auth.is_admin(auth.current_user(request))


@app.post("/api/clips")
async def api_save_clip(
    request: Request,
    file: UploadFile = File(...),
    camera_id: str = Form(...),
    name: str = Form(...),
    vcam_id: str = Form(""),
    start: float = Form(0.0),
    duration: float = Form(0.0),
):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty clip"}, status_code=400)
    if len(data) > 512 * 1024 * 1024:
        return JSONResponse({"error": "clip too large"}, status_code=413)

    ext = ".webm" if "webm" in (file.content_type or "").lower() else ".mp4"
    fname = f"{slugify(camera_id)}-{int(time.time())}{ext}"
    dest = cfg.storage.clips_dir / fname
    dest.write_bytes(data)

    vid = int(vcam_id) if vcam_id.strip().isdigit() else None
    clip_id = db.add_clip(
        camera_id=camera_id, name=(name.strip() or "Clip"), path=str(dest),
        mime=file.content_type or "video/webm", size=len(data),
        vcam_id=vid, start_ts=start or None, duration=duration or None,
    )
    return JSONResponse({"id": clip_id, "redirect": "/clips"})


@app.get("/api/clips/{clip_id}/file")
def api_clip_file(clip_id: int, request: Request):
    clip = db.clip(clip_id)
    if not clip or not _clip_allowed(request, clip):
        return Response("not found", status_code=404)
    path = Path(clip["path"])
    if not path.exists():
        return Response("clip file missing", status_code=404)
    return FileResponse(path, media_type=clip["mime"] or "video/webm")


@app.delete("/api/clips/{clip_id}")
def api_delete_clip(clip_id: int, request: Request):
    clip = db.clip(clip_id)
    if not clip or not _clip_allowed(request, clip):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        Path(clip["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.delete_clip(clip_id)
    return JSONResponse({"ok": True})


@app.get("/clips", response_class=HTMLResponse)
def clips_page(request: Request):
    clips = []
    for c in db.clips():
        if not _clip_allowed(request, c):
            continue
        camera = db.camera(c["camera_id"])
        clips.append({
            "id": c["id"],
            "name": c["name"],
            "camera_name": camera["name"] if camera else "(removed camera)",
            "duration": c["duration"],
            "size": c["size"],
            "created_at": c["created_at"],
            "mime": c["mime"],
        })
    return render(request, "clips.html", clips=clips)


# ---------------------------------------------------------------------------
# go2rtc proxy — live video, gated by the session
# ---------------------------------------------------------------------------


@app.api_route("/go2rtc/{path:path}", methods=["GET", "POST"])
async def go2rtc_proxy(request: Request, path: str):
    # Live video (WebRTC, MJPEG, frames) is addressed by a `src` stream name.
    # A viewer must not be able to pull a camera they're not allowed to see by
    # naming its stream directly, so resolve src -> camera and check access.
    src = request.query_params.get("src")
    if src and not auth.is_admin(auth.current_user(request)):
        camera_id = src[:-4] if src.endswith("_sub") else src
        camera = db.camera(camera_id)
        if not camera or not can_view(request, camera):
            return Response("forbidden", status_code=403)
    return await proxy.forward(request, path, cfg)

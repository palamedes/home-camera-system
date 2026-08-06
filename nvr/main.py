"""Application entry point: routes, wiring, lifecycle."""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import httpx
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

from . import (
    appsettings, auth, camera_control, config as config_module, discovery,
    playback, proxy, streamprobe, streams,
)
from .db import Database
from .alerts import AlertService
from .events import EventService
from .recorder import RecordingService
from .retention import RetentionService
from .scheduler import SchedulerService
from .storage_migrate import StorageMigrator
from .weather import WeatherService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("nvr")

HERE = Path(__file__).resolve().parent

cfg = config_module.load()
db = Database(cfg.db_path)
# Replay any in-app settings edits over the config.yaml defaults before the
# services (which hold references to cfg.weather/cfg.alerts) are constructed.
appsettings.load_overrides(cfg, db)
go2rtc = streams.Go2rtcManager(cfg, db)
recording = RecordingService(cfg, db, go2rtc)
retention = RetentionService(cfg, db)
scheduler = SchedulerService(cfg, db, recording)
alerts = AlertService(cfg, db)
weather = WeatherService(cfg, alerts)
events = EventService(cfg, db, alerts)
migrator = StorageMigrator(cfg, db)

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
    scheduler.start()
    retention.start()
    weather.start()
    events.start()
    log.info("NVR ready on http://%s:%s", cfg.server.host, cfg.server.port)
    try:
        yield
    finally:
        events.stop()
        weather.stop()
        retention.stop()
        scheduler.stop()
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


def _online(camera: Any, info: Any) -> bool:
    """A camera is online if it is streaming now OR simply reachable.

    go2rtc pulls cameras on demand, so one that is neither being recorded nor
    watched has no bytes flowing — that is idle, not offline. Fall back to a
    cheap (cached) TCP reachability probe so "not recording" never reads as
    "offline". A camera the admin has disabled is deliberately offline and is
    never probed.
    """
    if "enabled" in camera.keys() and not camera["enabled"]:
        return False
    return streams.stream_online(info) or go2rtc.camera_reachable(camera)


def camera_view_models(request: Request) -> list[dict[str, Any]]:
    """Camera rows decorated with live status, for the dashboard and grid.

    Filtered to what the current user is allowed to see.
    """
    cameras = [dict(row) for row in db.cameras() if can_view(request, row)]
    status = go2rtc.stream_status()
    recorder_status = recording.status()
    for camera in cameras:
        info = status.get(streams.main_stream_name(camera["id"]))
        camera["online"] = _online(camera, info)
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
            "show_on_grid": bool(v["show_on_grid"]),
            "online": _online(parent, info),
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
        virtuals=[v for v in virtual_view_models(request) if v["show_on_grid"]],
        total=len(cameras),
        online=sum(1 for c in cameras if c["online"]),
        recording_count=sum(1 for c in cameras if c["record"]),
        storage=retention.estimate(),
        weather_enabled=cfg.weather.enabled,
    )


@app.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request):
    cameras = camera_view_models(request)
    return render(
        request, "cameras.html",
        cameras=[c for c in cameras if c["show_on_grid"]],
        total=len(cameras),
        online=sum(1 for c in cameras if c["online"]),
        recording_count=sum(1 for c in cameras if c["record"]),
        virtuals=[v for v in virtual_view_models(request) if v["show_on_grid"]],
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
        virtuals=[v for v in virtual_view_models(request) if v["show_on_grid"]],
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
        talk_stream_name=streams.talk_stream_name(camera_id),
        # HD (main) on an H.265 camera is served via a go2rtc QSV transcode;
        # surface that in the live-view mode label so the extra cost is visible.
        main_is_hevc=streams._is_hevc_url(camera["main_url"]),
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
    schedules: dict[str, list[dict[str, Any]]] = {}
    for row in db.schedules():
        schedules.setdefault(row["camera_id"], []).append(_schedule_dict(row))
    status = go2rtc.stream_status()
    cameras = []
    for row in db.cameras():
        cam = dict(row)
        cam["online"] = _online(row, status.get(streams.main_stream_name(row["id"])))
        cam["sched_count"] = len(schedules.get(row["id"], []))
        cameras.append(cam)
    return render(
        request, "settings.html",
        cameras=cameras,
        schedules=schedules,
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
        camera["online"] = _online(row, info)
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
               "rolling_keep_seconds", "fisheye", "viewer_visible", "show_on_grid"}
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

        # Footage may be spread across every pool volume.
        for base in cfg.storage.volume_paths():
            shutil.rmtree(base / camera_id, ignore_errors=True)
    db.delete_camera(camera_id)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True, "purged": purge})


@app.get("/api/cameras/{camera_id}/streams")
def api_camera_streams(request: Request, camera_id: str):
    """Actual resolution/bitrate of a camera's main and sub streams, plus any
    settable encoder options. Called async by the settings page to label the
    record-stream selector; a probe that fails just returns nulls."""
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(streamprobe.describe_streams(dict(camera), cfg))


@app.post("/api/cameras/{camera_id}/encoder")
async def api_set_encoder(camera_id: str, request: Request):
    """Change a Reolink camera's encoder resolution/bitrate for one stream.

    Admin-gated centrally (POST under /api/cameras). Degrades with a clear error
    on non-Reolink cameras or when the device rejects the change; never crashes.
    """
    camera = db.camera(camera_id)
    if not camera:
        return JSONResponse({"error": "not found"}, status_code=404)
    if (camera["brand"] or "").lower() != "reolink":
        return JSONResponse(
            {"error": "Encoder control is only supported on Reolink cameras."},
            status_code=400,
        )
    payload = await request.json()
    stream = payload.get("stream")
    if stream not in ("main", "sub"):
        return JSONResponse(
            {"error": "stream must be 'main' or 'sub'"}, status_code=400
        )
    resolution = payload.get("resolution")
    bitrate = payload.get("bitrate")
    if resolution in (None, "") and bitrate in (None, ""):
        return JSONResponse({"error": "nothing to change"}, status_code=400)

    from . import reolink

    try:
        with reolink.ReolinkClient(
            camera["host"], camera["username"] or "", camera["password"] or ""
        ) as client:
            client.set_encoding(
                stream,
                size=resolution or None,
                bitrate=int(bitrate) if bitrate else None,
            )
            client.logout()
    except Exception as exc:
        return JSONResponse(
            {"error": f"Camera rejected the change: {exc}"}, status_code=502
        )

    # The encoder's output just changed shape; bounce the recorder for this
    # camera so a fresh segment starts on the new resolution rather than a
    # -c copy stream whose dimensions changed mid-file.
    recording.restart(camera_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API — camera schedules (time-of-day rules; admin only via /api/cameras)
# ---------------------------------------------------------------------------

SCHEDULE_ACTIONS = {"record", "light", "nightvision"}
NIGHTVISION_MODES = {"auto", "color", "bw"}


def _schedule_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "action": row["action"],
        "days": row["days"],
        "start_min": row["start_min"],
        "end_min": row["end_min"],
        "value": row["value"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


@app.get("/api/cameras/{camera_id}/schedules")
def api_list_schedules(camera_id: str):
    if not db.camera(camera_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse([_schedule_dict(s) for s in db.schedules_for(camera_id)])


@app.post("/api/cameras/{camera_id}/schedules")
async def api_create_schedule(camera_id: str, request: Request):
    if not db.camera(camera_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()

    action = payload.get("action")
    if action not in SCHEDULE_ACTIONS:
        return JSONResponse({"error": "invalid action"}, status_code=400)

    try:
        days = int(payload.get("days"))
        start_min = int(payload.get("start_min"))
        end_min = int(payload.get("end_min"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "days and times must be integers"}, status_code=400)

    if not (0 <= days <= 127):
        return JSONResponse({"error": "days must be a 0..127 bitmask"}, status_code=400)
    if days == 0:
        return JSONResponse({"error": "select at least one day"}, status_code=400)
    if not (0 <= start_min <= 1439 and 0 <= end_min <= 1439):
        return JSONResponse({"error": "times must be within 0..1439"}, status_code=400)
    if start_min == end_min:
        return JSONResponse({"error": "start and end must differ"}, status_code=400)

    if action == "nightvision":
        value = payload.get("value") or "auto"
        if value not in NIGHTVISION_MODES:
            return JSONResponse({"error": "invalid nightvision mode"}, status_code=400)
    else:
        value = "on"

    sid = db.add_schedule(
        camera_id=camera_id, action=action, days=days,
        start_min=start_min, end_min=end_min, value=value,
    )
    return JSONResponse(_schedule_dict(db.one(
        "SELECT * FROM schedules WHERE id = ?", (sid,)
    )))


@app.patch("/api/cameras/{camera_id}/schedules/{sid}")
async def api_update_schedule(camera_id: str, sid: int, request: Request):
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not row or row["camera_id"] != camera_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    if "enabled" not in payload:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.set_schedule_enabled(sid, bool(payload["enabled"]))
    return JSONResponse({"ok": True})


@app.delete("/api/cameras/{camera_id}/schedules/{sid}")
def api_delete_schedule(camera_id: str, sid: int):
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not row or row["camera_id"] != camera_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_schedule(sid)
    return JSONResponse({"ok": True})


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
    if "show_on_grid" in payload:
        fields["show_on_grid"] = 1 if payload["show_on_grid"] else 0
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


# ---------------------------------------------------------------------------
# API — device control (spotlight / night vision)
# ---------------------------------------------------------------------------
#
# GET .../controls is read-only and fine for any viewer who can see the camera.
# The POST mutations sit under /api/cameras/{id}/... so AuthMiddleware
# auto-forbids non-admins (POST under /api/cameras is admin-only).


@app.get("/api/cameras/{camera_id}/controls")
def api_camera_controls(request: Request, camera_id: str):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(camera_control.get_controls(camera))


@app.post("/api/cameras/{camera_id}/light")
async def api_camera_light(camera_id: str, request: Request):
    camera = db.camera(camera_id)
    if not camera:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    if "on" not in payload:
        return JSONResponse({"error": "'on' is required"}, status_code=400)
    try:
        camera_control.set_light(camera, bool(payload["on"]))
    except camera_control.CameraControlError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/api/cameras/{camera_id}/nightvision")
async def api_camera_nightvision(camera_id: str, request: Request):
    camera = db.camera(camera_id)
    if not camera:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    mode = payload.get("mode")
    ir = payload.get("ir")
    if mode is None and ir is None:
        return JSONResponse(
            {"error": "at least one of 'mode' or 'ir' is required"}, status_code=400
        )
    try:
        camera_control.set_night_vision(camera, mode=mode, ir=ir)
    except camera_control.CameraControlError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
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
        "events": [_event_dict(e) for e in db.events_in_range(camera_id, start, end)],
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


@app.get("/api/weather")
def api_weather():
    """Cached weather + river level for the dashboard card. Served from memory,
    refreshed on a background timer, so this never blocks on the network."""
    return JSONResponse(weather.snapshot())


# ---------------------------------------------------------------------------
# API — events & alerts
# ---------------------------------------------------------------------------

def _event_dict(row: Any) -> dict[str, Any]:
    meta = row["meta"]
    try:
        meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    except (ValueError, TypeError):
        meta = {}
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "ts": row["ts"],
        "type": row["type"],
        "label": row["label"],
        "score": row["score"],
        "meta": meta,
    }


@app.get("/api/events")
def api_events(request: Request, limit: int = 50):
    """Recent events, filtered to what the requester may see. Non-camera events
    (e.g. river-level alerts) are visible to everyone logged in."""
    out = []
    for e in db.recent_events(limit=min(max(limit, 1), 500)):
        cid = e["camera_id"]
        if cid:
            cam = db.camera(cid)
            if not cam or not can_view(request, cam):
                continue
        out.append(_event_dict(e))
    return JSONResponse({"events": out})


@app.post("/api/alerts/test")
def api_alerts_test(request: Request):
    """Fire a one-off test alert to the configured webhook (admin only)."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        sent = alerts.test()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"sent": bool(sent)})


# ---------------------------------------------------------------------------
# API — app settings (weather / alerts), editable from the settings page
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def api_settings(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(appsettings.current(cfg))


@app.patch("/api/settings/weather")
async def api_settings_weather(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    try:
        applied = appsettings.update_section(cfg, db, "weather", body)
    except appsettings.SettingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Reflect the change immediately: re-fetch off-thread so the card updates
    # without blocking this request on the weather APIs.
    threading.Thread(target=weather.refresh, name="weather-refresh", daemon=True).start()
    return JSONResponse({"applied": applied, "settings": appsettings.current(cfg)})


@app.patch("/api/settings/alerts")
async def api_settings_alerts(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    try:
        applied = appsettings.update_section(cfg, db, "alerts", body)
    except appsettings.SettingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Start/stop the AI poller to match the new enabled/detect state.
    events.apply()
    return JSONResponse({"applied": applied, "settings": appsettings.current(cfg)})


@app.get("/api/settings/geocode")
def api_settings_geocode(request: Request, q: str = ""):
    """Look up coordinates for a place name (Open-Meteo geocoding), so the
    weather location can be set by search instead of typing lat/lon."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    try:
        resp = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": q, "count": 6, "language": "en", "format": "json"},
            timeout=6.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"results": []})
    results = [
        {
            "label": ", ".join(
                p for p in (r.get("name"), r.get("admin1"), r.get("country_code")) if p
            ),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
        }
        for r in raw
        if r.get("latitude") is not None and r.get("longitude") is not None
    ]
    return JSONResponse({"results": results})


# ---------------------------------------------------------------------------
# API — storage location (relocate recordings/clips, e.g. onto a NAS)
# ---------------------------------------------------------------------------

@app.get("/api/settings/storage")
def api_storage(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({
        "current": appsettings.storage_current(cfg),
        "migrate": migrator.status(),
    })


@app.post("/api/settings/storage/check")
async def api_storage_check(request: Request):
    """Dry-run: validate candidate paths and report free space, without applying."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    return JSONResponse({"checks": appsettings.check_storage(body)})


@app.post("/api/settings/storage")
async def api_storage_apply(request: Request):
    """Relocate the recordings/clips roots. New footage writes to the new
    location immediately; existing footage stays put (migrate it separately)."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    try:
        applied = appsettings.apply_storage(cfg, db, body)
    except appsettings.SettingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Rebuild recorders so their ffmpeg writes into the new directory. The
    # supervisor recreates each from current config on its next tick (~5s).
    if "recordings_dir" in applied:
        for cam_id in list(recording.recorders.keys()):
            recording.restart(cam_id)
    return JSONResponse({
        "applied": applied,
        "current": appsettings.storage_current(cfg),
    })


@app.post("/api/settings/storage/migrate")
def api_storage_migrate(request: Request):
    """Kick off moving existing footage into the current storage location."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    started = migrator.start()
    return JSONResponse({"started": started, "migrate": migrator.status()})


@app.get("/api/settings/storage/migrate")
def api_storage_migrate_status(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(migrator.status())


@app.get("/api/settings/volumes")
def api_volumes(request: Request):
    """The recordings pool with live per-volume usage (path, cap, used, free)."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"volumes": retention.estimate()["volumes"]})


@app.post("/api/settings/volumes")
async def api_volumes_apply(request: Request):
    """Set the ordered recordings pool. New footage begins landing on the first
    volume with room on the next supervisor tick."""
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    try:
        appsettings.apply_volumes(cfg, db, body.get("volumes"))
    except appsettings.SettingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Rebuild recorders so volume selection re-evaluates immediately (otherwise
    # they'd keep writing to the old volume until the next natural rebuild).
    for cam_id in list(recording.recorders.keys()):
        recording.restart(cam_id)
    return JSONResponse({"volumes": retention.estimate()["volumes"]})


@app.patch("/api/settings/storage_limits")
async def api_settings_storage_limits(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    try:
        applied, restarts = appsettings.update_advanced(cfg, db, "storage_limits", body)
    except appsettings.SettingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # A segment-length change takes effect by rebuilding each recorder's ffmpeg.
    if "recorder" in restarts:
        for cam_id in list(recording.recorders.keys()):
            recording.restart(cam_id)
    return JSONResponse({
        "applied": applied,
        "restart_required": [r for r in restarts if r == "app"],
        "settings": appsettings.current(cfg),
    })


@app.patch("/api/settings/network")
async def api_settings_network(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    try:
        applied, restarts = appsettings.update_advanced(cfg, db, "network", body)
    except appsettings.SettingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Live fields (discovery, playback, sessions) are already in effect. Host,
    # port, and go2rtc ports can only rebind on a full restart.
    return JSONResponse({
        "applied": applied,
        "restart_required": [r for r in restarts if r == "app"],
        "settings": appsettings.current(cfg),
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

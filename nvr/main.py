"""Application entry point: routes, wiring, lifecycle."""

from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
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
    for v in db.virtual_cameras():
        parent = db.camera(v["parent_id"])
        if not parent or not can_view(request, parent):
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
            "online": streams.stream_online(info),
        })
    return result


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cameras = camera_view_models(request)
    grid = [c for c in cameras if c["show_on_grid"]]
    return render(
        request, "dashboard.html",
        grid=grid,
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
    """Chromeless video wall: every camera tiled to fill the viewport."""
    cameras = [c for c in camera_view_models(request) if c["show_on_grid"]]
    return render(request, "wall.html", cameras=cameras)


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
def history_page(request: Request, camera_id: str):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return render(request, "404.html", status_code=404)
    bounds = db.segment_bounds(camera_id)
    return render(
        request, "history.html",
        camera=dict(camera),
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

"""Application entry point: routes, wiring, lifecycle."""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import threading
import time
import uuid

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
from starlette.concurrency import run_in_threadpool

from . import (
    appsettings, auth, camera_control, config as config_module, devices as devicelib,
    discovery, netscan, playback, proxy, shades as shadelib, streamprobe, streams,
)
from .db import Database
from .alerts import AlertService
from .automations import AutomationService
from . import automations as automationlib
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

# Saved-clip upload limits. The cap is enforced while streaming, so a body that
# lies about its length still cannot exhaust memory.
MAX_CLIP_BYTES = 512 * 1024 * 1024
CLIP_CHUNK_BYTES = 1024 * 1024

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
automations = AutomationService(cfg, db, devices=devicelib, shades=shadelib)
# Wired after construction: the dispatcher is what every detector funnels
# through, so it is the one place automations need to observe.
alerts.automations = automations
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
    automations.start()
    log.info("NVR ready on http://%s:%s", cfg.server.host, cfg.server.port)
    try:
        yield
    finally:
        automations.stop()
        events.stop()
        weather.stop()
        retention.stop()
        scheduler.stop()
        recording.stop()
        go2rtc.stop()


class _HTTPSUpgradeMiddleware:
    """Redirect plain-HTTP requests for a real public domain up to HTTPS.

    Sentry serves plain HTTP on :80; the optional Caddy front (see
    deploy/Caddyfile*) serves HTTPS on :443 and proxies back here, tagging
    those requests X-Forwarded-Proto=https. A request NOT so tagged that
    carries a real-domain Host (has a dot, isn't *.local / localhost / a bare
    IP) arrived over plain HTTP and gets bounced to https://. LAN access over
    http://<host>.local or http://<ip> is left alone, and proxied HTTPS traffic
    never loops. Temporary (307) so it's never cached — pull the middleware and
    plain HTTP works again immediately. Pure ASGI so it can't buffer the
    WebRTC/SSE streaming responses the way BaseHTTPMiddleware would.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _is_ip(host: str) -> bool:
        parts = host.split(".")
        return len(parts) == 4 and all(p.isdigit() and p.isascii() for p in parts)

    def _upgrade(self, host: str, xfp: str) -> bool:
        return (
            xfp != "https"
            and "." in host
            and not host.endswith(".local")
            and host != "localhost"
            and not self._is_ip(host)
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1").split(":")[0].lower()
            xfp = headers.get(b"x-forwarded-proto", b"").decode("latin-1").lower()
            if host and self._upgrade(host, xfp):
                target = f"https://{host}{scope.get('path', '')}"
                qs = scope.get("query_string") or b""
                if qs:
                    target += "?" + qs.decode("latin-1")
                await send({
                    "type": "http.response.start",
                    "status": 307,
                    "headers": [
                        (b"location", target.encode("latin-1")),
                        (b"content-length", b"0"),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


app = FastAPI(title="Sentry NVR", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.add_middleware(auth.AuthMiddleware, db=db, config=cfg)


# A malformed JSON body is the caller's mistake, not ours. Handled centrally
# rather than at each of the twenty-odd endpoints that read one: a per-site
# try/except is something every future endpoint has to remember, and forgetting
# it turns a typo in somebody's curl command into a 500 and a stack trace in
# the log.
@app.exception_handler(json.JSONDecodeError)
async def _malformed_json(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "bad request: body is not valid JSON"},
                        status_code=400)


@app.exception_handler(UnicodeDecodeError)
async def _undecodable_body(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "bad request: body is not valid UTF-8"},
                        status_code=400)

# Added last => outermost => runs before auth, so a plain-HTTP hit upgrades to
# HTTPS instead of first bouncing to http://.../login.
app.add_middleware(_HTTPSUpgradeMiddleware)


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
    # Same-site paths only. "/" alone isn't enough: "//evil.example" and
    # "/\evil.example" are protocol-relative URLs that browsers send off-site,
    # which would turn a login link into a credential-harvesting redirect.
    target = next if re.match(r"^/(?![/\\])", next or "") else "/"
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
        # An archived parent has no go2rtc stream any more, so its virtual views
        # would render as permanently dead tiles on every grid — and _online()
        # would still label them online. Removing a camera removes its views.
        if parent["archived"]:
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
            "show_on_dashboard": bool(v["show_on_dashboard"]),
            "sort_order": v["sort_order"],
            "mode": v["mode"],
            # For crop virtuals the calib JSON is the normalised {x,y,w,h} rect.
            "crop": calib if v["mode"] == "crop" else {},
            "online": _online(parent, info),
        })
    return result


def grid_items(request: Request, *, only_grid: bool = False,
               only_dashboard: bool = False) -> list[dict[str, Any]]:
    """Cameras and virtual cameras merged into one list in the shared sort_order,
    so the dashboard, Cameras page and wall all show the same interleaved
    arrangement. Each item is tagged kind='camera'|'virtual'.

    Two independent visibility flags: show_on_grid covers the Cameras page and
    the wall, show_on_dashboard covers the front page.
    """
    def hidden(item: Any) -> bool:
        if only_grid and not item["show_on_grid"]:
            return True
        return bool(only_dashboard and not item["show_on_dashboard"])

    items: list[dict[str, Any]] = []
    for c in camera_view_models(request):
        if hidden(c):
            continue
        items.append({"kind": "camera", "order": c.get("sort_order") or 0, "cam": c})
    for v in virtual_view_models(request):
        if hidden(v):
            continue
        items.append({"kind": "virtual", "order": v.get("sort_order") or 0, "vcam": v})
    # Stable tie-break: cameras before virtuals when orders collide.
    items.sort(key=lambda it: (it["order"], 0 if it["kind"] == "camera" else 1))
    return items


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cameras = camera_view_models(request)
    grid = [c for c in cameras if c["show_on_dashboard"]]
    return render(
        request, "dashboard.html",
        cameras=cameras,   # all viewable, for the System recording controls
        grid=grid,         # only those shown as tiles here
        items=grid_items(request, only_dashboard=True),
        virtuals=[v for v in virtual_view_models(request)
                  if v["show_on_dashboard"]],
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
        items=grid_items(request, only_grid=True),  # cameras+virtuals, interleaved
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
        items=grid_items(request, only_grid=True),  # cameras+virtuals, interleaved
        virtuals=[v for v in virtual_view_models(request) if v["show_on_grid"]],
    )


# Backchannel (two-way Talk) capability, probed once per camera and cached.
# Some cameras (e.g. Reolink FE-P) only do two-way audio over a proprietary
# protocol go2rtc can't reach, so the Talk button is hidden for them.
_talk_cache: dict[str, bool] = {}


def _talk_supported(camera: Any) -> bool:
    cid = camera["id"]
    if cid not in _talk_cache:
        url = camera["sub_url"] or camera["main_url"]
        # None (couldn't determine) -> assume yes, so a blip never hides a
        # working button; only a definitive "no backchannel" hides it.
        _talk_cache[cid] = streamprobe.backchannel_supported(url) is not False
    return _talk_cache[cid]


@app.get("/cameras/{camera_id}", response_class=HTMLResponse)
def camera_page(request: Request, camera_id: str, vcam: int | None = None):
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return render(request, "404.html", status_code=404)
    # When opening a crop virtual camera, hand the template its rect so the live
    # view can render the sub-region (from the crisp main stream).
    vcam_ctx = None
    if vcam is not None:
        v = db.virtual_camera(vcam)
        if v and v["parent_id"] == camera_id and v["mode"] == "crop":
            import json as _json
            try:
                rect = _json.loads(v["calib"]) if v["calib"] else {}
            except (ValueError, TypeError):
                rect = {}
            vcam_ctx = {"id": v["id"], "name": v["name"], "crop": rect}
    return render(
        request, "camera.html",
        camera=dict(camera),
        vcam=vcam_ctx,
        stats=db.camera_stats(camera_id),
        recorder=recording.status().get(camera_id, {}),
        stream_name=streams.main_stream_name(camera_id),
        sub_stream_name=streams.sub_stream_name(camera_id),
        talk_stream_name=streams.talk_stream_name(camera_id),
        # HD (main) on an H.265 camera is served via a go2rtc QSV transcode;
        # surface that in the live-view mode label so the extra cost is visible.
        main_is_hevc=streams._is_hevc_url(camera["main_url"]),
        talk_supported=_talk_supported(camera),
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
    archived = []
    for row in db.archived_cameras():
        a = dict(row)
        a["stats"] = db.camera_stats(row["id"])
        archived.append(a)
    return render(
        request, "settings.html",
        cameras=cameras,
        archived_cameras=archived,
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
               "rolling_keep_seconds", "fisheye", "viewer_visible", "show_on_grid",
               "show_on_dashboard",
               "preferred_volume"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    for flag in ("record", "enabled", "fisheye", "viewer_visible", "show_on_grid",
                 "show_on_dashboard"):
        if flag in fields:
            fields[flag] = 1 if fields[flag] else 0
    # Empty string (the "Default (pool)" option) clears the pin back to NULL.
    if "preferred_volume" in fields and not fields["preferred_volume"]:
        fields["preferred_volume"] = None
    db.update_camera(camera_id, **fields)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True})


def _reolink_devinfo(host: str, user: str, pw: str) -> dict | None:
    """A Reolink camera's configured name + model, or None. Needs credentials —
    which is why only the re-link scan (per camera) can fill these in, not the
    credential-less discovery. Retried once: a single login can time out when
    the camera is busy mid-scan, which otherwise left some devices unlabelled."""
    from . import reolink
    for _ in range(2):
        try:
            with reolink.ReolinkClient(host, user, pw, timeout=6.0) as client:
                client.login()
                data = client._call([{"cmd": "GetDevInfo", "action": 0, "param": {}}])
                return data[0]["value"]["DevInfo"]
        except Exception:
            continue
    return None


@app.post("/api/cameras/{camera_id}/relink-scan")
def api_relink_scan(camera_id: str):
    """Discovery for the re-link picker, enriched with each Reolink device's
    configured name + model (via this camera's credentials) so the list reads
    like the router's — IP, name, model, MAC — not just IP + brand + MAC."""
    camera = db.camera(camera_id)
    if not camera:
        return JSONResponse({"error": "not found"}, status_code=404)
    camera = dict(camera)
    user, pw = camera.get("username") or "", camera.get("password") or ""
    found = discovery.discover(
        subnets=cfg.discovery.subnets or None,
        timeout=cfg.discovery.timeout,
        onvif_wait=cfg.discovery.onvif_wait,
        known_hosts={},
    )
    out = []
    for candidate in found:
        item = candidate.to_dict()
        if user and item.get("brand") == "reolink":
            info = _reolink_devinfo(item["host"], user, pw)
            if info:
                item["name"] = info.get("name") or item.get("name")
                item["model"] = info.get("model") or item.get("model")
        out.append(item)
    return JSONResponse(out)


@app.post("/api/cameras/{camera_id}/relink")
async def api_relink_camera(camera_id: str, request: Request):
    """Point an existing camera at a new IP/host without losing anything.

    A camera that moves (DHCP change, wired<->WiFi — a different MAC and IP) is
    the same device with the same stream paths and credentials; only its address
    changed. Swap the host into the stored URLs and re-probe to confirm, keeping
    the camera's id — so its name, virtual cameras, schedules, and recordings all
    stay attached. This is the "re-link" the Add-camera flow is not.
    """
    camera = db.camera(camera_id)
    if not camera:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    new_host = (payload.get("host") or "").strip()
    if not new_host:
        return JSONResponse({"error": "host is required"}, status_code=400)

    camera = dict(camera)

    def swap_host(url: str | None) -> str | None:
        # Replace the host in rtsp://[user:pass@]HOST[:port]/path, leaving the
        # credentials, port and stream path exactly as they were.
        if not url:
            return url
        # Userinfo may itself contain '@' (passwords do), so match all of it up
        # to the LAST '@' before the path, then the host up to :port or /path.
        return re.sub(r"(rtsp://(?:[^/]*@)?)[^:/@]+", r"\g<1>" + new_host, url, count=1)

    new_main = swap_host(camera["main_url"])
    new_sub = swap_host(camera["sub_url"])

    # Verify the camera actually answers at the new address before committing —
    # otherwise we'd point a healthy camera at a dead one.
    check = streams.probe_rtsp(new_main)
    if not check.get("ok"):
        return JSONResponse(
            {"error": f"No camera stream at {new_host}: {check.get('error')}"},
            status_code=400,
        )

    db.update_camera(camera_id, host=new_host, main_url=new_main, sub_url=new_sub)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True, "host": new_host})


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


@app.post("/api/cameras/{camera_id}/archive")
def api_archive_camera(camera_id: str):
    """Soft-delete: drop the camera from live views and stop recording it, but
    keep its footage viewable (under Removed cameras). Admin only."""
    if not db.camera(camera_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.set_camera_archived(camera_id, True)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True})


@app.post("/api/cameras/{camera_id}/restore")
def api_restore_camera(camera_id: str):
    """Bring an archived camera back to its prior state. Admin only."""
    if not db.camera(camera_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.set_camera_archived(camera_id, False)
    go2rtc.reload()
    recording.sync()
    return JSONResponse({"ok": True})


@app.post("/api/cameras/order")
async def api_set_camera_order(request: Request):
    """Persist the drag-reordered grid order (admin only via AuthMiddleware).
    Body: {"order": ["cam:<id>", "vcam:<id>", ...]}. Cameras and virtuals share
    one order space, so this drives the interleaved dashboard, Cameras page and
    wall alike."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    order = payload.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return JSONResponse({"error": "order must be a list of tokens"}, status_code=400)
    cam_ids = {row["id"] for row in db.cameras()}
    vcam_ids = {str(v["id"]) for v in db.virtual_cameras()}
    cam_pairs: list[tuple[int, str]] = []
    vcam_pairs: list[tuple[int, int]] = []
    i = 0
    for token in order:
        if token.startswith("cam:") and token[4:] in cam_ids:
            cam_pairs.append((i, token[4:])); i += 1
        elif token.startswith("vcam:") and token[5:] in vcam_ids:
            vcam_pairs.append((i, int(token[5:]))); i += 1
    db.set_camera_sort(cam_pairs)
    db.set_virtual_sort(vcam_pairs)
    return JSONResponse({"ok": True, "count": i})


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
        "device_id": row["device_id"],
        "action": row["action"],
        "days": row["days"],
        "start_min": row["start_min"],
        "end_min": row["end_min"],
        "value": row["value"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


@app.get("/api/cameras/{camera_id}/schedules")
def api_list_schedules(request: Request, camera_id: str):
    # can_view like every other camera-scoped GET: a camera hidden from viewers
    # shouldn't leak its light/record timetable (i.e. the household's routine).
    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
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

    window, error = _parse_window(payload)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    days, start_min, end_min = window

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


def _parse_window(payload: dict[str, Any]) -> tuple[tuple[int, int, int] | None, str | None]:
    """Validate the days/start/end shared by every schedule kind.

    Returns ((days, start_min, end_min), None) or (None, error message).
    """
    try:
        days = int(payload.get("days"))
        start_min = int(payload.get("start_min"))
        end_min = int(payload.get("end_min"))
    except (TypeError, ValueError):
        return None, "days and times must be integers"
    if not (0 <= days <= 127):
        return None, "days must be a 0..127 bitmask"
    if days == 0:
        return None, "select at least one day"
    if not (0 <= start_min <= 1439 and 0 <= end_min <= 1439):
        return None, "times must be within 0..1439"
    if start_min == end_min:
        return None, "start and end must differ"
    return (days, start_min, end_min), None


# --- device schedules (admin-gated by the /api/devices prefix) --------------

@app.get("/api/devices/{device_id}/schedules")
def api_list_device_schedules(device_id: str):
    if not db.device(device_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        [_schedule_dict(s) for s in db.schedules_for_device(device_id)]
    )


@app.post("/api/devices/{device_id}/schedules")
async def api_create_device_schedule(device_id: str, request: Request):
    """A device schedule is one shape: on for this window, off outside it."""
    if not db.device(device_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    window, error = _parse_window(payload)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    days, start_min, end_min = window
    sid = db.add_schedule(
        device_id=device_id, action="power", days=days,
        start_min=start_min, end_min=end_min, value="on",
    )
    return JSONResponse(_schedule_dict(db.one(
        "SELECT * FROM schedules WHERE id = ?", (sid,)
    )))


@app.patch("/api/devices/{device_id}/schedules/{sid}")
async def api_update_device_schedule(device_id: str, sid: int, request: Request):
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not row or row["device_id"] != device_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if "enabled" not in payload:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.set_schedule_enabled(sid, bool(payload["enabled"]))
    return JSONResponse({"ok": True})


@app.delete("/api/devices/{device_id}/schedules/{sid}")
def api_delete_device_schedule(device_id: str, sid: int):
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not row or row["device_id"] != device_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_schedule(sid)
    return JSONResponse({"ok": True})


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

    mode = payload.get("mode") or "fisheye"
    if mode not in ("fisheye", "crop"):
        return JSONResponse({"error": f"unknown mode {mode!r}"}, status_code=400)
    vid = db.add_virtual_camera(
        parent_id=parent_id,
        name=name,
        yaw=float(payload.get("yaw") or 0.0),
        pitch=float(payload.get("pitch") or 0.0),
        fov=float(payload.get("fov") or 1.5708),
        # For a crop virtual this is the normalised {x,y,w,h} sub-rectangle.
        calib=_json.dumps(payload.get("calib") or {}),
        mode=mode,
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
        "mode": v["mode"], "crop": calib if v["mode"] == "crop" else {},
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
    if "show_on_dashboard" in payload:
        fields["show_on_dashboard"] = 1 if payload["show_on_dashboard"] else 0
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


# --- automation: token-authed hooks for scene switches / Home Assistant ------
def _automation_token() -> str:
    """The shared secret for /api/hook. Generated (and persisted) on first use."""
    tok = db.get_setting("automation.token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        db.set_setting("automation.token", tok)
    return tok


@app.api_route("/api/hook/cameras/{camera_id}/light", methods=["GET", "POST"])
async def api_hook_light(camera_id: str, request: Request):
    """Toggle a camera's floodlight/spotlight from a smart switch or Home
    Assistant — no login, authed by the shared token instead. Reachable as a
    plain GET so even a dumb HTTP button works:
        /api/hook/cameras/<id>/light?token=<t>&state=on|off|toggle
    """
    token = request.query_params.get("token") or request.headers.get("X-Sentry-Token")
    state = (request.query_params.get("state") or "").lower()
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = token or body.get("token")
        state = state or (body.get("state") or "").lower()
    if not token or not secrets.compare_digest(str(token), _automation_token()):
        return JSONResponse({"error": "bad token"}, status_code=403)
    camera = db.camera(camera_id)
    # db.camera() resolves archived rows so History keeps working; a removed
    # camera must not still be controllable from a scene switch.
    if not camera or camera["archived"]:
        return JSONResponse({"error": "camera not found"}, status_code=404)
    if state not in ("", "on", "off", "toggle"):
        return JSONResponse({"error": "state must be on, off or toggle"}, status_code=400)
    if state in ("", "toggle"):
        want = not bool(camera_control.get_controls(camera).get("light"))
    else:
        want = state == "on"
    try:
        camera_control.set_light(camera, want)
    except camera_control.CameraControlError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "light": want})


# ---------------------------------------------------------------------------
# Calendar — a household calendar. Everyone shares the family calendar; each
# person may also keep private ones. Deliberately trusting, like the house it
# runs in: anyone signed in can add to a shared calendar.
# ---------------------------------------------------------------------------


def _ensure_shared_calendar() -> None:
    """There is always somewhere to put an event. Idempotent."""
    if any(c["owner_user_id"] is None for c in db.calendars()):
        return
    db.add_calendar(id="family", name="Family", color="#2563eb", owner_user_id=None)


def _calendar_dict(row: Any, user_id: int | None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "color": row["color"],
        "shared": row["owner_user_id"] is None,
        "mine": row["owner_user_id"] == user_id,
        "enabled": bool(row["enabled"]),
    }


def _calendar_event_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        # Distinguishes a real event from a task's due date, which rides along
        # in the same feed but is read-only there.
        "kind": "event",
        "calendar_id": row["calendar_id"],
        "title": row["title"],
        "description": row["description"],
        "location": row["location"],
        "start": row["start_utc"],
        "end": row["end_utc"],
        "all_day": bool(row["all_day"]),
    }


def _visible_calendars(user: Any) -> list[Any]:
    _ensure_shared_calendar()
    return db.calendars(user["id"] if user else None)


def _may_write_calendar(calendar: Any, user: Any) -> bool:
    """Shared calendars are writable by anyone signed in — that is the point of
    a family calendar. A private one is its owner's alone (an admin can still
    delete the calendar itself, but does not get to read or edit inside it)."""
    if calendar is None or user is None:
        return False
    return calendar["owner_user_id"] is None or calendar["owner_user_id"] == user["id"]


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    _ensure_shared_calendar()
    return render(request, "calendar.html")


@app.get("/api/calendar/calendars")
def api_calendars(request: Request):
    user = auth.current_user(request)
    return JSONResponse([
        _calendar_dict(c, user["id"] if user else None)
        for c in _visible_calendars(user)
    ])


@app.post("/api/calendar/calendars")
async def api_add_calendar(request: Request):
    user = auth.current_user(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "A name is required."}, status_code=400)
    shared = bool(payload.get("shared"))
    if shared and not auth.is_admin(user):
        return JSONResponse(
            {"error": "Only an admin can add a shared calendar."}, status_code=403
        )
    base = slugify(name) or "calendar"
    candidate, suffix = base, 2
    while db.calendar(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    db.add_calendar(
        id=candidate, name=name,
        color=(payload.get("color") or "#2563eb"),
        owner_user_id=None if shared else user["id"],
    )
    return JSONResponse({"id": candidate, "name": name})


@app.patch("/api/calendar/calendars/{calendar_id}")
async def api_update_calendar(calendar_id: str, request: Request):
    user = auth.current_user(request)
    calendar = db.calendar(calendar_id)
    if not calendar or not _may_write_calendar(calendar, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields = {k: v for k, v in payload.items() if k in ("name", "color", "enabled")}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_calendar(calendar_id, **fields)
    return JSONResponse({"ok": True})


@app.delete("/api/calendar/calendars/{calendar_id}")
def api_delete_calendar(request: Request, calendar_id: str):
    user = auth.current_user(request)
    calendar = db.calendar(calendar_id)
    if not calendar:
        return JSONResponse({"error": "not found"}, status_code=404)
    shared = calendar["owner_user_id"] is None
    allowed = auth.is_admin(user) if shared else calendar["owner_user_id"] == user["id"]
    if not allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.delete_calendar(calendar_id)
    return JSONResponse({"ok": True})


@app.get("/api/calendar/events")
def api_calendar_events(request: Request, start: float, end: float):
    """Events overlapping a window, from the calendars this user can see."""
    if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
        return JSONResponse({"error": "bad window"}, status_code=400)
    user = auth.current_user(request)
    visible = [c["id"] for c in _visible_calendars(user) if c["enabled"]]
    rows = db.calendar_events(start, end, calendar_ids=visible)
    events = [_calendar_event_dict(r) for r in rows]
    # Tasks with a due date show up alongside real events. They are synthesised
    # per request rather than mirrored into calendar_events, so there is one
    # copy of the truth and ticking a task off cannot leave a ghost behind.
    events.extend(_due_task_events(start, end))
    return JSONResponse(events)


def _due_task_events(start: float, end: float) -> list[dict[str, Any]]:
    names = _user_names()
    out = []
    for task in db.tasks_due_between(start, end):
        who = names.get(task["assignee_id"])
        out.append({
            # Namespaced so the calendar never mistakes one for an event it
            # can edit or delete — these are read-only over there.
            "id": f"task-{task['id']}",
            "calendar_id": None,
            "kind": "task",
            "task_id": task["id"],
            "title": f"{task['title']}" + (f" — {who}" if who else ""),
            "description": task["notes"],
            "location": None,
            "start": task["due_utc"],
            "end": task["due_utc"] + 3600,
            "all_day": True,
        })
    return out


def _event_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    title = (payload.get("title") or "").strip()
    if not title:
        return None, "A title is required."
    try:
        start = float(payload.get("start"))
        end = float(payload.get("end"))
    except (TypeError, ValueError):
        return None, "start and end must be timestamps"
    if not (math.isfinite(start) and math.isfinite(end)):
        return None, "start and end must be real times"
    if end <= start:
        return None, "the end must come after the start"
    return {
        "title": title,
        "description": (payload.get("description") or "").strip() or None,
        "location": (payload.get("location") or "").strip() or None,
        "start_utc": start,
        "end_utc": end,
        "all_day": 1 if payload.get("all_day") else 0,
    }, None


@app.post("/api/calendar/events")
async def api_add_calendar_event(request: Request):
    user = auth.current_user(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    # Every entry point guarantees somewhere to write, not just the ones that
    # happen to list calendars first.
    _ensure_shared_calendar()
    calendar = db.calendar(payload.get("calendar_id") or "")
    if not calendar or not _may_write_calendar(calendar, user):
        return JSONResponse({"error": "unknown calendar"}, status_code=404)
    fields, error = _event_payload(payload)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    event_id = db.add_calendar_event(
        calendar_id=calendar["id"],
        uid=f"{uuid.uuid4()}@sentry.local",
        created_by=user["id"] if user else None,
        **fields,
    )
    return JSONResponse(_calendar_event_dict(db.calendar_event(event_id)))


@app.patch("/api/calendar/events/{event_id}")
async def api_update_calendar_event(event_id: int, request: Request):
    user = auth.current_user(request)
    event = db.calendar_event(event_id)
    if not event or not _may_write_calendar(db.calendar(event["calendar_id"]), user):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    # Merge over the stored row so a partial edit keeps the rest.
    merged = {
        "title": payload.get("title", event["title"]),
        "description": payload.get("description", event["description"]),
        "location": payload.get("location", event["location"]),
        "start": payload.get("start", event["start_utc"]),
        "end": payload.get("end", event["end_utc"]),
        "all_day": payload.get("all_day", bool(event["all_day"])),
    }
    fields, error = _event_payload(merged)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if "calendar_id" in payload:
        target = db.calendar(payload["calendar_id"])
        if not target or not _may_write_calendar(target, user):
            return JSONResponse({"error": "unknown calendar"}, status_code=404)
        fields["calendar_id"] = target["id"]
    db.update_calendar_event(event_id, **fields)
    return JSONResponse(_calendar_event_dict(db.calendar_event(event_id)))


@app.delete("/api/calendar/events/{event_id}")
def api_delete_calendar_event(request: Request, event_id: int):
    user = auth.current_user(request)
    event = db.calendar_event(event_id)
    if not event or not _may_write_calendar(db.calendar(event["calendar_id"]), user):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_calendar_event(event_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API — devices (relays / smart switches). Admin-gated in AuthMiddleware.
# ---------------------------------------------------------------------------

_DEVICE_SECRET_FIELDS = ("password",)


def _device_dict(row: Any, *, include_secrets: bool = False) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    if not include_secrets:
        for field in _DEVICE_SECRET_FIELDS:
            data.pop(field, None)
    data["enabled"] = bool(data.get("enabled"))
    if data.get("last_state") is not None:
        data["last_state"] = bool(data["last_state"])
    return data


def _remember(device_id: str, state: bool | None, error: str | None) -> None:
    """Cache what we last saw, so the UI can show a device without waiting on it."""
    db.update_device(
        device_id,
        last_state=None if state is None else (1 if state else 0),
        last_seen=time.time(),
        last_error=error,
    )


@app.get("/api/devices")
def api_devices(request: Request):
    return JSONResponse({
        "devices": [_device_dict(d) for d in db.devices()],
        "drivers": devicelib.driver_choices(),
    })


@app.post("/api/devices")
async def api_add_device(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    if not name or not host:
        return JSONResponse({"error": "A name and address are required."}, status_code=400)
    driver = payload.get("driver") or "shelly"
    if driver not in devicelib.DRIVERS:
        return JSONResponse({"error": f"unknown driver {driver!r}"}, status_code=400)
    device_id = _unique_device_id(name)
    db.add_device(
        id=device_id, name=name, driver=driver, host=host,
        channel=int(payload.get("channel") or 0),
        username=(payload.get("username") or "").strip() or None,
        password=payload.get("password") or None,
    )
    return JSONResponse({"id": device_id, "name": name})


def _unique_device_id(name: str) -> str:
    base = slugify(name) or "device"
    candidate, suffix = base, 2
    while db.device(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


@app.patch("/api/devices/{device_id}")
async def api_update_device(device_id: str, request: Request):
    if not db.device(device_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    allowed = {"name", "driver", "host", "channel", "username", "password", "enabled"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if "driver" in fields and fields["driver"] not in devicelib.DRIVERS:
        return JSONResponse({"error": "unknown driver"}, status_code=400)
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if "channel" in fields:
        try:
            fields["channel"] = int(fields["channel"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "channel must be a number"}, status_code=400)
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_device(device_id, **fields)
    return JSONResponse({"ok": True})


@app.delete("/api/devices/{device_id}")
def api_delete_device(device_id: str):
    if not db.device(device_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_device(device_id)
    return JSONResponse({"ok": True})


@app.post("/api/devices/{device_id}/state")
async def api_set_device_state(device_id: str, request: Request):
    """Turn a device on/off/toggle. Body: {"state": "on"|"off"|"toggle"}."""
    device = db.device(device_id)
    if not device:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    state = (payload.get("state") or "toggle").lower()
    if state not in ("on", "off", "toggle"):
        return JSONResponse({"error": "state must be on, off or toggle"}, status_code=400)
    try:
        result = await run_in_threadpool(_apply_device_state, device, state)
    except devicelib.DeviceError as exc:
        moved = await run_in_threadpool(_rehome, "devices", device)
        if moved is None:
            _remember(device_id, None, str(exc))
            return JSONResponse({"error": str(exc)}, status_code=502)
        try:
            result = await run_in_threadpool(
                _apply_device_state, db.device(device_id), state
            )
        except devicelib.DeviceError as exc2:
            _remember(device_id, None, str(exc2))
            return JSONResponse({"error": str(exc2)}, status_code=502)
    _remember(device_id, result, None)
    await run_in_threadpool(_learn_mac, "devices", device_id, device["host"],
                            device["mac"] if "mac" in device.keys() else None)
    return JSONResponse({"ok": True, "state": result})


def _apply_device_state(device: Any, state: str) -> bool:
    if state == "toggle":
        return devicelib.toggle(device)
    return devicelib.set_state(device, state == "on")


@app.post("/api/devices/{device_id}/test")
async def api_test_device(device_id: str):
    """Ask the device what it is — confirms address, auth and driver in one go."""
    device = db.device(device_id)
    if not device:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        info = await run_in_threadpool(devicelib.identify, device)
        state = await run_in_threadpool(devicelib.get_state, device)
    except devicelib.DeviceError as exc:
        _remember(device_id, None, str(exc))
        return JSONResponse({"error": str(exc)}, status_code=502)
    _remember(device_id, state, None)
    return JSONResponse({"ok": True, "info": info, "state": state})


@app.api_route("/api/hook/devices/{device_id}/state", methods=["GET", "POST"])
async def api_hook_device(device_id: str, request: Request):
    """Token-authed device control, for a Shelly's own input button, a phone
    shortcut or anything else that can fetch a URL:
        /api/hook/devices/<id>/state?token=<t>&state=on|off|toggle
    """
    token = request.query_params.get("token") or request.headers.get("X-Sentry-Token")
    state = (request.query_params.get("state") or "toggle").lower()
    if not token or not secrets.compare_digest(str(token), _automation_token()):
        return JSONResponse({"error": "bad token"}, status_code=403)
    device = db.device(device_id)
    if not device or not device["enabled"]:
        return JSONResponse({"error": "device not found"}, status_code=404)
    if state not in ("on", "off", "toggle"):
        return JSONResponse({"error": "state must be on, off or toggle"}, status_code=400)
    try:
        result = await run_in_threadpool(_apply_device_state, device, state)
    except devicelib.DeviceError as exc:
        _remember(device_id, None, str(exc))
        return JSONResponse({"error": str(exc)}, status_code=502)
    _remember(device_id, result, None)
    return JSONResponse({"ok": True, "state": result})


# ---------------------------------------------------------------------------
# Tasks — a shared household to-do list.
#
# Lists are the *thing* the work belongs to (the house, the boat, the car),
# not a workflow stage: for a household, "which thing is this about" sorts the
# work usefully, whereas To-do/Doing/Done mostly creates a column nobody moves
# cards out of.
#
# Everyone signed in can see and edit everything, like the shared calendar. A
# family to-do list where you cannot tick off a job somebody else wrote down,
# or add one for them, is not a household feature.
# ---------------------------------------------------------------------------

DEFAULT_TASK_LISTS = (("House", "#2563eb"), ("Boat", "#0891b2"), ("Car", "#7c3aed"))


def _ensure_task_lists() -> None:
    """Seed the obvious categories on first use, so the board is never empty."""
    if db.task_lists():
        return
    for index, (name, color) in enumerate(DEFAULT_TASK_LISTS):
        db.add_task_list(name, color=color, sort_order=index)


def _task_dict(row: Any, names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "list_id": row["list_id"],
        "title": row["title"],
        "notes": row["notes"],
        "assignee_id": row["assignee_id"],
        "assignee": names.get(row["assignee_id"]),
        "due": row["due_utc"],
        "done": bool(row["done"]),
        "done_utc": row["done_utc"],
        "sort_order": row["sort_order"],
    }


def _task_list_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "color": row["color"],
            "sort_order": row["sort_order"]}


def _user_names() -> dict[int, str]:
    return {u["id"]: u["username"] for u in db.users()}


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    _ensure_task_lists()
    return render(request, "tasks.html")


@app.get("/api/tasks")
def api_tasks(request: Request):
    _ensure_task_lists()
    names = _user_names()
    me = auth.current_user(request)
    return JSONResponse({
        "lists": [_task_list_dict(l) for l in db.task_lists()],
        "tasks": [_task_dict(t, names) for t in db.tasks()],
        "users": [{"id": u["id"], "username": u["username"]} for u in db.users()],
        "me": me["id"] if me else None,
        "can_edit_lists": auth.is_admin(me),
    })


def _parse_due(value: Any) -> tuple[float | None, str | None]:
    """A due date is optional, and clearing it must be expressible."""
    if value in (None, ""):
        return None, None
    try:
        due = float(value)
    except (TypeError, ValueError):
        return None, "due must be a timestamp"
    if not math.isfinite(due):
        return None, "due must be a real time"
    return due, None


@app.post("/api/tasks")
async def api_add_task(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    title = (payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "A title is required."}, status_code=400)

    fields: dict[str, Any] = {"title": title}
    list_id = payload.get("list_id")
    if list_id not in (None, "", 0):
        try:
            list_id = int(list_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": "list_id must be a number"}, status_code=400)
        if not db.task_list(list_id):
            return JSONResponse({"error": "unknown list"}, status_code=404)
        fields["list_id"] = list_id

    assignee = payload.get("assignee_id")
    if assignee not in (None, "", 0):
        try:
            assignee = int(assignee)
        except (TypeError, ValueError):
            return JSONResponse({"error": "assignee_id must be a number"},
                                status_code=400)
        if not db.user_by_id(assignee):
            return JSONResponse({"error": "unknown user"}, status_code=404)
        fields["assignee_id"] = assignee

    due, error = _parse_due(payload.get("due"))
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if due is not None:
        fields["due_utc"] = due

    notes = (payload.get("notes") or "").strip()
    if notes:
        fields["notes"] = notes

    me = auth.current_user(request)
    if me:
        fields["created_by"] = me["id"]

    task_id = db.add_task(**fields)
    return JSONResponse(_task_dict(db.task(task_id), _user_names()))


@app.patch("/api/tasks/{task_id}")
async def api_update_task(task_id: int, request: Request):
    if not db.task(task_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    fields: dict[str, Any] = {}
    if "title" in payload:
        title = (payload.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "A title is required."}, status_code=400)
        fields["title"] = title
    if "notes" in payload:
        fields["notes"] = (payload.get("notes") or "").strip() or None
    if "list_id" in payload:
        list_id = payload["list_id"]
        if list_id in (None, "", 0):
            fields["list_id"] = None
        else:
            try:
                list_id = int(list_id)
            except (TypeError, ValueError):
                return JSONResponse({"error": "list_id must be a number"},
                                    status_code=400)
            if not db.task_list(list_id):
                return JSONResponse({"error": "unknown list"}, status_code=404)
            fields["list_id"] = list_id
    if "assignee_id" in payload:
        assignee = payload["assignee_id"]
        if assignee in (None, "", 0):
            fields["assignee_id"] = None
        else:
            try:
                assignee = int(assignee)
            except (TypeError, ValueError):
                return JSONResponse({"error": "assignee_id must be a number"},
                                    status_code=400)
            if not db.user_by_id(assignee):
                return JSONResponse({"error": "unknown user"}, status_code=404)
            fields["assignee_id"] = assignee
    if "due" in payload:
        due, error = _parse_due(payload["due"])
        if error:
            return JSONResponse({"error": error}, status_code=400)
        fields["due_utc"] = due
    if "done" in payload:
        done = bool(payload["done"])
        fields["done"] = 1 if done else 0
        fields["done_utc"] = time.time() if done else None

    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_task(task_id, **fields)
    return JSONResponse(_task_dict(db.task(task_id), _user_names()))


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: int, request: Request):
    if not db.task(task_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_task(task_id)
    return JSONResponse({"ok": True})


@app.post("/api/tasks/order")
async def api_task_order(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    order = payload.get("order")
    if not isinstance(order, list):
        return JSONResponse({"error": "order must be a list"}, status_code=400)
    try:
        db.set_task_order([int(t) for t in order])
    except (TypeError, ValueError):
        return JSONResponse({"error": "order must be task ids"}, status_code=400)
    return JSONResponse({"ok": True})


# --- task lists (admin manages the categories) -----------------------------

@app.post("/api/tasks/lists")
async def api_add_task_list(request: Request):
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "A name is required."}, status_code=400)
    color = (payload.get("color") or "").strip() or "#2563eb"
    list_id = db.add_task_list(name, color=color)
    return JSONResponse(_task_list_dict(db.task_list(list_id)))


@app.patch("/api/tasks/lists/{list_id}")
async def api_update_task_list(list_id: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.task_list(list_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields: dict[str, Any] = {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "A name is required."}, status_code=400)
        fields["name"] = name
    if "color" in payload:
        fields["color"] = (payload.get("color") or "").strip() or "#2563eb"
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_task_list(list_id, **fields)
    return JSONResponse(_task_list_dict(db.task_list(list_id)))


@app.delete("/api/tasks/lists/{list_id}")
def api_delete_task_list(list_id: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.task_list(list_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_task_list(list_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Blinds — rooms and motorised window coverings on a Connector/Motionblinds hub.
#
# Viewers may look and may operate; only admins may add hardware, rename things
# or change schedules. Opening a blind is closer to switching on a lamp than to
# reconfiguring the NVR, and a household where only one person can raise the
# shades is not a household feature. The admin gate is therefore applied per
# route below rather than by a blanket /api prefix rule.
# ---------------------------------------------------------------------------

LAYERS = ("sheer", "blackout")
LAYER_LABELS = {"sheer": "Light filtering", "blackout": "Blackout"}
COVERING_KINDS = ("shade", "blind", "curtain")

_HUB_SECRET_FIELDS = ("api_key", "token")


def _is_admin(request: Request) -> bool:
    return auth.is_admin(auth.current_user(request))


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": "forbidden"}, status_code=403)


def _room_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "sort_order": row["sort_order"]}


def _covering_dict(row: Any) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    data["enabled"] = bool(data.get("enabled"))
    data["bidirectional"] = bool(data.get("bidirectional"))
    data["layer_label"] = LAYER_LABELS.get(data.get("layer"), data.get("layer"))
    data["battery_percent"] = shadelib.battery_percent(data.get("battery_mv"))
    # Volts is the number to trust; percent is an estimate off a linear curve.
    mv = data.get("battery_mv")
    data["battery_volts"] = round(mv / 100, 2) if mv else None
    return data


def _hub_dict(row: Any) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    for field in _HUB_SECRET_FIELDS:
        data.pop(field, None)
    # Whether a key is set is not a secret, and the UI needs it to explain why
    # a write failed.
    data["has_key"] = bool(row["api_key"])
    data["enabled"] = bool(data.get("enabled"))
    return data


def _learn_mac(table: str, row_id: str, host: str, current: Any) -> None:
    """Record what MAC answered at this address, the first time we reach it.

    Cheap, and it is the whole basis of recovering from a lease change later:
    without it there is nothing stable to search for.
    """
    if current:
        return
    mac = netscan.mac_for(host)
    if not mac:
        return
    if table == "devices":
        db.update_device(row_id, mac=mac)
    elif table == "shade_hubs":
        db.update_shade_hub(row_id, mac=mac)
    log.info("learned %s for %s %s", mac, table, row_id)


def _rehome(table: str, row: Any, reachable: Any = None) -> str | None:
    """A stored address stopped answering: find where the device went.

    Only ever accepts an exact MAC match. Guessing — by hostname, or by "the
    only other thing answering that protocol" — would mean commanding somebody
    else's hardware, which is far worse than staying broken until a human looks.
    """
    mac = row["mac"] if "mac" in row.keys() else None
    if not mac:
        return None
    address, moved = netscan.resolve_moved(row["host"], mac, reachable)
    if not moved or not address:
        return None
    if table == "devices":
        db.update_device(row["id"], host=address)
    elif table == "shade_hubs":
        db.update_shade_hub(row["id"], host=address)
    return address


def _remember_covering(covering_id: str, summary: dict[str, Any] | None,
                       error: str | None) -> None:
    fields: dict[str, Any] = {"last_seen": time.time(), "last_error": error}
    if summary:
        if summary.get("position") is not None:
            fields["last_position"] = summary["position"]
        if summary.get("battery_mv") is not None:
            fields["battery_mv"] = summary["battery_mv"]
        if summary.get("rssi") is not None:
            fields["rssi"] = summary["rssi"]
        fields["bidirectional"] = 1 if summary.get("bidirectional") else 0
    db.update_covering(covering_id, **fields)


@app.get("/blinds", response_class=HTMLResponse)
def blinds_page(request: Request):
    return render(request, "blinds.html")


@app.get("/api/blinds")
def api_blinds(request: Request):
    """Everything the page needs in one round trip: rooms, coverings, hubs."""
    return JSONResponse({
        "rooms": [_room_dict(r) for r in db.rooms()],
        "coverings": [_covering_dict(c) for c in db.coverings()],
        "hubs": [_hub_dict(h) for h in db.shade_hubs()],
        "layers": [{"value": v, "label": LAYER_LABELS[v]} for v in LAYERS],
        "kinds": list(COVERING_KINDS),
        "schedules": [_covering_schedule_dict(s) for s in db.covering_schedules()],
        "can_edit": _is_admin(request),
    })


# --- rooms -----------------------------------------------------------------

@app.post("/api/blinds/rooms")
async def api_add_room(request: Request):
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "A room name is required."}, status_code=400)
    room_id = db.add_room(name)
    return JSONResponse(_room_dict(db.room(room_id)))


@app.patch("/api/blinds/rooms/{room_id}")
async def api_update_room(room_id: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.room(room_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "A room name is required."}, status_code=400)
    db.update_room(room_id, name=name)
    return JSONResponse(_room_dict(db.room(room_id)))


@app.delete("/api/blinds/rooms/{room_id}")
def api_delete_room(room_id: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.room(room_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_room(room_id)
    return JSONResponse({"ok": True})


@app.post("/api/blinds/rooms/order")
async def api_room_order(request: Request):
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    order = payload.get("order")
    if not isinstance(order, list):
        return JSONResponse({"error": "order must be a list"}, status_code=400)
    try:
        db.set_room_order([int(r) for r in order])
    except (TypeError, ValueError):
        return JSONResponse({"error": "order must be room ids"}, status_code=400)
    return JSONResponse({"ok": True})


# --- hubs ------------------------------------------------------------------

@app.post("/api/blinds/hubs/discover")
async def api_discover_hubs(request: Request):
    """Find bridges on the LAN. Read-only — this cannot move anything."""
    if not _is_admin(request):
        return _forbidden()
    try:
        found = await run_in_threadpool(shadelib.discover)
    except shadelib.ShadeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    known = {h["id"] for h in db.shade_hubs()}
    return JSONResponse({
        "hubs": [{**h, "known": h["mac"] in known} for h in found]
    })


@app.post("/api/blinds/hubs")
async def api_add_hub(request: Request):
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    host = (payload.get("host") or "").strip()
    if not host:
        return JSONResponse({"error": "A hub address is required."}, status_code=400)
    try:
        info = await run_in_threadpool(shadelib.device_list, host)
    except shadelib.ShadeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    mac = info.get("mac")
    if not mac:
        return JSONResponse({"error": "that address did not answer as a hub"},
                            status_code=502)
    name = (payload.get("name") or "").strip() or "Shade hub"
    api_key = (payload.get("api_key") or "").strip() or None
    if db.shade_hub(mac):
        db.update_shade_hub(mac, host=host, token=info.get("token"),
                            protocol=info.get("protocol"), last_seen=time.time(),
                            last_error=None,
                            **({"api_key": api_key} if api_key else {}))
    else:
        db.add_shade_hub(id=mac, name=name, host=host, api_key=api_key,
                         token=info.get("token"), protocol=info.get("protocol"),
                         last_seen=time.time())
    added = _sync_hub_coverings(mac, info)
    await run_in_threadpool(_learn_mac, "shade_hubs", mac, host,
                            db.shade_hub(mac)["mac"])
    return JSONResponse({"hub": _hub_dict(db.shade_hub(mac)), "added": added})


def _sync_hub_coverings(hub_id: str, info: dict[str, Any]) -> list[str]:
    """Create rows for motors we have not seen before. Never removes: a motor
    that is out of radio range briefly vanishes from the hub's list, and losing
    its name and room assignment over that would be maddening."""
    added: list[str] = []
    existing = {c["id"] for c in db.coverings(hub_id=hub_id)}
    for index, device in enumerate(info.get("devices") or []):
        mac = device.get("mac")
        if not mac or mac in existing:
            continue
        db.add_covering(
            id=mac, hub_id=hub_id, name=f"Covering {index + 1}",
            device_type=device.get("deviceType") or "10000000",
        )
        added.append(mac)
    return added


@app.post("/api/blinds/hubs/{hub_id}/refresh")
async def api_refresh_hub(hub_id: str, request: Request):
    """Re-enumerate a hub and poll every motor behind it."""
    if not _is_admin(request):
        return _forbidden()
    hub = db.shade_hub(hub_id)
    if not hub:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        info = await run_in_threadpool(shadelib.device_list, hub["host"])
    except shadelib.ShadeError as exc:
        # It may simply have moved. Re-find it by MAC and try once more before
        # calling it dead — a DHCP lease change should not need a human.
        moved = await run_in_threadpool(_rehome, "shade_hubs", hub)
        if moved is None:
            db.update_shade_hub(hub_id, last_error=str(exc), last_seen=time.time())
            return JSONResponse({"error": str(exc)}, status_code=502)
        try:
            info = await run_in_threadpool(shadelib.device_list, moved)
        except shadelib.ShadeError as exc2:
            db.update_shade_hub(hub_id, last_error=str(exc2), last_seen=time.time())
            return JSONResponse({"error": str(exc2)}, status_code=502)
        hub = db.shade_hub(hub_id)
    db.update_shade_hub(hub_id, token=info.get("token"),
                        protocol=info.get("protocol"),
                        last_seen=time.time(), last_error=None)
    added = _sync_hub_coverings(hub_id, info)
    polled = await run_in_threadpool(_poll_hub_coverings, hub_id, hub["host"])
    return JSONResponse({"added": added, "polled": polled})


def _poll_hub_coverings(hub_id: str, host: str) -> int:
    """Read every covering on a hub. One unreachable motor must not stop the
    rest, so failures are recorded per covering and the loop continues."""
    polled = 0
    for covering in db.coverings(hub_id=hub_id, enabled_only=True):
        try:
            data = shadelib.read_device(host, covering["id"], covering["device_type"])
        except shadelib.ShadeError as exc:
            _remember_covering(covering["id"], None, str(exc))
            continue
        _remember_covering(covering["id"], shadelib.summarise(data), None)
        polled += 1
    return polled


@app.patch("/api/blinds/hubs/{hub_id}")
async def api_update_hub(hub_id: str, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.shade_hub(hub_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields: dict[str, Any] = {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "A name is required."}, status_code=400)
        fields["name"] = name
    if "host" in payload:
        host = (payload.get("host") or "").strip()
        if not host:
            return JSONResponse({"error": "An address is required."}, status_code=400)
        fields["host"] = host
    if "api_key" in payload:
        key = (payload.get("api_key") or "").strip()
        if key and len(key) != 16:
            return JSONResponse(
                {"error": "The key must be exactly 16 characters — keep the "
                          "dashes, e.g. 12ab345c-d67e-8f"},
                status_code=400,
            )
        fields["api_key"] = key or None
    if "enabled" in payload:
        fields["enabled"] = 1 if payload["enabled"] else 0
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_shade_hub(hub_id, **fields)
    return JSONResponse(_hub_dict(db.shade_hub(hub_id)))


@app.delete("/api/blinds/hubs/{hub_id}")
def api_delete_hub(hub_id: str, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.shade_hub(hub_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_shade_hub(hub_id)
    return JSONResponse({"ok": True})


# --- coverings -------------------------------------------------------------

@app.patch("/api/blinds/coverings/{covering_id}")
async def api_update_covering(covering_id: str, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.covering(covering_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields: dict[str, Any] = {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "A name is required."}, status_code=400)
        fields["name"] = name
    if "layer" in payload:
        if payload["layer"] not in LAYERS:
            return JSONResponse(
                {"error": f"layer must be one of {', '.join(LAYERS)}"},
                status_code=400,
            )
        fields["layer"] = payload["layer"]
    if "kind" in payload:
        if payload["kind"] not in COVERING_KINDS:
            return JSONResponse({"error": "unknown kind"}, status_code=400)
        fields["kind"] = payload["kind"]
    if "room_id" in payload:
        room_id = payload["room_id"]
        if room_id in (None, "", 0):
            fields["room_id"] = None
        else:
            try:
                room_id = int(room_id)
            except (TypeError, ValueError):
                return JSONResponse({"error": "room_id must be a number"},
                                    status_code=400)
            if not db.room(room_id):
                return JSONResponse({"error": "unknown room"}, status_code=404)
            fields["room_id"] = room_id
    if "enabled" in payload:
        fields["enabled"] = 1 if payload["enabled"] else 0
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_covering(covering_id, **fields)
    return JSONResponse(_covering_dict(db.covering(covering_id)))


@app.delete("/api/blinds/coverings/{covering_id}")
def api_delete_covering(covering_id: str, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.covering(covering_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_covering(covering_id)
    return JSONResponse({"ok": True})


@app.post("/api/blinds/coverings/order")
async def api_covering_order(request: Request):
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    order = payload.get("order")
    if not isinstance(order, list):
        return JSONResponse({"error": "order must be a list"}, status_code=400)
    db.set_covering_order([str(c) for c in order])
    return JSONResponse({"ok": True})


def _parse_command(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Read a move command. Either an action, or a position 0-100.

    0 is fully open and 100 fully closed, matching the protocol rather than
    intuition — the UI does the flipping so the wire format always agrees with
    the vendor documentation.
    """
    if "position" in payload:
        try:
            position = int(payload["position"])
        except (TypeError, ValueError):
            return None, "position must be a number"
        if not 0 <= position <= 100:
            return None, "position must be 0-100"
        return {"position": position}, None
    action = (payload.get("action") or "").lower()
    if action in ("open", "close", "stop"):
        return {"action": action}, None
    return None, "send an action of open, close or stop, or a position 0-100"


def _run_command(covering: Any, hub: Any, command: dict[str, Any]) -> None:
    key, token = hub["api_key"], hub["token"]
    if "position" in command:
        shadelib.set_position(
            hub["host"], covering["id"], covering["device_type"],
            command["position"], api_key=key, hub_token=token,
        )
    else:
        shadelib.operate(
            hub["host"], covering["id"], covering["device_type"],
            command["action"], api_key=key, hub_token=token,
        )


@app.post("/api/blinds/coverings/{covering_id}/command")
async def api_command_covering(covering_id: str, request: Request):
    """Move one covering. Any signed-in user may do this."""
    covering = db.covering(covering_id)
    if not covering or not covering["enabled"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    hub = db.shade_hub(covering["hub_id"])
    if not hub or not hub["enabled"]:
        return JSONResponse({"error": "its hub is unavailable"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    command, error = _parse_command(payload)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    try:
        await run_in_threadpool(_run_command, covering, hub, command)
    except shadelib.ShadeError as exc:
        _remember_covering(covering_id, None, str(exc))
        return JSONResponse({"error": str(exc)}, status_code=502)
    # The motor takes several seconds to travel, so the position read back now
    # would be the old one. Record the target and let the next poll correct it.
    fields: dict[str, Any] = {"last_seen": time.time(), "last_error": None}
    if "position" in command:
        fields["last_position"] = command["position"]
    elif command["action"] in ("open", "close"):
        fields["last_position"] = 0 if command["action"] == "open" else 100
    db.update_covering(covering_id, **fields)
    return JSONResponse({"ok": True, **command})


@app.post("/api/blinds/group/command")
async def api_command_group(request: Request):
    """Move a group: "close the blackouts in the bedroom", "open everything".

    Body: {"room_id": int|null, "layer": "sheer"|"blackout"|null,
           "action": ... | "position": ...}
    A null room means every room; a null layer means both layers.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    command, error = _parse_command(payload)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    layer = payload.get("layer")
    if layer not in (None, "", *LAYERS):
        return JSONResponse({"error": "unknown layer"}, status_code=400)
    room_id = payload.get("room_id")
    if room_id in (None, "", 0):
        room_id = None
    else:
        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": "room_id must be a number"}, status_code=400)
        if not db.room(room_id):
            return JSONResponse({"error": "unknown room"}, status_code=404)

    targets = _select_coverings(room_id, layer or None)
    if not targets:
        return JSONResponse({"error": "nothing matches that group"}, status_code=404)
    result = await run_in_threadpool(_run_group, targets, command)
    return JSONResponse({"ok": True, **command, **result})


def _select_coverings(room_id: int | None, layer: str | None) -> list[Any]:
    rows = db.coverings(room_id=room_id, enabled_only=True) if room_id is not None \
        else db.coverings(enabled_only=True)
    if layer:
        rows = [r for r in rows if r["layer"] == layer]
    return rows


def _run_group(targets: list[Any], command: dict[str, Any]) -> dict[str, Any]:
    """Best effort across the group: one dead motor must not stop the others."""
    moved, failed = [], []
    hubs: dict[str, Any] = {}
    for covering in targets:
        hub = hubs.get(covering["hub_id"])
        if hub is None:
            hub = db.shade_hub(covering["hub_id"])
            if hub is None:
                continue
            hubs[covering["hub_id"]] = hub
        if not hub["enabled"]:
            continue
        try:
            _run_command(covering, hub, command)
        except shadelib.ShadeError as exc:
            _remember_covering(covering["id"], None, str(exc))
            failed.append({"id": covering["id"], "name": covering["name"],
                           "error": str(exc)})
            continue
        fields: dict[str, Any] = {"last_seen": time.time(), "last_error": None}
        if "position" in command:
            fields["last_position"] = command["position"]
        elif command["action"] in ("open", "close"):
            fields["last_position"] = 0 if command["action"] == "open" else 100
        db.update_covering(covering["id"], **fields)
        moved.append(covering["id"])
    return {"moved": moved, "failed": failed}


@app.post("/api/blinds/coverings/{covering_id}/poll")
async def api_poll_covering(covering_id: str, request: Request):
    """Read live state for one covering."""
    covering = db.covering(covering_id)
    if not covering:
        return JSONResponse({"error": "not found"}, status_code=404)
    hub = db.shade_hub(covering["hub_id"])
    if not hub:
        return JSONResponse({"error": "its hub is unavailable"}, status_code=404)
    try:
        data = await run_in_threadpool(
            shadelib.read_device, hub["host"], covering_id, covering["device_type"]
        )
    except shadelib.ShadeError as exc:
        _remember_covering(covering_id, None, str(exc))
        return JSONResponse({"error": str(exc)}, status_code=502)
    summary = shadelib.summarise(data)
    _remember_covering(covering_id, summary, None)
    return JSONResponse({"ok": True, **summary,
                         "covering": _covering_dict(db.covering(covering_id))})


# --- covering schedules ----------------------------------------------------

@app.get("/api/blinds/schedules")
def api_covering_schedules(request: Request):
    return JSONResponse([_covering_schedule_dict(s) for s in db.covering_schedules()])


# A covering rule fires once, when its window opens, so the window is a
# catch-up grace period rather than a duration. Ten minutes wide: a restart
# just after the moment still applies the rule, but a shade never moves an
# hour late because the box was off.
COVER_GRACE_MIN = 10


def _covering_schedule_dict(row: Any) -> dict[str, Any]:
    try:
        position = int(row["value"])
    except (TypeError, ValueError):
        position = None
    return {
        "id": row["id"],
        "covering_id": row["covering_id"],
        "room_id": row["covering_room_id"],
        "layer": row["covering_layer"],
        "days": row["days"],
        # Minutes past midnight. `at` is the contract; start/end are how the
        # shared scheduler stores it.
        "at": row["start_min"],
        "position": position,
        "enabled": bool(row["enabled"]),
    }


def _parse_cover_rule(payload: dict[str, Any]) -> tuple[tuple[int, int, int] | None, str | None]:
    """Read a covering rule's day mask and time-of-day.

    Unlike a recording window there is no end: the rule is a moment, not a
    span. Asking for an end time would be asking the user to invent a number
    that changes nothing.
    """
    try:
        days = int(payload.get("days"))
        at = int(payload.get("at"))
    except (TypeError, ValueError):
        return None, "days and time must be numbers"
    if not (0 <= days <= 127):
        return None, "days must be a 0..127 bitmask"
    if days == 0:
        return None, "select at least one day"
    if not 0 <= at <= 1439:
        return None, "time must be within 0..1439 minutes past midnight"
    return (days, at, (at + COVER_GRACE_MIN) % 1440), None


@app.post("/api/blinds/schedules")
async def api_add_covering_schedule(request: Request):
    """A covering schedule moves its targets to a position when the window opens.

    Deliberately a one-shot on the rising edge, not an authoritative hold: a
    shade you raised by hand at noon should stay up, not be dragged back every
    thirty seconds until the window closes. "Open at 07:00, close at sunset" is
    two rules, which is also how a person describes it.
    """
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    window, error = _parse_cover_rule(payload)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    days, start_min, end_min = window
    try:
        position = int(payload.get("position"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "position must be a number"}, status_code=400)
    if not 0 <= position <= 100:
        return JSONResponse({"error": "position must be 0-100"}, status_code=400)

    covering_id = payload.get("covering_id") or None
    room_id = payload.get("room_id")
    layer = payload.get("layer") or None
    if covering_id:
        if not db.covering(covering_id):
            return JSONResponse({"error": "unknown covering"}, status_code=404)
        room_id, layer = None, None
    else:
        if room_id in (None, "", 0):
            room_id = None
        else:
            try:
                room_id = int(room_id)
            except (TypeError, ValueError):
                return JSONResponse({"error": "room_id must be a number"},
                                    status_code=400)
            if not db.room(room_id):
                return JSONResponse({"error": "unknown room"}, status_code=404)
        if layer is not None and layer not in LAYERS:
            return JSONResponse({"error": "unknown layer"}, status_code=400)

    sid = db.add_schedule(
        covering_id=covering_id, covering_room_id=room_id, covering_layer=layer,
        action="cover", days=days, start_min=start_min, end_min=end_min,
        value=str(position),
    )
    return JSONResponse(_covering_schedule_dict(
        db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    ))


@app.patch("/api/blinds/schedules/{sid}")
async def api_update_covering_schedule(sid: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not row or row["action"] != "cover":
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if "enabled" not in payload:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.set_schedule_enabled(sid, bool(payload["enabled"]))
    return JSONResponse({"ok": True})


@app.delete("/api/blinds/schedules/{sid}")
def api_delete_covering_schedule(sid: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    row = db.one("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not row or row["action"] != "cover":
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_schedule(sid)
    return JSONResponse({"ok": True})


@app.api_route("/api/hook/blinds/group", methods=["GET", "POST"])
async def api_hook_blinds(request: Request):
    """Token-authed group control, for a scene switch or a phone shortcut:
        /api/hook/blinds/group?token=<t>&layer=blackout&room_id=3&position=100
    """
    token = request.query_params.get("token") or request.headers.get("X-Sentry-Token")
    if not token or not secrets.compare_digest(str(token), _automation_token()):
        return JSONResponse({"error": "bad token"}, status_code=403)
    params = dict(request.query_params)
    command, error = _parse_command(params)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    layer = params.get("layer") or None
    if layer is not None and layer not in LAYERS:
        return JSONResponse({"error": "unknown layer"}, status_code=400)
    room_id = params.get("room_id")
    if room_id in (None, "", "0"):
        room_id = None
    else:
        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": "room_id must be a number"}, status_code=400)
    targets = _select_coverings(room_id, layer)
    if not targets:
        return JSONResponse({"error": "nothing matches that group"}, status_code=404)
    result = await run_in_threadpool(_run_group, targets, command)
    return JSONResponse({"ok": True, **command, **result})


# ---------------------------------------------------------------------------
# Automations — the generic framework. An automation binds a trigger (an event
# Sentry raised, or nothing at all) to a list of actions, and every one of them
# gets a URL whether or not it has a trigger:
#
#     /api/hook/run/<slug>?token=<t>
#
# which is the "poke Sentry" endpoint: a Shelly's input button, a phone
# shortcut, a scene controller, a cron job on another box.
# ---------------------------------------------------------------------------

def _automation_dict(row: Any) -> dict[str, Any]:
    def loads(raw: str, fallback: Any) -> Any:
        try:
            return json.loads(raw or "")
        except ValueError:
            return fallback

    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "enabled": bool(row["enabled"]),
        "trigger_kind": row["trigger_kind"],
        "match": loads(row["match"], {}),
        "actions": loads(row["actions"], []),
        "cooldown_seconds": row["cooldown_seconds"],
        "days": row["days"],
        "start_min": row["start_min"],
        "end_min": row["end_min"],
        "last_run": row["last_run"],
        "last_error": row["last_error"],
        "run_count": row["run_count"],
        "url": f"/api/hook/run/{row['slug']}",
    }


def _unique_slug(name: str) -> str:
    base = slugify(name) or "automation"
    candidate, suffix = base, 2
    while db.automation_by_slug(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _automation_fields(payload: dict[str, Any], *, creating: bool
                       ) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a whole automation up front.

    Actions are checked when they are saved rather than when they fire: a typo
    that only surfaces at 2am when the driveway camera sees somebody is a bad
    way to learn the automation never worked.
    """
    fields: dict[str, Any] = {}
    if creating or "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return None, "A name is required."
        fields["name"] = name
    if creating or "trigger_kind" in payload:
        kind = payload.get("trigger_kind") or "hook"
        if kind not in automationlib.TRIGGER_KINDS:
            return None, f"trigger must be one of {', '.join(automationlib.TRIGGER_KINDS)}"
        fields["trigger_kind"] = kind
    if creating or "actions" in payload:
        try:
            fields["actions"] = json.dumps(
                automationlib.validate_actions(payload.get("actions"))
            )
        except automationlib.AutomationError as exc:
            return None, str(exc)
    if "match" in payload:
        try:
            fields["match"] = json.dumps(automationlib.validate_match(payload["match"]))
        except automationlib.AutomationError as exc:
            return None, str(exc)
    if "cooldown_seconds" in payload:
        try:
            cooldown = int(payload["cooldown_seconds"])
        except (TypeError, ValueError):
            return None, "cooldown must be a number"
        if not 0 <= cooldown <= 86400:
            return None, "cooldown must be 0..86400 seconds"
        fields["cooldown_seconds"] = cooldown
    if "days" in payload:
        try:
            days = int(payload["days"])
        except (TypeError, ValueError):
            return None, "days must be a number"
        if not 0 <= days <= 127:
            return None, "days must be a 0..127 bitmask"
        fields["days"] = days
    for key in ("start_min", "end_min"):
        if key not in payload:
            continue
        value = payload[key]
        if value in (None, ""):
            fields[key] = None
            continue
        try:
            minute = int(value)
        except (TypeError, ValueError):
            return None, f"{key} must be a number"
        if not 0 <= minute <= 1439:
            return None, f"{key} must be within 0..1439"
        fields[key] = minute
    if "enabled" in payload:
        fields["enabled"] = 1 if payload["enabled"] else 0
    return fields, None


@app.get("/automations", response_class=HTMLResponse)
def automations_page(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "automations.html")


@app.get("/network", response_class=HTMLResponse)
def network_page(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "network.html")


def _known_macs() -> dict[str, dict[str, Any]]:
    """Everything Sentry already manages, keyed by MAC, so the inventory can
    separate "this is your driveway camera" from "no idea what this is"."""
    known: dict[str, dict[str, Any]] = {}
    for camera in db.cameras():
        mac = netscan.normalise_mac(camera["mac"] if "mac" in camera.keys() else None)
        if mac:
            known[mac] = {"kind": "camera", "name": camera["name"]}
    for device in db.devices():
        mac = netscan.normalise_mac(device["mac"] if "mac" in device.keys() else None)
        if mac:
            known[mac] = {"kind": "device", "name": device["name"]}
    for hub in db.shade_hubs():
        mac = netscan.normalise_mac(hub["mac"] if "mac" in hub.keys() else None)
        if mac:
            known[mac] = {"kind": "shade hub", "name": hub["name"]}
        # The Connector protocol reports the hub's own MAC as its id, which is
        # how it is identifiable before anyone has ever reached it by address.
        protocol_mac = netscan.normalise_mac(hub["id"])
        if protocol_mac:
            known.setdefault(protocol_mac, {"kind": "shade hub", "name": hub["name"]})
    return known


@app.post("/api/network/scan")
async def api_network_scan(request: Request):
    """Sweep the LAN and name what answers. Read-only, and the vendor lookup is
    offline — the house's MAC addresses are not sent anywhere."""
    if not _is_admin(request):
        return _forbidden()
    rows = await run_in_threadpool(netscan.inventory, _known_macs())
    return JSONResponse({
        "devices": rows,
        "network": str(netscan.local_network() or ""),
        "unknown": sum(1 for r in rows
                       if not r["known_kind"] and not r["randomised"]),
    })


@app.get("/api/automations")
def api_automations(request: Request):
    if not _is_admin(request):
        return _forbidden()
    return JSONResponse({
        "automations": [_automation_dict(a) for a in db.automations()],
        "action_kinds": list(automationlib.ACTION_KINDS),
        "trigger_kinds": list(automationlib.TRIGGER_KINDS),
        "devices": [{"id": d["id"], "name": d["name"]} for d in db.devices()],
        "rooms": [_room_dict(r) for r in db.rooms()],
        "layers": [{"value": v, "label": LAYER_LABELS[v]} for v in LAYERS],
        "cameras": [{"id": c["id"], "name": c["name"]}
                    for c in db.cameras() if not c["archived"]],
        "event_types": ["person", "vehicle", "animal", "motion", "flood"],
        "token": _automation_token(),
    })


@app.post("/api/automations")
async def api_add_automation(request: Request):
    if not _is_admin(request):
        return _forbidden()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields, error = _automation_fields(payload, creating=True)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    fields["slug"] = _unique_slug(fields["name"])
    automation_id = db.add_automation(**fields)
    return JSONResponse(_automation_dict(db.automation(automation_id)))


@app.patch("/api/automations/{automation_id}")
async def api_update_automation(automation_id: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.automation(automation_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields, error = _automation_fields(payload, creating=False)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not fields:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    db.update_automation(automation_id, **fields)
    return JSONResponse(_automation_dict(db.automation(automation_id)))


@app.delete("/api/automations/{automation_id}")
def api_delete_automation(automation_id: int, request: Request):
    if not _is_admin(request):
        return _forbidden()
    if not db.automation(automation_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_automation(automation_id)
    return JSONResponse({"ok": True})


@app.post("/api/automations/{automation_id}/run")
async def api_run_automation(automation_id: int, request: Request):
    """Run it now, from the UI. Reports what failed rather than pretending."""
    if not _is_admin(request):
        return _forbidden()
    if not db.automation(automation_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        result = await run_in_threadpool(automations.run_now, automation_id, {})
    except automationlib.AutomationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": not result["errors"], **result})


@app.api_route("/api/hook/run/{slug}", methods=["GET", "POST"])
async def api_hook_run(slug: str, request: Request):
    """The generic inbound trigger. Anything that can fetch a URL can drive
    Sentry through this: /api/hook/run/<slug>?token=<t>"""
    token = request.query_params.get("token") or request.headers.get("X-Sentry-Token")
    if not token or not secrets.compare_digest(str(token), _automation_token()):
        return JSONResponse({"error": "bad token"}, status_code=403)
    row = db.automation_by_slug(slug)
    if row is None or not row["enabled"]:
        return JSONResponse({"error": "automation not found"}, status_code=404)
    try:
        result = await run_in_threadpool(
            automations.run_now, row["id"], {"source": "hook"}
        )
    except automationlib.AutomationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": not result["errors"], **result})


@app.get("/api/automation/token")
def api_automation_token(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"token": _automation_token()})


@app.post("/api/automation/token")
async def api_automation_token_regen(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    tok = secrets.token_urlsafe(24)
    db.set_setting("automation.token", tok)
    return JSONResponse({"token": tok})


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

    # Reject an oversized upload from its declared length before reading a byte.
    # Advisory (a client can lie), which is why the streaming cap below is the
    # real enforcement — but it costs nothing and stops the honest case early.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_CLIP_BYTES:
        return JSONResponse({"error": "clip too large"}, status_code=413)

    # Never persist the client's Content-Type: it's echoed back on download, so
    # an uploaded text/html body would render as a page on this origin (stored
    # XSS with the viewer's own session). Derive it from a fixed whitelist.
    is_webm = "webm" in (file.content_type or "").lower()
    ext = ".webm" if is_webm else ".mp4"
    mime = "video/webm" if is_webm else "video/mp4"
    # The random suffix is not decoration. A second-resolution name collides
    # whenever two clips are saved from the same camera inside one second: the
    # second silently overwrote the first, leaving the first clip's database row
    # pointing at the wrong video. It also made the cleanup below destructive —
    # a rejected upload would delete the good clip it happened to collide with.
    fname = f"{slugify(camera_id)}-{int(time.time())}-{secrets.token_hex(4)}{ext}"
    dest = cfg.storage.clips_dir / fname

    # Streamed to disk in chunks and capped as it goes. Reading the whole body
    # first and checking its length afterwards means a 2 GB post is fully
    # materialised in memory before being refused — on a 4-core box with the
    # recorders running, that is enough to matter.
    size = 0
    too_large = False
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(CLIP_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CLIP_BYTES:
                    too_large = True
                    break
                out.write(chunk)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        log.warning("clip upload failed: %s", exc)
        return JSONResponse({"error": "could not save clip"}, status_code=500)

    # A partial file left behind is a disk leak nobody would ever go looking
    # for, so every rejection path cleans up after itself.
    if too_large:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": "clip too large"}, status_code=413)
    if size == 0:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": "empty clip"}, status_code=400)

    vid = int(vcam_id) if vcam_id.strip().isdigit() else None
    clip_id = db.add_clip(
        camera_id=camera_id, name=(name.strip() or "Clip"), path=str(dest),
        mime=mime, size=size,
        vcam_id=vid, start_ts=start or None, duration=duration or None,
    )
    return JSONResponse({"id": clip_id, "redirect": "/clips"})


@app.post("/api/cameras/{camera_id}/save-clip")
def api_save_clip_server(request: Request, camera_id: str,
                         start: float, duration: float, name: str = ""):
    """Export a time range from the server recording and save it to the clips
    library. This is the reliable path for a normal camera — an exact ffmpeg cut
    of the footage, not a real-time browser re-record (which dropped frames,
    ran short, and desynced audio). Dewarped virtual cameras still capture in the
    browser via POST /api/clips, since the dewarp only exists there.
    """
    import shutil

    camera = db.camera(camera_id)
    if not camera or not can_view(request, camera):
        return JSONResponse({"error": "not found"}, status_code=404)
    duration = max(1.0, min(duration, 7200.0))
    try:
        tmp = playback.export_clip(db, cfg, camera_id, start, duration)
    except FileNotFoundError:
        return JSONResponse({"error": "no footage for that time"}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": f"export failed: {exc}"}, status_code=500)

    dest = cfg.storage.clips_dir / f"{slugify(camera_id)}-{int(time.time())}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(dest))
    shutil.rmtree(tmp.parent, ignore_errors=True)

    clip_id = db.add_clip(
        camera_id=camera_id, name=(name.strip() or "Clip"), path=str(dest),
        mime="video/mp4", size=dest.stat().st_size,
        start_ts=start, duration=duration,
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
    # Defence in depth for rows written before the mime whitelist above: serve
    # only a known video type, and tell the browser never to sniff past it.
    mime = clip["mime"] if clip["mime"] in ("video/mp4", "video/webm") else "video/mp4"
    return FileResponse(
        path, media_type=mime, headers={"X-Content-Type-Options": "nosniff"}
    )


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
    # Every allowlisted go2rtc endpoint is addressed by `src`, so demand it up
    # front: without this, omitting the parameter skipped the access check
    # entirely, which is how api/streams leaked camera credentials to viewers.
    src = request.query_params.get("src")
    if not src:
        return Response("src required", status_code=400)
    if not auth.is_admin(auth.current_user(request)):
        camera_id = src[:-4] if src.endswith("_sub") else src
        camera = db.camera(camera_id)
        if not camera or not can_view(request, camera):
            return Response("forbidden", status_code=403)
    return await proxy.forward(request, path, cfg)

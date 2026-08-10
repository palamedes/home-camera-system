"""Authentication.

Session tokens are random and stored server-side rather than signed-and-stateless,
so logging out or revoking a session actually takes effect immediately. Passwords
use scrypt from the standard library — no external crypto dependency to keep
patched.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

COOKIE_NAME = "nvr_session"

# scrypt parameters. n=2**15 costs ~100ms and ~32MB per hash on this box, which
# is a sane brute-force cost for a login that happens rarely.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_DKLEN, maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookie(response: Response, token: str, *, days: int, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=days * 86400,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request) -> Any | None:
    """Resolve the logged-in user, or None. Populated by AuthMiddleware."""
    return getattr(request.state, "user", None)


def is_admin(user: Any | None) -> bool:
    """Whether a user row carries the admin role.

    A missing role reads as admin: pre-role accounts were the sole operator,
    and the migration defaults them to admin.
    """
    if user is None:
        return False
    try:
        return (user["role"] or "admin") == "admin"
    except (KeyError, IndexError, TypeError):
        return True


class AuthMiddleware:
    """Gates every route behind a session.

    Deliberately default-deny: new routes are protected unless their path is
    explicitly listed as public. The alternative — opting each route in — is how
    admin endpoints end up accidentally exposed.
    """

    # /api/hook is token-authed (a shared secret), not session-authed, so it's
    # reachable without a login — that's how scene switches / Home Assistant
    # reach it. The endpoint itself enforces the token.
    PUBLIC_PREFIXES = ("/login", "/setup", "/static/", "/health", "/api/hook")

    def __init__(self, app, db, config):
        self.app = app
        self.db = db
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request.state.user = None

        token = request.cookies.get(COOKIE_NAME)
        if token:
            session = self.db.session(token)
            if session:
                request.state.user = self.db.user_by_id(session["user_id"])
        scope["state"]["user"] = request.state.user

        path = scope.get("path", "")
        needs_setup = self.db.user_count() == 0

        if needs_setup and not path.startswith(("/setup", "/static/", "/health")):
            await RedirectResponse("/setup", status_code=303)(scope, receive, send)
            return

        if request.state.user is None and not path.startswith(self.PUBLIC_PREFIXES):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            if path.startswith("/api/"):
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
            nxt = request.url.path
            await RedirectResponse(f"/login?next={nxt}", status_code=303)(
                scope, receive, send
            )
            return

        # Admin gate. Viewers may watch (GET) but not change anything: all
        # user management, and any mutating camera/discovery call, requires
        # admin. Enforced centrally so a viewer cannot reach a write endpoint
        # by calling the API directly, regardless of what the UI shows.
        if request.state.user is not None and not is_admin(request.state.user):
            method = scope.get("method", "GET")
            admin_only = (
                path.startswith("/api/users")
                or path.startswith("/api/discover")
                # Reading a virtual camera (to restore its saved view) is fine
                # for anyone who can see it; only changes need admin.
                or (path.startswith("/api/virtual") and method != "GET")
                or (path.startswith("/api/cameras") and method in ("POST", "PATCH", "DELETE"))
            )
            if admin_only:
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "forbidden"}, status_code=403)(
                    scope, receive, send
                )
                return

        await self.app(scope, receive, send)

"""Reverse proxy to go2rtc.

go2rtc binds to loopback only, so nothing on the LAN can pull video from it
directly. Every browser request for live video comes through here, which means
live streams inherit the same session check as the rest of the app.

Streaming responses (MJPEG in particular) never end, so responses are relayed
chunk-by-chunk rather than buffered.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

log = logging.getLogger("nvr.proxy")

# Hop-by-hop headers must not be forwarded (RFC 9110 §7.6.1).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}

# Only these go2rtc endpoints are reachable through the proxy. go2rtc's API can
# rewrite its own configuration and add arbitrary stream sources — exposing all
# of it to any logged-in browser would turn a session into remote command
# execution on this box.
# NB: "api/streams" is deliberately NOT here. go2rtc's stream dump embeds each
# camera's full RTSP URL — credentials and all — and it takes no `src`, so the
# per-camera access check in go2rtc_proxy (which keys off `src`) can't gate it.
# Any logged-in session, viewer included, could have read every camera password.
# Nothing in the UI calls it; it only ever reaches go2rtc's own dashboard.
ALLOWED_PATHS = {
    "api/webrtc",
    "api/frame.jpeg",
    "api/stream.mjpeg",
    "api/stream.m3u8",
    "api/stream.ts",
}


def is_allowed(path: str) -> bool:
    return path.lstrip("/") in ALLOWED_PATHS


async def forward(request: Request, path: str, config: Any) -> Response:
    """Relay one request to go2rtc and stream the response back."""
    if not is_allowed(path):
        return Response("not found", status_code=404)

    url = f"{config.go2rtc.api_base}/{path.lstrip('/')}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP and key.lower() != "host"
    }

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))

    try:
        upstream = await client.send(
            client.build_request(
                request.method, url, headers=headers, content=body,
                params=dict(request.query_params),
            ),
            stream=True,
        )
    except Exception as exc:
        await client.aclose()
        log.warning("go2rtc proxy failed for %s: %s", path, exc)
        return Response("stream backend unavailable", status_code=502)

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get("content-type"),
    )

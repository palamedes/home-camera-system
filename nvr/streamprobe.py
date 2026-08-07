"""Per-stream resolution / bitrate discovery for the settings UI.

The settings page wants to show, for each camera, the actual resolution of its
main and sub streams — and, where the camera supports it, the encoder options
it advertises so an admin can change them.

Two ways to learn a stream's resolution, cheapest first:

  * Reolink GetEnc — one authenticated HTTP round-trip returns the encoder's
    configured size and bitrate for every stream, and (with action=1) the list
    of resolutions/bitrates the camera will accept. No video is decoded.
  * ffprobe of go2rtc's loopback RTSP — for any other brand. go2rtc is already
    holding the camera connection, so this reads a few packets of an existing
    stream rather than opening a second one against the camera. Bounded by a
    short timeout; on any failure the stream is simply reported as unknown.

Nothing here raises: a probe that fails returns None so the label is omitted
rather than breaking the page.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


def parse_resolution(value: Any) -> tuple[int, int] | None:
    """Parse a "WxH", "W*H", or "W×H" string into (width, height).

    Reolink reports "2560*1920"; ffprobe and ONVIF use separate integer fields
    but callers sometimes hand us a combined string. Anything unparseable — or a
    zero dimension — yields None.
    """
    if value is None:
        return None
    text = str(value).lower().replace("*", "x").replace("×", "x").strip()
    if "x" not in text:
        return None
    left, _, right = text.partition("x")
    left, right = left.strip(), right.strip()
    if left.isdigit() and right.isdigit() and int(left) and int(right):
        return int(left), int(right)
    return None


def _parse_fps(raw: str | None) -> float | None:
    if not raw or "/" not in raw:
        return None
    num, _, den = raw.partition("/")
    try:
        d = int(den)
        return round(int(num) / d, 1) if d else None
    except (ValueError, ZeroDivisionError):
        return None


def ffprobe_stream(url: str, timeout: int = 6) -> dict[str, Any] | None:
    """Resolution/codec/bitrate of a single RTSP video stream, or None.

    Kept short: this runs synchronously inside a request handler, so a dead
    stream must not hang it. bit_rate is often absent for RTSP (the container
    carries no overall bitrate) — that's fine, the field is just omitted.
    """
    command = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,bit_rate,avg_frame_rate",
        "-of", "json",
        "-timeout", str(timeout * 1_000_000),
        url,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout + 4
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        stream = (json.loads(result.stdout).get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return None

    width, height = stream.get("width"), stream.get("height")
    if not width or not height:
        return None

    out: dict[str, Any] = {"w": int(width), "h": int(height)}
    if stream.get("codec_name"):
        out["codec"] = stream["codec_name"]
    raw_bitrate = stream.get("bit_rate")
    if raw_bitrate and str(raw_bitrate).isdigit() and int(raw_bitrate) > 0:
        # ffprobe reports bits/sec; the rest of the app talks kbps (as Reolink
        # does), so normalise here.
        out["bitrate"] = int(raw_bitrate) // 1000
    fps = _parse_fps(stream.get("avg_frame_rate"))
    if fps:
        out["fps"] = fps
    return out


def normalise_enc_range(block: Any) -> dict[str, Any] | None:
    """Flatten a Reolink GetEnc `range` block into {sizes, bitrates}.

    Firmware is inconsistent here: the per-stream range is sometimes a single
    dict, sometimes a list of dicts (one per resolution), and `size`/`bitRate`
    may each be a scalar or a list. We defensively collect every advertised
    resolution and bitrate we can find, de-duplicated and order-preserving, so
    the UI has something to offer even on an unfamiliar firmware. Returns None
    when nothing usable is present.
    """
    if not block:
        return None
    entries = block if isinstance(block, list) else [block]
    sizes: list[str] = []
    bitrates: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        size = entry.get("size")
        for value in (size if isinstance(size, list) else [size]):
            if value:
                sizes.append(str(value))
        bitrate = entry.get("bitRate")
        for value in (bitrate if isinstance(bitrate, list) else [bitrate]):
            if value is not None and str(value).isdigit():
                bitrates.append(int(value))
    sizes = list(dict.fromkeys(sizes))
    bitrates = sorted(set(bitrates))
    if not sizes and not bitrates:
        return None
    return {"sizes": sizes, "bitrates": bitrates}


def _reolink_streams(camera: dict[str, Any]) -> dict[str, Any] | None:
    """Resolutions + advertised encoder options via the Reolink native API.

    Returns None (so the caller falls back to ffprobe) if login fails, the model
    exposes no encoder, or the call raises for any reason.
    """
    from . import reolink

    host = camera.get("host")
    if not host:
        return None
    try:
        with reolink.ReolinkClient(
            host, camera.get("username") or "", camera.get("password") or "",
            timeout=6.0,
        ) as client:
            info = client.device_info()
            options = client.encoder_options()
            client.logout()
    except Exception:
        return None

    result: dict[str, Any] = {"main": None, "sub": None}
    for stream in info.streams:
        entry: dict[str, Any] = {}
        if stream.width and stream.height:
            entry["w"], entry["h"] = stream.width, stream.height
        if stream.codec:
            entry["codec"] = stream.codec
        if stream.bitrate:
            entry["bitrate"] = stream.bitrate
        if stream.fps:
            entry["fps"] = stream.fps
        if stream.name in ("main", "sub") and entry:
            result[stream.name] = entry

    if options:
        result["encoder"] = {"supported": True, "options": options}
    if not (result["main"] or result["sub"] or options):
        return None
    return result


def describe_streams(camera: dict[str, Any], cfg: Any) -> dict[str, Any]:
    """Best-effort {main, sub, encoder?} description of a camera's streams.

    Reolink first (cheap, and the only source of settable encoder options),
    ffprobe of the go2rtc loopback otherwise. Any individual failure degrades to
    a null entry rather than an error.
    """
    from . import streams as streams_mod

    out: dict[str, Any] = {"main": None, "sub": None, "encoder": None}

    if (camera.get("brand") or "").lower() == "reolink":
        reo = _reolink_streams(camera)
        if reo:
            out.update(reo)
            return out

    if camera.get("main_url"):
        out["main"] = ffprobe_stream(
            cfg.go2rtc.local_rtsp(streams_mod.main_stream_name(camera["id"]))
        )
    if camera.get("sub_url"):
        out["sub"] = ffprobe_stream(
            cfg.go2rtc.local_rtsp(streams_mod.sub_stream_name(camera["id"]))
        )
    return out


# --- two-way-audio (backchannel) capability -------------------------------

import hashlib as _hashlib
import re as _re
import socket as _socket


def backchannel_supported(url: str, timeout: float = 4.0) -> bool | None:
    """Whether an RTSP camera advertises a two-way-audio backchannel.

    Sends a DESCRIBE with the ONVIF backchannel Require header and looks for an
    audio media section the client can *send* to (a=sendonly) — the talk track.
    Reolink models split here: wired PoE cams (e.g. RLC-810WA) expose it; some
    others (e.g. the FE-P 360) only do two-way audio over Reolink's proprietary
    protocol, which go2rtc can't reach, so they return no such track.

    Returns True/False when the camera answers, or None if it couldn't be
    determined (unreachable / auth failure). Callers should treat None as
    "assume yes" so a transient blip never hides a working Talk button.
    """
    m = _re.match(r"rtsp://(?:([^:@/]+):([^@/]+)@)?([^:/]+)(?::(\d+))?(/.*)?$", url or "")
    if not m:
        return None
    user, pw, host, port, path = m.groups()
    full = f"rtsp://{host}:{int(port or 554)}{path or '/'}"
    hdr = "Require: www.onvif.org/ver20/backchannel\r\nAccept: application/sdp\r\n"
    try:
        sock = _socket.create_connection((host, int(port or 554)), timeout=timeout)
        sock.settimeout(timeout)

        def describe(cseq: int, extra: str = "") -> str:
            sock.send(f"DESCRIBE {full} RTSP/1.0\r\nCSeq: {cseq}\r\n{hdr}{extra}\r\n".encode())
            return sock.recv(8192).decode("utf-8", "replace")

        resp = describe(1)
        if " 401 " in resp.split("\r\n", 1)[0] and user:
            ch = _re.search(r'realm="([^"]+)".*?nonce="([^"]+)"', resp, _re.S)
            if ch:
                realm, nonce = ch.group(1), ch.group(2)
                ha1 = _hashlib.md5(f"{user}:{realm}:{pw}".encode()).hexdigest()
                ha2 = _hashlib.md5(f"DESCRIBE:{full}".encode()).hexdigest()
                rr = _hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                auth = (
                    f'Authorization: Digest username="{user}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{full}", response="{rr}"\r\n'
                )
                resp = describe(2, auth)
        sock.close()

        if " 200 " not in resp.split("\r\n", 1)[0]:
            return None
        sdp = resp.split("\r\n\r\n", 1)[-1]
        for section in _re.split(r"(?=^m=)", sdp, flags=_re.M):
            if section.startswith("m=audio") and _re.search(r"^a=sendonly", section, _re.M):
                return True
        return False
    except Exception:
        return None

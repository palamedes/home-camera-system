"""Camera reachability probe: endpoint parsing + cached TCP check.

No real network: _tcp_reachable is monkeypatched. Confirms a camera that isn't
streaming (nobody watching/recording) still reads as online when reachable.
"""

import sqlite3

from nvr import streams


def _row(**cols):
    """A stand-in sqlite3.Row-like mapping (supports [] and .keys())."""
    class R(dict):
        def keys(self):  # noqa: D401 - sqlite3.Row parity
            return list(super().keys())
    return R(cols)


def test_rtsp_endpoint_from_main_url():
    cam = _row(main_url="rtsp://admin:pw@192.168.1.53:554/h264", sub_url=None, host="x")
    assert streams._rtsp_endpoint(cam) == ("192.168.1.53", 554)


def test_rtsp_endpoint_defaults_port_554():
    cam = _row(main_url="rtsp://10.0.0.5/stream", sub_url=None, host="10.0.0.5")
    assert streams._rtsp_endpoint(cam) == ("10.0.0.5", 554)


def test_rtsp_endpoint_falls_back_to_sub_then_host():
    cam = _row(main_url=None, sub_url="rtsp://10.0.0.6:8554/sub", host="10.0.0.6")
    assert streams._rtsp_endpoint(cam) == ("10.0.0.6", 8554)
    cam2 = _row(main_url=None, sub_url=None, host="10.0.0.7")
    assert streams._rtsp_endpoint(cam2) == ("10.0.0.7", 554)


def test_camera_reachable_caches(monkeypatch):
    mgr = streams.Go2rtcManager(config=object(), db=object())
    calls = []
    monkeypatch.setattr(streams, "_tcp_reachable",
                        lambda h, p, timeout=1.0: calls.append((h, p)) or True)
    cam = _row(id="cam1", main_url="rtsp://1.2.3.4:554/x", sub_url=None, host="1.2.3.4")

    assert mgr.camera_reachable(cam) is True
    assert mgr.camera_reachable(cam) is True  # served from cache
    assert len(calls) == 1  # probed only once within the TTL


def test_camera_reachable_false_when_unreachable(monkeypatch):
    mgr = streams.Go2rtcManager(config=object(), db=object())
    monkeypatch.setattr(streams, "_tcp_reachable", lambda h, p, timeout=1.0: False)
    cam = _row(id="down", main_url="rtsp://9.9.9.9:554/x", sub_url=None, host="9.9.9.9")
    assert mgr.camera_reachable(cam) is False

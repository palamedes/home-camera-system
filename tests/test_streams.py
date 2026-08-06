"""go2rtc source generation: H.265 mains get an on-demand H.264 transcode for
WebRTC (which cannot carry HEVC), while H.264 streams stay passthrough."""

from __future__ import annotations

from nvr import streams
from nvr.streams import Go2rtcManager, _is_hevc_url


def test_is_hevc_url():
    assert _is_hevc_url("rtsp://x/h265Preview_01_main")
    assert _is_hevc_url("rtsp://x/hevcPreview_01")
    assert _is_hevc_url("RTSP://X/H265PREVIEW")          # case-insensitive
    assert not _is_hevc_url("rtsp://x/h264Preview_01_sub")
    assert not _is_hevc_url("")
    assert not _is_hevc_url(None)


def test_sources_h264_is_passthrough_plus_opus(app_module):
    mgr = Go2rtcManager(app_module.cfg, None)
    url = "rtsp://x/h264Preview_01_main"
    assert mgr._sources("cam", url) == [url, "ffmpeg:cam#audio=opus"]


def test_sources_hevc_adds_video_transcode(app_module):
    cfg = app_module.cfg
    cfg.playback.qsv_device = "/dev/dri/renderD128"
    mgr = Go2rtcManager(cfg, None)
    url = "rtsp://x/h265Preview_01_main"
    srcs = mgr._sources("cam", url)
    assert srcs[0] == url                                # raw HEVC kept for passthrough
    assert srcs[1] == "ffmpeg:cam#video=h264#audio=opus#hardware=qsv"


def test_sources_hevc_without_qsv_uses_software(app_module):
    cfg = app_module.cfg
    orig = cfg.playback.qsv_device
    cfg.playback.qsv_device = None
    try:
        mgr = Go2rtcManager(cfg, None)
        srcs = mgr._sources("cam", "rtsp://x/hevcPreview_01_main")
        assert srcs[1] == "ffmpeg:cam#video=h264#audio=opus"   # no #hardware
    finally:
        cfg.playback.qsv_device = orig


def test_build_config_transcodes_only_hevc_main(app_module, db):
    """A camera with an HEVC main + H.264 sub: only the main gets the video
    transcode; the sub stays passthrough."""
    from conftest import add_camera
    cfg = app_module.cfg
    cfg.playback.qsv_device = "/dev/dri/renderD128"
    add_camera(db, "drive",
               main_url="rtsp://x/h265Preview_01_main",
               sub_url="rtsp://x/h264Preview_01_sub")
    mgr = Go2rtcManager(cfg, db)
    conf = mgr.build_config()["streams"]
    assert conf["drive"][1] == "ffmpeg:drive#video=h264#audio=opus#hardware=qsv"
    assert conf["drive_sub"][1] == "ffmpeg:drive_sub#audio=opus"

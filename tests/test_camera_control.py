"""camera_control: public value mapping and brand-gating.

No camera network is touched here — only the pure mapping helpers and the
"unsupported brand" behaviour, which must never raise on a read and must raise
CameraControlError on a write.
"""

import pytest

from nvr import camera_control as cc


def test_mode_to_reolink():
    assert cc._mode_to_reolink("auto") == "Auto"
    assert cc._mode_to_reolink("color") == "Color"
    assert cc._mode_to_reolink("bw") == "Black&White"
    with pytest.raises(cc.CameraControlError):
        cc._mode_to_reolink("sepia")


def test_mode_from_reolink():
    assert cc._mode_from_reolink("Auto") == "auto"
    assert cc._mode_from_reolink("Color") == "color"
    assert cc._mode_from_reolink("Black&White") == "bw"
    assert cc._mode_from_reolink("BlackWhite") == "bw"
    assert cc._mode_from_reolink("nonsense") is None
    assert cc._mode_from_reolink(None) is None


def test_ir_to_reolink():
    assert cc._ir_to_reolink("auto") == "Auto"
    assert cc._ir_to_reolink("on") == "On"
    assert cc._ir_to_reolink("off") == "Off"
    with pytest.raises(cc.CameraControlError):
        cc._ir_to_reolink("blink")


def test_ir_from_reolink():
    assert cc._ir_from_reolink("Auto") == "auto"
    assert cc._ir_from_reolink("On") == "on"
    assert cc._ir_from_reolink("Off") == "off"
    assert cc._ir_from_reolink("Close") == "off"   # some firmware uses "Close"
    assert cc._ir_from_reolink(None) is None


def test_non_reolink_reads_none():
    cam = {"brand": "hikvision", "host": "10.0.0.5"}
    assert cc.get_controls(cam) == {"light": None, "night_vision": None}


def test_non_reolink_writes_raise():
    cam = {"brand": "hikvision", "host": "10.0.0.5"}
    with pytest.raises(cc.CameraControlError):
        cc.set_light(cam, True)
    with pytest.raises(cc.CameraControlError):
        cc.set_night_vision(cam, mode="color")


def test_set_night_vision_requires_an_argument():
    cam = {"brand": "reolink", "host": "10.0.0.5", "username": "a", "password": "b"}
    with pytest.raises(cc.CameraControlError):
        cc.set_night_vision(cam)


def test_set_night_vision_validates_before_network():
    # An invalid value must be rejected up front, without a login attempt.
    cam = {"brand": "reolink", "host": "10.0.0.5", "username": "a", "password": "b"}
    with pytest.raises(cc.CameraControlError):
        cc.set_night_vision(cam, mode="sepia")
    with pytest.raises(cc.CameraControlError):
        cc.set_night_vision(cam, ir="blink")

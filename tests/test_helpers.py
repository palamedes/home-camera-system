"""Pure helpers in main.py: slugify, unique_camera_id, detect_fisheye."""

from conftest import add_camera


def test_slugify(app_module):
    s = app_module.slugify
    assert s("Front Door") == "front-door"
    assert s("  Weird!!  Name  ") == "weird-name"
    assert s("../../etc/passwd") == "etc-passwd"  # no path traversal survives
    assert s("!!!") == "camera"  # never empty


def test_unique_camera_id_dedupes(app_module, db):
    uid = app_module.unique_camera_id
    assert uid("Front Door") == "front-door"
    add_camera(db, "front-door", "Front Door")
    assert uid("Front Door") == "front-door-2"
    add_camera(db, "front-door-2", "Front Door")
    assert uid("Front Door") == "front-door-3"


def test_detect_fisheye_by_name(app_module):
    d = app_module.detect_fisheye
    assert d("Reolink FE-W", None, None)
    assert d("360 panoramic cam", None, None)
    assert d("some Fisheye lens", None, None)
    assert not d("RLC-810A", None, None)


def test_detect_fisheye_by_aspect(app_module):
    d = app_module.detect_fisheye
    assert d("unknown", 2560, 2560)      # square -> circular fisheye frame
    assert d("unknown", 2000, 1980)      # ~1:1 within tolerance
    assert not d("unknown", 1920, 1080)  # 16:9 is not a fisheye

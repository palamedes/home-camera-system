"""Pure helpers in streamprobe: resolution parsing and Reolink range flattening."""

from nvr import streamprobe


def test_parse_resolution_formats():
    p = streamprobe.parse_resolution
    assert p("2560*1920") == (2560, 1920)   # Reolink's separator
    assert p("640x480") == (640, 480)
    assert p("1920×1080") == (1920, 1080)   # unicode ×
    assert p("2560 * 1920") == (2560, 1920)


def test_parse_resolution_rejects_junk():
    p = streamprobe.parse_resolution
    assert p(None) is None
    assert p("") is None
    assert p("hd") is None
    assert p("0*0") is None          # zero dimension is not a resolution
    assert p("1920") is None         # missing a dimension


def test_normalise_enc_range_dict():
    # A single dict with list-valued size/bitRate (common firmware shape).
    block = {"size": ["2560*1920", "640*480"], "bitRate": [1024, 2048, 4096]}
    out = streamprobe.normalise_enc_range(block)
    assert out == {"sizes": ["2560*1920", "640*480"], "bitrates": [1024, 2048, 4096]}


def test_normalise_enc_range_list_of_dicts():
    # A list of per-resolution dicts (other firmware shape); dedupe + sort.
    block = [
        {"size": "2560*1920", "bitRate": [4096, 2048]},
        {"size": "640*480", "bitRate": [1024, 2048]},
    ]
    out = streamprobe.normalise_enc_range(block)
    assert out["sizes"] == ["2560*1920", "640*480"]
    assert out["bitrates"] == [1024, 2048, 4096]


def test_normalise_enc_range_empty():
    assert streamprobe.normalise_enc_range(None) is None
    assert streamprobe.normalise_enc_range({}) is None
    assert streamprobe.normalise_enc_range([{"foo": "bar"}]) is None

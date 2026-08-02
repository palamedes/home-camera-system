"""Config parsing and the env-override seam used for isolation."""

import pytest

from nvr import config


@pytest.mark.parametrize("value,expected", [
    (1024, 1024),
    ("1024", 1024),
    ("1K", 1024),
    ("1KB", 1024),
    ("2M", 2 * 1024**2),
    ("3G", 3 * 1024**3),
    ("1.5G", int(1.5 * 1024**3)),
])
def test_parse_size_absolute(value, expected):
    assert config.parse_size(value) == expected


def test_parse_size_percentage_of_total():
    assert config.parse_size("50%", total=1000) == 500


def test_parse_size_percentage_needs_total():
    with pytest.raises(ValueError):
        config.parse_size("50%")


def test_parse_size_rejects_empty():
    with pytest.raises(ValueError):
        config.parse_size("")


@pytest.mark.parametrize("num,expected", [
    (0, "0 B"),
    (512, "512 B"),
    (1024, "1.0 KB"),
    (1024**2, "1.0 MB"),
    (1024**3, "1.0 GB"),
])
def test_human_size(num, expected):
    assert config.human_size(num) == expected


def test_env_override_redirects_storage(app_module):
    """The temp-dir isolation actually took effect — proves tests never write
    to the real ~/Cameras/data."""
    cfg = app_module.cfg
    assert "sentry-tests-" in str(cfg.db_path)
    assert "sentry-tests-" in str(cfg.storage.clips_dir)
    # QSV disabled in the test config, so playback won't reach for a GPU.
    assert cfg.playback.qsv_device is None

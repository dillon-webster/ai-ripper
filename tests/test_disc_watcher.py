from pathlib import Path
from unittest.mock import patch
from modules.disc_watcher import _is_optical_disc, disc_type, wait_for_disc


def test_is_optical_disc_dvd(tmp_path):
    (tmp_path / "VIDEO_TS").mkdir()
    assert _is_optical_disc(tmp_path) is True


def test_disc_type_bluray(tmp_path):
    (tmp_path / "BDMV").mkdir()
    assert disc_type(tmp_path) == "bluray"


def test_disc_type_dvd(tmp_path):
    (tmp_path / "VIDEO_TS").mkdir()
    assert disc_type(tmp_path) == "dvd"


def test_disc_type_unknown(tmp_path):
    (tmp_path / "Documents").mkdir()
    assert disc_type(tmp_path) == "unknown"


def test_disc_type_prefers_bluray_on_hybrid(tmp_path):
    # A disc carrying both structures is treated as Blu-ray (BDMV checked first).
    (tmp_path / "BDMV").mkdir()
    (tmp_path / "VIDEO_TS").mkdir()
    assert disc_type(tmp_path) == "bluray"


def test_is_optical_disc_bluray(tmp_path):
    (tmp_path / "BDMV").mkdir()
    assert _is_optical_disc(tmp_path) is True


def test_is_optical_disc_usb_drive(tmp_path):
    # No VIDEO_TS or BDMV
    (tmp_path / "Documents").mkdir()
    assert _is_optical_disc(tmp_path) is False


def test_wait_for_disc_returns_on_new_optical_volume(tmp_path):
    # Simulate: first poll sees only existing volumes, second poll sees disc added
    disc_path = tmp_path / "FRIENDS_S1D2"
    disc_path.mkdir()
    (disc_path / "VIDEO_TS").mkdir()

    existing = tmp_path / "Macintosh HD"
    existing.mkdir()

    # _list_volumes is called twice: initial snapshot, then first poll
    volume_snapshots = [
        {existing},              # initial known set
        {existing, disc_path},   # poll finds new disc
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=volume_snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert name == "FRIENDS_S1D2"
    assert path == disc_path


def test_wait_for_disc_detects_disc_that_mounts_before_video_ts(tmp_path):
    # Regression: udisks2 exposes the mount-point directory a poll before
    # VIDEO_TS is stat-able. The disc must still be detected once it appears,
    # not permanently written off as a non-optical volume.
    disc_path = tmp_path / "FRIENDS_SEASON6_DISC2"

    calls = {"n": 0}

    def list_volumes():
        # poll 0: startup baseline, drive empty
        # poll 1: mount-point dir present but VIDEO_TS not stat-able yet
        # poll 2: filesystem fully mounted
        calls["n"] += 1
        if calls["n"] == 1:
            return set()
        if calls["n"] == 2:
            disc_path.mkdir(exist_ok=True)  # dir exists, no VIDEO_TS
            return {disc_path}
        (disc_path / "VIDEO_TS").mkdir(exist_ok=True)
        return {disc_path}

    with patch("modules.disc_watcher._list_volumes", side_effect=list_volumes), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert name == "FRIENDS_SEASON6_DISC2"
    assert path == disc_path

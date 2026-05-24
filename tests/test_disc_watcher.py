from pathlib import Path
from unittest.mock import patch
from modules.disc_watcher import _is_optical_disc, wait_for_disc


def test_is_optical_disc_dvd(tmp_path):
    (tmp_path / "VIDEO_TS").mkdir()
    assert _is_optical_disc(tmp_path) is True


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

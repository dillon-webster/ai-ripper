from pathlib import Path
from unittest.mock import patch
from modules.disc_watcher import _is_optical_disc, _list_volumes, _mount_roots, wait_for_disc


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


# --- Windows-specific tests ---

def test_mount_roots_windows_returns_drive_letter_paths():
    with patch("platform.system", return_value="Windows"), \
         patch("modules.disc_watcher._windows_drives", return_value=[Path("D:\\"), Path("E:\\")]):
        roots = _mount_roots()
    assert Path("D:\\") in roots
    assert Path("E:\\") in roots


def test_mount_roots_linux_unaffected_by_windows_changes():
    # Guard: ensure the Windows branch doesn't bleed into Linux behavior.
    with patch("platform.system", return_value="Linux"), \
         patch.dict("os.environ", {"USER": "testuser"}, clear=False):
        roots = _mount_roots()
    root_strs = [str(r) for r in roots]
    assert any("media" in r for r in root_strs)
    assert not any(":\\" in r for r in root_strs)


def test_list_volumes_windows_returns_drive_roots_not_children(tmp_path):
    # On Windows each drive root IS the volume — we must not call iterdir() on it.
    # Simulate D:\ containing a disc (VIDEO_TS lives directly under the drive root).
    d_root = tmp_path / "D"
    d_root.mkdir()
    (d_root / "VIDEO_TS").mkdir()

    with patch("platform.system", return_value="Windows"), \
         patch("modules.disc_watcher._windows_drives", return_value=[d_root]):
        volumes = _list_volumes()

    assert d_root in volumes
    # Children (VIDEO_TS) must not appear as separate volumes
    assert d_root / "VIDEO_TS" not in volumes


def test_list_volumes_windows_empty_drive_not_included(tmp_path):
    # A drive that doesn't exist (e.g. E:\ with no disc) should be excluded.
    missing_drive = tmp_path / "E"  # intentionally not created

    with patch("platform.system", return_value="Windows"), \
         patch("modules.disc_watcher._windows_drives", return_value=[missing_drive]):
        volumes = _list_volumes()

    assert missing_drive not in volumes


def test_wait_for_disc_windows_detects_disc_on_drive_letter(tmp_path):
    # Simulate a DVD appearing on D:\ (Windows drive root layout).
    d_root = tmp_path / "D"
    d_root.mkdir()
    (d_root / "VIDEO_TS").mkdir()

    volume_snapshots = [
        set(),      # initial: no disc
        {d_root},   # poll: disc inserted on D:\
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=volume_snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert path == d_root


def test_wait_for_disc_windows_detects_bluray_on_drive_letter(tmp_path):
    d_root = tmp_path / "D"
    d_root.mkdir()
    (d_root / "BDMV").mkdir()

    volume_snapshots = [
        set(),
        {d_root},
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=volume_snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert path == d_root


def test_wait_for_disc_windows_ignores_non_optical_drives(tmp_path):
    # A data drive on D:\ should not trigger detection; disc on E:\ should.
    d_root = tmp_path / "D"
    d_root.mkdir()
    (d_root / "Documents").mkdir()  # not optical

    e_root = tmp_path / "E"
    e_root.mkdir()
    (e_root / "VIDEO_TS").mkdir()  # optical

    volume_snapshots = [
        set(),
        {d_root, e_root},
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=volume_snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert path == e_root

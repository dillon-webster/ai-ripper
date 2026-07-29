from pathlib import Path
from unittest.mock import patch

import pytest

from modules import disc_watcher
from modules.disc_watcher import _is_optical_disc, disc_type, wait_for_disc

# udev properties for a real Blu-ray (SANTA_CLAUSE) sitting in an unmounted drive.
BD_PROPS = {
    "DEVNAME": "/dev/sr0",
    "DISKSEQ": "70",
    "ID_CDROM": "1",
    "ID_CDROM_MEDIA": "1",
    "ID_CDROM_MEDIA_BD": "1",
    "ID_FS_LABEL": "SANTA_CLAUSE",
    "ID_FS_TYPE": "udf",
    "ID_FS_USAGE": "filesystem",
    "ID_FS_UUID": "8cf4a7f134f3bd63",
}

DVD_PROPS = {
    "DEVNAME": "/dev/sr0",
    "DISKSEQ": "12",
    "ID_CDROM": "1",
    "ID_CDROM_MEDIA": "1",
    "ID_CDROM_MEDIA_DVD": "1",
    "ID_FS_LABEL": "FRIENDS_S1D2",
    "ID_FS_TYPE": "udf",
    "ID_FS_USAGE": "filesystem",
    "ID_FS_UUID": "1a2b3c4d5e6f7788",
}

EMPTY_DRIVE_PROPS = {"DEVNAME": "/dev/sr0", "ID_CDROM": "1"}


class _Slept(Exception):
    """Raised in place of a poll delay so a blocking wait_for_disc can be asserted."""


@pytest.fixture(autouse=True)
def _forget_handled_discs():
    """wait_for_disc remembers handled discs for the life of the process; each test
    needs a clean drive."""
    disc_watcher._handled.clear()
    yield
    disc_watcher._handled.clear()


@pytest.fixture
def no_optical_devices():
    """Silence the device-level watcher so mount-based tests see only their tmp_path
    volumes and not the real drive on the machine running the suite."""
    with patch("modules.disc_watcher._device_nodes", return_value=[]):
        yield


def _device_watch(props_by_poll, mount_point=None):
    """Patch the device layer: each poll yields the next dict of udev properties."""
    return (
        patch("modules.disc_watcher._device_nodes", return_value=[Path("/dev/sr0")]),
        patch("modules.disc_watcher._udev_properties", side_effect=props_by_poll),
        patch("modules.disc_watcher._mount_point", return_value=mount_point),
        patch("modules.disc_watcher._list_volumes", return_value=set()),
        patch("modules.disc_watcher.POLL_INTERVAL", 0),
    )


def _run_device_watch(props_by_poll, mount_point=None):
    patches = _device_watch(props_by_poll, mount_point)
    for p in patches:
        p.start()
    try:
        return wait_for_disc()
    finally:
        for p in patches:
            p.stop()


# --- on-disc structure / media classification --------------------------------

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


def test_disc_type_of_unmounted_device_uses_media_type():
    # Nothing mounted the disc, so there is no BDMV/ to stat — the physical media
    # type from udev has to carry the bluray-vs-dvd routing decision.
    with patch("modules.disc_watcher._udev_properties", return_value=BD_PROPS):
        assert disc_type(Path("/dev/sr0")) == "bluray"


def test_disc_type_of_unmounted_dvd_device_uses_media_type():
    with patch("modules.disc_watcher._udev_properties", return_value=DVD_PROPS):
        assert disc_type(Path("/dev/sr0")) == "dvd"


def test_disc_type_of_device_with_unrecognized_media():
    with patch("modules.disc_watcher._udev_properties", return_value=EMPTY_DRIVE_PROPS):
        assert disc_type(Path("/dev/sr0")) == "unknown"


# --- device-level detection (no mount required) ------------------------------

def test_detects_unmounted_bluray_on_device():
    # The regression this module exists to prevent: under a desktop that doesn't
    # automount optical media (KDE), the disc never appears under /run/media, so a
    # mount-only watcher waits forever with a disc in the drive.
    name, path = _run_device_watch([BD_PROPS])
    assert name == "SANTA_CLAUSE"
    assert path == Path("/dev/sr0")


def test_detects_disc_present_at_startup():
    # A disc already in the drive when the daemon starts must be ripped, not
    # silently written off as "already handled".
    name, _ = _run_device_watch([BD_PROPS])
    assert name == "SANTA_CLAUSE"


def test_prefers_mount_point_when_disc_is_mounted():
    # When something DID mount the disc, hand back the mount point so the richer
    # on-disc-structure classification (BDMV vs VIDEO_TS) is still available.
    mount = Path("/run/media/dillon/SANTA_CLAUSE")
    name, path = _run_device_watch([BD_PROPS], mount_point=mount)
    assert name == "SANTA_CLAUSE"
    assert path == mount


def test_waits_while_drive_is_empty():
    # Drive empty on the first poll, disc inserted by the second.
    name, path = _run_device_watch([EMPTY_DRIVE_PROPS, BD_PROPS])
    assert name == "SANTA_CLAUSE"
    assert path == Path("/dev/sr0")


def test_ignores_disc_with_no_filesystem():
    # A blank or audio disc reports media present but carries no filesystem —
    # there is nothing for makemkv to rip, so it must not wake the pipeline.
    blank = {**BD_PROPS}
    del blank["ID_FS_USAGE"]
    del blank["ID_FS_LABEL"]

    with patch("modules.disc_watcher._device_nodes", return_value=[Path("/dev/sr0")]), \
         patch("modules.disc_watcher._udev_properties", return_value=blank), \
         patch("modules.disc_watcher._mount_point", return_value=None), \
         patch("modules.disc_watcher._list_volumes", return_value=set()), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0), \
         patch("modules.disc_watcher.time.sleep", side_effect=_Slept):
        with pytest.raises(_Slept):
            wait_for_disc()


def test_does_not_return_the_same_disc_twice():
    # The disc stays in the drive after a rip that was held for manual handling;
    # the next wait must not immediately re-rip it.
    _run_device_watch([BD_PROPS])

    with patch("modules.disc_watcher._device_nodes", return_value=[Path("/dev/sr0")]), \
         patch("modules.disc_watcher._udev_properties", return_value=BD_PROPS), \
         patch("modules.disc_watcher._mount_point", return_value=None), \
         patch("modules.disc_watcher._list_volumes", return_value=set()), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0), \
         patch("modules.disc_watcher.time.sleep", side_effect=_Slept):
        with pytest.raises(_Slept):
            wait_for_disc()


def test_reinserted_disc_is_detected_again():
    # Box sets reuse volume labels and UUIDs, so identity comes from DISKSEQ, which
    # the kernel bumps on every media change. Same disc back in the drive = new rip.
    _run_device_watch([BD_PROPS])
    reinserted = {**BD_PROPS, "DISKSEQ": "71"}
    name, path = _run_device_watch([reinserted])
    assert name == "SANTA_CLAUSE"
    assert path == Path("/dev/sr0")


def test_falls_back_to_device_name_when_disc_has_no_label():
    unlabeled = {**BD_PROPS}
    del unlabeled["ID_FS_LABEL"]
    name, path = _run_device_watch([unlabeled])
    assert name == "sr0"
    assert path == Path("/dev/sr0")


# --- mounted-volume detection (macOS, and any desktop that does automount) ----

def test_wait_for_disc_returns_on_new_optical_volume(tmp_path, no_optical_devices):
    # Simulate: first poll sees only existing volumes, second poll sees disc added
    disc_path = tmp_path / "FRIENDS_S1D2"
    disc_path.mkdir()
    (disc_path / "VIDEO_TS").mkdir()

    existing = tmp_path / "Macintosh HD"
    existing.mkdir()

    volume_snapshots = [
        {existing},              # first poll: drive empty
        {existing, disc_path},   # second poll: disc appears
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=volume_snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert name == "FRIENDS_S1D2"
    assert path == disc_path


def test_wait_for_disc_detects_disc_that_mounts_before_video_ts(tmp_path, no_optical_devices):
    # Regression: udisks2 exposes the mount-point directory a poll before
    # VIDEO_TS is stat-able. The disc must still be detected once it appears,
    # not permanently written off as a non-optical volume.
    disc_path = tmp_path / "FRIENDS_SEASON6_DISC2"

    calls = {"n": 0}

    def list_volumes():
        # poll 1: mount-point dir present but VIDEO_TS not stat-able yet
        # poll 2: filesystem fully mounted
        calls["n"] += 1
        if calls["n"] == 1:
            disc_path.mkdir(exist_ok=True)  # dir exists, no VIDEO_TS
            return {disc_path}
        (disc_path / "VIDEO_TS").mkdir(exist_ok=True)
        return {disc_path}

    with patch("modules.disc_watcher._list_volumes", side_effect=list_volumes), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert name == "FRIENDS_SEASON6_DISC2"
    assert path == disc_path


def test_mounted_volume_is_not_ripped_twice(tmp_path, no_optical_devices):
    disc_path = tmp_path / "FRIENDS_S1D2"
    (disc_path / "VIDEO_TS").mkdir(parents=True)

    with patch("modules.disc_watcher._list_volumes", return_value={disc_path}), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, _ = wait_for_disc()
        assert name == "FRIENDS_S1D2"

        with patch("modules.disc_watcher.time.sleep", side_effect=_Slept):
            with pytest.raises(_Slept):
                wait_for_disc()


def test_ejected_mounted_volume_is_detected_on_reinsert(tmp_path, no_optical_devices):
    # A mounted volume has no DISKSEQ to distinguish inserts, so re-detection
    # relies on the volume disappearing while the drive is empty.
    disc_path = tmp_path / "FRIENDS_S1D2"
    (disc_path / "VIDEO_TS").mkdir(parents=True)

    snapshots = [
        {disc_path},   # first wait: disc detected
        set(),         # ejected
        {disc_path},   # same label back in the drive
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        assert wait_for_disc()[0] == "FRIENDS_S1D2"
        assert wait_for_disc()[0] == "FRIENDS_S1D2"

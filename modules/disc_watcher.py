"""Optical disc detection.

Detection is DEVICE-level, not mount-level. The watcher polls the optical drives
themselves (`/dev/sr*`, via udev) and asks the kernel whether media is present —
it does NOT wait for anything to mount the disc.

That distinction is the whole point of this module. Mount-only detection silently
depends on a desktop session automounting removable media: it worked under GNOME
(gvfs/Nautilus automount) and stopped working the day the box moved to KDE Plasma,
whose automounter is off by default. The daemon sat logging "Waiting for disc..."
with a Blu-ray in the drive because nothing ever created /run/media/$USER/<label>.
Nothing in the rip pipeline actually needs the mount — makemkvcon reads `disc:0`
(the raw device) — so the mount is treated as a bonus, never a requirement.

Mounted volumes are still watched as a second signal: it's how macOS (/Volumes)
works, and on Linux a mount point gives us the on-disc structure (BDMV/VIDEO_TS)
for a more precise classification than the physical media type.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds
UDEV_TIMEOUT = 10  # seconds; a hung udevadm must not wedge the watcher

# Discs this process has already handed to the pipeline. Deliberately module-level
# (process-lifetime) rather than per-call: a rip that's held for manual handling
# leaves the disc in the drive, and the next wait must not immediately re-rip it.
# Nothing is pre-seeded, so a disc already in the drive when the daemon starts DOES
# get ripped — the previous per-call seeding made that case a silent no-op.
_handled: Set[tuple] = set()


@dataclass(frozen=True)
class _Disc:
    name: str      # volume label, for the namer
    path: Path     # mount point when mounted, else the device node
    key: tuple     # identity used to suppress re-detection of the same insert


# --- mounted volumes ---------------------------------------------------------

def _mount_roots() -> List[Path]:
    """Return candidate mount-point directories for the current OS."""
    if platform.system() == "Darwin":
        return [Path("/Volumes")]
    # Linux: udisks2/udev mounts under /media/$USER or /run/media/$USER
    username = os.getenv("USER") or os.getenv("LOGNAME") or ""
    candidates = [Path(f"/media/{username}"), Path(f"/run/media/{username}")]
    # Return whichever ones exist; fall back to all candidates so we don't
    # silently drop the right path just because it's not created yet.
    existing = [p for p in candidates if p.exists()]
    return existing if existing else candidates


def _list_volumes() -> Set[Path]:
    """Return current set of mounted volumes. Isolated for testability."""
    volumes: Set[Path] = set()
    for root in _mount_roots():
        if root.exists():
            volumes.update(root.iterdir())
    return volumes


def _is_optical_disc(volume_path: Path) -> bool:
    return (volume_path / "VIDEO_TS").exists() or (volume_path / "BDMV").exists()


# --- optical devices ---------------------------------------------------------

def _device_nodes() -> List[Path]:
    """Optical device nodes to watch. Empty on macOS, where /Volumes is the signal."""
    if platform.system() == "Darwin":
        return []
    return sorted(Path("/dev").glob("sr*"))


def _udev_properties(device: Path) -> Dict[str, str]:
    """Return udev's properties for a device, or {} if they can't be read.

    Read via `udevadm` rather than pyudev to keep the dependency list at zero.
    """
    if not shutil.which("udevadm"):
        return {}
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device}"],
            capture_output=True, text=True, timeout=UDEV_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.debug(f"Could not read udev properties for {device}: {e}")
        return {}
    if result.returncode != 0:
        return {}
    props: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            props[key] = value
    return props


def _unescape_mount_path(path: str) -> str:
    """/proc/mounts octal-escapes spaces and tabs; disc labels contain spaces
    (e.g. 'THE YEAR WITHOUT A SANTA CLAUS')."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def _mount_point(device: Path) -> Optional[Path]:
    """Return where `device` is mounted, or None if it isn't."""
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        return None
    wanted = {str(device), os.path.realpath(device)}
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in wanted:
            return Path(_unescape_mount_path(fields[1]))
    return None


def _media_kind(props: Dict[str, str]) -> str:
    """Classify the physical media from udev: 'bluray', 'dvd', or 'unknown'.
    BD is checked first so a hybrid disc is treated as Blu-ray, matching the
    structure-based classification in disc_type()."""
    if props.get("ID_CDROM_MEDIA_BD"):
        return "bluray"
    if props.get("ID_CDROM_MEDIA_DVD"):
        return "dvd"
    return "unknown"


def _disc_on_device(device: Path) -> Optional[_Disc]:
    """Return the disc currently in `device`, or None if the drive is empty or the
    disc holds nothing rippable (blank/audio media carries no filesystem)."""
    props = _udev_properties(device)
    if not props.get("ID_CDROM_MEDIA"):
        return None
    if props.get("ID_FS_USAGE") != "filesystem":
        log.debug(f"Ignoring non-data disc in {device} "
                  f"(fs usage: {props.get('ID_FS_USAGE') or 'none'})")
        return None
    # DISKSEQ is bumped by the kernel on every media change, which makes it the one
    # reliable "this is a different insert" signal: box sets reuse both volume labels
    # AND filesystem UUIDs across discs, and re-inserting the same disc must re-rip.
    ident = props.get("DISKSEQ") or props.get("ID_FS_UUID") or props.get("ID_FS_LABEL", "")
    return _Disc(
        name=props.get("ID_FS_LABEL") or device.name,
        path=_mount_point(device) or device,
        key=("device", str(device), ident),
    )


# --- public API --------------------------------------------------------------

def disc_type(volume_path: Path) -> str:
    """Classify a disc as 'bluray', 'dvd', or 'unknown'. The distinction drives
    routing: Blu-rays are staged locally for manual encoding (their raw rips are
    20-40GB+), while DVDs transfer straight to the server.

    Prefers the on-disc structure (BDMV/ or VIDEO_TS/) when the disc is mounted,
    checked BDMV-first so a hybrid disc carrying both is treated as Blu-ray. For an
    unmounted disc there is no structure to stat, so the physical media type from
    udev decides — which is the same BD-vs-DVD distinction routing cares about.
    """
    if (volume_path / "BDMV").exists():
        return "bluray"
    if (volume_path / "VIDEO_TS").exists():
        return "dvd"
    if volume_path.parent == Path("/dev"):
        return _media_kind(_udev_properties(volume_path))
    return "unknown"


def _list_discs() -> List[_Disc]:
    """Every disc visible right now, from both signals: the optical devices
    themselves, plus mounted volumes that no watched device accounts for."""
    discs = [d for d in (_disc_on_device(dev) for dev in _device_nodes()) if d]
    claimed = {d.path for d in discs}
    for volume in _list_volumes():
        # A mounted disc already reported by its device must not be handed over
        # twice under two different identities.
        if volume in claimed or not _is_optical_disc(volume):
            continue
        discs.append(_Disc(name=volume.name, path=volume, key=("volume", str(volume))))
    return discs


def wait_for_disc() -> Tuple[str, Path]:
    """Block until a rippable disc is present. Returns (volume_name, path), where
    path is the mount point if the disc happens to be mounted and the device node
    otherwise — makemkvcon rips `disc:0` either way, and `eject` accepts both."""
    while True:
        discs = _list_discs()
        for disc in discs:
            if disc.key in _handled:
                continue
            _handled.add(disc.key)
            return disc.name, disc.path
        # Forget discs that are gone so a re-insert is detected again. Essential for
        # mounted volumes, which have no DISKSEQ to tell one insert from the next;
        # for devices it just keeps the set from growing disc by disc.
        _handled.intersection_update({d.key for d in discs})
        time.sleep(POLL_INTERVAL)

import os
import platform
import time
from pathlib import Path
from typing import List, Set, Tuple

POLL_INTERVAL = 5  # seconds


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


def disc_type(volume_path: Path) -> str:
    """Classify an optical disc by its on-disc structure: 'bluray' (BDMV/),
    'dvd' (VIDEO_TS/), or 'unknown'. The distinction drives routing: Blu-rays
    are staged locally for manual encoding (their raw rips are 20-40GB+), while
    DVDs transfer straight to the server. Checked BDMV-first so a hybrid disc
    carrying both structures is treated as Blu-ray."""
    if (volume_path / "BDMV").exists():
        return "bluray"
    if (volume_path / "VIDEO_TS").exists():
        return "dvd"
    return "unknown"


def wait_for_disc() -> Tuple[str, Path]:
    """Block until an optical disc is inserted. Returns (volume_name, volume_path)."""
    # Track optical discs we've already handled by path. We deliberately do NOT
    # remember non-optical volumes: udisks2 creates the mount-point directory a
    # beat before VIDEO_TS/BDMV becomes stat-able, so a freshly inserted disc can
    # look non-optical on the first poll. If we folded that path into a "known"
    # set, it would never be re-examined once the filesystem finished mounting
    # (the bug that made inserted discs go unseen). Instead we re-check every
    # volume each poll and only suppress discs we've actually returned before.
    handled: Set[Path] = {p for p in _list_volumes() if _is_optical_disc(p)}
    while True:
        current = _list_volumes()
        for path in current:
            if _is_optical_disc(path) and path not in handled:
                handled.add(path)
                return path.name, path
        # Drop discs that have been ejected so a re-insert at the same path
        # (box sets reuse volume labels) is detected again.
        handled &= current
        time.sleep(POLL_INTERVAL)

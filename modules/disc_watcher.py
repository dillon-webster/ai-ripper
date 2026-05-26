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


def wait_for_disc() -> Tuple[str, Path]:
    """Block until an optical disc is inserted. Returns (volume_name, volume_path)."""
    known = _list_volumes()
    while True:
        current = _list_volumes()
        new_volumes = current - known
        for path in new_volumes:
            if _is_optical_disc(path):
                return path.name, path
        known = current
        time.sleep(POLL_INTERVAL)

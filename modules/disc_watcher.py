import time
from pathlib import Path
from typing import Set, Tuple

VOLUMES_PATH = Path("/Volumes")
POLL_INTERVAL = 5  # seconds


def _list_volumes() -> Set[Path]:
    """Return current set of /Volumes entries. Isolated for testability."""
    return set(VOLUMES_PATH.iterdir())


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

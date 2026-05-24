import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

RETRY_DELAYS = [10, 30, 60]  # seconds between attempts 1→2, 2→3, 3→4


class TransferError(Exception):
    pass


def _scp(local_path: Path, remote: str) -> None:
    result = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", str(local_path), remote],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TransferError(f"scp failed (exit {result.returncode}): {result.stderr.strip()}")


def send_all(named_titles: List[Dict], config) -> List[str]:
    """
    SCP each title to the home server.
    Returns list of remote paths for successfully transferred files.
    Raises TransferError if any file fails all retries.
    """
    remote_paths = []

    for title in named_titles:
        filename = title["jellyfin_filename"]
        destination = title["destination"]
        remote = (
            f"{config.server_user}@{config.server_ip}:"
            f"/home/{config.server_user}/jellyfin/media/{destination}/{filename}"
        )

        last_error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt > 0:
                delay = RETRY_DELAYS[attempt - 1]
                log.warning(f"Retrying transfer of {filename} in {delay}s (attempt {attempt + 1})")
                time.sleep(delay)
            try:
                _scp(title["path"], remote)
                remote_paths.append(remote)
                log.info(f"Transferred: {filename} → {remote}")
                break
            except TransferError as e:
                last_error = e
                log.warning(f"Transfer attempt {attempt + 1} failed: {e}")
        else:
            raise TransferError(
                f"Failed to transfer {filename} after {len(RETRY_DELAYS) + 1} attempts: {last_error}"
            )

    return remote_paths

import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

RETRY_DELAYS = [10, 30, 60]  # seconds between attempts 1→2, 2→3, 3→4

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]


class TransferError(Exception):
    pass


def _remote_subpath(title: Dict) -> str:
    """
    Return the relative path within the media root.
    TV:    'tvshows/Friends/Season 01/Friends.S01E01.mkv'
    Movie: 'movies/Inception.2010.mkv'
    """
    filename = title["jellyfin_filename"]
    dest = title["destination"]
    if title["media_type"] == "tv":
        m = re.search(r"^(.*?)\.S(\d{2})E\d{2}", filename, re.IGNORECASE)
        if m:
            show = m.group(1).replace(".", " ")
            season = m.group(2)
            return f"{dest}/{show}/Season {season}/{filename}"
    return f"{dest}/{filename}"


def _ssh_mkdir(remote_dir: str, config) -> None:
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"{config.server_user}@{config.server_ip}",
         f"mkdir -p {shlex.quote(remote_dir)}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TransferError(f"mkdir failed: {result.stderr.strip()}")


def _scp(local_path: Path, remote: str) -> None:
    result = subprocess.run(
        ["scp", *SSH_OPTS, str(local_path), remote],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TransferError(f"scp failed (exit {result.returncode}): {result.stderr.strip()}")


def list_existing_episodes(config) -> List[str]:
    """Return sorted list of existing episode filenames on the server (e.g. 'Friends.S01E01.mkv')."""
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"{config.server_user}@{config.server_ip}",
         f"find {shlex.quote(config.media_root)}/tvshows -name '*.mkv' | sort"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [Path(line.strip()).name for line in result.stdout.splitlines() if line.strip()]


def send_all(named_titles: List[Dict], config) -> List[str]:
    """
    SCP each title to the home server in the proper Jellyfin directory structure.
    Returns list of remote paths for successfully transferred files.
    Raises TransferError if any file fails all retries.
    """
    remote_paths = []
    media_root = config.media_root

    for title in named_titles:
        filename = title["jellyfin_filename"]
        subpath = _remote_subpath(title)
        remote_file = f"{media_root}/{subpath}"
        remote_dir = remote_file.rsplit("/", 1)[0]
        remote = f"{config.server_user}@{config.server_ip}:{remote_file}"

        last_error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt > 0:
                delay = RETRY_DELAYS[attempt - 1]
                log.warning(f"Retrying transfer of {filename} in {delay}s (attempt {attempt + 1})")
                time.sleep(delay)
            try:
                _ssh_mkdir(remote_dir, config)
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

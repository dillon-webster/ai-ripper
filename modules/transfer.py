import logging
import re
import shlex
import shutil
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
    Movie: 'movies/Inception (2010)/Inception (2010).mkv'

    Movies use the same folder-per-movie layout as _staging_subpath. Keeping the
    two in step matters: this is the DVD path and that one is the Blu-ray path,
    so a flat movie file here meant DVD rips landed in a shape the encode script
    and the dashboard's transfer helpers (both of which address a movie by its
    folder) couldn't see.
    """
    filename = title["jellyfin_filename"]
    dest = title["destination"]
    if title["media_type"] == "tv":
        m = re.search(r"^(.*?)\.S(\d{2})E\d{2}", filename, re.IGNORECASE)
        if m:
            show = m.group(1).replace(".", " ")
            season = m.group(2)
            return f"{dest}/{show}/Season {season}/{filename}"
    folder = _movie_folder_name(filename)
    return f"{dest}/{folder}/{folder}.mkv"


def _movie_folder_name(filename: str) -> str:
    """Convert a namer movie filename ('The.Conjuring.2013.mkv') into the
    folder-per-movie name Jellyfin and the HEVC encode script expect:
    'The Conjuring (2013)' — dots→spaces, trailing year wrapped in parens. The
    year is taken as the last 4-digit 19xx/20xx token so titles that themselves
    contain a number ('Blade.Runner.2049.2017') resolve correctly. With no year
    found, just de-dot the base name."""
    base = filename[:-4] if filename.lower().endswith(".mkv") else filename
    m = re.match(r"^(.*)[.\s]((?:19|20)\d{2})$", base)
    if m:
        title = m.group(1).replace(".", " ").strip()
        return f"{title} ({m.group(2)})"
    return base.replace(".", " ").strip()


def _staging_subpath(title: Dict) -> str:
    """Relative path within the local Blu-ray staging root. Movies use the
    folder-per-movie layout the encode pipeline consumes
    ('movies/<Title (Year)>/<Title (Year)>.mkv'); TV reuses the flat server
    layout ('tvshows/Show/Season NN/Show.SxxExx.mkv')."""
    if title.get("media_type") == "movie":
        folder = _movie_folder_name(title["jellyfin_filename"])
        return f"{title['destination']}/{folder}/{folder}.mkv"
    return _remote_subpath(title)


def _ssh_mkdir(remote_dir: str, config) -> None:
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"{config.server_user}@{config.server_ip}",
         f"mkdir -p {shlex.quote(remote_dir)}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TransferError(f"mkdir failed: {result.stderr.strip()}")


def _remote_file_exists(remote_file: str, config) -> bool:
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"{config.server_user}@{config.server_ip}",
         f"test -f {shlex.quote(remote_file)}"],
        capture_output=True,
    )
    return result.returncode == 0


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
         f"find -L {shlex.quote(config.media_root)}/tvshows -name '*.mkv' | sort"],
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
                if _remote_file_exists(remote_file, config):
                    log.info(f"Skipping {filename} — already exists on server")
                    break
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


def stage_local(named_titles: List[Dict], config) -> List[Path]:
    """
    Move each ripped title into the local Blu-ray staging tree instead of sending
    it to the server. Blu-ray rips are 20-40GB+ raw, so they're staged here to be
    encoded down before a later manual copy to the server. Layout (see
    _staging_subpath): movies use the folder-per-movie form the HEVC encode script
    reads ('movies/<Title (Year)>/<Title (Year)>.mkv'); TV uses 'tvshows/Show/
    Season NN/...'. Returns the list of staged local paths.

    Uses shutil.move: an instant rename when TEMP_DIR and the staging dir share a
    filesystem, else a copy+delete (correct, just slower). A title already present
    at its destination is skipped (its temp file is left for the normal cleanup).
    Raises TransferError if a move fails, so the disc is held rather than lost.
    """
    staging_root = Path(config.bluray_staging_dir).expanduser()
    staged: List[Path] = []

    for title in named_titles:
        filename = title["jellyfin_filename"]
        dest = staging_root / _staging_subpath(title)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            log.info(f"Skipping {filename} — already staged at {dest}")
            continue
        try:
            shutil.move(str(title["path"]), str(dest))
        except OSError as e:
            raise TransferError(f"Failed to stage {filename} to {dest}: {e}") from e
        staged.append(dest)
        log.info(f"Staged for encoding: {filename} → {dest}")

    return staged

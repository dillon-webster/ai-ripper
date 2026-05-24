import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

MIN_TITLE_DURATION_SECS = 300  # 5 minutes — skip extras/menus


class RipError(Exception):
    pass


def _hms_to_secs(hms: str) -> int:
    """Convert 'H:MM:SS' or 'MM:SS' to total seconds."""
    parts = hms.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def _parse_info(output: str) -> Dict[int, Dict]:
    """Parse 'makemkvcon -r info' output into {title_idx: {duration_secs}}."""
    titles: Dict[int, Dict] = {}
    for line in output.splitlines():
        m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.*)"', line)
        if not m:
            continue
        title_idx, code = int(m.group(1)), int(m.group(2))
        value = m.group(3)
        if title_idx not in titles:
            titles[title_idx] = {}
        if code == 9:  # duration field
            titles[title_idx]["duration_secs"] = _hms_to_secs(value)
    return titles


def _parse_title_index(filename: str) -> int:
    """Extract title index from MakeMKV output name like 'title_t03.mkv'."""
    m = re.search(r"title_t(\d+)", filename)
    return int(m.group(1)) if m else -1


def rip(volume_path: Path, temp_dir: Path) -> List[Dict]:
    """
    Rip all titles from the disc to temp_dir.
    Returns list of dicts: [{path, duration_secs, title_index}].
    Titles shorter than MIN_TITLE_DURATION_SECS are excluded.
    Raises RipError on failure.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: get title metadata
    log.info("Querying disc info...")
    info_result = subprocess.run(
        ["makemkvcon", "-r", "info", "disc:0"],
        capture_output=True,
        text=True,
    )
    title_info = _parse_info(info_result.stdout)

    # Phase 2: rip all titles
    log.info(f"Ripping disc to {temp_dir}...")
    rip_result = subprocess.run(
        ["makemkvcon", "mkv", "disc:0", "all", str(temp_dir)],
        check=False,
    )
    if rip_result.returncode != 0:
        raise RipError(f"makemkvcon exited with exit code {rip_result.returncode}")

    mkv_files = sorted(temp_dir.glob("*.mkv"))
    if not mkv_files:
        raise RipError("No MKV files produced by makemkvcon")

    # Match output files to title info by index parsed from filename
    output = []
    for mkv in mkv_files:
        idx = _parse_title_index(mkv.name)
        info = title_info.get(idx, {})
        duration_secs = info.get("duration_secs", 0)

        if duration_secs < MIN_TITLE_DURATION_SECS:
            log.info(f"Skipping {mkv.name} (duration {duration_secs}s < {MIN_TITLE_DURATION_SECS}s)")
            continue

        output.append({
            "path": mkv,
            "duration_secs": duration_secs,
            "title_index": idx,
        })

    if not output:
        raise RipError("No valid titles found after filtering short titles")

    return output

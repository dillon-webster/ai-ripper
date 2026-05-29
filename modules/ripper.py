import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

MIN_TITLE_DURATION_SECS = 300  # 5 minutes — skip extras/menus
PLAY_ALL_TOLERANCE = 0.10  # title within 10% of sum of others is "Play All"


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
    """Extract title index from MakeMKV output name like 'title_t03.mkv' or 'DISC NAME_t03.mkv'."""
    m = re.search(r"_t(\d+)", filename)
    return int(m.group(1)) if m else -1


def rip(volume_path: Path, temp_dir: Path) -> List[Dict]:
    """
    Rip eligible titles from the disc to temp_dir.
    Returns list of dicts: [{path, duration_secs, title_index}].
    Titles outside [MIN, MAX] duration range are excluded BEFORE ripping
    (skips menus/extras and TV "Play All" combined titles).
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

    # Filter titles by minimum duration (drop menus/extras)
    eligible_indices = []
    for idx, info in sorted(title_info.items()):
        dur = info.get("duration_secs", 0)
        if dur == 0:
            log.info(f"Title #{idx}: unknown duration, including anyway")
            eligible_indices.append(idx)
        elif dur < MIN_TITLE_DURATION_SECS:
            log.info(f"Skipping title #{idx} (duration {dur}s < {MIN_TITLE_DURATION_SECS}s — extra/menu)")
        else:
            eligible_indices.append(idx)

    # Detect "Play All": if the longest title ≈ sum of the others, drop it
    if len(eligible_indices) >= 3:
        durations = [(idx, title_info[idx].get("duration_secs", 0)) for idx in eligible_indices]
        longest_idx, longest_dur = max(durations, key=lambda x: x[1])
        others_sum = sum(d for idx, d in durations if idx != longest_idx)
        if others_sum > 0 and abs(longest_dur - others_sum) / others_sum <= PLAY_ALL_TOLERANCE:
            log.info(f"Skipping title #{longest_idx} (duration {longest_dur}s ≈ sum of others {others_sum}s — 'Play All')")
            eligible_indices.remove(longest_idx)

    if not eligible_indices:
        raise RipError("No eligible titles found on disc after duration filtering")

    # Phase 2: rip eligible titles one at a time
    log.info(f"Ripping {len(eligible_indices)} title(s) to {temp_dir}...")
    for idx in eligible_indices:
        log.info(f"Ripping title #{idx}...")
        rip_result = subprocess.run(
            ["makemkvcon", "mkv", "disc:0", str(idx), str(temp_dir)],
            check=False,
        )
        if rip_result.returncode != 0:
            raise RipError(f"makemkvcon exited with code {rip_result.returncode} on title #{idx}")

    mkv_files = sorted(temp_dir.glob("*.mkv"))
    if not mkv_files:
        raise RipError("No MKV files produced by makemkvcon")

    # Match output files to title info, only including eligible titles
    eligible_set = set(eligible_indices)
    output = []
    for mkv in mkv_files:
        idx = _parse_title_index(mkv.name)
        if idx not in eligible_set:
            continue
        info = title_info.get(idx, {})
        output.append({
            "path": mkv,
            "duration_secs": info.get("duration_secs", 0),
            "title_index": idx,
        })

    return output

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

MIN_TITLE_DURATION_SECS = 960  # 16 minutes — a real ~21-min episode clears this, but
# short bonuses/fragments (deleted scenes, 5–15 min featurettes, animatic teasers) don't.
# NOTE: assumes ~20-min+ episodes. A show with genuinely short episodes (e.g. 11-min
# cartoons) would need this lowered.
PLAY_ALL_TOLERANCE = 0.10  # title within 10% of sum of others is "Play All"
MIN_CLUSTER_TO_PRUNE = 3  # only prune outlier source sets when one set has >= this many titles
INFO_SCAN_ATTEMPTS = 5  # retry the disc info scan while the drive spins up
INFO_SCAN_RETRY_SECS = 6  # wait between info-scan attempts


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
        elif code == 49:  # source-set label (VTS), e.g. "B1" — shared by a disc's real episodes
            titles[title_idx]["source_set"] = value
    return titles


def _prune_outlier_source_sets(indices: List[int], title_info: Dict[int, Dict]):
    """Drop titles that sit ALONE in their own source set when a clear real-episode
    cluster exists. Returns (kept_indices, dropped_indices).

    Full-length bonus features — animatic/storyboard versions of an episode, cell-scramble
    copy-protection decoys — run the SAME ~22 min as a real episode, so the duration floor
    can't catch them. But a disc's real episodes all come from ONE MakeMKV source set (VTS),
    while each such bonus lives in its own. So when one source set holds the bulk of the
    titles (>= MIN_CLUSTER_TO_PRUNE), any source set contributing just a single title is
    almost certainly a bonus, and is dropped.

    Guards against false drops:
    - If no source set reaches MIN_CLUSTER_TO_PRUNE (e.g. a disc that legitimately spreads
      episodes one-per-source-set), nothing is pruned.
    - A second genuine cluster (>= 2 titles) is kept — only true singletons are dropped.
    - Titles with an unknown source set are never dropped.
    """
    counts: Dict[str, int] = {}
    for idx in indices:
        ss = title_info.get(idx, {}).get("source_set")
        if ss:
            counts[ss] = counts.get(ss, 0) + 1
    if not counts or max(counts.values()) < MIN_CLUSTER_TO_PRUNE:
        return list(indices), []
    kept, dropped = [], []
    for idx in indices:
        ss = title_info.get(idx, {}).get("source_set")
        if ss and counts.get(ss, 0) == 1:
            dropped.append(idx)
        else:
            kept.append(idx)
    return kept, dropped


def _parse_title_index(filename: str) -> int:
    """Extract title index from MakeMKV output name like 'title_t03.mkv' or 'DISC NAME_t03.mkv'."""
    m = re.search(r"_t(\d+)", filename)
    return int(m.group(1)) if m else -1


def rip(volume_path: Path, temp_dir: Path, max_title_secs: int = None) -> List[Dict]:
    """
    Rip eligible titles from the disc to temp_dir.
    Returns list of dicts: [{path, duration_secs, title_index}].
    Titles below MIN_TITLE_DURATION_SECS are excluded BEFORE ripping (skips
    menus/extras), as is the TV "Play All" combined title.
    max_title_secs (from the episode guide: longest real episode × omnibus factor)
    also excludes over-long titles BEFORE ripping — a title no single episode can
    fill is a 'Play All'/omnibus chunk, and ripping one wastes an hour just to
    drop it post-rip. Titles with unknown duration are never excluded by it.
    Raises RipError on failure.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: get title metadata. Right after insertion the drive may still be
    # spinning up, so makemkvcon can return an empty list — retry a few times
    # before giving up rather than failing the whole disc on a cold drive.
    log.info("Querying disc info...")
    title_info: Dict[int, Dict] = {}
    for attempt in range(1, INFO_SCAN_ATTEMPTS + 1):
        info_result = subprocess.run(
            ["makemkvcon", "-r", "info", "disc:0"],
            capture_output=True,
            text=True,
        )
        title_info = _parse_info(info_result.stdout)
        if title_info:
            break
        if attempt < INFO_SCAN_ATTEMPTS:
            log.warning(
                f"No titles read from disc (attempt {attempt}/{INFO_SCAN_ATTEMPTS}) — "
                f"drive may still be spinning up; retrying in {INFO_SCAN_RETRY_SECS}s"
            )
            time.sleep(INFO_SCAN_RETRY_SECS)

    # Filter titles by minimum duration (drop menus/extras)
    eligible_indices = []
    for idx, info in sorted(title_info.items()):
        dur = info.get("duration_secs", 0)
        if dur == 0:
            log.info(f"Title #{idx}: unknown duration, including anyway")
            eligible_indices.append(idx)
        elif dur < MIN_TITLE_DURATION_SECS:
            log.info(f"Skipping title #{idx} (duration {dur}s < {MIN_TITLE_DURATION_SECS}s — extra/menu)")
        elif max_title_secs and dur > max_title_secs:
            log.info(f"Skipping title #{idx} (duration {dur}s > {max_title_secs}s — "
                     "longer than any episode, 'Play All'/omnibus)")
        else:
            eligible_indices.append(idx)

    # Drop full-length bonus features (animatics, cell-scramble decoys) that the duration
    # floor can't catch: they occupy their own source set while a disc's real episodes share
    # one. See _prune_outlier_source_sets for the guards against false drops.
    pruned, dropped = _prune_outlier_source_sets(eligible_indices, title_info)
    for idx in sorted(dropped):
        ss = title_info.get(idx, {}).get("source_set")
        dur = title_info.get(idx, {}).get("duration_secs", 0)
        log.info(f"Skipping title #{idx} (source set {ss}, lone outlier — likely bonus/animatic, {dur}s)")
    eligible_indices = pruned

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
            # --progress=-same sends the percentage to our stdout. Without it
            # makemkvcon sees a non-TTY and prints no progress at all, so the log
            # has nothing for the dashboard to show a rip bar from.
            ["makemkvcon", "--progress=-same", "mkv", "disc:0", str(idx), str(temp_dir)],
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

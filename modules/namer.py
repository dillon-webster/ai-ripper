import json
import logging
import re
from pathlib import Path
from typing import Dict, List

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


class NamerError(Exception):
    pass


def _duration_hms(secs: int) -> str:
    """Convert seconds to 'H:MM:SS' string."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _build_prompt(volume_name: str, titles: List[Dict], existing_episodes: List[str] = None) -> str:
    title_list = [
        {
            "index": t["title_index"],
            "filename": t["path"].name,
            "duration": _duration_hms(t["duration_secs"]),
        }
        for t in titles
    ]
    existing_section = ""
    if existing_episodes:
        existing_section = (
            "\nFiles already on the server:\n"
            + "\n".join(f"  {e}" for e in sorted(existing_episodes))
            + "\n"
        )

    return f"""You are identifying content from a DVD/Blu-ray disc for Jellyfin media server organization.

Disc volume label: {volume_name}
Titles on disc:
{json.dumps(title_list, indent=2)}
{existing_section}

For each title, determine what movie or TV show episode it contains and return the correct Jellyfin-compatible filename.

IMPORTANT for multi-disc TV sets:
- The disc volume label encodes the show, season, and disc number (e.g. "FRIENDS SEASON 2-A1" means Friends Season 2 Disc 1).
- Look at the existing files on the server to determine the correct episode number(s) for this disc:
  - FIRST check for gaps in the existing file list — episode numbers missing between the lowest and highest existing number in this season. If the disc title's duration and position make it a plausible fit for a gap, use that gap's episode number (e.g. existing files are S02E07, S02E09, S02E10 → S02E08 is the gap; a single new title from a Season 2 disc likely belongs at S02E08).
  - If there are no gaps (or this title clearly comes after the highest existing one), continue numbering from the next one (e.g. if S01E12 exists, start at S01E13).
  - If this disc is a new season that has no episodes yet, start at E01 of that season (e.g. if only S01 exists and this disc is Season 2, start at S02E01).
- Never reuse an episode number that already exists in the server file list above.

IMPORTANT for episode ordering: Titles are listed in disc playback order (first episode first). Assign episode
numbers sequentially in the order they are presented — do NOT reorder them.

IMPORTANT for double-length / two-part episodes:
- A single title whose duration is roughly DOUBLE the typical episode length on this disc (e.g. ~44+ min when the
  other episodes run ~22 min) is almost always a special that aired as one feature-length episode but that episode
  databases (TheTVDB, which Jellyfin matches against) count as TWO consecutive episode numbers (e.g. Friends Season 2
  "The One After the Super Bowl" is one file but is episodes 12 AND 13).
- Name that single file with a COMBINED episode range so Jellyfin maps the one file onto both slots:
  Show.Name.S02E12-E13.mkv (one file, hyphenated range, both numbers zero-padded).
- Such a title CONSUMES BOTH episode numbers. Continue numbering the following titles after the END of the range
  (e.g. after S02E12-E13 the next title is S02E14, NOT S02E13). Getting this wrong shifts every later episode.

Return ONLY a valid JSON array with no other text, markdown, or explanation:
[
  {{
    "index": <title_index as integer>,
    "jellyfin_filename": "<name>.mkv",
    "media_type": "movie" or "tv",
    "destination": "movies" or "tvshows"
  }}
]

Jellyfin filename conventions:
- TV shows: Show.Name.S01E01.mkv (use dots not spaces, season+episode zero-padded)
- Double-length episode spanning two numbers: Show.Name.S02E12-E13.mkv (single file, hyphenated range)
- Movies: Movie.Name.2023.mkv (include year if known, use dots not spaces)"""


def _strip_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```) if present."""
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    return m.group(1) if m else text


def identify(volume_name: str, titles: List[Dict], api_key: str, existing_episodes: List[str] = None) -> List[Dict]:
    """
    Call Anthropic API to identify titles and generate Jellyfin-compatible filenames.
    Returns original title dicts merged with naming fields.
    Raises NamerError if JSON parsing fails after one retry.
    """
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(volume_name, titles, existing_episodes)

    messages = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=messages,
    )
    raw = response.content[0].text.strip()

    naming_data = None
    try:
        naming_data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        log.warning("Malformed JSON from Anthropic; retrying with correction prompt")
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": "Your response was not valid JSON. Return ONLY the JSON array with no other text.",
        })
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=messages,
        )
        raw2 = retry_response.content[0].text.strip()
        try:
            naming_data = json.loads(_strip_fences(raw2))
        except json.JSONDecodeError as e:
            raise NamerError(f"Failed to parse naming response after retry: {e}") from e

    naming_by_index = {entry["index"]: entry for entry in naming_data}

    result = []
    for t in titles:
        idx = t["title_index"]
        if idx in naming_by_index:
            result.append({**t, **naming_by_index[idx]})
        else:
            log.warning(f"Title index {idx} not found in naming response; skipping")

    if not result:
        raise NamerError("No titles were matched in the naming API response")

    return result

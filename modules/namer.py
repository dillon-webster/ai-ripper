import json
import logging
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


def _build_prompt(volume_name: str, titles: List[Dict]) -> str:
    title_list = [
        {
            "index": t["title_index"],
            "filename": t["path"].name,
            "duration": _duration_hms(t["duration_secs"]),
        }
        for t in titles
    ]
    return f"""You are identifying content from a DVD/Blu-ray disc for Jellyfin media server organization.

Disc volume label: {volume_name}
Titles on disc:
{json.dumps(title_list, indent=2)}

For each title, determine what movie or TV show episode it contains and return the correct Jellyfin-compatible filename.

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
- Movies: Movie.Name.2023.mkv (include year if known, use dots not spaces)"""


def identify(volume_name: str, titles: List[Dict], api_key: str) -> List[Dict]:
    """
    Call Anthropic API to identify titles and generate Jellyfin-compatible filenames.
    Returns original title dicts merged with naming fields.
    Raises NamerError if JSON parsing fails after one retry.
    """
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(volume_name, titles)

    messages = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=messages,
    )
    raw = response.content[0].text.strip()

    naming_data = None
    try:
        naming_data = json.loads(raw)
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
            naming_data = json.loads(raw2)
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

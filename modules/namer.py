import json
import logging
import re
from typing import Dict, List

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"


class NamerError(Exception):
    pass


def _duration_hms(secs: int) -> str:
    """Convert seconds to 'H:MM:SS' string."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _show_key(name: str) -> str:
    """Normalize a show label to a comparison key: lowercase alphanumerics only.
    'Family.Guy' and 'FAMILY_GUY' both become 'familyguy'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _scope_existing(existing: List[str], volume_name: str, season: int) -> List[str]:
    """Filter existing server filenames down to the SAME show (per the volume label)
    and the SAME season. Without this the namer sees every show/season at once and
    counts episode numbers across the boundary (e.g. Season 1's 7 episodes made a
    Season 2 disc start at E08 instead of E01)."""
    # Isolate the show name from the volume label by stripping the numbering tokens
    # discs carry — DISC/D, VOLUME/VOL, SEASON/SERIES/S — wherever they appear, not
    # just at the end. A trailing-only strip left "FAMILY_GUY_VOLUME_13_DISC_2" as
    # "familyguyvolume13", which never matched "familyguy" and silently discarded
    # every existing episode, restarting numbering at E01.
    show_label = re.sub(
        r"[_\s-]*\b(disc|d|volume|vol|season|series|s)\b[_\s-]*\d+",
        " ", volume_name, flags=re.IGNORECASE,
    )
    want_show = _show_key(show_label)
    want_tag = f"S{season:02d}E"
    scoped = []
    for e in existing:
        m = re.match(r"^(.*?)\.S\d{2}E", e, re.IGNORECASE)
        if not m or want_tag not in e.upper():
            continue
        file_show = _show_key(m.group(1))
        # Tolerate leftover label tokens by accepting a prefix-subset match in either
        # direction, not just equality (e.g. label "familyguyvolume13" vs file
        # "familyguy"). Guards against matching a different show while surviving a
        # label that still carries extra words.
        if file_show and want_show and (
            file_show == want_show
            or file_show.startswith(want_show)
            or want_show.startswith(file_show)
        ):
            scoped.append(e)
    return scoped


def _episode_guide_section(episode_guide: List[Dict], season: int = None) -> str:
    """Render the authoritative Jellyfin episode list into a prompt section that
    constrains the model to real episode numbers and blocks invented ranges."""
    if not episode_guide:
        return ""
    lines = []
    for e in episode_guide:
        rng = f"E{e['index']:02d}" + (f"-E{e['index_end']:02d}" if e.get("index_end") else "")
        nm = f' "{e["name"]}"' if e.get("name") else ""
        dur = f" (~{round(e['runtime_secs'] / 60)} min)" if e.get("runtime_secs") else ""
        lines.append(f"  {rng}{nm}{dur}")
    season_lbl = f"Season {season}" if season is not None else "this season"
    return (
        f"\nAUTHORITATIVE EPISODE LIST for {season_lbl} (from Jellyfin — these are the ONLY "
        f"episodes that exist for this season):\n"
        + "\n".join(lines)
        + "\nRules using this list:\n"
        "- NEVER assign an episode number that is not in this list. If the list ends at E06 there "
        "is no E07/E08 — a leftover long title is a 'Play All'/compilation title, not a new episode.\n"
        "- Only name a title as a hyphenated double range (e.g. S01E05-E06) if a listed episode above "
        "shows a spanning range. If NO listed episode spans two numbers, a ~double-length title is a "
        "'Play All' compilation, NOT two episodes — do not invent a range for it.\n"
        "- Match each ripped title to one listed episode using its duration and the playback order.\n"
    )


def _build_prompt(volume_name: str, titles: List[Dict], existing_episodes: List[str] = None,
                  season: int = None, disc: int = None, show: str = None,
                  episode_guide: List[Dict] = None) -> str:
    title_list = [
        {
            "index": t["title_index"],
            "filename": t["path"].name,
            "duration": _duration_hms(t["duration_secs"]),
        }
        for t in titles
    ]
    shown_existing = list(existing_episodes) if existing_episodes else []
    if season is not None and shown_existing:
        shown_existing = _scope_existing(shown_existing, volume_name, season)

    existing_section = ""
    if shown_existing:
        scope_note = f" for Season {season} of this show" if season is not None else ""
        existing_section = (
            f"\nFiles already on the server{scope_note}:\n"
            + "\n".join(f"  {e}" for e in sorted(shown_existing))
            + "\n"
        )

    override_section = ""
    if season is not None:
        disc_line = (
            f"- This is DISC {disc} of that season; earlier discs hold earlier episodes.\n"
            if disc is not None else ""
        )
        override_section = (
            "\nMANUAL OVERRIDE (authoritative — trust this over the volume label and any inference):\n"
            f"- Every TV title on this disc belongs to SEASON {season} of the show named by the volume label.\n"
            f"- Use season number {season:02d} (e.g. S{season:02d}E01). Do NOT infer the season from the "
            "volume label or the existing files — the label may omit or misstate the season.\n"
            f"{disc_line}"
            f"- The listed server files (if any) are ONLY Season {season} of this show. Number episodes "
            f"WITHIN Season {season}: continue after the highest listed episode, or fill a gap in it.\n"
            f"- If NO Season {season} files are listed, this is the first disc of the season — start at E01. "
            "Do NOT carry a running count over from an earlier season.\n"
        )

    show_section = ""
    if show:
        show_section = (
            f"\nShow (authoritative — this IS the show; use this exact name and ignore the "
            f"volume label for the show name): {show}\n"
        )

    guide_section = _episode_guide_section(episode_guide, season)

    # The generic "double length -> two episode numbers" heuristic is WRONG for providers
    # (like TMDB) that count a feature-length episode as a single number, and it invents
    # phantom episodes on 'Play All' titles. When we have the real episode list from Jellyfin,
    # that list governs instead (see _episode_guide_section), so drop this heuristic entirely.
    double_section = "" if episode_guide else """
IMPORTANT for double-length / two-part episodes:
- A single title whose duration is roughly DOUBLE the typical episode length on this disc (e.g. ~44+ min when the
  other episodes run ~22 min) is almost always a special that aired as one feature-length episode but that episode
  databases (TheTVDB, which Jellyfin matches against) count as TWO consecutive episode numbers (e.g. Friends Season 2
  "The One After the Super Bowl" is one file but is episodes 12 AND 13).
- Name that single file with a COMBINED episode range so Jellyfin maps the one file onto both slots:
  Show.Name.S02E12-E13.mkv (one file, hyphenated range, both numbers zero-padded).
- Such a title CONSUMES BOTH episode numbers. Continue numbering the following titles after the END of the range
  (e.g. after S02E12-E13 the next title is S02E14, NOT S02E13). Getting this wrong shifts every later episode."""

    return f"""You are identifying content from a DVD/Blu-ray disc for Jellyfin media server organization.

Disc volume label: {volume_name}
{show_section}{override_section}
Titles on disc:
{json.dumps(title_list, indent=2)}
{existing_section}{guide_section}

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

IMPORTANT for movie discs (bonus content):
- A movie disc usually has ONE main feature (the film itself, typically 70+ min) plus bonus content:
  director/cast commentary tracks (often the SAME length as the film), featurettes, "making of"
  documentaries, deleted scenes, and gag reels.
- Set "is_extra": true for every movie title that is NOT the main feature. Set "is_extra": false for the
  main feature. When two titles have the same duration (the film + a commentary version), the commentary
  version is the extra — keep only ONE as the main feature.
- Use duration and the title filename as clues. This flag applies to MOVIES ONLY.
- For TV episodes, ALWAYS set "is_extra": false. Every episode is real content that must be kept.
{double_section}

Return ONLY a valid JSON array with no other text, markdown, or explanation:
[
  {{
    "index": <title_index as integer>,
    "jellyfin_filename": "<name>.mkv",
    "media_type": "movie" or "tv",
    "destination": "movies" or "tvshows",
    "is_extra": true or false
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


def identify(volume_name: str, titles: List[Dict], api_key: str, existing_episodes: List[str] = None,
             season: int = None, disc: int = None, show: str = None,
             episode_guide: List[Dict] = None) -> List[Dict]:
    """
    Call Anthropic API to identify titles and generate Jellyfin-compatible filenames.
    Returns original title dicts merged with naming fields.
    When `season` is given it is authoritative — the model uses it verbatim instead of
    inferring the season from the (possibly season-less) volume label.
    When `show` is given it is the authoritative show name (overrides the volume label).
    When `episode_guide` is given (the real Jellyfin episode list) it constrains the model
    to real episode numbers and disables the phantom-generating double-length heuristic.
    Raises NamerError if JSON parsing fails after one retry.
    """
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(volume_name, titles, existing_episodes, season=season, disc=disc,
                           show=show, episode_guide=episode_guide)

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

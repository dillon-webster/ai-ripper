"""Fetch the real episode list for a show/season from Jellyfin.

Phase 1 of the content-based identification plan
(docs/episode-identification-plan.md): give the namer an authoritative episode
list so it stops inventing episode numbers past the end of a season (the phantom
S01E07-E08 bug) and only names a title as a double-length range when Jellyfin
actually has a spanning episode.

Uses stdlib urllib (same as modules/notifier.py) — no new dependency.
"""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10
_TICKS_PER_SEC = 10_000_000  # Jellyfin RunTimeTicks are 100-ns units


class EpisodeGuideError(Exception):
    pass


def _norm(name: str) -> str:
    """Lowercase alphanumerics only, for tolerant show-name comparison.
    'The Office' and 'the.office' both become 'theoffice'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _get_json(url: str, api_key: str) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"X-Emby-Token": api_key})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, ValueError) as e:
        raise EpisodeGuideError(f"Jellyfin request failed ({url}): {e}") from e


def find_series_id(show_name: str, config) -> Optional[str]:
    """Resolve a show name to its Jellyfin series id. Prefers an exact
    (normalized) name match; falls back to the first search result. Returns
    None if nothing matches."""
    query = urllib.parse.urlencode({
        "IncludeItemTypes": "Series",
        "Recursive": "true",
        "SearchTerm": show_name,
        "fields": "ProductionYear",
    })
    data = _get_json(f"{config.jellyfin_url}/Items?{query}", config.jellyfin_api_key)
    items = data.get("Items", [])
    if not items:
        return None
    want = _norm(show_name)
    for it in items:
        if _norm(it.get("Name", "")) == want:
            return it.get("Id")
    return items[0].get("Id")


def get_season_episodes(show_name: str, season: int, config) -> List[Dict]:
    """Return the real episode list for `show_name` season `season` from Jellyfin:
    [{"index", "index_end", "name", "runtime_secs"}], sorted by episode number.

    Unnumbered items (specials with no IndexNumber) are skipped.
    Raises EpisodeGuideError if the show can't be found or a request fails.
    """
    series_id = find_series_id(show_name, config)
    if not series_id:
        raise EpisodeGuideError(f"Show not found in Jellyfin: {show_name!r}")

    query = urllib.parse.urlencode({"season": season, "fields": "RunTimeTicks"})
    data = _get_json(
        f"{config.jellyfin_url}/Shows/{series_id}/Episodes?{query}",
        config.jellyfin_api_key,
    )
    episodes = []
    for it in data.get("Items", []):
        idx = it.get("IndexNumber")
        if idx is None:
            continue  # specials / unnumbered — not a numbered episode slot
        ticks = it.get("RunTimeTicks")
        episodes.append({
            "index": idx,
            "index_end": it.get("IndexNumberEnd"),
            "name": it.get("Name"),
            "runtime_secs": int(ticks // _TICKS_PER_SEC) if ticks else None,
        })
    episodes.sort(key=lambda e: e["index"])
    return episodes

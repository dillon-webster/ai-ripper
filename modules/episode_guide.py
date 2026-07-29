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
    """Return the real episode list for `show_name` season `season`:
    [{"season", "index", "index_end", "name", "runtime_secs", "overview"}], sorted by
    episode number.

    Every entry carries its `season` so lists for two seasons can be concatenated into
    one candidate pool (a volume disc that straddles a boundary) without E10 from one
    season being mistaken for E10 of the other. See identify.episode_key.

    Prefers **TMDB** when `config.tmdb_api_key` is set — TMDB has every season whether
    or not it's in your library, so content-ID works when ripping a NEW season (Jellyfin
    only knows seasons you've already added). Falls back to Jellyfin when there's no TMDB
    key, or TMDB errors / returns nothing.

    `overview` is the plot summary — content-based identification (modules/identify.py)
    matches OCR'd dialogue against it, which disambiguates episodes whose terse
    "The One with..." titles don't name the plot in the sampled scene.

    Raises EpisodeGuideError only if BOTH sources fail (or Jellyfin fails with no TMDB key).
    """
    return [{**e, "season": season} for e in _fetch_season(show_name, season, config)]


def _fetch_season(show_name: str, season: int, config) -> List[Dict]:
    if getattr(config, "tmdb_api_key", ""):
        try:
            episodes = _tmdb_season_episodes(show_name, season, config)
            if episodes:
                log.info(f"Episode guide from TMDB: {len(episodes)} episode(s)")
                return episodes
            log.warning(f"TMDB has no episodes for {show_name!r} S{season} — trying Jellyfin")
        except EpisodeGuideError as e:
            log.warning(f"TMDB guide failed ({e}); falling back to Jellyfin")
    return _jellyfin_season_episodes(show_name, season, config)


def _jellyfin_season_episodes(show_name: str, season: int, config) -> List[Dict]:
    """Episode list from the Jellyfin library. Unnumbered items (specials with no
    IndexNumber) are skipped. Raises EpisodeGuideError if the show can't be found."""
    series_id = find_series_id(show_name, config)
    if not series_id:
        raise EpisodeGuideError(f"Show not found in Jellyfin: {show_name!r}")

    query = urllib.parse.urlencode({"season": season, "fields": "Overview,RunTimeTicks"})
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
            "overview": it.get("Overview"),
        })
    episodes.sort(key=lambda e: e["index"])
    return episodes


# ---------------------------------------------------------------------------
# TMDB source (has every season, even ones not yet in your library)
# ---------------------------------------------------------------------------

_TMDB_BASE = "https://api.themoviedb.org/3"


def _tmdb_get(path: str, api_key: str, **params) -> dict:
    params["api_key"] = api_key
    url = f"{_TMDB_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, ValueError) as e:
        raise EpisodeGuideError(f"TMDB request failed ({_TMDB_BASE}{path}): {e}") from e


def find_series_id_tmdb(show_name: str, config) -> Optional[str]:
    """Resolve a show name to its TMDB series id. Prefers an exact (normalized) name
    match; TMDB sorts results by popularity, so among ties this picks the most popular
    (e.g. 'The Office' → the US series, not the UK one). None if nothing matches."""
    data = _tmdb_get("/search/tv", config.tmdb_api_key, query=show_name)
    results = data.get("results", [])
    if not results:
        return None
    want = _norm(show_name)
    exact = [r for r in results if _norm(r.get("name", "")) == want]
    return str((exact or results)[0].get("id"))


def _tmdb_season_episodes(show_name: str, season: int, config) -> List[Dict]:
    """Episode list for a season from TMDB, in the same shape as the Jellyfin source.
    TMDB has no double-episode ranges, so index_end is always None. When an episode's
    runtime is missing, falls back to the show's average episode run time."""
    series_id = find_series_id_tmdb(show_name, config)
    if not series_id:
        raise EpisodeGuideError(f"Show not found on TMDB: {show_name!r}")

    default_runtime = None
    try:
        show = _tmdb_get(f"/tv/{series_id}", config.tmdb_api_key)
        run_times = show.get("episode_run_time") or []
        if run_times:
            default_runtime = run_times[0]
    except EpisodeGuideError:
        pass  # runtime default is best-effort; matching still works without it

    data = _tmdb_get(f"/tv/{series_id}/season/{season}", config.tmdb_api_key)
    episodes = []
    for it in data.get("episodes", []):
        idx = it.get("episode_number")
        if idx is None:
            continue
        runtime = it.get("runtime") or default_runtime
        episodes.append({
            "index": idx,
            "index_end": None,
            "name": it.get("name"),
            "runtime_secs": int(runtime * 60) if runtime else None,
            "overview": it.get("overview") or None,
        })
    episodes.sort(key=lambda e: e["index"])
    return episodes

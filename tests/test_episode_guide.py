import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules import episode_guide
from modules.episode_guide import (
    EpisodeGuideError,
    find_series_id,
    get_season_episodes,
    _norm,
)

CONFIG = SimpleNamespace(jellyfin_url="http://jf.local", jellyfin_api_key="tok")


def _resp(payload):
    """A fake urlopen() context manager whose read() returns JSON bytes."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    cm.__exit__.return_value = False
    return cm


def test_norm():
    assert _norm("The Office") == "theoffice"
    assert _norm("the.office") == "theoffice"
    assert _norm("FAMILY_GUY") == "familyguy"


def test_find_series_id_prefers_exact_match():
    payload = {"Items": [
        {"Name": "The Office (UK)", "Id": "uk"},
        {"Name": "The Office", "Id": "us"},
    ]}
    with patch("modules.episode_guide.urllib.request.urlopen", return_value=_resp(payload)):
        assert find_series_id("The Office", CONFIG) == "us"


def test_find_series_id_falls_back_to_first():
    payload = {"Items": [{"Name": "The Office (UK)", "Id": "uk"}]}
    with patch("modules.episode_guide.urllib.request.urlopen", return_value=_resp(payload)):
        assert find_series_id("The Office", CONFIG) == "uk"


def test_find_series_id_none_when_empty():
    with patch("modules.episode_guide.urllib.request.urlopen", return_value=_resp({"Items": []})):
        assert find_series_id("Nonexistent", CONFIG) is None


def test_get_season_episodes_parses_and_sorts():
    series = {"Items": [{"Name": "The Office", "Id": "us"}]}
    episodes = {"Items": [
        {"IndexNumber": 2, "Name": "Diversity Day", "RunTimeTicks": 13320000000},  # 22.2 min
        {"IndexNumber": 1, "Name": "Pilot", "Overview": "Michael films a documentary.",
         "RunTimeTicks": 14040000000},                                             # 23.4 min
        {"IndexNumber": None, "Name": "A Special", "RunTimeTicks": 10000000},       # skipped
    ]}
    with patch("modules.episode_guide.urllib.request.urlopen",
               side_effect=[_resp(series), _resp(episodes)]):
        result = get_season_episodes("The Office", 1, CONFIG)

    assert [e["index"] for e in result] == [1, 2]  # sorted, special dropped
    # Every entry carries its season, so two seasons' lists can be pooled for a
    # volume disc without E10 of one being mistaken for E10 of the other.
    assert result[0] == {"season": 1, "index": 1, "index_end": None, "name": "Pilot",
                         "runtime_secs": 1404, "overview": "Michael films a documentary."}
    assert result[1]["name"] == "Diversity Day"
    assert result[1]["runtime_secs"] == 1332
    assert result[1]["overview"] is None  # missing Overview → None, not an error


def test_get_season_episodes_preserves_index_end():
    series = {"Items": [{"Name": "Friends", "Id": "f"}]}
    episodes = {"Items": [
        {"IndexNumber": 12, "IndexNumberEnd": 13, "Name": "The One After the Super Bowl",
         "RunTimeTicks": 26400000000},
    ]}
    with patch("modules.episode_guide.urllib.request.urlopen",
               side_effect=[_resp(series), _resp(episodes)]):
        result = get_season_episodes("Friends", 2, CONFIG)
    assert result[0]["index"] == 12
    assert result[0]["index_end"] == 13


def test_get_season_episodes_raises_when_show_missing():
    with patch("modules.episode_guide.urllib.request.urlopen", return_value=_resp({"Items": []})):
        with pytest.raises(EpisodeGuideError, match="not found"):
            get_season_episodes("Ghost Show", 1, CONFIG)


def test_get_json_wraps_url_error():
    import urllib.error
    with patch("modules.episode_guide.urllib.request.urlopen",
               side_effect=urllib.error.URLError("boom")):
        with pytest.raises(EpisodeGuideError, match="Jellyfin request failed"):
            find_series_id("The Office", CONFIG)


# --- TMDB source -----------------------------------------------------------

TMDB_CONFIG = SimpleNamespace(jellyfin_url="http://jf.local", jellyfin_api_key="tok",
                              tmdb_api_key="tmdb-key")


def test_find_series_id_tmdb_prefers_exact_name_over_popular_partial():
    # TMDB orders by popularity; we still want the exact-name match (the US Office).
    payload = {"results": [
        {"name": "The Office (UK)", "id": 111},
        {"name": "The Office", "id": 2316},
    ]}
    with patch("modules.episode_guide.urllib.request.urlopen", return_value=_resp(payload)):
        assert episode_guide.find_series_id_tmdb("The Office", TMDB_CONFIG) == "2316"


def test_tmdb_season_episodes_parses_and_defaults_runtime():
    search = {"results": [{"name": "The Office", "id": 2316}]}
    show = {"episode_run_time": [22]}
    season = {"episodes": [
        {"episode_number": 2, "name": "Sexual Harassment", "overview": "ob", "runtime": None},
        {"episode_number": 1, "name": "The Dundies", "overview": "oa", "runtime": 21},
    ]}
    with patch("modules.episode_guide.urllib.request.urlopen",
               side_effect=[_resp(search), _resp(show), _resp(season)]):
        eps = episode_guide._tmdb_season_episodes("The Office", 2, TMDB_CONFIG)
    assert [e["index"] for e in eps] == [1, 2]              # sorted
    assert eps[0]["runtime_secs"] == 21 * 60               # from the episode
    assert eps[1]["runtime_secs"] == 22 * 60               # fell back to show default
    assert eps[0]["index_end"] is None
    assert eps[0]["name"] == "The Dundies"


_JF = [{"index": 1, "index_end": None, "name": "The Dundies",
        "runtime_secs": 1320, "overview": "x"}]


def test_get_season_episodes_prefers_tmdb_when_key_set():
    with patch("modules.episode_guide._tmdb_season_episodes", return_value=_JF) as tmdb, \
         patch("modules.episode_guide._jellyfin_season_episodes") as jf:
        assert get_season_episodes("The Office", 2, TMDB_CONFIG) == [{**_JF[0], "season": 2}]
        tmdb.assert_called_once()
        jf.assert_not_called()  # Jellyfin not even consulted


def test_get_season_episodes_falls_back_to_jellyfin_when_tmdb_empty():
    with patch("modules.episode_guide._tmdb_season_episodes", return_value=[]), \
         patch("modules.episode_guide._jellyfin_season_episodes", return_value=_JF) as jf:
        assert get_season_episodes("The Office", 2, TMDB_CONFIG) == [{**_JF[0], "season": 2}]
        jf.assert_called_once()


def test_get_season_episodes_falls_back_to_jellyfin_when_tmdb_errors():
    with patch("modules.episode_guide._tmdb_season_episodes",
               side_effect=EpisodeGuideError("tmdb down")), \
         patch("modules.episode_guide._jellyfin_season_episodes", return_value=_JF) as jf:
        assert get_season_episodes("The Office", 2, TMDB_CONFIG) == [{**_JF[0], "season": 2}]
        jf.assert_called_once()


def test_get_season_episodes_uses_jellyfin_when_no_tmdb_key():
    with patch("modules.episode_guide._tmdb_season_episodes") as tmdb, \
         patch("modules.episode_guide._jellyfin_season_episodes", return_value=_JF) as jf:
        # CONFIG has no tmdb key
        assert get_season_episodes("The Office", 2, CONFIG) == [{**_JF[0], "season": 2}]
        tmdb.assert_not_called()
        jf.assert_called_once()


def test_get_season_episodes_tags_every_entry_with_its_season():
    """The season tag is what lets two lists be concatenated into one candidate pool
    for a volume disc — without it E10 of S04 and E10 of S05 are indistinguishable."""
    two = [{"index": 10, "name": "a"}, {"index": 11, "name": "b"}]
    with patch("modules.episode_guide._tmdb_season_episodes", return_value=two):
        eps = get_season_episodes("Family Guy", 5, TMDB_CONFIG)
    assert [e["season"] for e in eps] == [5, 5]

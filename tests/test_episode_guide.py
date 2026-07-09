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
        {"IndexNumber": 1, "Name": "Pilot", "RunTimeTicks": 14040000000},          # 23.4 min
        {"IndexNumber": None, "Name": "A Special", "RunTimeTicks": 10000000},       # skipped
    ]}
    with patch("modules.episode_guide.urllib.request.urlopen",
               side_effect=[_resp(series), _resp(episodes)]):
        result = get_season_episodes("The Office", 1, CONFIG)

    assert [e["index"] for e in result] == [1, 2]  # sorted, special dropped
    assert result[0] == {"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}
    assert result[1]["name"] == "Diversity Day"
    assert result[1]["runtime_secs"] == 1332


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

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from modules.namer import _duration_hms, identify, NamerError


def test_duration_hms_formatting():
    assert _duration_hms(6127) == "1:42:07"
    assert _duration_hms(1335) == "0:22:15"
    assert _duration_hms(3600) == "1:00:00"


def _make_titles(tmp_path):
    f1 = tmp_path / "title_t00.mkv"
    f2 = tmp_path / "title_t01.mkv"
    f1.write_bytes(b"")
    f2.write_bytes(b"")
    return [
        {"path": f1, "duration_secs": 1320, "title_index": 0},
        {"path": f2, "duration_secs": 1290, "title_index": 1},
    ]


def _mock_anthropic_response(text: str):
    mock_client = MagicMock()
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=text)]
    mock_client.messages.create.return_value = mock_message
    return mock_client


def test_identify_returns_named_titles(tmp_path):
    titles = _make_titles(tmp_path)
    api_response = (
        '[{"index": 0, "jellyfin_filename": "Friends.S01E01.mkv", "media_type": "tv", "destination": "tvshows"},'
        ' {"index": 1, "jellyfin_filename": "Friends.S01E02.mkv", "media_type": "tv", "destination": "tvshows"}]'
    )
    mock_client = _mock_anthropic_response(api_response)

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        result = identify("FRIENDS_S1D1", titles, "sk-ant-test")

    assert len(result) == 2
    assert result[0]["jellyfin_filename"] == "Friends.S01E01.mkv"
    assert result[0]["destination"] == "tvshows"
    assert result[0]["path"] == titles[0]["path"]
    assert result[1]["jellyfin_filename"] == "Friends.S01E02.mkv"


def test_identify_retries_on_malformed_json(tmp_path):
    titles = _make_titles(tmp_path)
    bad_response = "Here is the JSON: [invalid json"
    good_response = '[{"index": 0, "jellyfin_filename": "Friends.S01E01.mkv", "media_type": "tv", "destination": "tvshows"}]'

    mock_client = MagicMock()
    call_count = 0

    def fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=bad_response if call_count == 1 else good_response)]
        return mock_message

    mock_client.messages.create.side_effect = fake_create

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        result = identify("FRIENDS_S1D1", titles, "sk-ant-test")

    assert call_count == 2
    assert len(result) == 1
    assert result[0]["jellyfin_filename"] == "Friends.S01E01.mkv"


def test_identify_raises_after_two_malformed_responses(tmp_path):
    titles = _make_titles(tmp_path)
    mock_client = _mock_anthropic_response("not json at all")

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        with pytest.raises(NamerError, match="parse"):
            identify("FRIENDS_S1D1", titles, "sk-ant-test")

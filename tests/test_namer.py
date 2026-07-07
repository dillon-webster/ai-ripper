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


def test_prompt_instructs_double_length_episode_range():
    from modules.namer import _build_prompt

    titles = [
        {"path": Path("title_t00.mkv"), "duration_secs": 2880, "title_index": 0},
        {"path": Path("title_t01.mkv"), "duration_secs": 1320, "title_index": 1},
    ]
    prompt = _build_prompt("FRIENDS SEASON 2-A2", titles)

    # The prompt must teach the model to merge a double-length title into a
    # hyphenated episode range and to skip the consumed number afterward.
    assert "S02E12-E13.mkv" in prompt
    assert "CONSUMES BOTH episode numbers" in prompt


def test_prompt_includes_season_override():
    from modules.namer import _build_prompt

    titles = [{"path": Path("title_t00.mkv"), "duration_secs": 1320, "title_index": 0}]
    prompt = _build_prompt("FAMILY_GUY_DISC1", titles, season=1, disc=1)

    # An explicit season must be stated as authoritative and override label inference.
    assert "MANUAL OVERRIDE" in prompt
    assert "SEASON 1" in prompt
    assert "S01E01" in prompt
    assert "DISC 1" in prompt
    assert "Do NOT infer the season" in prompt


def test_prompt_omits_override_when_no_season():
    from modules.namer import _build_prompt

    titles = [{"path": Path("title_t00.mkv"), "duration_secs": 1320, "title_index": 0}]
    prompt = _build_prompt("FRIENDS SEASON 2-A1", titles)

    assert "MANUAL OVERRIDE" not in prompt


def test_identify_forwards_season_to_prompt(tmp_path):
    titles = _make_titles(tmp_path)
    mock_client = _mock_anthropic_response(
        '[{"index": 0, "jellyfin_filename": "Family.Guy.S01E01.mkv", "media_type": "tv", "destination": "tvshows", "is_extra": false},'
        ' {"index": 1, "jellyfin_filename": "Family.Guy.S01E02.mkv", "media_type": "tv", "destination": "tvshows", "is_extra": false}]'
    )

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        identify("FAMILY_GUY_DISC1", titles, "sk-ant-test", season=1, disc=1)

    sent_prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "MANUAL OVERRIDE" in sent_prompt
    assert "SEASON 1" in sent_prompt


def test_scope_existing_filters_to_show_and_season():
    from modules.namer import _scope_existing

    existing = [
        "Family.Guy.S01E01.mkv", "Family.Guy.S01E07.mkv",   # earlier season, same show
        "Family.Guy.S02E01.mkv", "Family.Guy.S02E07.mkv",   # target
        "Friends.S02E01.mkv",                               # same season, other show
    ]
    scoped = _scope_existing(existing, "FAMILY_GUY_DISC3", 2)
    assert scoped == ["Family.Guy.S02E01.mkv", "Family.Guy.S02E07.mkv"]


def test_scope_existing_handles_volume_label_between_show_and_disc():
    from modules.namer import _scope_existing

    # Real failure: "FAMILY_GUY_VOLUME_13_DISC_2" left "VOLUME_13" in the show key,
    # so no existing Season 12 file matched and numbering restarted at E01, colliding
    # with the disc-1 episodes already on the server.
    existing = [
        "Family.Guy.S12E01.mkv", "Family.Guy.S12E07.mkv",
        "Family.Guy.S11E01.mkv",   # other season, same show
        "Friends.S12E01.mkv",      # same season tag, other show
    ]
    scoped = _scope_existing(existing, "FAMILY_GUY_VOLUME_13_DISC_2", 12)
    assert scoped == ["Family.Guy.S12E01.mkv", "Family.Guy.S12E07.mkv"]


def test_prompt_hides_other_season_episodes_under_override():
    from modules.namer import _build_prompt

    titles = [{"path": Path("title_t00.mkv"), "duration_secs": 1350, "title_index": 0}]
    # First Season 2 disc: only Season 1 exists on the server. Those must NOT be shown,
    # or the model counts across and starts Season 2 at E08 instead of E01.
    existing = ["Family.Guy.S01E01.mkv", "Family.Guy.S01E07.mkv"]
    prompt = _build_prompt("FAMILY_GUY_DISC2", titles, existing_episodes=existing, season=2)

    assert "S01E07" not in prompt
    assert "start at E01" in prompt


def test_prompt_instructs_movie_extras_flagging():
    from modules.namer import _build_prompt

    titles = [
        {"path": Path("title_t00.mkv"), "duration_secs": 6127, "title_index": 0},  # feature
        {"path": Path("title_t01.mkv"), "duration_secs": 6127, "title_index": 1},  # commentary
        {"path": Path("title_t02.mkv"), "duration_secs": 1800, "title_index": 2},  # featurette
    ]
    prompt = _build_prompt("INCEPTION", titles)

    # The prompt must teach the model to flag movie bonus content and to leave
    # TV episodes untouched.
    assert "is_extra" in prompt
    assert "main feature" in prompt
    assert "TV episodes, ALWAYS set" in prompt


def test_identify_passes_through_is_extra(tmp_path):
    f1 = tmp_path / "title_t00.mkv"
    f2 = tmp_path / "title_t01.mkv"
    f1.write_bytes(b"")
    f2.write_bytes(b"")
    titles = [
        {"path": f1, "duration_secs": 6127, "title_index": 0},  # feature
        {"path": f2, "duration_secs": 6127, "title_index": 1},  # commentary track
    ]
    api_response = (
        '[{"index": 0, "jellyfin_filename": "Inception.2010.mkv", "media_type": "movie", "destination": "movies", "is_extra": false},'
        ' {"index": 1, "jellyfin_filename": "Inception.2010.Commentary.mkv", "media_type": "movie", "destination": "movies", "is_extra": true}]'
    )
    mock_client = _mock_anthropic_response(api_response)

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        result = identify("INCEPTION", titles, "sk-ant-test")

    assert result[0]["is_extra"] is False
    assert result[1]["is_extra"] is True


def test_identify_accepts_double_length_episode_range(tmp_path):
    f1 = tmp_path / "title_t00.mkv"
    f2 = tmp_path / "title_t01.mkv"
    f1.write_bytes(b"")
    f2.write_bytes(b"")
    titles = [
        {"path": f1, "duration_secs": 2880, "title_index": 0},  # ~48 min double episode
        {"path": f2, "duration_secs": 1320, "title_index": 1},  # ~22 min normal episode
    ]
    api_response = (
        '[{"index": 0, "jellyfin_filename": "Friends.S02E12-E13.mkv", "media_type": "tv", "destination": "tvshows"},'
        ' {"index": 1, "jellyfin_filename": "Friends.S02E14.mkv", "media_type": "tv", "destination": "tvshows"}]'
    )
    mock_client = _mock_anthropic_response(api_response)

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        result = identify("FRIENDS SEASON 2-A2", titles, "sk-ant-test")

    assert result[0]["jellyfin_filename"] == "Friends.S02E12-E13.mkv"
    assert result[1]["jellyfin_filename"] == "Friends.S02E14.mkv"


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

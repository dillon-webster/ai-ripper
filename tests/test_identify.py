"""Tests for content-based episode identification (Phase 2, modules/identify.py).

The subprocess/OCR plumbing (ffprobe/mkvextract/vobsubocr/ffmpeg) is mocked; the
pure logic — prompt building, response clamping, SRT parsing, and reconcile rules —
is exercised directly, matching the plan's "test reconcile before wiring into main".
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules import identify
from modules.identify import (
    IdentifyError,
    _build_frame_prompt,
    _build_subtitle_prompt,
    _candidate_lines,
    _parse_match,
    _srt_dialogue,
    build_filename,
    build_named_title,
    identify_title,
    reconcile,
)

CANDIDATES = [
    {"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404},
    {"index": 2, "index_end": None, "name": "Diversity Day", "runtime_secs": 1332},
    {"index": 3, "index_end": None, "name": "Health Care", "runtime_secs": 1320},
]

CONFIG = SimpleNamespace(anthropic_api_key="sk-ant-test")


def _mock_anthropic(text):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client.messages.create.return_value = msg
    return client


# --- prompt building --------------------------------------------------------

def test_candidate_lines_render_numbers_names_durations():
    lines = _candidate_lines(CANDIDATES)
    assert "E01" in lines and "Pilot" in lines and "~23 min" in lines
    assert "E03" in lines and "Health Care" in lines


def test_candidate_lines_render_spanning_range():
    lines = _candidate_lines([{"index": 12, "index_end": 13, "name": "Super Bowl", "runtime_secs": 2640}])
    assert "E12-E13" in lines


def test_subtitle_prompt_includes_dialogue_and_choices():
    prompt = _build_subtitle_prompt("Michael: that's what she said", CANDIDATES)
    assert "that's what she said" in prompt
    assert "Pilot" in prompt
    assert "null" in prompt  # instructs the model it may decline to match


def test_frame_prompt_mentions_skipped_montage_and_choices():
    prompt = _build_frame_prompt(CANDIDATES)
    assert "montage" in prompt
    assert "Diversity Day" in prompt


# --- response parsing / clamping -------------------------------------------

def test_parse_match_accepts_valid_episode():
    result = _parse_match('{"episode": 2, "index_end": null, "confidence": 0.9}', CANDIDATES, "subtitles")
    assert result["episode"] == 2
    assert result["confidence"] == 0.9
    assert result["method"] == "subtitles"


def test_parse_match_clamps_out_of_list_episode_to_none():
    # E07 doesn't exist in a 3-episode season → must not be trusted.
    result = _parse_match('{"episode": 7, "confidence": 0.8}', CANDIDATES, "frames")
    assert result["episode"] is None
    assert result["index_end"] is None


def test_parse_match_preserves_null_match():
    result = _parse_match('{"episode": null, "confidence": 0.2, "reasoning": "Play All"}', CANDIDATES, "subtitles")
    assert result["episode"] is None
    assert result["reasoning"] == "Play All"


def test_parse_match_keeps_index_end_only_with_episode():
    result = _parse_match('{"episode": 12, "index_end": 13, "confidence": 0.9}',
                          [{"index": 12, "index_end": 13, "name": "x"}], "subtitles")
    assert result["episode"] == 12 and result["index_end"] == 13


def test_parse_match_drops_index_end_when_unmatched():
    result = _parse_match('{"episode": 99, "index_end": 100, "confidence": 0.5}', CANDIDATES, "frames")
    assert result["episode"] is None and result["index_end"] is None


def test_parse_match_raises_on_garbage():
    with pytest.raises(IdentifyError):
        _parse_match("not json", CANDIDATES, "subtitles")


# --- SRT dialogue extraction -----------------------------------------------

def test_srt_dialogue_keeps_only_opening_window():
    srt = (
        "1\n00:00:05,000 --> 00:00:08,000\nHello there\n\n"
        "2\n00:01:30,000 --> 00:01:33,000\nStill early\n\n"
        "3\n00:05:00,000 --> 00:05:03,000\nToo late\n"
    )
    text = _srt_dialogue(srt, max_secs=120)
    assert "Hello there" in text
    assert "Still early" in text
    assert "Too late" not in text


def test_srt_dialogue_strips_indices_and_blanks():
    srt = "1\n00:00:01,000 --> 00:00:04,000\nLine one\nLine two\n"
    assert _srt_dialogue(srt, 120) == "Line one Line two"


# --- reconcile rules --------------------------------------------------------

def _t(idx, episode, dur, **extra):
    return {"title_index": idx, "path": Path(f"t{idx}.mkv"),
            "duration_secs": dur, "episode": episode, **extra}


def test_reconcile_keeps_distinct_episodes():
    result = reconcile([_t(0, 1, 1320), _t(1, 2, 1320), _t(2, 3, 1320)])
    assert [t["episode"] for t in result["kept"]] == [1, 2, 3]
    assert result["dropped"] == []


def test_reconcile_drops_unmatched_as_bonus():
    result = reconcile([_t(0, 1, 1320), _t(1, None, 600)])
    assert [t["episode"] for t in result["kept"]] == [1]
    assert len(result["dropped"]) == 1
    assert "bonus" in result["dropped"][0]["drop_reason"]


def test_reconcile_drops_longer_duplicate_as_play_all():
    # Two titles both open with E01: the ~22-min one is real, the ~44-min one is 'Play All'.
    real = _t(0, 1, 1320)
    play_all = _t(1, 1, 2640)
    result = reconcile([play_all, real])
    assert len(result["kept"]) == 1
    assert result["kept"][0]["title_index"] == 0  # the shorter, real episode
    assert len(result["dropped"]) == 1
    assert "Play All" in result["dropped"][0]["drop_reason"]


def test_reconcile_sorts_kept_by_episode():
    result = reconcile([_t(0, 3, 1320), _t(1, 1, 1320), _t(2, 2, 1320)])
    assert [t["episode"] for t in result["kept"]] == [1, 2, 3]


# --- filename building -------------------------------------------------------

def test_build_filename_basic():
    assert build_filename("The Office", 1, 1) == "The.Office.S01E01.mkv"
    assert build_filename("The Office", 12, 7) == "The.Office.S12E07.mkv"


def test_build_filename_spanning_range():
    assert build_filename("Friends", 2, 12, 13) == "Friends.S02E12-E13.mkv"


def test_build_named_title_adds_transfer_fields():
    title = _t(0, 2, 1320, index_end=None, path=Path("t0.mkv"))
    named = build_named_title(title, "The Office", 1)
    assert named["jellyfin_filename"] == "The.Office.S01E02.mkv"
    assert named["media_type"] == "tv"
    assert named["destination"] == "tvshows"
    assert named["is_extra"] is False
    assert named["title_index"] == 0  # original fields preserved


# --- identify_title orchestration (subtitles primary, frames fallback) ------

def test_identify_title_uses_subtitles_when_they_match(tmp_path):
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}

    with patch.object(identify, "extract_subtitle_text", return_value="Michael speaking"), \
         patch("modules.identify.anthropic.Anthropic",
               return_value=_mock_anthropic('{"episode": 2, "confidence": 0.95}')):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] == 2
    assert result["method"] == "subtitles"
    assert result["title_index"] == 0


def test_identify_title_falls_back_to_frames(tmp_path):
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")  # minimal JPEG-ish bytes for base64

    with patch.object(identify, "extract_subtitle_text", return_value=None), \
         patch.object(identify, "extract_frames", return_value=[frame]), \
         patch("modules.identify.anthropic.Anthropic",
               return_value=_mock_anthropic('{"episode": 1, "confidence": 0.7}')):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] == 1
    assert result["method"] == "frames"


def test_identify_title_falls_back_when_subtitles_dont_match(tmp_path):
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")

    # Subtitles present but the model declines (episode null) → must try frames.
    responses = iter([
        _mock_anthropic('{"episode": null, "confidence": 0.1}'),
        _mock_anthropic('{"episode": 3, "confidence": 0.8}'),
    ])
    with patch.object(identify, "extract_subtitle_text", return_value="ambiguous"), \
         patch.object(identify, "extract_frames", return_value=[frame]), \
         patch("modules.identify.anthropic.Anthropic", side_effect=lambda **k: next(responses)):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] == 3
    assert result["method"] == "frames"


def test_identify_title_returns_none_when_no_signal(tmp_path):
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}

    with patch.object(identify, "extract_subtitle_text", return_value=None), \
         patch.object(identify, "extract_frames", return_value=[]):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] is None
    assert result["method"] == "none"

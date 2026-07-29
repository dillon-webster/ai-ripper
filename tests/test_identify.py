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


def test_candidate_lines_include_overview_when_present():
    lines = _candidate_lines([
        {"index": 11, "index_end": None, "name": "The Stripper Cries", "runtime_secs": 1800,
         "overview": "Joey competes on The $100,000 Pyramid; a stripper cries at the party."},
    ])
    assert "Pyramid" in lines  # the plot summary the model matches dialogue against


def test_candidate_lines_omit_overview_line_when_absent():
    # No overview → no extra line, no crash.
    lines = _candidate_lines([{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}])
    assert lines.strip().count("\n") == 0


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


def test_parse_match_extracts_json_after_reasoning_prose():
    # Regression: the model reasoned before the JSON (as it did on The Office S3
    # finale), which used to fail json.loads at char 0 and force a wrong fallback.
    raw = ('Looking at the dialogue, Dwight is comforted over a breakup and made '
           '"secret assistant to the regional manager" — this is "The Job".\n\n'
           '{"episode": 2, "confidence": 0.9, "reasoning": "matches The Job"}')
    result = _parse_match(raw, CANDIDATES, "subtitles")
    assert result["episode"] == 2
    assert result["confidence"] == 0.9


def test_parse_match_extracts_fenced_json():
    raw = '```json\n{"episode": 3, "confidence": 0.7}\n```'
    result = _parse_match(raw, CANDIDATES, "frames")
    assert result["episode"] == 3


# --- SRT dialogue extraction -----------------------------------------------

def test_srt_dialogue_samples_across_runtime_skipping_opening():
    # span = 600s (last cue). Sample windows land at ~180-225s and ~480-525s, so the
    # distinctive middle/late lines are captured and the shared opening is skipped —
    # the fix for serialized-arc episodes that all open the same way.
    srt = (
        "1\n00:00:05,000 --> 00:00:08,000\nShared opening chatter\n\n"
        "2\n00:03:25,000 --> 00:03:28,000\nDistinctive middle\n\n"
        "3\n00:08:15,000 --> 00:08:18,000\nDistinctive late\n\n"
        "4\n00:10:00,000 --> 00:10:03,000\nFinal beat\n"
    )
    text = _srt_dialogue(srt)
    assert "Distinctive middle" in text
    assert "Distinctive late" in text
    assert "Shared opening chatter" not in text


def test_srt_dialogue_uses_whole_short_track():
    # Too short to spread → all dialogue is used, indices/blank lines stripped.
    srt = "1\n00:00:01,000 --> 00:00:04,000\nLine one\nLine two\n"
    assert _srt_dialogue(srt) == "Line one Line two"


# --- subtitle extraction: text vs. image dispatch ---------------------------

def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _ffprobe_sub(codec):
    return _completed(stdout=f'{{"streams": [{{"index": 4, "codec_name": "{codec}"}}]}}')


def test_find_subtitle_track_returns_index_and_codec():
    with patch.object(identify, "_run", return_value=_ffprobe_sub("subrip")):
        assert identify._find_subtitle_track(Path("x.mkv")) == (4, "subrip")


def test_find_subtitle_track_none_when_no_subtitle_stream():
    with patch.object(identify, "_run", return_value=_completed(stdout='{"streams": []}')):
        assert identify._find_subtitle_track(Path("x.mkv")) is None


def test_extract_subtitle_text_uses_ffmpeg_for_text_track():
    # A subrip (text) track — Korra's case — must transcode with ffmpeg, NOT OCR.
    # Regression: the old code sent every track through vobsub2srt, which failed on
    # text tracks and forced the frame fallback on every episode.
    srt = "1\n00:00:01,000 --> 00:00:04,000\nTenzin: Earth. Fire. Air.\n"

    def fake_extract_text(mkv_path, track):
        assert track == 4
        return srt

    with patch.object(identify, "_find_subtitle_track", return_value=(4, "subrip")), \
         patch.object(identify, "_extract_text_srt", side_effect=fake_extract_text) as text, \
         patch.object(identify, "_ocr_vobsub") as ocr:
        dialogue = identify.extract_subtitle_text(Path("korra.mkv"))

    assert "Tenzin: Earth. Fire. Air." in dialogue
    text.assert_called_once()
    ocr.assert_not_called()  # never touches the OCR path for a text track


def test_extract_subtitle_text_uses_ocr_for_vobsub_track():
    # A DVD VobSub (image) track still goes through mkvextract + vobsub2srt OCR.
    srt = "1\n00:00:01,000 --> 00:00:04,000\nMichael: that's what she said\n"
    with patch.object(identify, "_find_subtitle_track", return_value=(3, "dvd_subtitle")), \
         patch.object(identify, "_ocr_vobsub", return_value=srt) as ocr, \
         patch.object(identify, "_extract_text_srt") as text:
        dialogue = identify.extract_subtitle_text(Path("office.mkv"))

    assert "that's what she said" in dialogue
    ocr.assert_called_once()
    text.assert_not_called()


def test_extract_subtitle_text_ocr_path_for_unknown_codec():
    # Unknown codec defaults to OCR (prior behavior) rather than being dropped.
    with patch.object(identify, "_find_subtitle_track", return_value=(3, "hdmv_pgs_subtitle")), \
         patch.object(identify, "_ocr_vobsub", return_value=None) as ocr:
        assert identify.extract_subtitle_text(Path("bluray.mkv")) is None
    ocr.assert_called_once()


def test_extract_subtitle_text_none_when_no_track():
    with patch.object(identify, "_find_subtitle_track", return_value=None):
        assert identify.extract_subtitle_text(Path("x.mkv")) is None


def test_extract_text_srt_returns_none_on_ffmpeg_failure():
    with patch.object(identify, "_run", return_value=_completed(returncode=1, stderr="boom")):
        assert identify._extract_text_srt(Path("x.mkv"), 4) is None


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


def test_reconcile_duplicate_keeps_episode_nearest_guide_runtime():
    # The Office S2 E20: the real ~21-min episode, a ~28-min Play-All, and a short gag
    # reel all matched E20. Old 'shortest wins' kept the gag reel and dropped the real
    # episode; now the title nearest the guide runtime (the real one) wins.
    real = _t(1, 20, 1265, confidence=0.90)
    play_all = _t(8, 20, 1698, confidence=0.80)
    gag_reel = _t(15, 20, 900, confidence=0.95)
    result = reconcile([real, play_all, gag_reel], episode_runtimes={20: 1260})
    assert [t["title_index"] for t in result["kept"]] == [1]  # the real episode
    reasons = " ".join(d["drop_reason"] for d in result["dropped"])
    assert "gag reel" in reasons and "Play All" in reasons


def test_reconcile_duplicate_without_guide_falls_back_to_shortest():
    # No guide runtime (legacy path): keep the old shortest-wins Play-All assumption.
    result = reconcile([_t(0, 1, 2640), _t(1, 1, 1320)])
    assert [t["title_index"] for t in result["kept"]] == [1]


def test_reconcile_drops_lone_title_far_too_short_for_its_episode():
    # A 9-min bonus content-matched E05 with no real E05 competing — too short to be it.
    guide = {1: 1320, 2: 1320, 3: 1320, 5: 1320}
    result = reconcile([_t(0, 1, 1300), _t(1, 2, 1300), _t(2, 3, 1300), _t(3, 5, 540)],
                       episode_runtimes=guide)
    assert [t["episode"] for t in result["kept"]] == [1, 2, 3]
    assert any("too short" in d["drop_reason"] for d in result["dropped"])


def test_reconcile_keeps_slightly_short_real_episode():
    # Real DVD rips run a bit under the listed runtime (~0.96×) — must NOT be dropped.
    guide = {1: 1320, 2: 1320, 3: 1320}
    result = reconcile([_t(0, 1, 1267), _t(1, 2, 1270), _t(2, 3, 1266)], episode_runtimes=guide)
    assert [t["episode"] for t in result["kept"]] == [1, 2, 3]
    assert result["dropped"] == []


def test_reconcile_drops_isolated_low_confidence_as_bonus():
    # The Office S2 disc 3 case: E13-E18 are a tight block; a 31-min bonus reel got
    # force-matched to E20 at 0.85 — isolated past the E19 gap → dropped, not numbered.
    block = [_t(i, 13 + i, 1260, confidence=0.95) for i in range(6)]
    bonus = _t(14, 20, 1860, confidence=0.85)
    result = reconcile(block + [bonus])
    assert [t["episode"] for t in result["kept"]] == [13, 14, 15, 16, 17, 18]
    assert len(result["dropped"]) == 1
    assert result["dropped"][0]["title_index"] == 14
    assert "isolated low-confidence" in result["dropped"][0]["drop_reason"]


def test_reconcile_keeps_isolated_but_confident_match():
    # A lone episode that matched confidently is a real (if oddly-placed) episode, not a bonus.
    block = [_t(i, 13 + i, 1260, confidence=0.95) for i in range(6)]
    lone = _t(14, 20, 1260, confidence=0.97)
    result = reconcile(block + [lone])
    assert 20 in [t["episode"] for t in result["kept"]]
    assert result["dropped"] == []


def test_reconcile_keeps_a_block_of_long_episodes():
    # The Office S4 disc 1: E01-E04 are all ~42 min. None must be dropped as an omnibus
    # (each matches its own 42-min episode, ratio ~1.0), and being same-length is fine —
    # content-ID distinguishes them, reconcile just must not false-drop.
    guide = {1: 2520, 2: 2520, 3: 2520, 4: 2520}
    titles = [_t(i, i + 1, 2520, confidence=0.95) for i in range(4)]
    result = reconcile(titles, episode_runtimes=guide)
    assert [t["episode"] for t in result["kept"]] == [1, 2, 3, 4]
    assert result["dropped"] == []


def test_reconcile_reassigns_off_disc_match_to_adjacent_by_runtime():
    # The Office S3 finale: disc holds E19-E22, plus the 42-min finale, which content-ID
    # matched to E10 'A Benihana Christmas' (the season's OTHER ~42-min episode, off this
    # disc) at 0.85. Instead of dropping it, reassign to the adjacent unclaimed episode
    # whose runtime fits — E23 'The Job' (43m), not the also-adjacent E18 (22m).
    guide = {18: 1320, 19: 1320, 20: 1320, 21: 1320, 22: 1320, 23: 2580, 10: 2520}
    block = [_t(i, 19 + i, 1320, confidence=0.98) for i in range(4)]
    finale = _t(4, 10, 2550, confidence=0.85)
    result = reconcile(block + [finale], episode_runtimes=guide)
    assert [t["episode"] for t in result["kept"]] == [19, 20, 21, 22, 23]
    assert result["dropped"] == []
    reassigned = next(t for t in result["kept"] if t["episode"] == 23)
    assert "runtime-repair" in reassigned["method"]


def test_reconcile_drops_off_disc_match_with_no_runtime_fit():
    # Same isolated low-confidence match, but no adjacent unclaimed episode fits its
    # runtime → still dropped for approval (a genuine bonus reel, not a moved episode).
    guide = {18: 1320, 19: 1320, 20: 1320, 21: 1320, 22: 1320, 23: 2580, 10: 1860}
    block = [_t(i, 19 + i, 1320, confidence=0.98) for i in range(4)]
    bonus = _t(4, 10, 1860, confidence=0.85)  # 31 min — fits no adjacent slot (E18/E23)
    result = reconcile(block + [bonus], episode_runtimes=guide)
    assert [t["episode"] for t in result["kept"]] == [19, 20, 21, 22]
    assert "isolated low-confidence" in result["dropped"][0]["drop_reason"]


def test_reconcile_keeps_low_confidence_match_when_adjacent():
    # Low confidence alone isn't enough — an episode sitting next to the block is kept.
    block = [_t(i, 13 + i, 1260, confidence=0.95) for i in range(6)]
    adjacent = _t(14, 19, 1260, confidence=0.80)
    result = reconcile(block + [adjacent])
    assert 19 in [t["episode"] for t in result["kept"]]
    assert result["dropped"] == []


def test_reconcile_trusts_small_sets_without_a_block():
    # With too few titles there's no contiguous block to be isolated from — trust the matches.
    result = reconcile([_t(0, 1, 1320, confidence=0.5), _t(1, 8, 1320, confidence=0.5)])
    assert [t["episode"] for t in result["kept"]] == [1, 8]
    assert result["dropped"] == []


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


def test_identify_title_falls_back_to_frames_on_malformed_subtitle_json(tmp_path):
    # A malformed model reply on the subtitle match must not crash the rip — it
    # degrades to frames. Regression for the IdentifyError that killed a season run.
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")

    responses = iter([
        _mock_anthropic("not json at all"),                       # subtitle match → IdentifyError
        _mock_anthropic('{"episode": 3, "confidence": 0.8}'),     # frame match recovers
    ])
    with patch.object(identify, "extract_subtitle_text", return_value="some dialogue"), \
         patch.object(identify, "extract_frames", return_value=[frame]), \
         patch("modules.identify.anthropic.Anthropic", side_effect=lambda **k: next(responses)):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] == 3
    assert result["method"] == "frames"


def test_identify_title_returns_none_when_both_matches_malformed(tmp_path):
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")

    with patch.object(identify, "extract_subtitle_text", return_value="dialogue"), \
         patch.object(identify, "extract_frames", return_value=[frame]), \
         patch("modules.identify.anthropic.Anthropic", return_value=_mock_anthropic("garbage")):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] is None
    assert result["method"] == "none"


def test_identify_title_returns_none_when_no_signal(tmp_path):
    mkv = tmp_path / "title_t00.mkv"
    mkv.write_bytes(b"")
    title = {"path": mkv, "duration_secs": 1320, "title_index": 0}

    with patch.object(identify, "extract_subtitle_text", return_value=None), \
         patch.object(identify, "extract_frames", return_value=[]):
        result = identify_title(title, CANDIDATES, CONFIG)

    assert result["episode"] is None
    assert result["method"] == "none"


# --- volume discs: two seasons in one candidate pool -------------------------
#
# A volume box set isn't a season set. Family Guy shipped as Volumes 1-12, and one
# disc inside a volume can hold the last episodes of one season and the first of the
# next — so the candidate pool spans a boundary and the bare episode number stops
# being an identity (S04E10 and S05E10 are different episodes).

VOLUME_CANDIDATES = [
    {"season": 4, "index": 29, "index_end": None, "name": "PTV", "runtime_secs": 1320},
    {"season": 4, "index": 30, "index_end": None, "name": "Brian Sings", "runtime_secs": 1320},
    {"season": 5, "index": 1, "index_end": None, "name": "Stewie Loves Lois", "runtime_secs": 1320},
    {"season": 5, "index": 2, "index_end": None, "name": "Mother Tucker", "runtime_secs": 1320},
]


def _vt(title_index, season, episode, duration, confidence=0.95):
    return {"title_index": title_index, "season": season, "episode": episode,
            "index_end": None, "duration_secs": duration, "confidence": confidence}


def test_episode_key_is_season_qualified_and_order_preserving():
    assert identify.episode_key(4, 29) < identify.episode_key(5, 1)
    assert identify.episode_key(4, 10) != identify.episode_key(5, 10)
    assert identify.split_key(identify.episode_key(4, 29)) == (4, 29)
    # No season → the bare episode number, so the single-season path is unchanged.
    assert identify.episode_key(None, 7) == 7
    assert identify.split_key(7) == (None, 7)


def test_candidate_lines_are_season_qualified_only_when_seasons_span():
    lines = _candidate_lines(VOLUME_CANDIDATES)
    assert "S04E29" in lines and "S05E01" in lines
    # One season → the proven single-season rendering, untouched.
    assert "S01" not in _candidate_lines(CANDIDATES)


def test_subtitle_prompt_asks_for_a_season_on_a_volume_disc():
    prompt = _build_subtitle_prompt("dialogue", VOLUME_CANDIDATES, 1320)
    assert '"season"' in prompt
    assert "VOLUME box set" in prompt
    # The single-season prompt never mentions a season field — that path is proven.
    assert '"season"' not in _build_subtitle_prompt("dialogue", CANDIDATES, 1320)


def test_parse_match_keeps_the_season_the_model_chose():
    result = _parse_match('{"season": 5, "episode": 1, "confidence": 0.93}',
                          VOLUME_CANDIDATES, "subtitles")
    assert (result["season"], result["episode"]) == (5, 1)


def test_parse_match_rejects_an_episode_from_the_wrong_season():
    # S05E29 is not a listed pair (29 belongs to season 4) — unmatched beats misfiled.
    result = _parse_match('{"season": 5, "episode": 29, "confidence": 0.9}',
                          VOLUME_CANDIDATES, "subtitles")
    assert result["episode"] is None


def test_parse_match_infers_the_sole_season_when_only_one_is_in_play():
    single = [{**c, "season": 2} for c in CANDIDATES]
    result = _parse_match('{"episode": 2, "confidence": 0.9}', single, "subtitles")
    assert (result["season"], result["episode"]) == (2, 2)


def test_reconcile_keeps_the_same_episode_number_from_both_seasons():
    """The collision this whole thing exists for: a disc holding S04E10 and S05E10
    must keep both, not treat the second as a duplicate of the first."""
    result = reconcile([_vt(0, 4, 10, 1320), _vt(1, 5, 10, 1320)])
    assert [(t["season"], t["episode"]) for t in result["kept"]] == [(4, 10), (5, 10)]
    assert result["dropped"] == []


def test_reconcile_treats_a_season_boundary_as_contiguous():
    """S04E30 → S05E01 is one continuous block, so a low-confidence title at the seam
    is anchored by its neighbors — not dropped as an isolated off-disc match."""
    runtimes = {identify.episode_key(c["season"], c["index"]): 1320 for c in VOLUME_CANDIDATES}
    keys = sorted(runtimes)
    titles = [_vt(0, 4, 29, 1320), _vt(1, 4, 30, 1320),
              _vt(2, 5, 1, 1320, confidence=0.55), _vt(3, 5, 2, 1320)]
    result = reconcile(titles, episode_runtimes=runtimes, guide_keys=keys)
    assert [(t["season"], t["episode"]) for t in result["kept"]] == [(4, 29), (4, 30), (5, 1), (5, 2)]


def test_reconcile_sorts_across_seasons_in_broadcast_order():
    result = reconcile([_vt(0, 5, 2, 1320), _vt(1, 4, 29, 1320), _vt(2, 5, 1, 1320)])
    assert [(t["season"], t["episode"]) for t in result["kept"]] == [(4, 29), (5, 1), (5, 2)]


def test_build_named_title_uses_the_titles_own_season():
    """Each episode is filed under the season it MATCHED, not the run's first season."""
    nt = build_named_title(_vt(0, 5, 1, 1320), "Family Guy", 4)
    assert nt["jellyfin_filename"] == "Family.Guy.S05E01.mkv"
    # No season on the title (single-season path) → the run's season still applies.
    assert build_named_title({"episode": 3}, "Family Guy", 4)["jellyfin_filename"] \
        == "Family.Guy.S04E03.mkv"

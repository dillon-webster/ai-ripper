"""Tests for the provider-aware (episode-guide) additions to the namer prompt."""
from pathlib import Path

from modules.namer import _build_prompt, _episode_guide_section


def _titles(tmp_path):
    f = tmp_path / "title_t00.mkv"
    f.write_bytes(b"")
    return [{"path": f, "duration_secs": 1320, "title_index": 0}]


GUIDE = [
    {"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404},
    {"index": 2, "index_end": None, "name": "Diversity Day", "runtime_secs": 1332},
]

GUIDE_WITH_DOUBLE = [
    {"index": 12, "index_end": 13, "name": "The One After the Super Bowl", "runtime_secs": 2640},
]


def test_guide_section_renders_numbers_names_durations():
    section = _episode_guide_section(GUIDE, season=1)
    assert "E01" in section and "Pilot" in section and "~23 min" in section
    assert "E02" in section and "Diversity Day" in section
    assert "AUTHORITATIVE EPISODE LIST" in section


def test_guide_section_renders_spanning_range():
    section = _episode_guide_section(GUIDE_WITH_DOUBLE, season=2)
    assert "E12-E13" in section


def test_guide_section_empty_without_guide():
    assert _episode_guide_section(None) == ""
    assert _episode_guide_section([]) == ""


def test_prompt_with_guide_drops_double_length_heuristic(tmp_path):
    prompt = _build_prompt("THE_OFFICE_DISC1", _titles(tmp_path), season=1,
                           show="The Office", episode_guide=GUIDE)
    # authoritative list present, old phantom-generating heuristic block gone
    assert "AUTHORITATIVE EPISODE LIST" in prompt
    assert "IMPORTANT for double-length" not in prompt
    assert "The One After the Super Bowl" not in prompt
    assert "The Office" in prompt  # show name is authoritative in the prompt


def test_prompt_without_guide_keeps_double_length_heuristic(tmp_path):
    prompt = _build_prompt("FRIENDS_S02_D1", _titles(tmp_path), season=2)
    # legacy behavior unchanged when no guide is supplied
    assert "AUTHORITATIVE EPISODE LIST" not in prompt
    assert "IMPORTANT for double-length" in prompt

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from modules.ripper import (
    _parse_info, _hms_to_secs, _parse_title_index, _prune_outlier_source_sets, rip, RipError,
)


def test_hms_to_secs_standard():
    assert _hms_to_secs("1:42:07") == 6127


def test_hms_to_secs_zero_hours():
    assert _hms_to_secs("0:22:15") == 1335


def test_parse_info_extracts_durations():
    output = (
        'MSG:1005,0,0,"MakeMKV started"\n'
        'TCOUNT:2\n'
        'TINFO:0,9,0,"1:42:07"\n'
        'TINFO:0,27,0,"Some Movie"\n'
        'TINFO:1,9,0,"0:22:15"\n'
        'TINFO:1,27,0,"Bonus Feature"\n'
    )
    result = _parse_info(output)
    assert result[0]["duration_secs"] == 6127
    assert result[1]["duration_secs"] == 1335


def test_parse_info_captures_source_set():
    output = (
        'TINFO:0,9,0,"0:21:35"\n'
        'TINFO:0,49,0,"B1"\n'
        'TINFO:1,9,0,"0:22:38"\n'
        'TINFO:1,49,0,"J1"\n'
    )
    result = _parse_info(output)
    assert result[0]["source_set"] == "B1"
    assert result[1]["source_set"] == "J1"


def test_parse_title_index_from_filename():
    assert _parse_title_index("title_t00.mkv") == 0
    assert _parse_title_index("title_t03.mkv") == 3
    assert _parse_title_index("title_t12.mkv") == 12


def test_prune_drops_lone_source_set_outliers():
    # Family Guy S12 disc 2: 7 real episodes share source set "B1"; the full-length
    # animatic ("J1") and the cell-scramble decoy ("K1") each sit alone. Both are the
    # same ~22 min as an episode, so only the source set separates them.
    title_info = {i: {"source_set": "B1", "duration_secs": 1300} for i in range(7)}
    title_info[7] = {"source_set": "J1", "duration_secs": 1358}  # animatic
    title_info[8] = {"source_set": "K1", "duration_secs": 1372}  # scramble decoy
    kept, dropped = _prune_outlier_source_sets(list(range(9)), title_info)
    assert kept == list(range(7))
    assert dropped == [7, 8]


def test_prune_keeps_all_when_no_dominant_cluster():
    # Every title in its own source set (no cluster reaches the threshold) → prune nothing.
    title_info = {i: {"source_set": f"S{i}", "duration_secs": 1300} for i in range(2)}
    kept, dropped = _prune_outlier_source_sets([0, 1], title_info)
    assert kept == [0, 1]
    assert dropped == []


def test_prune_keeps_second_genuine_cluster():
    # A disc split across two VTS (4 + 3 real episodes) — neither is a singleton, keep all.
    title_info = {i: {"source_set": "A1", "duration_secs": 1300} for i in range(4)}
    title_info.update({i: {"source_set": "B1", "duration_secs": 1300} for i in range(4, 7)})
    kept, dropped = _prune_outlier_source_sets(list(range(7)), title_info)
    assert kept == list(range(7))
    assert dropped == []


def test_prune_noop_when_source_set_unknown():
    # Older MakeMKV / parse miss: no source_set on any title → never drop anything.
    title_info = {i: {"duration_secs": 1300} for i in range(5)}
    kept, dropped = _prune_outlier_source_sets(list(range(5)), title_info)
    assert kept == list(range(5))
    assert dropped == []


def test_rip_returns_titles_with_duration(tmp_path):
    # title_t00: 1:42:07 = 6127s (kept), title_t01: 0:04:15 = 255s < 960s floor (filtered)
    info_output = (
        'TINFO:0,9,0,"1:42:07"\n'
        'TINFO:1,9,0,"0:04:15"\n'
    )

    # Create fake output files
    (tmp_path / "title_t00.mkv").write_bytes(b"fake mkv data")
    (tmp_path / "title_t01.mkv").write_bytes(b"fake mkv data")

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            mock.stdout = info_output
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path)

    assert len(titles) == 1  # title_t01 is 255s < 300s minimum, filtered out
    assert titles[0]["title_index"] == 0
    assert titles[0]["duration_secs"] == 6127
    assert titles[0]["path"] == tmp_path / "title_t00.mkv"


def test_rip_prunes_full_length_bonus_by_source_set(tmp_path):
    # 3 real episodes (source "B1") + 1 full-length animatic (source "J1"), all > 16 min.
    # The animatic must be pruned as a lone-source-set outlier and never ripped.
    info_output = (
        'TINFO:0,9,0,"0:21:35"\nTINFO:0,49,0,"B1"\n'
        'TINFO:1,9,0,"0:22:07"\nTINFO:1,49,0,"B1"\n'
        'TINFO:2,9,0,"0:21:40"\nTINFO:2,49,0,"B1"\n'
        'TINFO:3,9,0,"0:22:38"\nTINFO:3,49,0,"J1"\n'
    )
    for name in ("B1_t00.mkv", "B1_t01.mkv", "B1_t02.mkv", "J1_t03.mkv"):
        (tmp_path / name).write_bytes(b"x")

    ripped = []

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            mock.stdout = info_output
        elif "mkv" in cmd:
            ripped.append(cmd[cmd.index("mkv") + 2])  # title-index arg, whatever flags precede it
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path)

    assert sorted(t["title_index"] for t in titles) == [0, 1, 2]  # animatic excluded
    assert "3" not in ripped  # and never even ripped


def test_rip_skips_titles_over_max_title_secs(tmp_path):
    # 3 real ~22-min episodes + a 44-min two-episode 'Play All' chunk. The
    # sum-of-others heuristic can't see it (44 min is nowhere near the other
    # 65 min combined), so the episode-guide cap must skip it BEFORE ripping —
    # not rip it for an hour and drop it post-rip.
    info_output = (
        'TINFO:0,9,0,"0:21:45"\n'
        'TINFO:1,9,0,"0:22:12"\n'
        'TINFO:2,9,0,"0:21:38"\n'
        'TINFO:3,9,0,"0:44:00"\n'
    )
    for name in ("title_t00.mkv", "title_t01.mkv", "title_t02.mkv"):
        (tmp_path / name).write_bytes(b"x")

    ripped = []

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            mock.stdout = info_output
        elif "mkv" in cmd:
            ripped.append(cmd[cmd.index("mkv") + 2])  # title-index arg, whatever flags precede it
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path, max_title_secs=1998)  # 1332s × 1.5

    assert sorted(t["title_index"] for t in titles) == [0, 1, 2]
    assert "3" not in ripped  # the Play-All chunk was never ripped


def test_rip_keeps_long_title_without_cap(tmp_path):
    # Same disc, but no episode guide → no cap: the 44-min title still rips
    # (old behavior preserved; the post-rip content-ID filter handles it).
    info_output = (
        'TINFO:0,9,0,"0:21:45"\n'
        'TINFO:1,9,0,"0:22:12"\n'
        'TINFO:2,9,0,"0:21:38"\n'
        'TINFO:3,9,0,"0:44:00"\n'
    )
    for i in range(4):
        (tmp_path / f"title_t0{i}.mkv").write_bytes(b"x")

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            mock.stdout = info_output
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path)

    assert sorted(t["title_index"] for t in titles) == [0, 1, 2, 3]


def test_rip_filters_sub_16min_bonus(tmp_path):
    # A 15:09 (909s) featurette is below the 960s floor and must be dropped.
    info_output = (
        'TINFO:0,9,0,"0:21:35"\n'
        'TINFO:1,9,0,"0:15:09"\n'
    )
    (tmp_path / "title_t00.mkv").write_bytes(b"x")
    (tmp_path / "title_t01.mkv").write_bytes(b"x")

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            mock.stdout = info_output
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path)

    assert [t["title_index"] for t in titles] == [0]


def test_rip_raises_on_makemkv_failure(tmp_path):
    # Info reports one eligible title, but the actual rip command fails
    info_output = 'TINFO:0,9,0,"1:42:07"\n'

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if "info" in cmd:
            mock.returncode = 0
            mock.stdout = info_output
        else:
            mock.returncode = 1
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        with pytest.raises(RipError, match="exited with code 1"):
            rip(Path("/Volumes/FAKE_DISC"), tmp_path)


def test_rip_retries_info_scan_while_drive_spins_up(tmp_path):
    # First info scan returns nothing (cold drive); second returns a real title.
    info_output = 'TINFO:0,9,0,"0:22:00"\n'
    (tmp_path / "title_t00.mkv").write_bytes(b"x")
    info_calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            info_calls["n"] += 1
            mock.stdout = "" if info_calls["n"] == 1 else info_output
        return mock

    with patch("modules.ripper.time.sleep"), \
         patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path)

    assert info_calls["n"] == 2  # retried once, then succeeded
    assert len(titles) == 1


def test_rip_raises_after_all_info_scans_empty(tmp_path):
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""  # drive never returns titles
        return mock

    with patch("modules.ripper.time.sleep"), \
         patch("modules.ripper.subprocess.run", side_effect=fake_run):
        with pytest.raises(RipError, match="No eligible titles"):
            rip(Path("/Volumes/FAKE_DISC"), tmp_path)


def test_rip_raises_when_no_mkv_files_produced(tmp_path):
    info_output = 'TINFO:0,9,0,"1:42:07"\n'

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = info_output
        return mock

    # Don't create any .mkv files in tmp_path
    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        with pytest.raises(RipError, match="No MKV files"):
            rip(Path("/Volumes/FAKE_DISC"), tmp_path)

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from modules.ripper import _parse_info, _hms_to_secs, _parse_title_index, rip, RipError


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


def test_parse_title_index_from_filename():
    assert _parse_title_index("title_t00.mkv") == 0
    assert _parse_title_index("title_t03.mkv") == 3
    assert _parse_title_index("title_t12.mkv") == 12


def test_rip_returns_titles_with_duration(tmp_path):
    # title_t00: 1:42:07 = 6127s (kept), title_t01: 0:04:15 = 255s < 300s threshold (filtered)
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

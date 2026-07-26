import pytest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config
from modules.transfer import (
    send_all, stage_local, TransferError, _remote_subpath, _remote_file_exists,
    _movie_folder_name, _staging_subpath,
)


def _make_config():
    return Config(
        anthropic_api_key="",
        server_ip="100.100.212.32",
        server_user="dillon",
        jellyfin_url="",
        jellyfin_api_key="",
        discord_webhook_url="",
        temp_dir=Path("/tmp/ai-ripper"),
        media_root="/media",
    )


def _make_titled(tmp_path):
    f = tmp_path / "title_t00.mkv"
    f.write_bytes(b"")
    return [{
        "path": f,
        "duration_secs": 6127,
        "title_index": 0,
        "jellyfin_filename": "Friends.S01E01.mkv",
        "media_type": "tv",
        "destination": "tvshows",
    }]


def test_remote_subpath_tv():
    title = {"jellyfin_filename": "Friends.S01E01.mkv", "media_type": "tv", "destination": "tvshows"}
    assert _remote_subpath(title) == "tvshows/Friends/Season 01/Friends.S01E01.mkv"


def test_remote_subpath_tv_multiword_show():
    title = {"jellyfin_filename": "The.Office.S03E07.mkv", "media_type": "tv", "destination": "tvshows"}
    assert _remote_subpath(title) == "tvshows/The Office/Season 03/The.Office.S03E07.mkv"


def test_remote_subpath_movie_uses_folder_per_movie():
    title = {"jellyfin_filename": "Inception.2010.mkv", "media_type": "movie", "destination": "movies"}
    assert _remote_subpath(title) == "movies/Inception (2010)/Inception (2010).mkv"


def test_remote_subpath_movie_matches_staging_layout():
    """The DVD path (_remote_subpath) and the Blu-ray path (_staging_subpath)
    must put a movie in the same shape — a DVD rip that lands flat is invisible
    to the encode script and the dashboard's movie transfers."""
    title = {"jellyfin_filename": "Inception.2010.mkv", "media_type": "movie", "destination": "movies"}
    assert _remote_subpath(title) == _staging_subpath(title)


def test_remote_subpath_movie_without_year():
    """No parsable year → de-dotted base name, no parens (e.g. 'Obsession')."""
    title = {"jellyfin_filename": "Obsession.mkv", "media_type": "movie", "destination": "movies"}
    assert _remote_subpath(title) == "movies/Obsession/Obsession.mkv"


def test_send_all_transfers_to_correct_path(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    with patch("modules.transfer._ssh_mkdir") as mock_mkdir, \
         patch("modules.transfer._remote_file_exists", return_value=False), \
         patch("modules.transfer._scp") as mock_scp:
        result = send_all(titles, config)

    expected_dir = "/media/tvshows/Friends/Season 01"
    expected_remote = "dillon@100.100.212.32:/media/tvshows/Friends/Season 01/Friends.S01E01.mkv"
    mock_mkdir.assert_called_once_with(expected_dir, config)
    mock_scp.assert_called_once_with(titles[0]["path"], expected_remote)
    assert result == [expected_remote]


def test_send_all_retries_on_scp_failure(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    call_count = 0

    def fake_scp(path, remote):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise TransferError("Connection refused")

    with patch("modules.transfer._ssh_mkdir"), \
         patch("modules.transfer._remote_file_exists", return_value=False), \
         patch("modules.transfer._scp", side_effect=fake_scp), \
         patch("modules.transfer.time.sleep"):
        result = send_all(titles, config)

    assert call_count == 3
    assert len(result) == 1


def test_send_all_skips_existing_file(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    with patch("modules.transfer._ssh_mkdir"), \
         patch("modules.transfer._remote_file_exists", return_value=True), \
         patch("modules.transfer._scp") as mock_scp:
        result = send_all(titles, config)

    mock_scp.assert_not_called()
    assert result == []


def test_send_all_raises_after_all_retries_fail(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    with patch("modules.transfer._ssh_mkdir"), \
         patch("modules.transfer._remote_file_exists", return_value=False), \
         patch("modules.transfer._scp", side_effect=TransferError("Connection refused")), \
         patch("modules.transfer.time.sleep"):
        with pytest.raises(TransferError, match="Friends.S01E01.mkv"):
            send_all(titles, config)


def _make_movie_titled(tmp_path):
    f = tmp_path / "title_t00.mkv"
    f.write_bytes(b"data")
    return [{
        "path": f,
        "duration_secs": 8400,
        "title_index": 0,
        "jellyfin_filename": "Inception.2010.mkv",
        "media_type": "movie",
        "destination": "movies",
    }]


def test_movie_folder_name_wraps_year_in_parens():
    assert _movie_folder_name("The.Conjuring.2013.mkv") == "The Conjuring (2013)"


def test_movie_folder_name_uses_last_year_when_title_has_a_number():
    assert _movie_folder_name("Blade.Runner.2049.2017.mkv") == "Blade Runner 2049 (2017)"


def test_movie_folder_name_without_year_just_despaces():
    assert _movie_folder_name("Some.Untitled.Feature.mkv") == "Some Untitled Feature"


def test_staging_subpath_movie_is_folder_per_movie():
    title = {"jellyfin_filename": "Inception.2010.mkv", "media_type": "movie",
             "destination": "movies"}
    assert _staging_subpath(title) == "movies/Inception (2010)/Inception (2010).mkv"


def test_staging_subpath_tv_matches_server_layout():
    title = {"jellyfin_filename": "Friends.S01E01.mkv", "media_type": "tv",
             "destination": "tvshows"}
    assert _staging_subpath(title) == "tvshows/Friends/Season 01/Friends.S01E01.mkv"


def test_stage_local_moves_movie_to_folder_per_movie_layout(tmp_path):
    staging = tmp_path / "staging"
    titles = _make_movie_titled(tmp_path)
    src = titles[0]["path"]
    config = replace(_make_config(), bluray_staging_dir=str(staging))

    result = stage_local(titles, config)

    # Folder-per-movie layout the HEVC encode script reads (matches the real
    # ~/video-transfer/movies/The Conjuring (2013)/The Conjuring (2013).mkv).
    dest = staging / "movies" / "Inception (2010)" / "Inception (2010).mkv"
    assert result == [dest]
    assert dest.exists()
    assert not src.exists()  # moved, not copied
    assert dest.read_bytes() == b"data"


def test_stage_local_moves_tv_to_season_layout(tmp_path):
    staging = tmp_path / "staging"
    titles = _make_titled(tmp_path)  # Friends.S01E01.mkv, tv/tvshows
    config = replace(_make_config(), bluray_staging_dir=str(staging))

    result = stage_local(titles, config)

    dest = staging / "tvshows" / "Friends" / "Season 01" / "Friends.S01E01.mkv"
    assert result == [dest]
    assert dest.exists()


def test_stage_local_expands_user_home(tmp_path):
    titles = _make_movie_titled(tmp_path)
    config = replace(_make_config(), bluray_staging_dir="~/video-transfer")

    with patch("modules.transfer.shutil.move") as mock_move, \
         patch("pathlib.Path.mkdir"), patch("pathlib.Path.exists", return_value=False):
        stage_local(titles, config)

    dest_arg = Path(mock_move.call_args[0][1])
    assert "~" not in str(dest_arg)
    assert str(dest_arg).endswith("video-transfer/movies/Inception (2010)/Inception (2010).mkv")


def test_stage_local_skips_when_already_staged(tmp_path):
    staging = tmp_path / "staging"
    titles = _make_movie_titled(tmp_path)
    src = titles[0]["path"]
    dest = staging / "movies" / "Inception (2010)" / "Inception (2010).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing")
    config = replace(_make_config(), bluray_staging_dir=str(staging))

    with patch("modules.transfer.shutil.move") as mock_move:
        result = stage_local(titles, config)

    mock_move.assert_not_called()
    assert result == []
    assert src.exists()  # source left in place for normal cleanup
    assert dest.read_bytes() == b"existing"  # not overwritten


def test_stage_local_raises_transfer_error_on_move_failure(tmp_path):
    staging = tmp_path / "staging"
    titles = _make_movie_titled(tmp_path)
    config = replace(_make_config(), bluray_staging_dir=str(staging))

    with patch("modules.transfer.shutil.move", side_effect=OSError("No space left on device")):
        with pytest.raises(TransferError, match="Inception.2010.mkv"):
            stage_local(titles, config)

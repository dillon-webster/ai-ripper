import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config
from modules.transfer import send_all, TransferError, _remote_subpath, _remote_file_exists


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


def test_remote_subpath_movie():
    title = {"jellyfin_filename": "Inception.2010.mkv", "media_type": "movie", "destination": "movies"}
    assert _remote_subpath(title) == "movies/Inception.2010.mkv"


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


def test_send_all_raises_after_all_retries_fail(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    with patch("modules.transfer._ssh_mkdir"), \
         patch("modules.transfer._remote_file_exists", return_value=False), \
         patch("modules.transfer._scp", side_effect=TransferError("Connection refused")), \
         patch("modules.transfer.time.sleep"):
        with pytest.raises(TransferError, match="Friends.S01E01.mkv"):
            send_all(titles, config)

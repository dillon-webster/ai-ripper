import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config
from modules.transfer import send_all, TransferError


def _make_config():
    return Config(
        anthropic_api_key="",
        server_ip="100.100.212.32",
        server_user="dillon",
        jellyfin_url="",
        jellyfin_api_key="",
        discord_webhook_url="",
        temp_dir=Path("/tmp/ai-ripper"),
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


def test_send_all_calls_scp_with_correct_args(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("modules.transfer.subprocess.run", return_value=mock_result) as mock_run:
        result = send_all(titles, config)

    expected_remote = "dillon@100.100.212.32:/home/dillon/jellyfin/media/tvshows/Friends.S01E01.mkv"
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "scp"
    assert str(titles[0]["path"]) in args
    assert expected_remote in args
    assert result == [expected_remote]


def test_send_all_retries_on_scp_failure(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    fail = MagicMock()
    fail.returncode = 1
    fail.stderr = "Connection refused"

    succeed = MagicMock()
    succeed.returncode = 0

    # Fail twice, succeed on third attempt
    side_effects = [fail, fail, succeed]

    with patch("modules.transfer.subprocess.run", side_effect=side_effects), \
         patch("modules.transfer.time.sleep"):
        result = send_all(titles, config)

    assert len(result) == 1


def test_send_all_raises_after_all_retries_fail(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    fail = MagicMock()
    fail.returncode = 1
    fail.stderr = "Connection refused"

    with patch("modules.transfer.subprocess.run", return_value=fail), \
         patch("modules.transfer.time.sleep"):
        with pytest.raises(TransferError, match="Friends.S01E01.mkv"):
            send_all(titles, config)

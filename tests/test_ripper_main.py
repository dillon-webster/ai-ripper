import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config


def _make_config(tmp_path):
    return Config(
        anthropic_api_key="sk-ant-test",
        server_ip="100.100.212.32",
        server_user="dillon",
        jellyfin_url="http://100.100.212.32:8096",
        jellyfin_api_key="jf-key",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        temp_dir=tmp_path / "temp",
        media_root="/media",
    )


def _make_named_titles(tmp_path):
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


def test_main_loop_runs_full_pipeline(tmp_path):
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FRIENDS_S1D1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    named_titles = _make_named_titles(tmp_path)

    # Patch load_config to return our config, then raise StopIteration to break the while True
    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", side_effect=[("FRIENDS_S1D1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.namer.identify", return_value=named_titles), \
         patch("ripper.transfer.send_all", return_value=["remote/path"]), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    mock_discord.assert_called_once_with(
        ["Friends.S01E01.mkv"], success=True, config=config
    )


def test_main_loop_drops_movie_extras_before_transfer(tmp_path):
    config = _make_config(tmp_path)
    disc_path = tmp_path / "INCEPTION"
    disc_path.mkdir()

    raw_titles = [
        {"path": disc_path / "title_t00.mkv", "duration_secs": 6127, "title_index": 0},
        {"path": disc_path / "title_t01.mkv", "duration_secs": 6127, "title_index": 1},
    ]
    feature = {
        "path": tmp_path / "title_t00.mkv", "duration_secs": 6127, "title_index": 0,
        "jellyfin_filename": "Inception.2010.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": False,
    }
    commentary = {
        "path": tmp_path / "title_t01.mkv", "duration_secs": 6127, "title_index": 1,
        "jellyfin_filename": "Inception.2010.Commentary.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": True,
    }

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", side_effect=[("INCEPTION", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=[feature, commentary]), \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    # Only the main feature should be transferred; the commentary extra is dropped.
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == ["Inception.2010.mkv"]
    mock_discord.assert_called_once_with(
        ["Inception.2010.mkv"], success=True, config=config
    )


def test_main_loop_sends_failure_discord_on_rip_error(tmp_path):
    from modules.ripper import RipError
    config = _make_config(tmp_path)
    disc_path = tmp_path / "BAD_DISC"
    disc_path.mkdir()

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", side_effect=[("BAD_DISC", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", side_effect=RipError("makemkvcon crashed")), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    mock_discord.assert_called_once_with(
        [], success=False, error="makemkvcon crashed", config=config
    )

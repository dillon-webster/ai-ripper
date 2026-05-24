import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config
from modules.notifier import trigger_jellyfin_scan, send_discord


def _make_config():
    return Config(
        anthropic_api_key="",
        server_ip="",
        server_user="",
        jellyfin_url="http://100.100.212.32:8096",
        jellyfin_api_key="test-jellyfin-key",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        temp_dir=Path("/tmp"),
    )


def test_trigger_jellyfin_scan_posts_to_correct_url():
    config = _make_config()
    with patch("modules.notifier.urllib.request.urlopen") as mock_urlopen:
        trigger_jellyfin_scan(config)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://100.100.212.32:8096/Library/Refresh"
    assert req.get_header("X-emby-token") == "test-jellyfin-key"
    assert req.get_method() == "POST"


def test_trigger_jellyfin_scan_retries_on_failure():
    config = _make_config()
    import urllib.error

    call_count = 0

    def fake_urlopen(req, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise urllib.error.URLError("connection refused")

    with patch("modules.notifier.urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("modules.notifier.time.sleep"):
        trigger_jellyfin_scan(config)  # Should NOT raise

    assert call_count == 3


def test_trigger_jellyfin_scan_does_not_raise_after_all_retries_fail():
    config = _make_config()
    import urllib.error

    with patch("modules.notifier.urllib.request.urlopen",
               side_effect=urllib.error.URLError("timeout")), \
         patch("modules.notifier.time.sleep"):
        trigger_jellyfin_scan(config)  # Must not raise


def test_send_discord_success_message_contains_titles():
    config = _make_config()
    with patch("modules.notifier.urllib.request.urlopen") as mock_urlopen:
        send_discord(["Friends.S01E01.mkv", "Friends.S01E02.mkv"], success=True, config=config)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    import json
    body = json.loads(req.data.decode())
    assert "✅" in body["content"]
    assert "Friends.S01E01.mkv" in body["content"]
    assert "Friends.S01E02.mkv" in body["content"]


def test_send_discord_failure_message_contains_error():
    config = _make_config()
    with patch("modules.notifier.urllib.request.urlopen") as mock_urlopen:
        send_discord([], success=False, config=config, error="makemkvcon crashed")

    req = mock_urlopen.call_args[0][0]
    import json
    body = json.loads(req.data.decode())
    assert "❌" in body["content"]
    assert "makemkvcon crashed" in body["content"]


def test_send_discord_does_not_raise_after_all_retries_fail():
    config = _make_config()
    import urllib.error

    with patch("modules.notifier.urllib.request.urlopen",
               side_effect=urllib.error.URLError("timeout")), \
         patch("modules.notifier.time.sleep"):
        send_discord(["Friends.S01E01.mkv"], success=True, config=config)  # Must not raise

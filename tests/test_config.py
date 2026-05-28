import os
import pytest
from unittest.mock import patch


def test_load_config_returns_config_with_all_fields():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "SERVER_IP": "100.100.212.32",
        "SERVER_USER": "dillon",
        "JELLYFIN_URL": "http://100.100.212.32:8096",
        "JELLYFIN_API_KEY": "jellyfin-key",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
        "TEMP_DIR": "/tmp/ai-ripper",
        "MEDIA_ROOT": "/media",
    }
    with patch.dict(os.environ, env, clear=True):
        from config import load_config
        config = load_config()
    assert config.anthropic_api_key == "sk-ant-test"
    assert config.server_ip == "100.100.212.32"
    assert config.server_user == "dillon"
    assert config.jellyfin_url == "http://100.100.212.32:8096"
    assert config.jellyfin_api_key == "jellyfin-key"
    assert config.discord_webhook_url == "https://discord.com/api/webhooks/test"
    assert str(config.temp_dir) == "/tmp/ai-ripper"
    assert config.media_root == "/media"


def test_load_config_raises_on_missing_key():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        # SERVER_IP intentionally missing
        "SERVER_USER": "dillon",
        "JELLYFIN_URL": "http://100.100.212.32:8096",
        "JELLYFIN_API_KEY": "jellyfin-key",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
        "TEMP_DIR": "/tmp/ai-ripper",
    }
    with patch.dict(os.environ, env, clear=True), \
         patch("config.load_dotenv"):  # prevent real .env from overriding test env
        with pytest.raises(ValueError, match="SERVER_IP"):
            from config import load_config
            load_config()

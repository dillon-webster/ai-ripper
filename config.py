from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


@dataclass
class Config:
    anthropic_api_key: str
    server_ip: str
    server_user: str
    jellyfin_url: str
    jellyfin_api_key: str
    discord_webhook_url: str
    temp_dir: Path
    media_root: str


def load_config() -> Config:
    load_dotenv()

    required = [
        "ANTHROPIC_API_KEY",
        "SERVER_IP",
        "SERVER_USER",
        "JELLYFIN_URL",
        "JELLYFIN_API_KEY",
        "DISCORD_WEBHOOK_URL",
        "TEMP_DIR",
        "MEDIA_ROOT",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        server_ip=os.environ["SERVER_IP"],
        server_user=os.environ["SERVER_USER"],
        jellyfin_url=os.environ["JELLYFIN_URL"],
        jellyfin_api_key=os.environ["JELLYFIN_API_KEY"],
        discord_webhook_url=os.environ["DISCORD_WEBHOOK_URL"],
        temp_dir=Path(os.environ["TEMP_DIR"]),
        media_root=os.environ["MEDIA_ROOT"],
    )

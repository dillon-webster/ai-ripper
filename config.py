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
    # Phase 3 (Discord approval gate) — optional; blank ⇒ the --approve gate can't
    # run and holds files instead of transferring. The one-way webhook above stays
    # for success/failure notifications.
    discord_bot_token: str = ""
    discord_channel_id: str = ""
    # Optional: a Discord user ID to @mention in the approval message. A direct
    # mention pushes to your phone even when you're active on desktop (Discord
    # otherwise suppresses the mobile push). Blank ⇒ no mention.
    discord_mention_user_id: str = ""
    approval_timeout_secs: int = 1800
    # Web review UI (--review-ui): local curation page served from the rip-box,
    # reachable over the tailnet only — never expose the port to the internet.
    review_ui_port: int = 8765
    review_ui_timeout_secs: int = 1800
    review_ui_thumbs_per_title: int = 12
    # TMDB is the preferred episode-guide source: it has every season's episode list
    # (names + overviews + runtimes) whether or not it's been ripped yet, unlike
    # Jellyfin which only knows seasons already in your library. Blank ⇒ use Jellyfin.
    tmdb_api_key: str = ""


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
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        discord_channel_id=os.getenv("DISCORD_CHANNEL_ID", ""),
        discord_mention_user_id=os.getenv("DISCORD_MENTION_USER_ID", ""),
        approval_timeout_secs=int(os.getenv("APPROVAL_TIMEOUT_SECS", "1800")),
        review_ui_port=int(os.getenv("REVIEW_UI_PORT", "8765")),
        review_ui_timeout_secs=int(os.getenv("REVIEW_UI_TIMEOUT_SECS", "1800")),
        review_ui_thumbs_per_title=int(os.getenv("REVIEW_UI_THUMBS_PER_TITLE", "12")),
        tmdb_api_key=os.getenv("TMDB_API_KEY", ""),
    )

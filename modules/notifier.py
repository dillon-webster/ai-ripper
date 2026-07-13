import json
import logging
import time
import urllib.error
import urllib.request
from typing import List

log = logging.getLogger(__name__)

JELLYFIN_RETRY_DELAYS = [5, 10, 20]
DISCORD_RETRY_DELAYS = [5, 10, 20]


def _post(url: str, headers: dict, body: bytes = None) -> None:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers,
    )
    urllib.request.urlopen(req, timeout=10)


def _with_retry(fn, delays: List[int], label: str) -> None:
    """Call fn(); if it raises URLError, retry with backoff. Never raises."""
    for attempt in range(len(delays) + 1):
        if attempt > 0:
            time.sleep(delays[attempt - 1])
        try:
            fn()
            return
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.warning(f"{label} attempt {attempt + 1} failed: HTTP {e.code} {e.reason} — {body}")
        except urllib.error.URLError as e:
            log.warning(f"{label} attempt {attempt + 1} failed: {e}")
    log.warning(f"{label} failed after {len(delays) + 1} attempts — continuing")


def trigger_jellyfin_scan(config) -> None:
    """POST to Jellyfin Library/Refresh. Retries 3×. Never raises."""
    def do():
        _post(
            f"{config.jellyfin_url}/Library/Refresh",
            headers={"X-Emby-Token": config.jellyfin_api_key},
        )

    _with_retry(do, JELLYFIN_RETRY_DELAYS, "Jellyfin scan")
    log.info("Jellyfin library scan triggered")


def send_review_ready(url: str, show: str, season: int, config) -> None:
    """Ping Discord that the review UI is up, with the link. @mentions the user when
    discord_mention_user_id is set so the message pings/badges instead of scrolling
    by. NOTE: Discord still suppresses the mobile push while a desktop client is
    running (even minimized to tray, even for mentions — tested); fully quit desktop
    Discord to get the phone push. Retries 3×. Never raises."""
    mention = getattr(config, "discord_mention_user_id", "")
    prefix = f"<@{mention}> " if mention else ""
    content = (f"{prefix}🖼️ **{show} Season {season}** is ripped and waiting for review:\n"
               f"{url}\n"
               f"Open from any device on the tailnet (phone/laptop need Tailscale on).")
    payload = json.dumps({
        "content": content,
        # Explicitly allow the user ping — don't rely on webhook mention defaults.
        "allowed_mentions": {"parse": ["users"]},
    }).encode()

    def do():
        _post(
            config.discord_webhook_url,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ai-ripper/1.0",
            },
            body=payload,
        )

    _with_retry(do, DISCORD_RETRY_DELAYS, "Discord review-ready webhook")
    log.info("Discord review-ready notification sent")


def send_discord(titles: List[str], success: bool, config, error: str = "") -> None:
    """Send Discord webhook notification. Retries 3×. Never raises."""
    if success:
        title_lines = "\n".join(f"• {t}" for t in titles)
        content = f"✅ Rip complete! Added to Jellyfin:\n{title_lines}\n\nInsert next disc."
    else:
        content = f"❌ Ripper failed: {error}"

    payload = json.dumps({"content": content}).encode()

    def do():
        _post(
            config.discord_webhook_url,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ai-ripper/1.0",
            },
            body=payload,
        )

    _with_retry(do, DISCORD_RETRY_DELAYS, "Discord webhook")
    log.info(f"Discord notification sent (success={success})")

#!/usr/bin/env python3
"""DVD Auto-Ripper — main entry point."""
import logging
from pathlib import Path

from config import load_config
from modules import disc_watcher, namer, notifier, transfer
from modules import ripper as disc_ripper
from modules.namer import NamerError
from modules.ripper import RipError
from modules.transfer import TransferError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def cleanup_temp(temp_dir: Path) -> None:
    """Delete all .mkv files from temp_dir."""
    for f in temp_dir.glob("*.mkv"):
        f.unlink()
        log.info(f"Deleted temp file: {f}")


def eject_disc(volume_path: Path) -> None:
    """Eject the disc. Uses drutil on macOS, eject on Linux."""
    import platform
    import subprocess
    if platform.system() == "Darwin":
        subprocess.run(["drutil", "eject"], check=False)
    else:
        # On Linux, eject by device path or volume path
        subprocess.run(["eject", str(volume_path)], check=False)
    log.info("Disc ejected")


def main() -> None:
    config = load_config()
    log.info("DVD Auto-Ripper started. Waiting for disc...")

    while True:
        volume_name, volume_path = disc_watcher.wait_for_disc()
        log.info(f"Disc detected: {volume_name} at {volume_path}")

        try:
            titles = disc_ripper.rip(volume_path, config.temp_dir)
            log.info(f"Ripped {len(titles)} title(s)")

            existing = transfer.list_existing_episodes(config)
            titles_ordered = sorted(titles, key=lambda t: t["title_index"], reverse=True)
            named = namer.identify(volume_name, titles_ordered, config.anthropic_api_key, existing_episodes=existing)
            log.info(f"Named {len(named)} title(s)")

            transfer.send_all(named, config)
            log.info("Transfer complete")

            notifier.trigger_jellyfin_scan(config)
            notifier.send_discord(
                [t["jellyfin_filename"] for t in named], success=True, config=config
            )

        except RipError as e:
            log.error(f"Rip failed: {e}")
            notifier.send_discord([], success=False, error=str(e), config=config)

        except (TransferError, NamerError) as e:
            log.error(f"Pipeline failed: {e}")
            notifier.send_discord([], success=False, error=str(e), config=config)

        finally:
            cleanup_temp(config.temp_dir)
            eject_disc(volume_path)
            log.info("Ready for next disc.")


if __name__ == "__main__":
    main()

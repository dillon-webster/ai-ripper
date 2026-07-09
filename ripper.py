#!/usr/bin/env python3
"""DVD Auto-Ripper — main entry point."""
import logging
from pathlib import Path

from config import load_config
from modules import disc_watcher, episode_guide, namer, notifier, transfer
from modules import ripper as disc_ripper
from modules.episode_guide import EpisodeGuideError
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


def main(season: int = None, disc: int = None, show: str = None) -> None:
    config = load_config()
    log.info("DVD Auto-Ripper started. Waiting for disc...")
    if season is not None:
        log.info(
            f"Manual override active: Season {season}"
            + (f", Disc {disc}" if disc is not None else "")
            + (f", Show '{show}'" if show else "")
            + " — this applies to every disc until the ripper is restarted."
        )

    while True:
        volume_name, volume_path = disc_watcher.wait_for_disc()
        log.info(f"Disc detected: {volume_name} at {volume_path}")

        try:
            # Duration-based dedup is DISABLED: many box sets give every disc the
            # same volume label (e.g. "FRIENDS_SERIES_3"), so matching titles by
            # length across discs silently skipped real episodes. Rip every title;
            # the namer handles numbering. (Revisit only with a whole-disc fingerprint.)
            titles = disc_ripper.rip(volume_path, config.temp_dir)
            log.info(f"Ripped {len(titles)} title(s)")

            if not titles:
                log.info("Nothing new on disc — skipping namer/transfer")
                notifier.send_discord([], success=True, config=config)
                continue

            existing = transfer.list_existing_episodes(config)

            # Fetch the real episode list from Jellyfin when we know the show + season,
            # so the namer is constrained to episodes that actually exist (no phantom
            # S01E07-E08 on a 6-episode season) and won't invent double-episode ranges.
            # Degrade gracefully: if the lookup fails, name without it (old behavior).
            guide = None
            if show and season is not None:
                try:
                    guide = episode_guide.get_season_episodes(show, season, config)
                    log.info(f"Episode guide: {len(guide)} episode(s) for '{show}' S{season:02d}")
                except EpisodeGuideError as e:
                    log.warning(f"Episode guide lookup failed ({e}); naming without it")

            # reverse=True is INTENTIONAL and verified — do NOT change without testing
            # on a real disc. Passing titles ascending produced reversed episode numbers
            # (the bug fixed in commit 82e8601); descending is what names them correctly.
            titles_ordered = sorted(titles, key=lambda t: t["title_index"], reverse=True)
            named = namer.identify(
                volume_name, titles_ordered, config.anthropic_api_key,
                existing_episodes=existing, season=season, disc=disc,
                show=show, episode_guide=guide,
            )
            log.info(f"Named {len(named)} title(s)")

            # Drop non-episode content so only real episodes/features transfer:
            # movie bonus content (commentary tracks, featurettes), and — when an
            # episode_guide is in play — TV 'Play All'/compilation titles the namer
            # flags as is_extra because they don't map to a listed episode. Fallback:
            # if this would drop EVERYTHING (namer mis-flagged every title), keep them
            # all rather than transfer nothing.
            kept = [t for t in named if not t.get("is_extra")]
            if kept and len(kept) < len(named):
                for t in named:
                    if t.get("is_extra"):
                        log.info(f"Skipping non-episode title: {t['jellyfin_filename']}")
                named = kept

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
    import argparse

    parser = argparse.ArgumentParser(description="DVD Auto-Ripper")
    parser.add_argument(
        "--season", type=int, default=None,
        help="Force the season number for every disc this session (overrides the disc label). "
             "Use when the volume label has no season, e.g. FAMILY_GUY_DISC1.",
    )
    parser.add_argument(
        "--disc", type=int, default=None,
        help="Disc number within the season (hint for episode numbering when the server is empty).",
    )
    parser.add_argument(
        "--show", type=str, default=None,
        help="Exact show name to look up in Jellyfin (e.g. \"The Office\"). Enables the "
             "provider-aware episode list so numbering is constrained to real episodes. "
             "Requires --season. Overrides the show name inferred from the disc label.",
    )
    args = parser.parse_args()
    main(season=args.season, disc=args.disc, show=args.show)

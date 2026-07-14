#!/usr/bin/env python3
"""DVD Auto-Ripper — main entry point."""
import logging
from pathlib import Path

from config import load_config
from modules import approval, disc_watcher, episode_guide, identify, namer, notifier, transfer
from modules import review_ui as review_ui_mod  # aliased: main()'s review_ui flag shadows the name
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


def name_by_content(titles, guide, show, season, config):
    """Content-based naming (Phase 2): identify each title from its subtitles/frames,
    reconcile onto the real episode list, and drop Play-All/bonus titles. Returns
    (named, dropped): the transfer-ready named titles annotated with their episode
    name (for the approval message), and the dropped titles with their reasons.
    `named` is None if nothing could be kept, so the caller falls back to the legacy
    playback-order namer rather than transfer nothing."""
    guide_names = {e["index"]: e.get("name") for e in guide}
    guide_runtimes = {e["index"]: e.get("runtime_secs") for e in guide if e.get("runtime_secs")}

    # Pre-filter obvious 'Play All'/omnibus titles by length BEFORE the (slow) OCR:
    # a title longer than the season's longest real episode can't BE a single episode,
    # so skip identifying it (the 87-min half-disc compilation is also the slowest to
    # OCR). reconcile still catches subtler omnibus (a 2-episode chunk that's under the
    # cap but far longer than the episode it matched).
    max_runtime = max(guide_runtimes.values(), default=0)
    to_identify, omnibus = [], []
    for t in titles:
        dur = t.get("duration_secs", 0)
        if max_runtime and dur > max_runtime * identify.OMNIBUS_RUNTIME_FACTOR:
            omnibus.append(t)
        else:
            to_identify.append(t)
    for t in omnibus:
        log.info(f"Content-ID: skipping title #{t['title_index']} ({int(t.get('duration_secs', 0) // 60)} min) "
                 "— longer than any episode, not OCR'd (Play-All/omnibus)")

    identified = [identify.identify_title(t, guide, config) for t in to_identify]
    result = identify.reconcile(identified, episode_runtimes=guide_runtimes)
    for t in omnibus:
        result["dropped"].append({
            **t, "episode": None, "method": "skipped",
            "drop_reason": f"Play-All/omnibus ({int(t.get('duration_secs', 0) // 60)} min — "
                           "longer than any episode)",
        })
    for d in result["dropped"]:
        log.info(f"Content-ID dropped title #{d['title_index']} ({d.get('method')}): {d['drop_reason']}")
    if not result["kept"]:
        log.warning("Content-ID matched no episodes on this disc — falling back to legacy namer")
        return None, result["dropped"]
    named = []
    for t in result["kept"]:
        nt = identify.build_named_title(t, show, season)
        nt["episode_name"] = guide_names.get(t["episode"])
        named.append(nt)
        log.info(
            f"Content-ID: title #{nt['title_index']} → {nt['jellyfin_filename']} "
            f"({nt.get('method')}, confidence {nt.get('confidence', 0):.2f})"
        )
    return named, result["dropped"]


def main(season: int = None, disc: int = None, show: str = None,
         content_id: bool = False, dry_run: bool = False, approve: bool = False,
         review_ui: bool = False) -> None:
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

        # `held` (approval declined/timed out) keeps temp files + disc for manual
        # handling, mirroring --dry-run. Reset per disc, read in the finally block.
        held = False
        try:
            # Fetch the real episode list BEFORE ripping when we know the show + season.
            # It serves two jobs: (1) the namer is constrained to episodes that actually
            # exist (no phantom S01E07-E08 on a 6-episode season, no invented ranges);
            # (2) its longest runtime caps rip-time title length, so 'Play All'/omnibus
            # titles are skipped up front instead of ripped for an hour and then dropped.
            # Degrade gracefully: if the lookup fails, rip+name without it (old behavior).
            guide = None
            if show and season is not None:
                try:
                    guide = episode_guide.get_season_episodes(show, season, config)
                    log.info(f"Episode guide: {len(guide)} episode(s) for '{show}' S{season:02d}")
                except EpisodeGuideError as e:
                    log.warning(f"Episode guide lookup failed ({e}); naming without it")

            # A title longer than the longest real episode × the omnibus factor can't BE
            # a single episode — don't rip it at all. (Double-length episodes are safe:
            # their guide runtime is itself ~2×, so they raise the cap.)
            max_title_secs = None
            if guide:
                runtimes = [e["runtime_secs"] for e in guide if e.get("runtime_secs")]
                if runtimes:
                    max_title_secs = int(max(runtimes) * identify.OMNIBUS_RUNTIME_FACTOR)

            # Duration-based dedup is DISABLED: many box sets give every disc the
            # same volume label (e.g. "FRIENDS_SERIES_3"), so matching titles by
            # length across discs silently skipped real episodes. Rip every title;
            # the namer handles numbering. (Revisit only with a whole-disc fingerprint.)
            titles = disc_ripper.rip(volume_path, config.temp_dir, max_title_secs=max_title_secs)
            log.info(f"Ripped {len(titles)} title(s)")

            if not titles:
                log.info("Nothing new on disc — skipping namer/transfer")
                notifier.send_discord([], success=True, config=config)
                continue

            existing = transfer.list_existing_episodes(config)

            # Content-based identification (Phase 2) is the real fix for scrambled disc
            # order: it names each title by what it CONTAINS, not by playback position.
            # Opt-in via --content-id during rollout (it's unproven vs. the legacy namer)
            # and only when we have the guide + show + season to match against. If it
            # can't keep anything, fall through to the legacy playback-order namer.
            named = None
            dropped = []  # titles content-ID/extras filtering won't transfer — shown for approval
            if content_id and guide and show and season is not None:
                named, dropped = name_by_content(titles, guide, show, season, config)

            if named is None:
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
                        dropped.append({**t, "drop_reason": "non-episode extra"})
                named = kept

            # --dry-run: propose the mapping and STOP before writing anything to the
            # library — and, unlike --approve, without even asking. Ripped files are kept
            # and the disc is left in the drive so a real transfer can follow without
            # re-ripping. Drops are already logged above.
            if dry_run:
                log.info(f"DRY RUN — would transfer {len(named)} title(s) "
                         "(nothing written to Jellyfin):")
                for t in named:
                    src = Path(t["path"]).name if t.get("path") else "?"
                    extra = ""
                    if t.get("method"):
                        extra = f"  [{t['method']}, conf {t.get('confidence', 0):.2f}]"
                    log.info(f"  {src} → {t['jellyfin_filename']}{extra}")
                log.info(f"DRY RUN — temp files kept in {config.temp_dir}; disc not ejected.")
                return

            # Web review UI (--review-ui): the user hand-curates the full mapping in
            # the browser — every ripped title (kept AND dropped) can be reassigned,
            # added back, or excluded. Takes the place of the Discord approval gate
            # for this run; decline/timeout/failure HOLDS exactly like --approve.
            if review_ui:
                if approve:
                    log.info("--review-ui supersedes --approve this run — Discord approval skipped.")
                decision = review_ui_mod.request_review(
                    titles, named, dropped, guide, show, season, config)
                if not decision.approved:
                    log.warning(f"Rip held — {decision.reason}. "
                                f"Files kept in {config.temp_dir}; fix and re-run.")
                    notifier.send_discord(
                        [], success=False,
                        error=f"Held for manual handling: {decision.reason}", config=config)
                    held = True
                    continue
                named = decision.titles  # curated list replaces the pipeline's proposal
                log.info(f"Review complete — {decision.reason}. "
                         f"Transferring {len(named)} curated title(s).")

            # Phase 3 approval gate: a human confirms the mapping over Discord before
            # it's written to the library. On decline/timeout/misconfig we HOLD (keep
            # temp files + disc), never transfer a possibly-wrong mapping.
            elif approve:
                decision = approval.request_approval(named, dropped, config)
                if not decision.approved:
                    log.warning(f"Rip held — {decision.reason}. "
                                f"Files kept in {config.temp_dir}; fix and re-run.")
                    notifier.send_discord(
                        [], success=False,
                        error=f"Held for manual handling: {decision.reason}", config=config)
                    held = True
                    continue
                log.info(f"Approved — {decision.reason}. Transferring.")

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
            # Keep the rip + leave the disc in on a dry run OR a held approval so a
            # real transfer can follow without re-ripping.
            if not dry_run and not held:
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
    parser.add_argument(
        "--content-id", action="store_true",
        help="Identify each title by its CONTENT (subtitles→OCR, frames→vision) and match "
             "it to the real episode list, instead of trusting makemkv playback order. Fixes "
             "scrambled discs. Requires --show and --season; falls back to the legacy namer "
             "if it can't confidently match.",
    )
    parser.add_argument(
        "--approve", action="store_true",
        help="Post the proposed episode mapping to Discord and WAIT for a human to tap "
             "Approve/Fix before transferring (Phase 3 gate). On Fix/timeout, or if the "
             "bot isn't configured, files are held in TEMP_DIR and nothing is written. "
             "Requires DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID. Ignored with --dry-run.",
    )
    parser.add_argument(
        "--review-ui", action="store_true",
        help="Serve a local web page (tailnet-only, port REVIEW_UI_PORT) to review and "
             "hand-curate the episode mapping before transfer: every ripped title is "
             "shown with a filmstrip of stills and can be reassigned, added back, or "
             "excluded. Takes the place of --approve for the run. Requires --show and "
             "--season (needs the episode guide). On timeout/failure files are held in "
             "TEMP_DIR. Ignored with --dry-run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Rip and name the disc, print the proposed episode mapping, then STOP before "
             "transferring anything to Jellyfin. Ripped files are kept and the disc is left "
             "in the drive. Use this to validate --content-id on a real disc without writing "
             "a possibly-wrong mapping to the library (there is no approval gate yet).",
    )
    args = parser.parse_args()
    main(season=args.season, disc=args.disc, show=args.show,
         content_id=args.content_id, dry_run=args.dry_run, approve=args.approve,
         review_ui=args.review_ui)

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


def as_seasons(season) -> list:
    """Normalize the season override to a sorted list: 4 → [4], "4,5"/[5,4] → [4, 5],
    None → []. One disc can hold two seasons (a volume box set), so every consumer
    downstream takes a list."""
    if season is None:
        return []
    if isinstance(season, int):
        return [season]
    return sorted({int(s) for s in season})


def season_label(seasons: list) -> str:
    """How a season override reads in a log line or a Discord ping: 'S04', 'S04+S05'."""
    return "+".join(f"S{s:02d}" for s in seasons)


def name_by_content(titles, guide, show, seasons, config):
    """Content-based naming (Phase 2): identify each title from its subtitles/frames,
    reconcile onto the real episode list, and drop Play-All/bonus titles. Returns
    (named, dropped): the transfer-ready named titles annotated with their episode
    name (for the approval message), and the dropped titles with their reasons.
    `named` is None if nothing could be kept, so the caller falls back to the legacy
    playback-order namer rather than transfer nothing.

    `seasons` may hold more than one (a volume disc): the guide is then a single pool
    spanning both, every slot is addressed by identify.episode_key, and each title is
    named from the season of the episode it actually MATCHED — not from the run's."""
    key = identify.episode_key
    guide_names = {key(e.get("season"), e["index"]): e.get("name") for e in guide}
    guide_runtimes = {key(e.get("season"), e["index"]): e["runtime_secs"]
                      for e in guide if e.get("runtime_secs")}
    guide_keys = [key(e.get("season"), e["index"]) for e in guide]
    # Fallback season for a title that arrives without one (the legacy single-season
    # path, where the model was never asked which season it was looking at).
    sole_season = seasons[0] if len(seasons) == 1 else None

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
    if sole_season is not None:
        identified = [t if t.get("season") is not None else {**t, "season": sole_season}
                      for t in identified]
    result = identify.reconcile(identified, episode_runtimes=guide_runtimes,
                                guide_keys=guide_keys)
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
        nt = identify.build_named_title(t, show, sole_season)
        nt["episode_name"] = guide_names.get(identify.episode_key(t.get("season"), t["episode"]))
        named.append(nt)
        log.info(
            f"Content-ID: title #{nt['title_index']} → {nt['jellyfin_filename']} "
            f"({nt.get('method')}, confidence {nt.get('confidence', 0):.2f})"
        )
    return named, result["dropped"]


def fetch_guide(show, seasons, config):
    """The episode list for every season this disc might hold, as one pool sorted in
    broadcast order and tagged with the season each episode came from.

    A season whose lookup fails is skipped with a warning rather than sinking the whole
    guide: half a volume disc's episodes still get matched, and the other half surface
    as unmatched at the review gate instead of being misnamed. Returns None when
    nothing could be fetched (the caller then rips without a guide, as before)."""
    entries = []
    for s in seasons:
        try:
            episodes = episode_guide.get_season_episodes(show, s, config)
        except EpisodeGuideError as e:
            log.warning(f"Episode guide lookup failed for S{s:02d} ({e}); naming without it")
            continue
        entries.extend(
            {**e, "season": e["season"] if e.get("season") is not None else s}
            for e in episodes
        )
        log.info(f"Episode guide: {len(episodes)} episode(s) for '{show}' S{s:02d}")
    if not entries:
        return None
    entries.sort(key=lambda e: (e.get("season") or 0, e["index"]))
    if len(seasons) > 1:
        log.info(f"Volume disc: {len(entries)} candidate episode(s) across "
                 f"{season_label(seasons)} — each title is filed under the season it matches.")
    return entries


def process_disc(volume_name, volume_path, config, season=None, disc: int = None,
                 show: str = None, content_id: bool = False, dry_run: bool = False,
                 approve: bool = False, review_ui: bool = False) -> bool:
    """Run one disc through the whole pipeline: rip → name → gate → transfer.

    Returns whether the caller should keep watching for more discs. Only --dry-run
    returns False (it has produced its proposal and left the disc in the drive, so
    waiting for another one would do nothing). Every other outcome — success, a
    held approval, a rip failure — leaves the watcher ready for the next disc."""
    # Blu-ray vs DVD decides the destination: a Blu-ray's raw rip is 20-40GB+,
    # so it's staged locally for encoding before it ever reaches the server,
    # while a DVD transfers straight there. Detected from the disc structure
    # (BDMV vs VIDEO_TS) — no flag to remember/forget. See the transfer step below.
    seasons = as_seasons(season)
    kind = disc_watcher.disc_type(volume_path)
    is_bluray = kind == "bluray"
    log.info(f"Disc detected: {volume_name} at {volume_path} (type: {kind})")

    # `held` (approval declined/timed out) keeps temp files + disc for manual
    # handling, mirroring --dry-run. Read in the finally block.
    held = False
    try:
        # Fetch the real episode list BEFORE ripping when we know the show + season.
        # It serves two jobs: (1) the namer is constrained to episodes that actually
        # exist (no phantom S01E07-E08 on a 6-episode season, no invented ranges);
        # (2) its longest runtime caps rip-time title length, so 'Play All'/omnibus
        # titles are skipped up front instead of ripped for an hour and then dropped.
        # Degrade gracefully: if the lookup fails, rip+name without it (old behavior).
        guide = fetch_guide(show, seasons, config) if show and seasons else None

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
            return True

        existing = transfer.list_existing_episodes(config)

        # Content-based identification (Phase 2) is the real fix for scrambled disc
        # order: it names each title by what it CONTAINS, not by playback position.
        # Opt-in via --content-id during rollout (it's unproven vs. the legacy namer)
        # and only when we have the guide + show + season to match against. If it
        # can't keep anything, fall through to the legacy playback-order namer.
        named = None
        dropped = []  # titles content-ID/extras filtering won't transfer — shown for approval
        if content_id and guide and show and seasons:
            named, dropped = name_by_content(titles, guide, show, seasons, config)

        if named is None:
            # reverse=True is INTENTIONAL and verified — do NOT change without testing
            # on a real disc. Passing titles ascending produced reversed episode numbers
            # (the bug fixed in commit 82e8601); descending is what names them correctly.
            #
            # The legacy namer numbers within ONE season by playback order, so it can't
            # split a volume disc at the boundary: it gets the first season and names
            # everything into it. Loud, because that's exactly the mapping the review
            # gate exists to catch.
            #
            # --disc is withheld from it there, too. The namer reads it as "disc N OF
            # THAT SEASON; earlier discs hold earlier episodes" — and disc 2 of Volume 8
            # is not disc 2 of season 7. A volume's discs don't line up with either
            # season's disc order, so the hint could only mislead the one thing that
            # reads it.
            disc_hint = disc
            if len(seasons) > 1:
                disc_hint = None
                log.warning(f"Falling back to the legacy playback-order namer, which "
                            f"can't span {season_label(seasons)} — every title will be "
                            f"numbered into S{seasons[0]:02d}"
                            + (f", and disc {disc} is not a disc number within that "
                               "season so it's being ignored" if disc is not None else "")
                            + ". Check the review page carefully before transferring.")
            titles_ordered = sorted(titles, key=lambda t: t["title_index"], reverse=True)
            named = namer.identify(
                volume_name, titles_ordered, config.anthropic_api_key,
                existing_episodes=existing, season=seasons[0] if seasons else None,
                disc=disc_hint, show=show, episode_guide=guide,
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
            return False

        # --review-ui curates TV episode slots; a movie disc has none (no show,
        # season, or episode guide), so the review page would have nothing to
        # assign. Skip it for movies and transfer the namer's result directly —
        # still honoring --approve below if that was also passed.
        disc_is_movie = any(t.get("media_type") == "movie" for t in named)
        if review_ui and disc_is_movie:
            log.info("--review-ui applies to TV episode slots only; this is a "
                     "movie disc — skipping review and transferring the namer's result.")

        # Web review UI (--review-ui): the user hand-curates the full mapping in
        # the browser — every ripped title (kept AND dropped) can be reassigned,
        # added back, or excluded. Takes the place of the Discord approval gate
        # for this run; decline/timeout/failure HOLDS exactly like --approve.
        if review_ui and not disc_is_movie:
            if approve:
                log.info("--review-ui supersedes --approve this run — Discord approval skipped.")
            decision = review_ui_mod.request_review(
                titles, named, dropped, guide, show, seasons, config)
            if not decision.approved:
                log.warning(f"Rip held — {decision.reason}. "
                            f"Files kept in {config.temp_dir}; fix and re-run.")
                notifier.send_discord(
                    [], success=False,
                    error=f"Held for manual handling: {decision.reason}", config=config)
                held = True
                return True
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
                return True
            log.info(f"Approved — {decision.reason}. Transferring.")

        filenames = [t["jellyfin_filename"] for t in named]
        if is_bluray:
            # Blu-ray: stage locally for manual encoding — do NOT push to the
            # server or trigger a Jellyfin scan (the files aren't there yet).
            staged = transfer.stage_local(named, config)
            log.info(f"Staged {len(staged)} Blu-ray title(s) for encoding")
            notifier.send_discord(filenames, success=True, config=config, staged=True)
        else:
            transfer.send_all(named, config)
            log.info("Transfer complete")
            notifier.trigger_jellyfin_scan(config)
            notifier.send_discord(filenames, success=True, config=config)

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
    return True


def main(season=None, disc: int = None, show: str = None,
         content_id: bool = False, dry_run: bool = False, approve: bool = False,
         review_ui: bool = False, once: bool = False) -> None:
    config = load_config()
    seasons = as_seasons(season)
    log.info("DVD Auto-Ripper started. Waiting for disc...")
    if seasons:
        scope = (
            " — one disc only (--once)." if once else
            " — this applies to every disc until the ripper is restarted."
        )
        which = (f"Season {seasons[0]}" if len(seasons) == 1 else
                 "Seasons " + " and ".join(str(s) for s in seasons) + " (volume disc)")
        log.info(
            f"Manual override active: {which}"
            + (f", Disc {disc}" if disc is not None else "")
            + (f", Show '{show}'" if show else "")
            + scope
        )

    while True:
        volume_name, volume_path = disc_watcher.wait_for_disc()
        keep_watching = process_disc(
            volume_name, volume_path, config, season=season, disc=disc, show=show,
            content_id=content_id, dry_run=dry_run, approve=approve, review_ui=review_ui,
        )
        if not keep_watching:
            return
        # --once exists so a one-off run (the dashboard's TV-disc job) ENDS by
        # itself. Left looping, its --show/--season override would silently be
        # applied to whatever disc went in next — a movie named as an episode of
        # the last box set.
        if once:
            log.info("--once: disc handled; exiting instead of waiting for another.")
            return


# The season box is a hint for ONE disc. A wider net is just a bigger candidate pool
# for content-ID to get wrong, and no volume disc spans three seasons.
_MAX_SEASONS_PER_DISC = 3


def _seasons_arg(raw: str) -> list:
    """--season 4 | 4,5 | 4-5 → [4] | [4, 5] | [4, 5].

    A volume box set isn't a season (Family Guy shipped as Volumes 1-12), so one disc
    can hold the end of one season and the start of the next; naming both puts every
    episode of the disc in the candidate pool. Anything ambiguous is refused outright —
    a mistyped season silently misnames a whole disc."""
    import argparse

    raw = (raw or "").strip()
    if "-" in raw and "," not in raw:
        lo, _, hi = raw.partition("-")
        try:
            lo, hi = int(lo), int(hi)
        except ValueError:
            raise argparse.ArgumentTypeError("season range must look like 4-5")
        if hi < lo:
            raise argparse.ArgumentTypeError("season range must ascend, e.g. 4-5")
        values = list(range(lo, hi + 1))
    else:
        try:
            values = [int(p) for p in raw.split(",") if p.strip()]
        except ValueError:
            raise argparse.ArgumentTypeError(
                "season must be a number, or a list/range like 4,5 or 4-5")
    if not values:
        raise argparse.ArgumentTypeError("season is required")
    for n in values:
        if not 0 <= n <= 99:
            raise argparse.ArgumentTypeError("season must be between 0 and 99")
    values = sorted(set(values))
    if len(values) > _MAX_SEASONS_PER_DISC:
        raise argparse.ArgumentTypeError(
            f"at most {_MAX_SEASONS_PER_DISC} seasons on one disc")
    return values


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DVD Auto-Ripper")
    parser.add_argument(
        "--season", type=_seasons_arg, default=None,
        help="Force the season for every disc this session (overrides the disc label). "
             "Use when the volume label has no season, e.g. FAMILY_GUY_DISC1. Give BOTH "
             "seasons — \"4,5\" or \"4-5\" — when the disc is from a volume box set and "
             "straddles the boundary: the episode guide is then fetched for both and each "
             "title is named from the season of the episode it actually matched.",
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
        "--once", action="store_true",
        help="Handle ONE disc and exit, instead of looping to wait for the next one. "
             "Use for a one-off run with --show/--season: without it the override stays "
             "in force and the next disc inserted — a movie, another show — is named as "
             "an episode of this season.",
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
         review_ui=args.review_ui, once=args.once)

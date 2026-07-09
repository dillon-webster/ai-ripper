# Episode identification rework — progress & next steps

_Last updated: 2026-07-08. Branch: `phase1-provider-aware-episodes`._
_Full design: [episode-identification-plan.md](./episode-identification-plan.md)._

## Why we're doing this

The ripper decides episode identity from **makemkv playback order**, which is
unreliable per disc — sometimes reversed (Family Guy), sometimes fully scrambled
(The Office US S1 disc 1, which kicked this off). It also invents episodes from
"Play All" disc titles (fabricated `S01E07-E08` / `S01E09-E10` on a 6-episode
season). Root cause: identity comes from *position*, not from *content* or the
*real episode list*.

## What we did tonight

- **Hand-fixed The Office S1 disc 1** on the server: identified all 6 episodes by
  pulling thumbnails (they were scrambled, not just reversed), renamed via
  temp-name swap, triggered a Jellyfin refresh. Final order is correct.
  The two bogus Play-All files were deleted by the user.
- **Agreed a 4-phase plan** to fix the root cause (see design doc).
- **Built Phase 1** (provider-aware numbering), committed on the branch:
  - New `modules/episode_guide.py` — fetches a season's real episode list from
    Jellyfin (numbers, `IndexNumberEnd`, names, runtimes) over stdlib urllib.
  - New `--show "Name"` flag so the Jellyfin series is named explicitly instead
    of guessed from the (often garbage) disc label. Requires `--season`.
  - `namer.identify` / `_build_prompt` take the show name + episode guide; when a
    guide is present the model is constrained to real episode numbers and the
    phantom-generating "double-length → two episodes" heuristic is **disabled**.
  - Non-episode titles (Play-All/compilation) get flagged `is_extra` and dropped
    by the existing filter in `ripper.py::main`.
  - Backward compatible: no guide ⇒ old behavior unchanged.
  - Tests added (`test_episode_guide.py`, `test_namer_guide.py`); 66 pass.
  - **Verified live** against Jellyfin on The Office S1: 6 real titles → E01–E06,
    two Play-All titles flagged `is_extra`.

## Decisions locked

- Keep passing `--season/--disc` by hand; add `--show` to name the series.
- Source of truth = **Jellyfin** API (numbering matches what the library shows).
- Content signal = **subtitles** (primary), frames→vision (fallback).
  Confirmed the ripped MKVs embed `dvd_subtitle` (VobSub, image) tracks → needs OCR.
- Approval = **propose-then-confirm over Discord**, reviewable/approvable from
  phone. Needs upgrading the webhook to a **bot** (buttons: Approve / Fix).
  Chosen **blocking** wait over staged.

## Known limitation of Phase 1

Phase 1 does **not** fix scrambled order. When every episode runs ~22 min there's
no duration signal, so the model still numbers by playback order. Ordering is
fixed in Phase 2.

## What's next

- **Phase 2 — content-based identification** (`modules/identify.py`):
  extract the first ~2 min of the subtitle track → OCR (tesseract / vobsub2srt)
  → LLM-match against the candidate episode list; frames→vision fallback when a
  disc has no usable subs. Reconcile rules separate real episode / Play-All
  duplicate / genuine double / bonus feature. This is what fixes the scramble.
- **Phase 3 — Discord approval pipeline** (`modules/approval.py`): webhook → bot,
  post mapping + thumbnails + Approve/Fix buttons, blocking wait, transfer on
  approve. New deps: `discord.py`; new env: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`.
- **Phase 4 — rollout**: keep the legacy `reverse=True` path behind a fallback
  until content-ID is proven on several discs; merge the branch.

## Try Phase 1 on the next disc

```
python ripper.py --season 1 --disc 1 --show "The Office"
```

If the Jellyfin lookup fails it logs a warning and names the disc the old way.

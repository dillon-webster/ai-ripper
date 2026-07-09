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

## Phase 2 — content-based identification (module built, not yet wired)

`modules/identify.py` exists on this branch (`test_identify.py`, 20 tests; 86 total pass):

- `identify_title(title, candidates, config)` — tries the subtitle path first
  (ffprobe finds the track → mkvextract pulls VobSub → vobsubocr/tesseract OCR →
  first ~2 min of dialogue → `claude-opus-4-8` matches it against the Phase-1
  candidate list), falls back to frames→vision (~3 frames via ffmpeg, skipping the
  15–50s title montage) when subs are missing/ambiguous. Returns `episode=None`
  when nothing matches (bonus/compilation) — never a forced guess.
- Out-of-list episode numbers from the model are clamped to `None`, so a title can
  never be numbered as an episode the season doesn't have.
- `reconcile(identified)` maps identities onto real numbers: drops unmatched titles
  (bonus), and when several titles claim one episode keeps the shortest (real) and
  drops the longer 'Play All' omnibus copies. Nothing is deleted from disk — drops
  are surfaced for the Phase-3 approval step.
- Model note: `identify.py` uses `claude-opus-4-8` (current default). `namer.py`
  still pins the older `claude-sonnet-4-6` — left unchanged for now.

**Wired into `ripper.py::main` (2026-07-09), opt-in behind `--content-id`.** When
`--content-id --show "X" --season N` is passed and the guide resolves, `name_by_content`
runs `identify_title` per title → `reconcile` → `build_named_title`, bypassing the
legacy `sorted(reverse=True)` playback-order namer. Falls back to the legacy namer if
content-ID keeps nothing (never transfers nothing). `namer.py` model bumped to
`claude-opus-4-8` too. 91 tests pass (`test_ripper_main.py` covers the content-ID path
+ the fallback).

**Still TODO for Phase 2:** verify OCR output on a real disc. ffprobe/ffmpeg/mkvextract
were smoke-tested against a synthesized MKV (track detection + frame grab + degradation
work). **VobSub OCR uses a patched `vobsub2srt`.** History: upstream `vobsub2srt` won't
build on Tesseract 5 (removed API); tried `vobsubocr` (Rust) instead — its leptonica
bindings won't build on leptonica 1.86 either. Landed on patching `vobsub2srt` (no
leptonica dep, only libtiff + Tesseract): the patch is a cmake-min flag, `<climits>`,
`-ansi`→`-std=c++17`, forcing the modern-API `#define`, and `TesseractRect`→`SetImage`
+`GetUTF8Text`. **Build + link + run verified; OCR-output correctness NOT yet verified**
(couldn't synthesize a real VobSub offline — ffmpeg can't encode text→bitmap subs). The
patched binary is installed to `~/.local/bin`; `install-linux.sh` reproduces the patched
build. Missing tool → auto frames→vision fallback.

## What's next

- **Verify `--content-id` on a real disc** — install the toolchain, run
  `python ripper.py --content-id --show "The Office" --season 1`, confirm a scrambled
  disc comes out correctly numbered. This is the real proof the scramble is fixed.
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

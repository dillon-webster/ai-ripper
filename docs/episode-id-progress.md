# Episode identification rework — progress & next steps

_Last updated: 2026-07-09. Branch: `phase1-provider-aware-episodes`._
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

## Phase 2 — content-based identification (built + wired, opt-in) — 2026-07-09

`modules/identify.py` + wiring in `ripper.py::main`. **91 tests pass**
(`test_identify.py` 23, `test_ripper_main.py` 5 incl. the content-ID path + fallback).

- `identify_title(title, candidates, config)` — tries the subtitle path first
  (ffprobe finds the track → mkvextract pulls the VobSub `.idx/.sub` → `vobsub2srt`
  OCRs to SRT → first ~2 min of dialogue → `claude-opus-4-8` matches it against the
  Phase-1 candidate list), falls back to frames→vision (~3 frames via ffmpeg, skipping
  the 15–50s title montage) when subs are missing/ambiguous. Returns `episode=None`
  when nothing matches (bonus/compilation) — never a forced guess.
- Out-of-list episode numbers from the model are clamped to `None`, so a title can
  never be numbered as an episode the season doesn't have.
- `reconcile(identified)` maps identities onto real numbers: drops unmatched titles
  (bonus), and when several titles claim one episode keeps the shortest (real) and
  drops the longer 'Play All' omnibus copies. Nothing is deleted from disk — drops
  are surfaced for the Phase-3 approval step.
- `build_named_title` / `build_filename` turn a reconciled title into the
  `jellyfin_filename`/`media_type`/`destination` dict `transfer.send_all` expects.
- Both `identify.py` and `namer.py` now use `claude-opus-4-8`.

**Wiring (`ripper.py::main`, opt-in behind `--content-id`):** when
`--content-id --show "X" --season N` is passed and the Jellyfin guide resolves,
`name_by_content` runs `identify_title` per title → `reconcile` → `build_named_title`,
**bypassing** the legacy `sorted(reverse=True)` playback-order namer. If content-ID
keeps nothing it falls back to the legacy namer (never transfers nothing). No flag ⇒
old behavior, fully unchanged.

**OCR toolchain — solved, but a patch to carry.** VobSub OCR uses a **patched
`vobsub2srt`**. Two dead ends first: upstream `vobsub2srt` won't build on Tesseract 5
(removed API), and `vobsubocr` (Rust) won't build on leptonica 1.86 (its bindings lag).
`vobsub2srt` won because it has **no leptonica dep** (only libtiff + Tesseract). The
patch (5 changes, all scripted in `install-linux.sh`): `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`,
add `#include <climits>`, CMakeLists `-ansi -pedantic`→`-std=c++17`, add
`#define CONFIG_TESSERACT_NAMESPACE 1` (forces the modern instance-API branch), and
`TessBaseAPI::TesseractRect(image,1,stride,0,0,w,h)` → `SetImage(image,w,h,1,stride)`
+ `GetUTF8Text()`. **Build + link + run verified**; the patched binary is at
`~/.local/bin/vobsub2srt`. ffprobe/ffmpeg/mkvextract were smoke-tested against a
synthesized MKV (track detect + frame grab + graceful degradation all work). Missing
tool ⇒ auto frames→vision fallback.

**The one unverified thing:** OCR-*output* correctness. Couldn't synthesize a real
VobSub `.idx/.sub` offline (ffmpeg refuses text→bitmap sub encoding; no VobSub generator
packaged), so the `SetImage`/`GetUTF8Text` port is faithful-but-unproven end to end.
The real test is the first disc rip (see below).

## What's next

- **Verify `--content-id` on a real disc** — install the toolchain, run
  `python ripper.py --content-id --show "The Office" --season 1`, confirm a scrambled
  disc comes out correctly numbered. This is the real proof the scramble is fixed.
- **Phase 3 — Discord approval pipeline** (`modules/approval.py`): webhook → bot,
  post mapping + thumbnails + Approve/Fix buttons, blocking wait, transfer on
  approve. New deps: `discord.py`; new env: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`.
- **Phase 4 — rollout**: keep the legacy `reverse=True` path behind a fallback
  until content-ID is proven on several discs; merge the branch.

## Try it on the next disc

**Phase 2 (content-based, the scramble fix) — needs the patched `vobsub2srt` on PATH:**
```
python ripper.py --content-id --show "The Office" --season 1
```
Watch the logs for `Content-ID: title #N → The.Office.S01E0X.mkv (subtitles, confidence …)`.
`method=subtitles` means OCR matching ran; `method=frames` means it fell back to vision.
This is the disc that finally proves OCR output correctness end to end.

**Phase 1 only (provider-aware numbering, no content-ID):**
```
python ripper.py --season 1 --disc 1 --show "The Office"
```
If the Jellyfin lookup fails, either command logs a warning and names the disc the old way.

## Handy pointers for the next session

- Patched vobsub2srt source lived in the scratchpad this session; `install-linux.sh`
  rebuilds it from scratch (clone + scripted patch + `sudo make install`). The already-
  built binary is in `~/.local/bin/`.
- `sudo`/interactive installs can't run from the agent here (sudo needs a terminal) —
  ask the user to run them via the `!` prefix.
- Everything is **uncommitted** on `phase1-provider-aware-episodes` as of this handoff.

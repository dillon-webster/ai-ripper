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
  OCRs the whole track to SRT → `_srt_dialogue` samples dialogue **spread across the
  episode** (windows at 8/30/55/80% of runtime, skipping the opening recap) →
  `claude-opus-4-8` matches it against the Phase-1 candidate list), falls back to
  frames→vision (~3 frames via ffmpeg, skipping the 15–50s title montage) when subs are
  missing/ambiguous. Returns `episode=None` when nothing matches (bonus/compilation) —
  never a forced guess.
  - **Sampling was opening-only (first 2 min) until 2026-07-09 verification** showed
    that fooled serialized-arc episodes whose cold opens are near-identical (Friends
    S10 adoption B-plot). Spreading the sample across the runtime fixed it — see the
    verification section below.
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

## Phase 2 — verification against real ripped MKVs — 2026-07-09

Verified end-to-end **without a disc**, using the already-ripped `Friends/Season 10`
MKVs (17 files, VobSub tracks present) as a labelled corpus. Ground truth = the
filenames (spot-checked against actual OCR'd content).

- **OCR output correctness: PROVEN.** `vobsub2srt` produces clean, readable English
  (~11s/file). This was the one thing flagged as unverified — it works.
- **Opening-only sampling was a real accuracy bug.** First pass scored 3/6 on a
  sample; the misses were all in Friends S10's serialized adoption→birth-mother→finale
  arc, whose cold opens all discuss the same baby/adoption B-plot. The model reported
  **0.9+ confidence on wrong answers**, so confidence can't gate.
- **Fix 1 — spread the sample.** Sample dialogue across the runtime (`_srt_dialogue` +
  `SUBTITLE_SAMPLE_FRACTIONS`) instead of the first 120s. **Free** — `vobsub2srt` already
  OCRs the whole track; we just read more of the SRT. Took the sample from 3/6 → 5/6, but
  the full season was **14/17 (82%)** — misses E10→E8, E11→E6, E17-E18→E1.
- **Diagnosis of the residual misses:** the model was matching rich OCR'd dialogue against
  **cryptic episode titles only** ("The One with Ross's Grant" tells it nothing about the
  sampled scene). E11's sampled scene was the $100k-Pyramid / stripper subplot; nothing in
  its title says so, so the model guessed E6.
- **Fix 2 — feed the model plot summaries.** `episode_guide.get_season_episodes` now fetches
  Jellyfin `Overview` for each episode; `identify._candidate_lines` renders it under each
  choice (capped `_OVERVIEW_MAX_CHARS`). The match becomes dialogue → real summary instead
  of dialogue → title. **Result: 16/17 (94%).** E11 and the finale (now correctly `E17-E18`,
  range included) both resolved.
- **Fix 3 — robustness bug found by the run.** A malformed model reply raised `IdentifyError`
  and **crashed the whole season run** (`identify_title` didn't catch it, violating its own
  "never crash, degrade to episode=None" contract). `identify_title` now catches it and
  degrades subtitles→frames→`episode=None`. Regression tests added. **95 unit tests pass.**
- **Only remaining miss: E10→E8, at 0.85 — the model's lowest confidence of the run.** E10's
  sampled dialogue overlaps E08's Thanksgiving theme. It collides with the correct E08, so
  `reconcile` flags it (two titles claim E08, none claims E10) and surfaces it for approval —
  it won't silently corrupt the library.

**Read on the result:** Friends S10 is close to a worst case — heavily serialized, many
same-length episodes. At **94%** content-ID is a large improvement over playback-order
guessing, but not 100%, and confidence is only weakly informative (the one miss was the
lowest-confidence answer, but plenty of correct answers also sat at 0.9). This **confirms
the Phase-3 human-approval gate is load-bearing, not optional** — but the mapping it will
present is now usually right. A more episodic show (The Office, the original trigger) should
score higher. `reconcile`'s keep-shortest dedup still can't distinguish a wrong same-length
match, so drops MUST be surfaced for approval, never applied silently.

**Possible cheap follow-up (not done):** pass the ripped title's own runtime into the
subtitle prompt; widen/add sample windows. Diminishing returns vs. overfitting to this season.

## Phase 2 — PROVEN ON A REAL SCRAMBLED DISC — 2026-07-09

Ran `ripper.py --content-id --show "The Office" --season 1 --dry-run` on **The Office US
S1 disc 1 — the scrambled disc that started this project.** Full end-to-end: makemkv
ripped 8 titles → Jellyfin guide (6 eps) → subtitle OCR + match → reconcile → dry-run
proposal. **Complete success:**

- Disc playback order was genuinely scrambled: title indices t00,t01,t02,t03,t04,t05
  are episodes **1,3,6,2,4,5**. Content-ID recovered the true numbering — all six by
  **subtitles at 0.97–0.99 confidence**, no frame fallback:
  `t00→E01, t03→E02, t01→E03, t04→E04, t05→E05, t02→E06`.
- The two Play-All titles (44.8 min, 55.9 min) were correctly **dropped** by reconcile
  ("duplicate of E05 / E04 (longer — 'Play All')").
- **Order independently confirmed** (not just by the model's confidence): OCR'd the
  cold-open of each of the 6 files and matched them to the canonical episodes —
  E01 Pilot ("quarterlies/grasshopper"), E02 Diversity Day, E03 Health Care ("I heal
  them"), E04 The Alliance (downsizing / "assistant TO the regional manager"), E05
  Basketball, E06 Hot Girl (the $1,000 incentive). All six correct.
- `--dry-run` behaved: proposal printed, nothing transferred, 8 MKVs kept in
  `/var/tmp/ai-ripper`, disc left in the drive. So the real transfer can follow by
  re-running the same command **without** `--dry-run` (no re-rip).

This closes the last mock-only gap (rip→content-ID integration). The scramble bug that
motivated the whole rewrite is fixed. Note: the OCR phase for 8 titles (incl. two long
omnibus titles) took ~12 min — vobsub2srt OCRs the whole track; acceptable but a known cost.

## What's next

- ~~Verify `--content-id` on a real disc~~ **DONE 2026-07-09** — The Office S1 disc 1
  unscrambled 6/6 + 2 correct Play-All drops (see section above). Still **no approval gate**,
  so a plain `--content-id` run transfers straight to Jellyfin — use `--dry-run` until Phase 3.
- **Phase 3 — Discord approval pipeline** (`modules/approval.py`): webhook → bot,
  post mapping + thumbnails + Approve/Fix buttons, blocking wait, transfer on
  approve. New deps: `discord.py`; new env: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`.
- **Phase 4 — rollout**: keep the legacy `reverse=True` path behind a fallback
  until content-ID is proven on several discs; merge the branch.

## Try it on the next disc

**Phase 2 (content-based, the scramble fix) — needs the patched `vobsub2srt` on PATH.
Validate with `--dry-run` FIRST (no approval gate exists, so a plain run auto-transfers):**
```
python ripper.py --content-id --show "The Office" --season 1 --dry-run   # propose only
python ripper.py --content-id --show "The Office" --season 1             # real transfer
```
`--dry-run` logs `DRY RUN — would transfer …` with each `title → S01E0X.mkv [method, conf]`
and stops (rip + disc kept). Also watch for `Content-ID: title #N → …` and
`Content-ID dropped title #N …`. `method=subtitles` means OCR matching ran; `method=frames`
means it fell back to vision. This disc proves the rip→content-ID integration end to end.

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

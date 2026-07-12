# Episode identification rework — progress & next steps

_Last updated: 2026-07-09. Branch: `phase1-provider-aware-episodes` (commit `9152ede`, pushed)._
_Full design: [episode-identification-plan.md](./episode-identification-plan.md)._

**TL;DR for the next session:** Phases 1, 2 & 3 are **done and PROVEN LIVE.** On 2026-07-09
the full pipeline ran end-to-end on The Office **S2 disc 1**: TMDB guide (22 eps) → 3 Play-All
omnibus dropped → E01–E06 matched on subtitles at 0.95–0.99 → approved from phone over Discord
(with per-episode thumbnails) → transferred + Jellyfin scan. **115 tests pass.** Two things this
run added that you MUST know: (a) the episode guide now comes from **TMDB** (needs `TMDB_API_KEY`
in `.env`), because Jellyfin only knows already-ripped seasons — an empty guide was why a brand-new
season fell back to the legacy namer; (b) reconcile now drops **Play-All/omnibus** titles by
runtime. Remaining: two small follow-ups (held-notification + `--resume`, see "Follow-ups") then
Phase 4 merge to `main`. Commits `9c2e2dd`→`2f43d0f` on `phase1-provider-aware-episodes`, not pushed.

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

## Phase 3 — Discord approval gate (BUILT, not yet proven live) — 2026-07-09

Replaces the `--dry-run` stopgap with a human gate so a disc transfers with one tap from
your phone. **104 tests pass.** Code is committed on `phase1-provider-aware-episodes` (see
git log for the exact hash). Still uncommitted at time of writing if you're reading this
mid-session — check `git status`.

- **New module `modules/approval.py`.** `request_approval(named, dropped, config) -> Decision`.
  Uses a Discord **bot** (the one-way webhook can't receive taps). Posts a **header embed**
  (episode count + the dropped Play-All/bonus list with durations) followed by **one image
  embed per proposed episode** — title `SxxEyy — name`, source + `method · conf`, and a
  **thumbnail frame** grabbed ~40% into the episode by ffmpeg (`_extract_thumbnail`, downscaled
  480px, best-effort) — plus two buttons **✅ Approve / ✏️ Fix**. Capped at 9 episode embeds
  (Discord's 10-embed limit; overflow listed in the header). If no thumbnails can be extracted
  (ffmpeg missing/failed) it **falls back to a single compact text embed** so approval still
  works. **Blocking** wait with a long timeout (default 1800s, `APPROVAL_TIMEOUT_SECS`).
  Synchronous to the caller: extracts thumbnails, then runs its own asyncio loop, posts,
  blocks for the tap, tears down, returns.
  - **Fails safe, never raises.** Bot not configured / gateway or login failure / Fix tap /
    timeout all return `Decision(approved=False)` → the caller HOLDS the files. Only an
    explicit Approve returns `approved=True`.
  - `discord.py` is **lazy-imported inside the function**, so the rest of the pipeline and the
    whole test suite import fine without it installed. Pure formatting helpers
    (`format_mapping/format_dropped/format_proposal`) are unit-tested with no discord dep.
- **Config/env:** `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `APPROVAL_TIMEOUT_SECS` (all
  optional, blank ⇒ gate can't run and holds). Added to `config.py` + `.env.example`. New dep
  `discord.py` in `requirements.txt` (**not yet installed on the rip box — `pip install` it**).
- **Wiring (`ripper.py::main`, behind new `--approve` flag):** slots in right after the
  `--dry-run` return and before `transfer.send_all`. On Approve → transfer + Jellyfin scan +
  success webhook as before. On decline/timeout → `held=True`: skip transfer, keep temp files
  **and** leave the disc in the drive (same as `--dry-run`, via `if not dry_run and not held`
  in the `finally`), post a "Held for manual handling" webhook, `continue` to the next disc.
  `--dry-run` takes precedence (propose + stop, never even asks). `name_by_content` now returns
  `(named, dropped)` and annotates each named title with its `episode_name` for the embed.
- **Chose an explicit `--approve` flag** rather than making approval the default for
  `--content-id` (the plan floated that): explicit is non-breaking and predictable; recommended
  invocation is now `--content-id --approve`. Easy to flip to default later if desired.

### Phase 3 — what's left before it's done
1. **User: create the Discord bot (one-time).** Discord Developer Portal → New Application →
   Bot tab → Add Bot → **Reset Token** and copy it → OAuth2 → URL Generator → scope `bot`,
   permissions **Send Messages + Embed Links** → open the URL, invite it to your server. Get
   the channel id: Discord Settings → Advanced → **Developer Mode** on → right-click the target
   channel → **Copy Channel ID**. Put both in `.env` (`DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`).
   Message Content intent is **not** needed (buttons come over the gateway regardless).
2. **User: `pip install discord.py`** into the rip box venv (it's in requirements now).
3. **Prove it live** on a real disc: `python ripper.py --content-id --show "X" --season N
   --approve`. Confirm the embed renders, Approve transfers, Fix/timeout holds.

## Follow-ups — quick wins for next session (2026-07-09)

Both surfaced while proving Phase 3 live on The Office S2 disc 1. Small, independent,
well-scoped; do either first. **Branch `phase1-provider-aware-episodes`; run tests with
`./venv/bin/python -m pytest`.**

### 1. Held ≠ failure — give holds their own Discord notification
**Problem:** when you tap ✏️ Fix (or approval times out), `ripper.py::main`'s held branch calls
`notifier.send_discord([], success=False, error=f"Held for manual handling: {reason}")`, which
renders as `❌ Ripper failed: Held for manual handling…`. A deliberate hold reads as a crash.
**Fix:** give holds a distinct message. Either add a `held`/`status` path to
`notifier.send_discord`, or add `notifier.send_hold(reason, kept_count, temp_dir, config)` posting
e.g. `⏸️ Rip held for manual handling — N file(s) kept in {temp_dir}, nothing transferred. Fix and
re-run.` Call it from the held branch instead of `send_discord(success=False, …)`. Real failures
(RipError/TransferError/NamerError) keep the ❌ path. **Touches:** `modules/notifier.py`,
`ripper.py` (held branch, ~line 170), `tests/test_notifier.py`, and
`tests/test_ripper_main.py::test_main_loop_approve_holds_files_when_declined` (currently asserts
`send_discord` called with `success is False` — update to assert the new hold call).

### 2. `--resume` — reuse the held rip, skip re-ripping
**Problem:** after a Fix/hold or `--dry-run`, re-running re-rips AND re-OCRs the whole disc (~10+
min). Tonight we worked around it with a one-off finisher script. Make it first-class.
**Behavior:** `--resume` builds the `titles` list from the MKVs already in `TEMP_DIR` instead of
calling `disc_ripper.rip` — for each `*.mkv`: `path`, `duration_secs` via ffprobe, `title_index`
from the `_t(\d+)` in the name (reuse `modules.ripper._parse_title_index`). Then run the normal
content-ID → reconcile → approval → transfer path. There's no disc to wait for, so `--resume`
should SKIP `disc_watcher.wait_for_disc()` and do ONE pass then return (like `--dry-run`), not
loop. On a later hold it must still keep temp/disc. **Working prototype:** the scratchpad finisher
`finish_held_disc.py` (built exactly this — build titles from temp → `ripper.name_by_content` →
`approval.request_approval` → on approve `transfer.send_all` + `notifier.trigger_jellyfin_scan`);
port its title-building into a `titles_from_temp(temp_dir)` helper in `ripper.py`. Add tests
mirroring the content-ID main tests but feeding titles from a temp dir.

## What's next — Phase 4 (rollout / merge)

Legacy `reverse=True` path already stays as an auto-fallback when content-ID can't resolve.
Phase 3 is now **proven live** (The Office S2 disc 1: TMDB guide → 3 omnibus dropped → E01–E06
matched on subtitles 0.95–0.99 → approved on phone → transferred + Jellyfin scan). After a couple
more discs and the two follow-ups above, merge `phase1-provider-aware-episodes` → `main`.

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
- Phase 2 is **committed + pushed** (`9152ede`) on `phase1-provider-aware-episodes`. 96 tests pass.
- The verification rip (8 MKVs of The Office S1 disc 1) was watched, confirmed correct, and
  **deleted** from `/var/tmp/ai-ripper` — temp dir is clean.
- `dillon-pc` is the rip box: makemkvcon + ffmpeg/ffprobe/mkvextract/tesseract + patched
  `vobsub2srt` (`~/.local/bin`) + `mpv` all installed. Optical drive is `/dev/sr0`; udisks2
  mounts discs under `/run/media/$USER/<LABEL>`.
- **Gotcha for a live re-test:** `disc_watcher.wait_for_disc()` only fires on *insertion* — it
  seeds itself with discs already mounted at startup. If a disc is already in the drive when
  you launch, eject+reinsert after starting, or drive the pipeline directly.
- **Don't delete a show's season from Jellyfin to "test from scratch":** `episode_guide` pulls
  the episode list (the match key) *from* Jellyfin, so removing it makes content-ID fall back
  to the scrambled legacy namer.

## Review UI v1 (--review-ui) — built 2026-07-12, pending real-disc validation

`modules/review_ui.py` per docs/review-ui-plan.md: a stdlib `ThreadingHTTPServer`
inside the ripper serves a self-contained curation page (filmstrip of ffmpeg
stills per ripped title — kept AND dropped — episode dropdowns, season slot
panel, lightbox with ±10/±60s stepping, dupe guard client- AND server-side).
`request_review` mirrors `approval.request_approval`: blocks, never raises,
holds on timeout/failure. `--review-ui` supersedes `--approve` for the run;
`--dry-run` still wins. Thumbs cache in `<temp_dir>/review-thumbs/` (cleaned on
shutdown), grabs capped at 2 concurrent ffmpegs, filmstrips lazy-load per card.
Config: `REVIEW_UI_PORT` (8765), `REVIEW_UI_TIMEOUT_SECS` (1800),
`REVIEW_UI_THUMBS_PER_TITLE` (12).

Verified without a real rip (temp dir was clean): full HTTP round trip against
synthetic mpeg2+ac3 MKVs (page, thumb grabs incl. cache hit + out-of-range clamp,
400 on duplicate assignment, curated submit returning the reassigned list, cache
dir cleanup) plus the page JS run under gjs/SpiderMonkey with a DOM stub. **Next
disc ripped with `--review-ui` is the real validation pass** (v1 grabs are light —
safe even during a rip). v2 live playback: only if filmstrips prove insufficient.

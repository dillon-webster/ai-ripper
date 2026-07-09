# Plan: content-based episode identification + approval pipeline

Status: **proposed — for review, no code changes yet.**
Date: 2026-07-08

## Problem

Three recurring failures, all with one root cause.

1. **Scrambled order.** `ripper.py` sorts ripped titles by makemkv `title_index`
   (`reverse=True`) and the namer assigns E01, E02… *sequentially in that order*.
   makemkv's title order is unreliable per disc: sometimes reversed (Family Guy),
   sometimes a full random permutation (The Office S1 disc 1). When every episode
   runs ~22 min there is no duration signal to recover the truth, so numbering is a
   guess. No sort direction fixes a random permutation.
2. **Phantom episodes.** The namer's "double-length → two episode numbers" rule
   (`modules/namer.py::_build_prompt`) invented `S01E07-E08` and `S01E09-E10` on a
   **6-episode** season, from two "Play All" omnibus titles.
3. **Bonus-feature junk.** Omnibus / bonus titles get ripped and misnamed as
   episodes. The existing duration filter in `modules/ripper.py` only drops the
   *single* title whose length ≈ the sum of all others; *partial* omnibus titles
   (e.g. eps 1+2 only) slip through.

**Root cause:** the namer decides episode identity from *disc position*, not from
*content* or a *real episode list*.

## Design overview

Replace position-based numbering with **content-based identification against the
real season episode list**, then a **human-in-the-loop approval** before transfer.

New pipeline in `ripper.py::main` (replaces the `sorted(..., reverse=True)` +
`namer.identify` + `is_extra` block, lines ~67–90):

1. You pass `--season/--disc` (unchanged).
2. Rip every title (unchanged) → `disc_ripper.rip` returns
   `[{path, duration_secs, title_index}]`.
3. **Fetch the season's real episode list** from Jellyfin (numbers, titles,
   runtimes, `IndexNumberEnd`).
4. **Identify each ripped title by content** (subtitle dialogue → LLM match;
   frames → vision as fallback), producing `(episode_number, confidence)`.
5. **Reconcile**: drop unmatched (bonus) and duplicate (Play-All) titles; map the
   rest onto real episode numbers. No reliance on `title_index` order.
6. **Propose → confirm** over Discord (buttons); on approve → `transfer.send_all`
   + `notifier.trigger_jellyfin_scan`.

`reverse=True` and the double-length heuristic are retired from the primary path
(kept only behind a fallback flag during rollout — see Phase 4).

---

## Phase 1 — provider-aware episode list (deterministic, low risk)

**New module `modules/episode_guide.py`.**

- `get_season_episodes(show_name, season, config) -> List[Episode]`
  where `Episode = {index, index_end, name, runtime_secs}`.
- Source of truth = **Jellyfin** (its numbering is what the library displays;
  memory: Jellyfin is TMDB-numbered). Calls:
  - Resolve series id: `GET /Items?IncludeItemTypes=Series&Recursive=true&SearchTerm=<show>`
    (auth header `X-Emby-Token`). Pick best name match.
  - Episodes: `GET /Shows/{seriesId}/Episodes?season=<N>&fields=Overview,RunTimeTicks`
    → read `IndexNumber`, `IndexNumberEnd`, `Name`, `RunTimeTicks`.
- Fallback to TMDB direct if the show isn't in Jellyfin yet (new show, first rip).

**Deterministic guarantees this buys us, independent of identification:**
- Episode numbers are capped to the real list → no phantom E07+.
- A double-length title is only named as a range when the provider actually has a
  spanning episode (`IndexNumberEnd` set).
- Anything that can't map to a real episode is a candidate to drop.

**Tests:** unit-test the mapping/reconcile logic with fixture episode lists
(6-episode season + omnibus titles; a real double-episode season).

---

## Phase 2 — content-based identification

**New module `modules/identify.py`.**

`identify_title(mkv_path, candidates, config) -> {episode, confidence, method}`

### Primary signal: subtitle dialogue
- Extract the main subtitle track's **first ~2 min** from the ripped MKV.
  DVD subs are image-based (VobSub/PGS) → OCR needed:
  `mkvextract` the subtitle track → OCR with tesseract (`vobsub2srt` /
  Subtitle Edit CLI). Trim to the first ~2 min by timestamp.
- **Prereq:** the rip must *contain* the subtitle track. The current
  `makemkvcon mkv` call uses makemkv's default profile — verify it retains
  full+forced subtitle tracks (adjust the profile / `minlength` if not).
- LLM match: give Claude the candidate episode list (titles + short summaries +
  runtimes) and the OCR'd dialogue → return the matching episode + confidence.
  This is constrained multiple-choice, not open-ended → high accuracy.

### Fallback signal: frames → vision
- For titles with no usable subtitle track or low OCR/LLM confidence: grab ~3
  frames (avoid t≈15–50s — that's the shared title montage; use t<12s teaser and
  t≈300s/500s deep plot), send to Claude vision with the same candidate list.
  This is exactly the manual method that worked on The Office S1.

### Reconcile rules (Phase 1 list + Phase 2 identities)
For each ripped title we now have (identity, confidence, duration):
- **Real episode:** matches one candidate; runtime ≈ that candidate's runtime. Keep.
- **Play-All / omnibus:** matches a candidate already claimed by a shorter title,
  and runtime ≈ 2× (or more). Duplicate → **drop**.
- **Genuine double:** matches one candidate, runtime ≈ provider runtime for it, and
  provider marks it spanning (`IndexNumberEnd`). Keep as `E12-E13`.
- **Bonus feature:** matches *no* candidate. **Drop.**

Never drop a would-be episode silently — surface drops in the approval message
(Phase 3) so a bad OCR that orphaned a real episode is caught, not lost.

---

## Phase 3 — Discord approval pipeline

**Upgrade one-way webhook → a bot** (only new infra).

- New env: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` (or your user id for a DM),
  optional `APPROVAL_TIMEOUT_SECS`.
- New dep: `discord.py`.
- **New module `modules/approval.py`.**
  `request_approval(proposed_mapping, thumbnails, dropped, config) -> Decision`
  - Bot posts an embed: the mapping table (title → SxxEyy + episode name), a
    thumbnail per episode, and a "dropped as non-episode" list with durations.
  - Two buttons: **✅ Approve** / **✏️ Fix**. (Optionally also accept a text reply
    as a bonus path.) One tap on the mobile app.
  - **Blocking** wait (chosen default): the ripper waits for the tap, with a long
    timeout. On timeout or **Fix** → hold files in `temp_dir`, do **not** transfer,
    post "held for manual handling." (Staged/queued mode is a later option if the
    waiting annoys — parks pending rips so the next disc can start immediately.)
- On **Approve** → `transfer.send_all` + `notifier.trigger_jellyfin_scan`, then the
  existing success `send_discord`.

**Bot setup (one-time, click-by-click provided at implementation):** create a
Discord application → add a Bot → copy token → invite to your server with
Send Messages + Embed Links + (Message Content if using text replies) → put token
+ channel id in `.env`.

---

## Phase 4 — sequencing & regression safety

Ship in order; each phase is independently valuable:

1. **Phase 1** first — smallest, deterministic, kills phantom episodes and the old
   Friends double-episode shift on its own. Does *not* fix the scramble yet.
2. **Phase 2** — the load-bearing fix for the scramble.
3. **Phase 3** — the confirm/approval UX and bonus-feature drop reporting.

**Don't break shows that already work:**
- Keep the old `reverse=True` + double-length path behind a fallback flag
  (`--legacy-order` or auto-fallback when Phase 1/2 can't resolve confidently)
  until content-ID is proven on several discs.
- Since approval is human-gated, a mis-ID is caught on the phone, not written to
  the library — so rollout risk is low.
- Add fixtures/tests for reconcile logic before wiring it into `main`.

## New dependencies / env
- Python: `discord.py` (+ existing `anthropic`, `python-dotenv`).
- System: `mkvtoolnix` (`mkvextract`), `tesseract-ocr`, a VobSub OCR helper
  (`vobsub2srt` or Subtitle Edit CLI). Add to `install-linux.sh`.
- `.env`: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `APPROVAL_TIMEOUT_SECS?`.

## Open questions / risks
- **Subtitle presence:** confirm the makemkv profile actually retains subtitle
  tracks; if not, Phase 2 primary signal is unavailable and we lean on frames.
- **OCR quality** on old DVD subs — mitigated by "enough text to match," not perfect
  transcription, plus the frames fallback.
- **Show-name resolution** to a Jellyfin series id from a garbage volume label — you
  already pass `--season/--disc`; consider also allowing `--show` to remove guessing.
- **Blocking vs staged** — starting blocking; revisit if it gets annoying.

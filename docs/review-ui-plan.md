# Implementation Plan: Web-based Disc Review UI (`--review-ui`)

**Status:** not started. Write this feature on the `phase1-provider-aware-episodes`
branch (where the content-ID pipeline lives). Repo venv is `./venv`
(`./venv/bin/python -m pytest`) — the system python3 has no pytest.

## Goal

A local web page that lets the user review a ripped disc and **hand-curate** the
episode mapping before anything transfers to Jellyfin, replacing the need to
inspect frames from the command line. The current Discord approval gate
(`modules/approval.py`) is Approve/Fix only — it can't fix a wrong mapping. This
adds a full editor.

The user's requirement, verbatim intent: *"see everything that was ripped, and if
the ripper got it wrong, look at the other files and put them in or take something
out."* So the page must show **every ripped title** (the ones the content-ID
pipeline kept AND the ones it dropped), let the user **watch any of them**, and
**reassign / add / remove** titles against the season's episode list. The pipeline's
proposal is only the starting point.

## Decisions already locked with the user

- **Full video playback** (not just thumbnails). The user wants to actually watch
  a title to identify it. Files are DVD rips: `mpeg2video` + `ac3` audio +
  `dvd_subtitle` (VobSub) in an MKV — confirmed via ffprobe. **No browser plays any
  of those natively**, so playback REQUIRES on-the-fly transcoding to H.264/AAC MP4.
- **Supplement, not replace** the Discord flow. Add behind a new opt-in flag
  `--review-ui`. Do not touch `modules/approval.py`'s behavior. When `--review-ui`
  is passed it takes the place of the Discord approval call for that run.
- **No new pip dependency** for the server. Use the Python **stdlib**
  (`http.server.ThreadingHTTPServer`) — the endpoints are simple and this keeps the
  rip-box venv clean. (ffmpeg is already installed and used elsewhere.)

## Key facts / integration points (verified 2026-07-11)

- `ripper.py::main(season, disc, show, content_id, dry_run, approve)` — add a
  `review_ui: bool = False` parameter and an argparse `--review-ui` flag next to
  `--content-id` (~ripper.py:251). Pass it through at the bottom `main(...)` call
  (~ripper.py:272).
- `name_by_content(titles, guide, show, season, config)` returns `(named, dropped)`
  (ripper.py:41, called at :147). `titles` (the FULL rip list) is in scope in `main`
  — the review UI needs all three: `titles`, `named`, `dropped`.
- The approval gate is `ripper.py:194-204`:
  ```python
  if approve:
      decision = approval.request_approval(named, dropped, config)
      if not decision.approved:
          ... hold ...; held = True; continue
  transfer.send_all(named, config)
  ```
  The review UI slots in the same place. Precedence: `dry_run` (:179) still wins and
  returns early. Suggest: `if review_ui:` branch BEFORE `if approve:` (or make them
  mutually exclusive — if both passed, prefer review_ui and log that Discord is
  skipped this run).
- A title dict from the rip is `{path: Path, title_index: int, duration_secs: int}`.
- A `named` (kept) title additionally has `episode`, `index_end`, `jellyfin_filename`,
  `episode_name`, `confidence`, `method`, `is_extra`, `media_type`, `destination`.
- A `dropped` title has `drop_reason` (and `episode` may be set or None).
- **Reuse `identify.build_named_title(title, show, season)`** (identify.py:424) to
  produce a transfer-ready named title from a `{..., "episode": N}` dict — it sets
  `jellyfin_filename`, `media_type="tv"`, `destination="tvshows"`, `is_extra=False`.
  After calling it, set `episode_name` from the guide (`{e["index"]: e["name"]}`).
- The guide is `List[{index, index_end, name, runtime_secs, overview}]`
  (`episode_guide.get_season_episodes(show, season, config)`), TMDB-sourced.
- `transfer.send_all(named, config)` is what writes to the server; it consumes the
  named-title shape above. So the review UI's job is to return a correct `named` list.
- `config.temp_dir` = `/var/tmp/ai-ripper` (held MKVs live here — good test fixtures).
- Mirror the **contract of `approval.request_approval`**: synchronous from the
  caller, blocks until decision or timeout, and **NEVER raises** — on any failure
  (port in use, timeout, etc.) return "not approved" so `main` holds the files.

## Architecture

New module **`modules/review_ui.py`**. Public entry:

```python
@dataclass
class ReviewDecision:
    approved: bool
    reason: str = ""
    titles: Optional[List[Dict]] = None   # curated named-titles to transfer (when approved)

def request_review(all_titles, named, dropped, guide, show, season, config,
                   timeout=None) -> ReviewDecision:
    """Launch the local review server, log the URL, block until the user submits
    (approved with a curated `titles` list) or timeout/failure (approved=False,
    caller holds). Never raises."""
```

`main` usage:
```python
if review_ui:
    decision = review_ui.request_review(titles, named, dropped, guide, show, season, config)
    if not decision.approved:
        ... hold (same as approval decline) ...
    named = decision.titles          # curated list replaces the proposal
    # then fall through to transfer.send_all(named, config)
```

### Server

- `ThreadingHTTPServer` bound to `0.0.0.0` on a configurable port
  (`config.review_ui_port`, default e.g. 8765) so it's reachable from a laptop/phone
  on the LAN. Log the URL with the rip-box hostname/IP.
- Run it in a background thread; `request_review` waits on a `threading.Event` that
  the POST `/submit` handler (or a timeout) sets, then shuts the server down and
  returns.
- Timeout default from `config.review_ui_timeout_secs` (reuse
  `APPROVAL_TIMEOUT_SECS` style; ~1800s). On timeout → `ReviewDecision(False,
  "review timed out")`.
- State shared with the handler (the titles, guide, an `outcome` holder, the Event)
  via a closure or handler class attributes.

### Endpoints

| Route | Purpose |
|-------|---------|
| `GET /` | The review page (self-contained HTML; inline CSS/JS, no external assets). |
| `GET /thumb/<title_index>?t=<sec>` | One JPEG frame via ffmpeg (reuse the approach in `approval._extract_thumbnail`: `ffmpeg -ss <t> -i <mkv> -frames:v 1 -q:v 5 -vf scale=480:-1 -f mjpeg pipe:1`). Cache to a temp dir so repeat loads are instant. |
| `GET /play/<title_index>?t=<sec>` | **Live transcode stream.** See below. |
| `POST /submit` | Body = JSON `{ "assignments": { "<title_index>": <episode_int or null> } }`. Build the curated `named`, set outcome + Event, respond 200. `null`/absent = excluded. |

### Transcoding for playback (the hard part)

DVD MPEG-2 can't be remuxed — it must be re-encoded. Do it **on demand, from a seek
offset**, streamed as fragmented MP4 so playback starts in a few seconds without
pre-transcoding the whole 42-min file:

```
ffmpeg -ss <t> -i <mkv> \
  -c:v libx264 -preset veryfast -crf 26 \
  -c:a aac -ac 2 \
  -movflags frag_keyframe+empty_moov+default_base_moof \
  -f mp4 pipe:1
```

- Handler streams ffmpeg stdout to `wfile` in chunks; respond `200` with
  `Content-Type: video/mp4`. Kill the ffmpeg process when the client disconnects
  (broken pipe) so a closed tab doesn't leave encoders running.
- Seeking: the frontend can't scrub past the streamed buffer. Give the `<video>` a
  companion **time slider**; on change, reload `src = /play/<idx>?t=<sec>` to restart
  the stream at that point. Good enough to spot-check any part of a title.
- `-ss` before `-i` = fast input seek (keyframe-accurate enough for ID).
- **CPU note:** transcoding is heavy and only happens when the user hits Watch, so it
  won't compete with a rip unless actively used. Still, cap concurrency (one active
  transcode at a time is fine for one reviewer).
- Subtitles: not needed for v1 (the user is identifying by picture/action). Optionally
  burn in VobSub later with `-vf ...overlay` — skip for now.

### Frontend (single self-contained page)

- **Season panel:** list E01…EN from the guide. Each row shows the episode number +
  name, and its current assignment (which title_index fills it) or **EMPTY**. Update
  live as the user changes dropdowns (client-side JS) so gaps/duplicates are obvious.
- **Titles panel:** a card per entry in `all_titles`, ordered by `title_index`. Each
  card:
  - thumbnail (`<img src=/thumb/<idx>>`, lazy),
  - filename + duration + the pipeline's guess ("→ S04E01 Fun Run · subtitles 0.98"
    or "dropped: 44-min Play-All"),
  - a **Watch** button → reveals a `<video>` + time slider pointed at `/play/<idx>`,
  - an **episode `<select>`**: options = "— exclude —" plus every guide episode
    (`E01 … EN`), defaulting to the current assignment.
- **Duplicate guard:** if two titles select the same episode, highlight both and
  disable Submit with a message. (Server should also re-validate and reject dupes.)
- **Submit** button → POST the assignments JSON.
- Keep all CSS/JS inline in the page string. No frameworks, no CDN.

### Building the curated `named` on submit

```python
guide_names = {e["index"]: e.get("name") for e in guide}
by_index = {t["title_index"]: t for t in all_titles}
named = []
for tidx_str, episode in assignments.items():
    if episode is None:
        continue                      # excluded / not an episode
    src = by_index[int(tidx_str)]     # {path, title_index, duration_secs}
    nt = identify.build_named_title({**src, "episode": episode}, show, season)
    nt["episode_name"] = guide_names.get(episode)
    named.append(nt)
# validate: no duplicate episode numbers; at least one kept (else warn)
return ReviewDecision(True, "reviewed via web UI", titles=named)
```

## New config / flags

- argparse `--review-ui` (store_true) in `ripper.py`, threaded through `main`.
- `config.py`: `review_ui_port` (env `REVIEW_UI_PORT`, default 8765),
  `review_ui_timeout_secs` (env `REVIEW_UI_TIMEOUT_SECS`, default 1800). Follow the
  existing `load_config()` env pattern (config.py:34+).

## Testing

Mirror `approval.py`'s split: pure logic unit-tested, I/O deferred.

- **Unit (no server, no ffmpeg):**
  - assignment JSON → curated `named`: correct episodes, `jellyfin_filename`,
    `episode_name`; excluded titles dropped; duplicate-episode rejected.
  - page-model assembly: every ripped title appears; guide slots show filled/empty;
    a dropped title still appears with its `drop_reason`.
  - `request_review` failure paths return `approved=False` (never raises): bad port,
    timeout (use a tiny timeout in the test), empty guide.
- **Manual / integration (do when NO rip is running — transcoding is CPU-heavy):**
  drive it against the held MKVs in `/var/tmp/ai-ripper`. Verify: page loads, thumbs
  render, Watch streams and is seekable via the slider, reassigning + excluding +
  submitting yields the expected `named`, and `main` transfers exactly that set.
- Keep the full suite green (`./venv/bin/python -m pytest` — currently 128 tests).

## Task breakdown (suggested order)

1. `modules/review_ui.py`: `ReviewDecision`, pure helpers (page-model assembly,
   assignments→`named`, HTML render). Unit tests for these first.
2. Server + handler (`GET /`, `/thumb`, `/submit`) + blocking `request_review` with
   the Event/timeout contract. Unit-test the failure/timeout paths.
3. `/play` live-transcode endpoint + client disconnect handling.
4. Frontend page (season panel, title cards, watch/slider, dropdowns, dup guard).
5. Wire `--review-ui` into `ripper.py::main` + argparse + `config.py`. Ensure
   `dry_run` precedence and the hold path (decline/timeout keeps temp + disc,
   `held = True`, `continue`) match the existing approval branch.
6. Manual integration pass against held MKVs (no active rip). Update
   `docs/episode-id-progress.md` and the memory notes when done.

## Gotchas

- **Never raise** out of `request_review` — `main` must be able to hold on failure.
- Bind `0.0.0.0`, not `127.0.0.1`, or a phone/laptop can't reach it. Log the actual
  LAN URL.
- Kill ffmpeg on client disconnect (broken pipe on `wfile.write`) to avoid orphaned
  transcodes.
- `-ss` position semantics: fast-seek before `-i`; acceptable for identification.
- Don't run the heavy transcode integration test while a rip is in progress.
- Two titles → same episode = duplicate `jellyfin_filename` = clobber on transfer.
  Guard both client- and server-side.
- The held/decline path must replicate `ripper.py:196-203` exactly (send the "held"
  notifier, set `held = True`, `continue`) so temp files + disc are preserved.

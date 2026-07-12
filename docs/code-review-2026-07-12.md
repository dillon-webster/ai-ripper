# Codebase review — 2026-07-12

Read-only review of the full codebase (all modules, entry point, config) on branch
`phase1-provider-aware-episodes`. Overall the failure-handling philosophy — hold files
rather than transfer blind, never let approval/review crash the rip — is consistently
applied. Two bug-class issues and a handful of smaller improvements below.

## High priority

### 1. Held/dry-run files poison the next disc, then get deleted

When a review is declined or times out (`held=True` → `continue` at `ripper.py:209`),
the held `.mkv` files stay in `TEMP_DIR` — but the loop then waits for the *next* disc
and rips into the same directory. Two problems follow:

- `rip()` matches output files by globbing `temp_dir/*.mkv` and parsing `_t(\d+)` from
  the names (`modules/ripper.py:168-184`). The held disc's `SOMETHING_t03.mkv` and the
  new disc's `_t03.mkv` both parse to title index 3, so the **old disc's file** can be
  returned as part of the new disc's rip and transferred under the new disc's episode
  name.
- When the next disc succeeds, the `finally` block's `cleanup_temp()`
  (`ripper.py:248-249`) deletes **everything**, including the held rip that was "kept
  for manual handling; fix and re-run." The hold guarantee only survives until the next
  disc.

**Fix:** rip each disc into a fresh per-disc subdirectory (e.g.
`TEMP_DIR/<label>-<timestamp>/`) so holds are isolated and cleanup is scoped.
Cheaper stopgap: refuse to start a new rip while leftover `.mkv` files exist.

**Operational note until fixed:** after a Fix/timeout hold, deal with the held files
before inserting another disc.

### 2. An unexpected exception deletes a good rip

The main loop only catches `RipError`, `TransferError`, and `NamerError`
(`ripper.py:237-243`). The content-ID path can raise things outside that set:

- `subprocess.TimeoutExpired` from `identify._run` — the 300s OCR timeout propagates,
  since `extract_subtitle_text` only catches `FileNotFoundError`;
- any `anthropic.APIError` from the matching calls;
- a `KeyError` from a malformed namer response (`namer.py:259`).

Any of those crashes the process, and on the way out the `finally` block runs with
`held=False` — so it **deletes the freshly ripped files and ejects the disc**.

**Fix:** a broad `except Exception` in the per-disc loop that notifies and sets
`held=True`, so the crash path holds files like every other failure path does.

## Medium priority

- **`--content-id`/`--review-ui` prerequisites aren't enforced.** Both say "Requires
  --show and --season" but nothing validates it — forget `--show` and the run silently
  degrades to the legacy playback-order namer, the exact thing being avoided. Add a
  `parser.error()` when the combination is invalid.
- **`transfer.list_existing_episodes` swallows SSH failures** (`transfer.py:75-76`):
  a failed `ssh find` returns `[]` with no log, which the legacy namer reads as
  "empty season → start at E01." At minimum log a warning; ideally distinguish
  "server unreachable" and hold.
- **`namer.identify` uses `max_tokens=1024`** — a disc with many titles plus long
  filenames can truncate the JSON array, and the retry will truncate identically,
  ending in `NamerError`. Cheap to bump to 4096.
- **No timeout on `makemkvcon` subprocesses** (`modules/ripper.py:108,161`) — a wedged
  drive hangs the loop forever with no notification. Everything else in the codebase
  has a timeout.

## Low priority

- **OCR runs serially** (`ripper.py:69` — one `identify_title` at a time).
  mkvextract + Tesseract per title is the slow part of a content-ID run; a small
  thread pool (2–3 workers) would roughly halve wall-clock time, and the rip is
  already done at that point so CPU contention isn't a concern.
- **Review UI binds 0.0.0.0 with no auth** (`review_ui.py:357`) — the docstring treats
  the tailnet as the trust boundary, but 0.0.0.0 also exposes the port to the LAN the
  rip-box sits on. If that LAN has anything untrusted on it, bind to the Tailscale
  interface IP or check a token in the URL.
- **Cosmetic:** an empty disc sends the "✅ Rip complete! Added to Jellyfin" webhook
  with zero titles (`ripper.py:124`), and files skipped as already-existing on the
  server are still announced as added.

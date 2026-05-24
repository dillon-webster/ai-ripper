# DVD Auto-Ripper — Design Spec

**Date:** 2026-05-23  
**Status:** Approved

---

## Overview

A persistent background service that monitors for DVD/Blu-ray insertion on macOS, automatically rips all titles, uses the Anthropic API to identify and name each title in Jellyfin-compatible format, transfers files to a home server via SCP, triggers a Jellyfin library scan, and sends a Discord notification when ready for the next disc.

---

## Architecture

```
ai-ripper/
├── ripper.py              # Entry point: main polling loop + pipeline orchestration
├── config.py              # Loads .env, validates required vars, exposes typed Config object
├── modules/
│   ├── __init__.py
│   ├── disc_watcher.py    # Polls /Volumes/ for new optical disc
│   ├── ripper.py          # Shells out to makemkvcon; returns title list
│   ├── namer.py           # Calls Anthropic API; returns Jellyfin-compatible filenames
│   ├── transfer.py        # SCP each file to server with retry logic
│   └── notifier.py        # Jellyfin scan trigger + Discord webhook
├── .env.example           # Template for all required config vars
├── install.sh             # Registers ripper.py as a launchd daemon
├── com.dillon.ripper.plist # launchd plist template
└── requirements.txt
```

### Data Flow

```
disc_watcher → (volume_name, volume_path)
    → ripper → [{path, duration_secs, title_index}, ...]
        → namer → [{path, jellyfin_filename, media_type, destination}, ...]
            → transfer → files sent to server
                → notifier.jellyfin_scan()
                → notifier.discord_notify(titles)
                    → eject + wait for next disc
```

Each module is a plain Python module with a single public function. The orchestrator in `ripper.py` owns all retry and error logic.

---

## Module Interfaces

### `disc_watcher.py`
- `wait_for_disc() -> (volume_name: str, volume_path: Path)`
- Polls `/Volumes/` every 5 seconds; tracks last-seen set to detect new arrivals
- Identifies optical discs by presence of `VIDEO_TS/` (DVD) or `BDMV/` (Blu-ray) directory
- Blocks until a disc is detected

### `modules/ripper.py`
- `rip(volume_path: Path, temp_dir: Path) -> list[dict]`
- Runs `makemkvcon mkv disc:0 all <temp_dir>`, streaming stdout to log
- Captures duration info from makemkvcon output during the rip
- Returns `[{path: Path, duration_secs: int, title_index: int}]`
- Raises `RipError` on non-zero exit

### `namer.py`
- `identify(volume_name: str, titles: list[dict]) -> list[dict]`
- Sends one Anthropic API call with: volume label + `[{index, filename, duration_hms}]` per title
- Prompt instructs Claude to return JSON: `[{index, jellyfin_filename, media_type: "movie"|"tv", destination: "movies"|"tvshows"}]`
- Validates JSON response; retries once with correction prompt if malformed
- Returns original title dicts merged with naming fields
- Raises `NamerError` after retry failure

### `transfer.py`
- `send_all(named_titles: list[dict], config: Config) -> list[str]`
- Builds remote path: `{SERVER_USER}@{SERVER_IP}:/home/{SERVER_USER}/jellyfin/media/{destination}/{jellyfin_filename}`
- Uses `subprocess` + `scp` (no paramiko dependency)
- Retries each file up to 3 times with 10s / 30s / 60s backoff
- Raises `TransferError` after all retries exhausted

### `notifier.py`
- `trigger_jellyfin_scan(config: Config)` — `POST {JELLYFIN_URL}/Library/Refresh` with `X-Emby-Token` header; retries 3× with 5s / 10s / 20s backoff; logs warning on final failure (does not raise)
- `send_discord(titles: list[str], success: bool, config: Config, error: str = "")` — sends ✅ ready message or ❌ failure message; retries 3×; logs on final failure (does not raise)

---

## Orchestrator Flow

```python
while True:
    volume_name, volume_path = disc_watcher.wait_for_disc()
    try:
        titles = ripper.rip(volume_path, config.temp_dir)
        named  = namer.identify(volume_name, titles)
        transfer.send_all(named, config)
        notifier.trigger_jellyfin_scan(config)
        notifier.send_discord([t["jellyfin_filename"] for t in named], success=True, config=config)
    except RipError as e:
        log.error(f"Rip failed: {e}")
        notifier.send_discord([], success=False, error=str(e), config=config)
    except (TransferError, NamerError) as e:
        log.error(f"Pipeline failed: {e}")
        notifier.send_discord([], success=False, error=str(e), config=config)
    finally:
        cleanup_temp(config.temp_dir)
        eject_disc(volume_path)
```

---

## Retry Strategy

| Step | Retries | Backoff | On Final Failure |
|---|---|---|---|
| MakeMKV rip | 0 | — | Discord alert, eject |
| Anthropic naming | 1 (malformed JSON only) | immediate | Discord alert, eject |
| SCP transfer | 3 | 10s / 30s / 60s | Discord alert, eject |
| Jellyfin scan | 3 | 5s / 10s / 20s | Log warning only |
| Discord webhook | 3 | 5s / 10s / 20s | Log only |

Jellyfin and Discord failures are non-blocking — files are already transferred, so the pipeline doesn't halt for notification failures.

---

## Error Handling

- `RipError` — raised by `modules/ripper.py` on makemkvcon failure
- `NamerError` — raised by `namer.py` after naming retry exhausted
- `TransferError` — raised by `transfer.py` after all SCP retries exhausted
- All caught by orchestrator; always runs `finally` block (cleanup + eject)

---

## Configuration (`.env`)

```
ANTHROPIC_API_KEY=
SERVER_IP=100.100.212.32
SERVER_USER=dillon
JELLYFIN_URL=http://100.100.212.32:8096
JELLYFIN_API_KEY=
DISCORD_WEBHOOK_URL=
TEMP_DIR=/tmp/ai-ripper
```

`config.py` loads these with `python-dotenv`, validates all required keys are present, and raises a clear error on startup if any are missing.

---

## Process Management

**Development:** `python ripper.py` — runs in terminal, logs to stdout.

**Production:** `install.sh` registers the script as a launchd daemon via `com.dillon.ripper.plist`. Starts on login, restarts on crash, logs stdout/stderr to `~/Library/Logs/ai-ripper.log`.

---

## Dependencies

```
anthropic
python-dotenv
```

MakeMKV must be installed separately (`makemkvcon` available in PATH). No paramiko — SCP is invoked via subprocess.

---

## Multi-Title Behavior

All titles on a disc are ripped (not just the longest). `namer.py` receives all titles and assigns each a Jellyfin-compatible filename. For TV show discs this produces multiple named episode files (e.g. `Friends.S01E01.mkv`, `Friends.S01E02.mkv`). All are transferred to the server before the disc is ejected.

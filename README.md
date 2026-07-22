# ai-ripper

Automatically rips DVDs and Blu-rays when inserted, identifies the content with
Claude, names the files so Jellyfin recognizes them, transfers them to a home
server, and notifies you over Discord. Put a disc in, walk away.

## How it works

The daemon loops forever, one disc at a time (`ripper.py`):

1. **Watch** — polls for an inserted disc (`modules/disc_watcher.py`, macOS + Linux)
2. **Episode guide** — when you pass `--show`/`--season`, it fetches the season's
   real episode list (TMDB, or Jellyfin) *before* ripping. This constrains naming
   to episodes that actually exist and caps title length so hour-long "Play All"
   titles are skipped instead of ripped (`modules/episode_guide.py`)
3. **Rip** — rips the main titles with MakeMKV (`modules/ripper.py`)
4. **Name** — turns each anonymous title into a Jellyfin filename like
   `Friends.S01E01.mkv`. Two strategies (see below)
5. **Review / approve** — optional human gate before anything is written
6. **Transfer** — routed by disc type (`modules/transfer.py`):
   - **DVD** → SCPs the named files straight to the server.
   - **Blu-ray** → *staged locally* to `BLURAY_STAGING_DIR` (default `~/video-transfer`)
     so you can encode/compress the 20-40GB+ raw rips before copying to the server.
     Movies use the folder-per-movie layout the HEVC encode script expects
     (`movies/<Title (Year)>/<Title (Year)>.mkv`); TV uses `tvshows/Show/Season NN/…`.
     Detected automatically from the disc structure (`BDMV` vs `VIDEO_TS`) — no flag.
7. **Notify** — for a DVD, triggers a Jellyfin library scan and sends a Discord
   message; for a staged Blu-ray, sends a "ripped and staged for encoding" message
   and skips the scan (the files aren't on the server yet) (`modules/notifier.py`)
8. **Eject** and wait for the next disc

### Why Claude is in the loop

A disc doesn't tell you which title is which episode — it's just a pile of
numbered video blobs in an order MakeMKV made up, and that order is unreliable
(Family Guy discs come out reversed; The Office S1D1 comes out scrambled). Turning
"nameless title" into the correct `Show.SxxExx.mkv` is the one step a dumb script
kept getting wrong, so it's the step the AI owns. There are two naming paths:

- **Legacy namer** (`modules/namer.py`, the default) — gives Claude the disc label,
  title durations, and the real episode list, and asks it to assign numbers. Fast,
  but it trusts MakeMKV's playback order.
- **Content-ID** (`modules/identify.py`, opt-in via `--content-id`) — identifies
  each title by what it *contains*: it OCRs the first minutes of the subtitle track
  (VobSub → text) and LLM-matches that against the episode list, falling back to
  sending a few video frames to Claude vision when there are no usable subtitles.
  This is the real fix for scrambled disc order. If it can't confidently match, it
  falls back to the legacy namer rather than transferring nothing.

### Safety gates (choose at most one)

Because naming isn't 100%, you can require a human to confirm the mapping before
it's written to your library:

- `--dry-run` — rip, name, print the proposed mapping, then stop. Keeps the ripped
  files and leaves the disc in so a real run can follow without re-ripping.
- `--approve` — post the proposed mapping to Discord and wait for a one-tap
  **Approve / Fix** from your phone (`modules/approval.py`). Needs a Discord bot.
- `--review-ui` — serve a local web page (tailnet-only) showing every ripped title
  with a filmstrip of stills, where you reassign / add back / exclude titles by
  hand before transfer (`modules/review_ui.py`).

On decline, timeout, or misconfiguration, a gate **holds** the rip: temp files are
kept and the disc stays in the drive so nothing wrong reaches the library.

## Requirements

- Python 3.10+
- [MakeMKV](https://www.makemkv.com/) installed and licensed (`makemkvcon` in PATH)
- SSH key access to your home server (no password prompt)
- A running [Jellyfin](https://jellyfin.org/) instance
- An [Anthropic API key](https://console.anthropic.com/)
- A Discord webhook URL (for notifications)

Optional, only for `--content-id`: `ffmpeg`/`ffprobe`, `mkvtoolnix` (`mkvextract`),
and `vobsub2srt` for subtitle OCR. `install-linux.sh` builds these (it compiles a
patched `vobsub2srt`, since upstream won't build on Tesseract 5). Missing tools
degrade gracefully to the frame-based fallback.

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/dillon-webster/ai-ripper
cd ai-ripper
pip install -r requirements.txt
```

### 2. Configure environment

Copy this into a `.env` file in the project root:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...
SERVER_IP=192.168.1.x
SERVER_USER=yourusername
JELLYFIN_URL=http://192.168.1.x:8096
JELLYFIN_API_KEY=your_jellyfin_api_key
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
TEMP_DIR=/tmp/ai-ripper
MEDIA_ROOT=/home/yourusername/jellyfin/media

# Optional — episode guide source (preferred over Jellyfin: TMDB knows every
# season whether or not it's been ripped yet)
TMDB_API_KEY=

# Optional — Blu-ray staging root on THIS machine. Blu-rays (BDMV) are staged
# here for encoding instead of going straight to the server; DVDs ignore it.
BLURAY_STAGING_DIR=~/video-transfer

# Optional — Discord approval gate (--approve). Needs a bot, not just the webhook
DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_ID=
DISCORD_MENTION_USER_ID=   # @mention yourself so the approval pushes to your phone
APPROVAL_TIMEOUT_SECS=1800

# Optional — web review UI (--review-ui), tailnet-only
REVIEW_UI_PORT=8765
REVIEW_UI_TIMEOUT_SECS=1800
REVIEW_UI_THUMBS_PER_TITLE=12
REVIEW_UI_ADVERTISE_HOST=   # blank ⇒ Tailscale IP if available, else LAN hostname
```

DVD files land under `MEDIA_ROOT` on the server:

```
$MEDIA_ROOT/movies/
$MEDIA_ROOT/tvshows/
```

Make sure those directories exist on the server. Blu-ray rips instead land
locally under `BLURAY_STAGING_DIR` (same `movies/` and `tvshows/` layout), where
you encode them before copying to the server yourself.

> **Security note:** the review UI binds `0.0.0.0` and trusts the tailnet as its
> boundary. Never port-forward it, reverse-proxy it, or expose it via Tailscale
> Funnel.

### 3. Install as a background daemon

**macOS (launchd):**
```bash
chmod +x install.sh
./install.sh
```

**Linux (systemd):**
```bash
chmod +x install-linux.sh
./install-linux.sh
```

The daemon starts automatically on login and restarts if it crashes.

## Managing the daemon

### macOS
```bash
# View logs
tail -f ~/Library/Logs/ai-ripper.log

# Stop / Start
launchctl unload ~/Library/LaunchAgents/com.dillon.ripper.plist
launchctl load  ~/Library/LaunchAgents/com.dillon.ripper.plist

# Uninstall
launchctl unload ~/Library/LaunchAgents/com.dillon.ripper.plist
rm ~/Library/LaunchAgents/com.dillon.ripper.plist
```

### Linux
```bash
# View logs
tail -f ~/.local/share/ai-ripper/ripper.log

# Stop / Start / Status
systemctl --user stop   ai-ripper
systemctl --user start  ai-ripper
systemctl --user status ai-ripper

# Uninstall
systemctl --user disable --now ai-ripper
rm ~/.config/systemd/user/ai-ripper.service
```

## Running manually

```bash
# Fully automatic (label-based naming, no gate)
python3 ripper.py

# Recommended for TV box sets: name against the real episode list and confirm
# the mapping in the browser before it's written
python3 ripper.py --show "The Office" --season 8 --content-id --review-ui
```

### Flags

| Flag | What it does |
|------|--------------|
| `--show "Name"` | Exact show name to look up. Enables the episode guide. Requires `--season`. Overrides the name inferred from the disc label. |
| `--season N` | Force the season for every disc this session (disc labels like `FAMILY_GUY_DISC1` carry no season). |
| `--disc N` | Disc number within the season — a numbering hint when the server is empty. |
| `--content-id` | Identify each title by its content (subtitles → OCR, frames → vision) instead of playback order. Requires `--show` and `--season`. |
| `--dry-run` | Rip + name + print the mapping, then stop. Nothing written; disc kept. |
| `--approve` | One-tap Approve/Fix over Discord before transfer. |
| `--review-ui` | Hand-curate the mapping in a local web page before transfer. |

Overrides apply to **every disc until the ripper is restarted.**

## Running tests

```bash
pytest
```

## Project structure

```
ripper.py                 # Entry point / main loop, CLI flags, gate orchestration
config.py                 # Loads .env into a Config dataclass
modules/
  disc_watcher.py         # Polls for disc insertion + DVD/Blu-ray detection (macOS + Linux)
  ripper.py               # Wraps makemkvcon to rip titles
  episode_guide.py        # Fetches the real season episode list (TMDB / Jellyfin)
  namer.py                # Legacy naming: Claude assigns numbers from label + durations
  identify.py             # Content-ID: subtitle OCR / frame vision → episode match
  approval.py             # --approve: Discord bot approval gate
  review_ui.py            # --review-ui: local filmstrip curation page
  transfer.py             # DVD → SCP to server; Blu-ray → stage locally for encoding
  notifier.py             # Triggers Jellyfin scan + Discord webhook
docs/                     # Design notes: episode-identification-plan.md, review-ui-plan.md, ...
```

## Status

The content-based identification work is rolling out in phases (see
`docs/episode-identification-plan.md`):

- **Phase 1 — provider-aware episode lists** (`episode_guide.py`): done. The namer
  is constrained to real episodes so it stops inventing numbers.
- **Phase 2 — content identification** (`identify.py`): built, opt-in behind
  `--content-id` while it's proven against real discs.
- **Phase 3 — human approval** (`approval.py`, `review_ui.py`): built.

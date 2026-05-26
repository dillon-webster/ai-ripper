# ai-ripper

Automatically rips DVDs and Blu-rays when inserted, identifies the content using Claude AI, transfers the files to a home server, and notifies you via Discord.

## How it works

1. **Watches** for a disc to be inserted
2. **Rips** all main titles using MakeMKV
3. **Names** the files with Claude (Jellyfin-compatible filenames like `Friends.S01E01.mkv`)
4. **Transfers** the files to your home server via SCP
5. **Notifies** Jellyfin to scan the library and sends a Discord message
6. **Ejects** the disc and waits for the next one

## Requirements

- Python 3.10+
- [MakeMKV](https://www.makemkv.com/) installed and licensed (`makemkvcon` in PATH)
- SSH key access to your home server (no password prompt)
- A running [Jellyfin](https://jellyfin.org/) instance
- An [Anthropic API key](https://console.anthropic.com/)
- A Discord webhook URL

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/dillon-webster/ai-ripper
cd ai-ripper
pip install -r requirements.txt
```

### 2. Configure environment

Copy the example below into a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-...
SERVER_IP=192.168.1.x
SERVER_USER=yourusername
JELLYFIN_URL=http://192.168.1.x:8096
JELLYFIN_API_KEY=your_jellyfin_api_key
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
TEMP_DIR=/tmp/ai-ripper
```

Files are transferred to:
```
/home/<SERVER_USER>/jellyfin/media/movies/
/home/<SERVER_USER>/jellyfin/media/tvshows/
```
Make sure those directories exist on the server.

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
python3 ripper.py
```

## Running tests

```bash
pytest
```

## Project structure

```
ripper.py               # Entry point / main loop
config.py               # Loads .env into a Config dataclass
modules/
  disc_watcher.py       # Polls for disc insertion (macOS + Linux)
  ripper.py             # Wraps makemkvcon to rip titles
  namer.py              # Calls Claude API to generate Jellyfin filenames
  transfer.py           # SCPs files to home server with retry
  notifier.py           # Triggers Jellyfin scan + Discord webhook
```

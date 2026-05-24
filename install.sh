#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG_PATH="$HOME/Library/Logs/ai-ripper.log"
PLIST_DEST="$HOME/Library/LaunchAgents/com.dillon.ripper.plist"

echo "Installing ai-ripper launchd agent..."
echo "  Script dir : $SCRIPT_DIR"
echo "  Python     : $PYTHON"
echo "  Log file   : $LOG_PATH"
echo "  Plist dest : $PLIST_DEST"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Library/Logs"

sed \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__RIPPER_PATH__|$SCRIPT_DIR/ripper.py|g" \
    -e "s|__WORKING_DIR__|$SCRIPT_DIR|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    "$SCRIPT_DIR/com.dillon.ripper.plist" > "$PLIST_DEST"

# Unload if already registered, then load fresh
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo ""
echo "✅ ai-ripper installed as launchd agent (com.dillon.ripper)"
echo "   Starts automatically on login. Running now."
echo ""
echo "Useful commands:"
echo "  View logs  : tail -f $LOG_PATH"
echo "  Stop       : launchctl unload $PLIST_DEST"
echo "  Start      : launchctl load $PLIST_DEST"
echo "  Uninstall  : launchctl unload $PLIST_DEST && rm $PLIST_DEST"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG_PATH="$HOME/.local/share/ai-ripper/ripper.log"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_DEST="$SERVICE_DIR/ai-ripper.service"

echo "Installing ai-ripper systemd user service..."
echo "  Script dir : $SCRIPT_DIR"
echo "  Python     : $PYTHON"
echo "  Log file   : $LOG_PATH"
echo "  Service    : $SERVICE_DEST"

mkdir -p "$SERVICE_DIR"
mkdir -p "$(dirname "$LOG_PATH")"

sed \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__RIPPER_PATH__|$SCRIPT_DIR/ripper.py|g" \
    -e "s|__WORKING_DIR__|$SCRIPT_DIR|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    "$SCRIPT_DIR/ai-ripper.service" > "$SERVICE_DEST"

# Reload systemd and enable/start the service
systemctl --user daemon-reload
systemctl --user enable --now ai-ripper.service

echo ""
echo "✅ ai-ripper installed as systemd user service"
echo "   Starts automatically on login. Running now."
echo ""
echo "Useful commands:"
echo "  View logs  : tail -f $LOG_PATH"
echo "  Status     : systemctl --user status ai-ripper"
echo "  Stop       : systemctl --user stop ai-ripper"
echo "  Start      : systemctl --user start ai-ripper"
echo "  Uninstall  : systemctl --user disable --now ai-ripper && rm $SERVICE_DEST"

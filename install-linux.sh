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

# Phase 2 content-based episode identification (modules/identify.py) needs these
# system tools: ffmpeg/ffprobe (frame grab + subtitle-track probe), mkvtoolnix
# (mkvextract, to pull the VobSub subtitle track), and vobsubocr (OCR the image-based
# DVD subs to text via Tesseract 5). Missing tools degrade to the frame→vision
# fallback, but the subtitle path is the primary/most-accurate signal — install them.
#
# vobsub2srt is abandoned and won't build as-is on Tesseract 5 (removed API + old
# C++ std + a cmake-min guard modern cmake rejects). We build a PATCHED copy from
# source — it has no leptonica dependency (the thing that sinks the Rust alternative
# vobsubocr on leptonica 1.86), only libtiff + Tesseract, both packaged. Built LAST
# and separately so a build hiccup can't take down the packaged tools.
if command -v apt-get >/dev/null 2>&1; then
    echo "Installing packaged episode-ID dependencies (ffmpeg, mkvtoolnix, tesseract)..."
    sudo apt-get update
    sudo apt-get install -y ffmpeg mkvtoolnix tesseract-ocr tesseract-ocr-eng

    if ! command -v vobsub2srt >/dev/null 2>&1; then
        echo "Building patched vobsub2srt from source (Tesseract-5 API + C++17)..."
        sudo apt-get install -y git cmake pkg-config build-essential \
            libtesseract-dev libtiff-dev
        _v2s="$(mktemp -d)"
        if git clone --depth 1 https://github.com/ruediger/VobSub2SRT.git "$_v2s"; then
            python3 - "$_v2s" <<'PYEOF'
import sys
r = sys.argv[1]
src = f"{r}/src/vobsub2srt.c++"
s = open(src).read()
# UINT_MAX needs <climits>
s = s.replace('#include <cstdio>\n', '#include <cstdio>\n#include <climits>\n', 1)
# Force the modern Tesseract instance-API branch (cmake auto-detect is unreliable on T5)
if '#define CONFIG_TESSERACT_NAMESPACE' not in s:
    s = s.replace('#include "cmd_options.h++"\n',
                  '#include "cmd_options.h++"\n#define CONFIG_TESSERACT_NAMESPACE 1\n', 1)
# Tesseract 5 removed TessBaseAPI::TesseractRect → SetImage + GetUTF8Text
s = s.replace(
    'char *text = tess_base_api.TesseractRect(image, 1, stride, 0, 0, width, height);',
    'tess_base_api.SetImage(image, width, height, 1, stride);\n'
    '      char *text = tess_base_api.GetUTF8Text();', 1)
open(src, 'w').write(s)
# Tesseract 5 headers need C++17; -ansi (c++98) + -pedantic break them
cml = f"{r}/CMakeLists.txt"
c = open(cml).read().replace(
    'set(CMAKE_CXX_FLAGS "-ansi -pedantic -Wall -Wextra -Wno-long-long")',
    'set(CMAKE_CXX_FLAGS "-std=c++17 -Wall -Wextra -Wno-long-long")')
open(cml, 'w').write(c)
PYEOF
            if ( cd "$_v2s" && mkdir -p build && cd build \
                 && cmake .. -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release \
                 && make && sudo make install ); then
                sudo ldconfig
                echo "✅ vobsub2srt installed"
            else
                echo "⚠️  vobsub2srt build failed — subtitle OCR unavailable; identify.py"
                echo "    falls back to frames→vision."
            fi
        fi
        rm -rf "$_v2s"
    fi
else
    echo "⚠️  Non-apt system: install ffmpeg, mkvtoolnix, and a Tesseract-5-patched"
    echo "    vobsub2srt manually for content-based identification."
fi

mkdir -p "$SERVICE_DIR"
mkdir -p "$(dirname "$LOG_PATH")"

sed \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__RIPPER_PATH__|$SCRIPT_DIR/ripper.py|g" \
    -e "s|__WORKING_DIR__|$SCRIPT_DIR|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    "$SCRIPT_DIR/ai-ripper.service" > "$SERVICE_DEST"

# Enable linger so the user service survives logout (required for headless/server use)
loginctl enable-linger "$USER"

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

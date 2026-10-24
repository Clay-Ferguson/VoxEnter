#!/bin/bash
# Setup script for VoxEnter (PyQt6)
#
# Installs system dependencies, downloads the whisper model, and installs the
# desktop entry. The Python environment itself needs no separate step -- uv
# builds it from pyproject.toml the first time it runs.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🎤 VoxEnter - PyQt6 App Setup"
echo "==========================="
echo ""

# Check if running as root
if [[ $EUID -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

# Detect package manager
if command -v apt &> /dev/null; then
    PKG_MANAGER="apt"
elif command -v dnf &> /dev/null; then
    PKG_MANAGER="dnf"
elif command -v pacman &> /dev/null; then
    PKG_MANAGER="pacman"
else
    echo "❌ Unsupported package manager. Please install dependencies manually."
    exit 1
fi

echo "📦 Installing system dependencies..."
echo ""

# PyQt6 ships Qt itself in its wheels, and faster-whisper ships its inference
# engine the same way, so the system packages are only what those wheels link
# against. Qt's platform plugin needs a few X/xkb libraries that a minimal
# install may lack -- libxcb-cursor0 in particular is required by Qt 6.5+, and
# its absence produces the notorious "could not load the Qt platform plugin
# xcb" failure. No ffmpeg: audio reaches whisper as a numpy array, never as a
# file that needs decoding.
case $PKG_MANAGER in
    apt)
        $SUDO apt update
        $SUDO apt install -y \
            portaudio19-dev \
            libxcb-cursor0 \
            libxkbcommon-x11-0
        ;;
    dnf)
        $SUDO dnf install -y \
            portaudio-devel \
            xcb-util-cursor \
            libxkbcommon-x11
        ;;
    pacman)
        $SUDO pacman -S --noconfirm \
            portaudio \
            xcb-util-cursor \
            libxkbcommon-x11
        ;;
esac

echo ""
echo "✅ System dependencies installed!"
echo ""

# Check for PortAudio (required by sounddevice)
if ! ldconfig -p | grep -q libportaudio; then
    echo "❌ PortAudio library not found!"
    echo ""
    echo "Install with:"
    echo "  Ubuntu/Debian: sudo apt install libportaudio2 portaudio19-dev"
    echo "  Fedora:        sudo dnf install portaudio portaudio-devel"
    echo "  Arch:          sudo pacman -S portaudio"
    echo ""
    exit 1
fi

# Ensure uv is installed
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Download the whisper model now rather than on the first utterance. This
# also builds the virtualenv, and loading the model proves the whole
# faster-whisper stack works on this machine.
echo "🧠 Downloading whisper model (first run only, ~145MB)..."
uv run --directory "$SCRIPT_DIR" python -c \
    "from voice_typer.transcribe import get_model; get_model()"
echo "✅ Whisper model ready!"
echo ""

# Install desktop file for dock/taskbar integration.
# Exec/Path/Icon are absolute paths, so the checked-in template is rewritten
# for wherever this checkout actually lives.
echo "🖼️  Installing desktop integration..."
APP_ID="com.voxenter.voicetyper"
DESKTOP_DIR="$HOME/.local/share/applications"

chmod +x "$SCRIPT_DIR/run.sh"
mkdir -p "$DESKTOP_DIR"

sed \
  -e "s|^Exec=.*|Exec=$SCRIPT_DIR/run.sh|" \
  -e "s|^Path=.*|Path=$SCRIPT_DIR|" \
  -e "s|^Icon=.*|Icon=$SCRIPT_DIR/voxenter-logo.png|" \
  "$SCRIPT_DIR/$APP_ID.desktop" > "$DESKTOP_DIR/$APP_ID.desktop"

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "✅ Desktop integration installed!"

echo ""
echo "✅ VoxEnter setup complete!"
echo ""
echo "Start the app with: ./run.sh"
echo ""
echo "Note: You may need to log out/in for the dock icon to appear."
echo ""

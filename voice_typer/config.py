"""User settings and audio-device enumeration. Deliberately Qt-free.

Settings live in a single YAML file so they can be inspected and hand-edited
without the app running, which matters for the silence threshold in
particular -- it is the one value users are told to tune when dictation is
not triggering.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# =============================================================================
# Defaults
# =============================================================================

# Lower threshold for quieter mics (0.001-0.005), higher for louder mics
# (0.01-0.02). Adjust based on your microphone -- check the "Audio RMS" log
# values when speaking.
DEFAULT_SILENCE_THRESHOLD = 0.005  # RMS below this is silence (raised to filter electrical spikes)

# Automatically disable the microphone after this many seconds of inactivity
# (no successful transcription sent to the keyboard).
DEFAULT_MIC_AUTO_OFF_TIMEOUT_S = 30

CONFIG_DIR = Path.home() / ".config"

# Also holds the saved portal_restore_token, so renaming this file would
# orphan the token and force the user through the Remote Desktop permission
# dialog again.
CONFIG_FILE = CONFIG_DIR / "phonon.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "audio_device": None,  # None means system default
    "portal_restore_token": None,  # Saved token for XDG Remote Desktop Portal session persistence
    "silence_threshold": DEFAULT_SILENCE_THRESHOLD,
    "mic_auto_off_timeout": DEFAULT_MIC_AUTO_OFF_TIMEOUT_S,
}

# =============================================================================
# Load / Save
# =============================================================================


def load_config() -> dict[str, Any]:
    """Load configuration from the YAML file, creating defaults if needed."""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r") as f:
            config = yaml.safe_load(f) or {}
        # Merge with defaults for any missing keys
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value
        # Coerce the two numeric settings: they are the ones users hand-edit,
        # so a stray string here must not crash the app on startup.
        try:
            config["silence_threshold"] = float(config.get("silence_threshold", DEFAULT_SILENCE_THRESHOLD))
        except (TypeError, ValueError):
            config["silence_threshold"] = DEFAULT_SILENCE_THRESHOLD
        try:
            config["mic_auto_off_timeout"] = int(config.get("mic_auto_off_timeout", DEFAULT_MIC_AUTO_OFF_TIMEOUT_S))
        except (TypeError, ValueError):
            config["mic_auto_off_timeout"] = DEFAULT_MIC_AUTO_OFF_TIMEOUT_S
        return config
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return DEFAULT_CONFIG.copy()


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to the YAML file."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        log.info(f"Config saved to {CONFIG_FILE}")
    except Exception as e:
        log.error(f"Failed to save config: {e}")


# =============================================================================
# Audio Devices
# =============================================================================


def get_input_devices(rescan: bool = False) -> list[dict[str, Any]]:
    """List available audio input devices.

    `rescan` tears down and reinitializes PortAudio, which is the only way to
    notice a USB mic plugged in after the process started. It is not free, so
    callers do it once at startup rather than every time the list is shown.

    sounddevice is imported here rather than at module scope so that merely
    reading settings -- which keyboard.py does for the portal restore token --
    does not require a working PortAudio install.
    """
    import sounddevice as sd

    if rescan:
        sd._terminate()
        sd._initialize()

    devices = sd.query_devices()
    input_devices = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            input_devices.append({"index": i, "name": d["name"]})
    return input_devices

"""Entry point: python -m voice_typer

Close the window with the title bar close button.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from . import APP_ID, APP_NAME
from .transcribe import COMPUTE_TYPE, DEVICE, MODEL_NAME, preload_model

# =============================================================================
# Logging Setup
# =============================================================================

# The log lives beside the package rather than inside it, keeping it at
# Phonon/voice_typer.log where the docs say it is.
APP_DIR = Path(__file__).parent.parent.absolute()
LOG_FILE = APP_DIR / "voice_typer.log"
ICON = APP_DIR / "phonon-logo.png"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w"),  # Overwrite each run
        logging.StreamHandler(),  # Also print to console
    ],
)
log = logging.getLogger(__name__)

# huggingface_hub logs every HTTP request at INFO, through httpx. Only the
# first-run model download makes any, and they would bury the app's own lines.
logging.getLogger("httpx").setLevel(logging.WARNING)

# =============================================================================
# Startup Checks
# =============================================================================


def check_dependencies() -> list[str]:
    """Return a list of human-readable problems, empty if everything is present.

    The whisper model is deliberately not checked here: faster-whisper
    downloads it on first use, and preload_model() logs if that fails.
    """
    errors = []

    # Imported here, not at module scope: sounddevice raises at import time
    # when PortAudio is missing, and that is precisely one of the problems
    # this function exists to report.
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        input_devices = [d for d in devices if d["max_input_channels"] > 0]
        if not input_devices:
            errors.append("No audio input devices found")
    except OSError as e:
        errors.append(f"PortAudio not available ({e}). Run ./setup.sh to install it.")
    except Exception as e:
        errors.append(f"Error querying audio devices: {e}")

    return errors


def main() -> int:
    print(f"{APP_NAME} Voice Typer - Starting...")
    print(f"Whisper model: {MODEL_NAME} (faster-whisper, {DEVICE}, {COMPUTE_TYPE})")

    errors = check_dependencies()
    if errors:
        print("\n⚠️  Missing dependencies:")
        for error in errors:
            print(f"   - {error}")
        print("\nRun ./setup.sh to install the system packages and download the model.")
        return 1

    print("✅ All dependencies found. Starting application...")

    # Imported only once the checks pass, so a missing audio stack produces the
    # message above rather than an import traceback.
    from .window import VoiceTyperWindow

    # Load the model in the background while the window comes up, so the
    # first utterance does not pay for it.
    preload_model()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    # Ties the window to com.phonon.voicetyper.desktop so the dock and alt-tab
    # show our icon. Without it the Wayland app_id comes from argv[0]
    # ("python3") and matches nothing.
    app.setDesktopFileName(APP_ID)
    if ICON.is_file():
        app.setWindowIcon(QIcon(str(ICON)))

    window = VoiceTyperWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

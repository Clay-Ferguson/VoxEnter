"""The Settings dialog: what the main window used to expand to show.

Split out of `window.py` so the main window can be a fixed row of icons. The
main window is a utility that sits on top of whatever you are dictating into,
so it should never grow, and -- more importantly -- never *resize itself*.
A self-resizing non-resizable Wayland toplevel is the rare path that produced
the stale-configure bug `_enforce_size()` in `window.py` exists to absorb; a
second toplevel that the user can resize does not take that path at all.

This dialog is deliberately dumb. It owns widgets and input validation and
nothing else: it never touches the config file and never sees the recorder.
Validated values leave through signals, and the window applies them. That
keeps the window the single owner of both, which matters because two of the
three settings have live side effects on a running audio stream.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout,
                             QLabel, QLineEdit, QVBoxLayout, QWidget)

log = logging.getLogger(__name__)

# =============================================================================
# Copy
# =============================================================================

# Width budget for the wrapped help paragraphs, and therefore the width of the
# dialog. See _help_label for why this is fixed rather than maximum.
HELP_TEXT_WIDTH = 340

THRESHOLD_HELP = (
    "Typical range is 0.001–0.02: lower values make the mic more sensitive; "
    "raise it to ignore background noise. Set the value as low as possible for "
    "best reliablility. If you have any problems with this application try "
    "lowering this threshold value."
)

AUTO_OFF_HELP = (
    "Automatically turn off the microphone after this many seconds without a "
    "successful transcription. Set to 0 to disable auto-off."
)


def build_dialog_stylesheet() -> str:
    """QSS for the dialog only.

    Kept separate from the window's sheet rather than inherited from it. A
    child dialog does inherit its parent's stylesheet, so the window's rules
    are scoped to widgets the window actually owns -- otherwise the white
    "typing" phase, which forces child text to black, would black out this
    dialog's text on its own dark background every time you dictate a word.

    The muted color is the theme's own window text color, faded, so the help
    paragraphs stay readable on both light and dark desktops.
    """
    muted = QApplication.palette().color(QPalette.ColorRole.WindowText)
    muted.setAlpha(180)
    return (
        f"QLabel#helpText {{ font-size: 14px; color: rgba({muted.red()},"
        f" {muted.green()}, {muted.blue()}, 0.7); }}"
    )


# =============================================================================
# Dialog
# =============================================================================


class SettingsDialog(QDialog):
    """Modeless settings panel. Edits apply as they are made -- no OK/Cancel.

    Signals carry values that are already parsed and range-checked; a value
    that does not survive validation never leaves this class, and the entry it
    came from is reverted to the value still in force.
    """

    # object rather than str: "System Default" is None, not a device name.
    audio_device_changed = pyqtSignal(object)
    silence_threshold_changed = pyqtSignal(float)
    auto_off_timeout_changed = pyqtSignal(int)

    def __init__(
        self,
        parent: QWidget | None,
        input_devices: list[dict[str, Any]],
        current_device: str | None,
        silence_threshold: float,
        auto_off_timeout: int,
    ) -> None:
        super().__init__(parent)

        self.input_devices = input_devices
        self._help_labels: list[QLabel] = []
        self._sized = False
        # Mirrors of the window's config values, kept only so an invalid entry
        # can be reverted and an unchanged one can be ignored.
        self._silence_threshold = float(silence_threshold)
        self._auto_off_timeout = int(auto_off_timeout)

        self.setWindowTitle("Phonon Settings")
        self.setStyleSheet(build_dialog_stylesheet())
        # Modeless: the mic keeps running and the main window stays clickable
        # while this is open.
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(6)

        # Microphone device dropdown
        self.mic_dropdown = QComboBox()
        self._populate_mic_dropdown(current_device)
        # Connected after populating, so filling the list does not read as a
        # user-initiated device change.
        self.mic_dropdown.currentIndexChanged.connect(self._on_device_index_changed)
        layout.addLayout(self._labeled_row("Input device", self.mic_dropdown))

        # Silence threshold
        self.threshold_entry = QLineEdit()
        self.threshold_entry.setMaxLength(8)
        self.threshold_entry.setFixedWidth(80)
        self.threshold_entry.setToolTip(
            "Lower values detect quieter audio; higher values filter background noise."
        )
        self.threshold_entry.setText(self._format_threshold(self._silence_threshold))
        # editingFinished covers Enter *and* focus-out.
        self.threshold_entry.editingFinished.connect(self._commit_threshold_entry)
        layout.addLayout(self._labeled_row("Silence threshold", self.threshold_entry))
        layout.addWidget(self._help_label(THRESHOLD_HELP))

        # Auto-off timeout
        self.auto_off_entry = QLineEdit()
        self.auto_off_entry.setMaxLength(6)
        self.auto_off_entry.setFixedWidth(80)
        self.auto_off_entry.setToolTip(
            "Seconds of inactivity before the microphone turns off automatically. 0 to disable."
        )
        self.auto_off_entry.setText(str(self._auto_off_timeout))
        self.auto_off_entry.editingFinished.connect(self._commit_auto_off_entry)
        layout.addLayout(self._labeled_row("Auto-off timeout (s)", self.auto_off_entry))
        layout.addWidget(self._help_label(AUTO_OFF_HELP))

        # No SizeConstraint here on purpose: the dialog stays user-resizable,
        # which is what keeps it off the self-resizing path described above.

    def showEvent(self, event) -> None:
        """Pin the wrapped paragraphs' heights the first time we are shown.

        A word-wrapped QLabel only knows its real height once it has been
        polished with the font the stylesheet gives it, which has not happened
        while __init__ is still running. Qt sizes the dialog before then and
        lands on one line per paragraph, clipping the rest. Measuring here and
        fixing the heights makes the layout's minimum equal its hint, so the
        dialog opens at the right size and cannot be dragged small enough to
        clip the text again.
        """
        super().showEvent(event)
        if self._sized:
            return
        self._sized = True
        self.layout().activate()
        for label in self._help_labels:
            label.setFixedHeight(label.heightForWidth(HELP_TEXT_WIDTH))
        self.layout().activate()
        self.resize(self.sizeHint())

    # -- construction ------------------------------------------------------

    def _labeled_row(self, text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel(text))
        row.addStretch(1)
        row.addWidget(widget)
        return row

    def _help_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("helpText")
        label.setWordWrap(True)
        # Fixed, not maximum. A wrapped label's height is a function of its
        # width (hasHeightForWidth() is True), so with only a maximum the
        # dialog's sizeHint depends on how the layout happens to negotiate
        # width. Pinning the width makes the required height deterministic,
        # which is what keeps the dialog from opening at the wrong height.
        label.setFixedWidth(HELP_TEXT_WIDTH)
        self._help_labels.append(label)
        return label

    def _populate_mic_dropdown(self, current_device: str | None) -> None:
        """Fill the dropdown, selecting the saved device if it is still present."""
        self.mic_dropdown.addItem("System Default")
        for device in self.input_devices:
            self.mic_dropdown.addItem(device["name"])

        if current_device is None:
            self.mic_dropdown.setCurrentIndex(0)
            return

        for i, device in enumerate(self.input_devices):
            if device["name"] == current_device:
                self.mic_dropdown.setCurrentIndex(i + 1)  # +1 for "System Default"
                return

        log.warning(f"Saved device '{current_device}' not found, using default")
        self.mic_dropdown.setCurrentIndex(0)

    # -- editing -----------------------------------------------------------

    def _on_device_index_changed(self, index: int) -> None:
        name = None if index <= 0 else self.input_devices[index - 1]["name"]
        self.audio_device_changed.emit(name)

    def _format_threshold(self, value: float) -> str:
        formatted = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return formatted if formatted else "0"

    def _commit_threshold_entry(self) -> None:
        text = self.threshold_entry.text().strip()
        if not text:
            self.threshold_entry.setText(self._format_threshold(self._silence_threshold))
            return
        try:
            value = float(text)
        except ValueError:
            print("⚠️  Silence threshold must be a number.")
            self.threshold_entry.setText(self._format_threshold(self._silence_threshold))
            return
        if value <= 0:
            print("⚠️  Silence threshold must be positive.")
            self.threshold_entry.setText(self._format_threshold(self._silence_threshold))
            return

        self.threshold_entry.setText(self._format_threshold(value))
        if abs(self._silence_threshold - value) < 1e-6:
            return
        self._silence_threshold = value
        self.silence_threshold_changed.emit(value)

    def _commit_auto_off_entry(self) -> None:
        text = self.auto_off_entry.text().strip()
        if not text:
            self.auto_off_entry.setText(str(self._auto_off_timeout))
            return
        try:
            value = int(text)
        except ValueError:
            print("⚠️  Auto-off timeout must be an integer.")
            self.auto_off_entry.setText(str(self._auto_off_timeout))
            return
        if value < 0:
            print("⚠️  Auto-off timeout must be 0 or positive.")
            self.auto_off_entry.setText(str(self._auto_off_timeout))
            return

        self.auto_off_entry.setText(str(value))
        if self._auto_off_timeout == value:
            return
        self._auto_off_timeout = value
        self.auto_off_timeout_changed.emit(value)

"""The single control window: a mic toggle and a gear.

The feature that makes the app usable without reading anything is that the
whole window changes color to report
what the pipeline is doing (green = hearing you, orange = transcribing,
white = typing). Those four colors are semantic signals rather than theming,
so unlike the rest of the styling they are fixed hex values.

The window is deliberately one row of controls and nothing else. Settings live
in a separate dialog (see settings_dialog.py): this window sits on top of
whatever you are dictating into, so every pixel it occupies covers up the real
work. The user sets its width by dragging the frame; the app itself never
resizes it after it first appears.
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout,
                             QMessageBox, QSizePolicy, QSpacerItem, QStyle,
                             QToolButton, QVBoxLayout, QWidget)
from windowchrome import apply_checkboxes

from . import APP_NAME, UI_FONT_PX
from .audio import AudioRecorder
from .config import (DEFAULT_MIC_AUTO_OFF_TIMEOUT_S, DEFAULT_SILENCE_THRESHOLD,
                     DEFAULT_WINDOW_WIDTH,
                     get_input_devices, load_config, save_config)
from .keyboard import close_keyboard_injector, get_keyboard_injector, type_text
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

# =============================================================================
# Styling
# =============================================================================

PHASE_COLORS = {
    "speech-detected": "#27AE60",
    "transcribing": "#E67E22",
    "typing": "#FFFFFF",
}

# The checkbox label runs at UI_FONT_PX and its indicator is enlarged by
# `windowchrome.apply_checkboxes()` (see _build_top_row), because this window
# is glanced at rather than read. The icons are sized to sit at the same
# visual weight rather than looking like afterthoughts next to it.
ICON_PX = 22
ICON_BUTTON_PX = 32


def build_stylesheet() -> str:
    """QSS for the window.

    The phase backgrounds hang off a dynamic `phase` property rather than
    separate stylesheets, so switching phase is a property set plus a repolish.
    The 'typing' phase forces text black because its background is white, and
    Qt does not repaint child text just because the ancestor's background
    changed -- each child type has to be named.

    Only widget types this window actually owns are named here. A stylesheet
    set on a widget also applies to child dialogs, so a bare `QLabel` rule
    would reach into the settings dialog and, during the white phase, black
    out its text against its own dark background. The settings dialog has no
    QCheckBox and no #iconButton, so nothing below can escape into it.

    The indicator's *size* is deliberately not here. It was a
    `QCheckBox::indicator` rule and is now `windowchrome.apply_checkboxes()`,
    shared with the other apps -- see `windowchrome/README.md` §12, which is
    also where the reason it is a style and not a stylesheet lives. What
    remains this sheet's business is the font size.

    Checking the mic deliberately does *not* recolor the label. It used to go
    red (`color: #e53935`), which fought the window's own phase colors -- the
    background is already saying what the pipeline is doing, in three colors
    chosen for that job, and a fourth color on the text competed with it. So
    the label keeps the theme's foreground in every state, which is white on
    a dark desktop and black on a light one. Do not pin it to a hex value:
    the 'typing' phase paints the window white, and a hard-coded white label
    would vanish into it -- which is exactly what the rule below exists to
    prevent.
    """
    rules = [
        f"QCheckBox {{ font-size: {UI_FONT_PX}px; }}",
        f"QToolButton#iconButton {{ min-width: {ICON_BUTTON_PX}px;"
        f" min-height: {ICON_BUTTON_PX}px; border-radius: 4px; }}",
    ]

    for phase, color in PHASE_COLORS.items():
        rules.append(f'QWidget#voiceTyperWindow[phase="{phase}"] {{ background: {color}; }}')

    # White background needs dark text on every child that draws any. The icon
    # buttons draw none: the gear is a white pixmap in every phase.
    rules.append('QWidget#voiceTyperWindow[phase="typing"] QCheckBox { color: #000000; }')
    return "\n".join(rules)


# =============================================================================
# Icons
# =============================================================================

# Symbolic icons first, and not only out of taste: a symbolic icon is a
# single-color silhouette carried in its alpha channel, so it can be painted
# white. The full-color fallbacks cannot -- painting Yaru's
# `preferences-system` turns the gear into a filled blob -- so those are used
# as they ship.
GEAR_SYMBOLIC = ("preferences-system-symbolic", "emblem-system-symbolic",
                 "applications-system-symbolic")
GEAR_FALLBACK = ("preferences-system", "emblem-system", "system-run")

# The icons are white in every phase. The window's background changes color to
# report what the pipeline is doing, but the icons do not follow it -- one
# fixed color is all this window needs, and it is what the theme's own dark
# titlebar draws in.
ICON_COLOR = QColor("#FFFFFF")


def load_icon(
    symbolic_names: tuple[str, ...],
    fallback_names: tuple[str, ...],
    standard_pixmap: QStyle.StandardPixmap,
    dpr: float,
) -> QIcon:
    """Resolve a theme icon, painted white if it is safe to paint.

    A thin icon theme that has neither name still yields a usable icon via the
    Qt style, at the cost of not being white.
    """
    for name in symbolic_names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return paint(icon, ICON_COLOR, dpr)
    for name in fallback_names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return QApplication.style().standardIcon(standard_pixmap)


def paint(icon: QIcon, color: QColor, dpr: float) -> QIcon:
    """Repaint a symbolic icon's silhouette in `color`.

    SourceIn keeps the alpha channel and replaces everything else, which is
    exactly a symbolic icon's shape. `dpr` is passed through so the pixmap is
    rendered at the screen's real resolution rather than upscaled.
    """
    source = icon.pixmap(QSize(ICON_PX, ICON_PX), dpr)
    tinted = QPixmap(source.size())
    tinted.setDevicePixelRatio(source.devicePixelRatio())
    tinted.fill(Qt.GlobalColor.transparent)

    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), color)
    painter.end()
    return QIcon(tinted)


# =============================================================================
# Window
# =============================================================================


class VoiceTyperWindow(QWidget):
    """Small always-on-top-of-mind control panel for dictation."""

    def __init__(self) -> None:
        super().__init__()

        self.recorder: AudioRecorder | None = None
        self.is_recording = False
        self.config = load_config()
        self._active_phase: str | None = None
        self._settings_dialog: SettingsDialog | None = None

        # Auto-off timer state
        self._last_audio_detection_time: float | None = None

        # Scanned here rather than when the settings dialog is first opened.
        # The rescan tears down and reinitializes PortAudio, which is safe at
        # startup -- _auto_start_microphone has not run yet -- and decidedly
        # not safe later, with a capture stream open.
        self.input_devices = get_input_devices(rescan=True)

        self.setObjectName("voiceTyperWindow")
        # Without this a plain QWidget ignores stylesheet backgrounds entirely,
        # and every phase color silently does nothing.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(APP_NAME)
        # Title bar with a close button only. CustomizeWindowHint drops the
        # default hint set, so the two hints after it are the whole menu:
        # no minimize, no maximize. The compositor decides whether to honor
        # it, so this is a request, not a guarantee. On GNOME Wayland Qt's own
        # adwaita decoration ignores it and takes its buttons from GNOME's
        # button-layout setting.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(build_stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        layout.addLayout(self._build_top_row())

        # The width is the user's: it starts at the saved width (400 by
        # default) and can be dragged down to what the controls need. The
        # title bar may clip the title well before that; that is accepted,
        # and the user can see it happening. The height is pinned to the one
        # row of controls and is not saved.
        self.setFixedHeight(self.sizeHint().height())
        self.resize(self._get_window_width(), self.height())

        self._auto_off_timer = QTimer(self)
        self._auto_off_timer.setInterval(1000)
        self._auto_off_timer.timeout.connect(self._check_auto_off)

        QTimer.singleShot(0, self._auto_start_microphone)

    # -- construction ------------------------------------------------------

    def _build_top_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)

        self.mic_checkbox = QCheckBox("Speech-to-Text")
        # The enlarged indicator is windowchrome's, shared with the other
        # apps. It has to be applied to the widget rather than written into
        # build_stylesheet(); §12 of that README says why.
        apply_checkboxes(self.mic_checkbox)
        self.mic_checkbox.toggled.connect(self.on_mic_toggled)
        row.addWidget(self.mic_checkbox)

        row.addItem(QSpacerItem(
            0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        ))

        self.settings_button = self._icon_button(
            GEAR_SYMBOLIC, GEAR_FALLBACK,
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "Settings", self.open_settings,
        )
        row.addWidget(self.settings_button)
        # No close button: the title bar already has one, at the same size, a
        # few pixels away. A second one only made the window wider.
        return row

    def _icon_button(self, symbolic, fallback, standard_pixmap, name, on_click) -> QToolButton:
        """A compact square icon-only button.

        The name doubles as tooltip and accessible name: an icon-only button
        with neither is invisible to a screen reader.
        """
        button = QToolButton()
        button.setObjectName("iconButton")
        button.setAutoRaise(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setIconSize(QSize(ICON_PX, ICON_PX))
        button.setToolTip(name)
        button.setAccessibleName(name)
        button.clicked.connect(on_click)

        button.setIcon(
            load_icon(symbolic, fallback, standard_pixmap, self.devicePixelRatioF())
        )
        return button

    # -- settings dialog ---------------------------------------------------

    def open_settings(self) -> None:
        """Show the settings dialog, reusing the one instance if it exists."""
        if self._settings_dialog is None:
            dialog = SettingsDialog(
                self,
                input_devices=self.input_devices,
                current_device=self.config.get("audio_device"),
                silence_threshold=self._get_silence_threshold(),
                auto_off_timeout=self._get_auto_off_timeout(),
            )
            dialog.audio_device_changed.connect(self.on_audio_device_changed)
            dialog.silence_threshold_changed.connect(self._update_silence_threshold)
            dialog.auto_off_timeout_changed.connect(self._update_auto_off_timeout)
            self._settings_dialog = dialog

        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    # -- phase display -----------------------------------------------------

    def on_processing_phase_changed(self, phase: str) -> None:
        """Repaint the window to match the pipeline's current phase."""
        target = phase if phase in PHASE_COLORS else None
        if self._active_phase == target:
            return
        self._active_phase = target

        self.setProperty("phase", target or "")
        # Qt only re-evaluates property-based selectors on a repolish, and a
        # repolish reaches exactly one widget. The background rule is on this
        # window, but the rule that flips the checkbox's text black for the
        # white phase is on the checkbox, so it has to be repolished too --
        # without this the "Speech-to-Text" label stays white on white.
        for widget in (self, self.mic_checkbox):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.update()
        log.debug(f"Processing phase active: {phase}")

    # -- settings: silence threshold --------------------------------------

    def _get_silence_threshold(self) -> float:
        return float(self.config.get("silence_threshold", DEFAULT_SILENCE_THRESHOLD))

    def _update_silence_threshold(self, value: float) -> None:
        """Persist a new threshold and push it at the running stream."""
        value = float(value)
        self.config["silence_threshold"] = value
        save_config(self.config)
        if self.recorder:
            self.recorder.set_silence_threshold(value)
        formatted = f"{value:.4f}".rstrip("0").rstrip(".") or "0"
        print(f"🎚️ Silence threshold set to {formatted}")

    # -- settings: auto-off timeout ---------------------------------------

    def _get_window_width(self) -> int:
        return int(self.config.get("window_width", DEFAULT_WINDOW_WIDTH))

    def _get_auto_off_timeout(self) -> int:
        return int(self.config.get("mic_auto_off_timeout", DEFAULT_MIC_AUTO_OFF_TIMEOUT_S))

    def _update_auto_off_timeout(self, value: int) -> None:
        # Read live by _check_auto_off on every tick, so there is nothing to
        # push anywhere -- saving it is enough.
        value = int(value)
        self.config["mic_auto_off_timeout"] = value
        save_config(self.config)
        if value == 0:
            print("⏱️  Mic auto-off disabled")
        else:
            print(f"⏱️  Mic auto-off timeout set to {value}s")

    # -- microphone --------------------------------------------------------

    def _auto_start_microphone(self) -> None:
        """Turn the mic on at launch, showing orange while it initializes."""
        self.on_processing_phase_changed("transcribing")
        # Let the orange actually paint before the blocking device open.
        QTimer.singleShot(100, self._complete_auto_start)

    def _complete_auto_start(self) -> None:
        self.mic_checkbox.setChecked(True)  # triggers on_mic_toggled
        self.on_processing_phase_changed("idle")

    def on_audio_device_changed(self, name: str | None) -> None:
        """Handle microphone selection change."""
        # The stream is already open on the old device, so stop first.
        if self.mic_checkbox.isChecked():
            self.mic_checkbox.setChecked(False)

        self.config["audio_device"] = name
        save_config(self.config)
        log.info(f"Microphone changed to: {name or 'System Default'}")

    def on_mic_toggled(self, checked: bool) -> None:
        if checked:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self) -> None:
        """Start audio recording."""
        if self.is_recording:
            return

        self.recorder = AudioRecorder(
            audio_device=self.config.get("audio_device"),
            silence_threshold=self._get_silence_threshold(),
        )
        self.recorder.phase_changed.connect(self.on_processing_phase_changed)
        self.recorder.speech_transcribed.connect(self.on_speech_transcribed)

        success, error_msg = self.recorder.start()
        if not success:
            self.recorder = None
            self._show_mic_error(error_msg or "Unknown microphone error.")
            self.mic_checkbox.setChecked(False)
            return

        self.is_recording = True
        print("🎤 Microphone ON - listening...")

        self._start_auto_off_timer()

        # First use triggers the portal permission dialog, so get it out of
        # the way now rather than mid-sentence.
        injector = get_keyboard_injector()
        injector.initialize(callback=self._on_keyboard_ready)

    def stop_recording(self) -> None:
        """Stop audio recording."""
        if not self.is_recording:
            return

        self.is_recording = False
        if self.recorder:
            self.recorder.stop()
            self.recorder = None

        self._auto_off_timer.stop()
        print("🔇 Microphone OFF")

    def _show_mic_error(self, message: str) -> None:
        QMessageBox.warning(self, "Microphone Error", message)

    def _on_keyboard_ready(self, success: bool) -> None:
        if success:
            print("⌨️  Keyboard access granted - ready to type!")
        else:
            print("⚠️  Keyboard access denied - text will not be typed")

    # -- transcription results --------------------------------------------

    def on_speech_transcribed(self, text: str) -> None:
        """Type a finished transcription wherever the cursor is."""
        # Marks the mic as actively in use, deferring auto-off.
        self._last_audio_detection_time = time.time()

        recorder = self.recorder

        def finished() -> None:
            if recorder:
                recorder.notify_typing_done()
            else:
                self.on_processing_phase_changed("idle")

        try:
            type_text(text, on_finished=finished)
        except Exception as e:
            log.error(f"Failed to type transcribed text: {e}", exc_info=True)
            finished()

    # -- auto-off inactivity timer ----------------------------------------

    def _start_auto_off_timer(self) -> None:
        self._last_audio_detection_time = time.time()  # reset baseline
        self._auto_off_timer.start()
        log.debug("Auto-off inactivity timer started")

    def _check_auto_off(self) -> None:
        """Disable the mic once it has gone unused for the configured time."""
        if not self.is_recording:
            self._auto_off_timer.stop()
            return

        timeout = self._get_auto_off_timeout()
        if timeout <= 0:
            return  # auto-off disabled; keep ticking in case it is re-enabled

        elapsed = time.time() - (self._last_audio_detection_time or 0)
        if elapsed >= timeout:
            log.info(f"Mic auto-off: no transcription for {elapsed:.0f}s (threshold {timeout}s)")
            print(f"⏱️  Mic auto-off after {timeout}s of inactivity")
            self._auto_off_timer.stop()
            self.mic_checkbox.setChecked(False)  # triggers stop_recording

    # -- teardown ----------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Clean up when the window closes."""
        # A child dialog does not close with its parent, and while it is still
        # open Qt has a window left and will not quit the app.
        if self._settings_dialog is not None:
            self._settings_dialog.close()
        if self.width() != self._get_window_width():
            self.config["window_width"] = self.width()
            save_config(self.config)
        self.stop_recording()
        close_keyboard_injector()
        super().closeEvent(event)

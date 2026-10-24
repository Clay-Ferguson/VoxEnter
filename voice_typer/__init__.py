"""VoxEnter: system-wide voice-to-text input for Linux.

Listens to the microphone, transcribes speech with faster-whisper, and injects
the result as keystrokes via the XDG Remote Desktop Portal, so the text lands
in whatever application currently has keyboard focus.

Module layout deliberately keeps the Qt-free logic separate from the GUI:
`config` and `transcribe` import no Qt at all and can be exercised without a
display; `audio` and `keyboard` are QObjects only because they need signals
and D-Bus; `window` is the only module that knows about widgets.
"""

APP_NAME = "VoxEnter"

# Matches the .desktop filename and its StartupWMClass. Passed to
# QGuiApplication.setDesktopFileName() so Wayland can tie the window to the
# desktop entry and show our icon rather than a generic one.
APP_ID = "com.voxenter.voicetyper"

# The window is a small always-visible control, glanced at rather than read,
# so its controls run well above the desktop default point size.
UI_FONT_PX = 20

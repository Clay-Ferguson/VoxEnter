"""Keyboard injection via the XDG Remote Desktop Portal.

This is what lets a Wayland session receive synthetic keystrokes without root
or X11, and it is the most Linux-specific part of the app. The portal protocol
is CreateSession -> SelectDevices -> Start, then NotifyKeyboardKeysym per key,
all spoken over QtDBus.

Four QtDBus facts the port depends on, each established by experiment against
the live portal; none of them are interchangeable with the obvious alternative:

1. `a{sv}` options marshal correctly from a **plain** Python dict. Wrapping the
   values in QDBusVariant does not merely fail, it crashes xdg-desktop-portal.
2. uint32 arguments must be built with `QDBusArgument(v, QMetaType.Type.UInt)`.
   A plain Python int marshals as int32, and the portal rejects the message
   with "(oa{sv}ii) does not match expected type (oa{sv}iu)".
3. Object paths must be `QDBusObjectPath`, never a bare string.
4. `QDBusConnection.connect()` only accepts a `@pyqtSlot`-decorated method of a
   QObject -- hence this class being a QObject and the response handlers being
   decorated. A plain callable raises TypeError.

A marshalling mistake in (2) never raises: `_send_key` only logs the portal's
error reply, so the symptom is an error line per keystroke in the log while
nothing gets typed.
"""

from __future__ import annotations

import logging
import random
import string
from typing import Callable

from PyQt6.QtCore import QMetaType, QObject, QTimer, pyqtSlot
from PyQt6.QtDBus import (QDBusArgument, QDBusConnection, QDBusMessage,
                          QDBusObjectPath, QDBusPendingCallWatcher,
                          QDBusPendingReply)

from .config import load_config, save_config

log = logging.getLogger(__name__)

# =============================================================================
# DBus Portal Constants
# =============================================================================

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJ_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# SelectDevices device types: 1=keyboard, 2=pointer, 3=both
DEVICE_TYPE_KEYBOARD = 1
# persist_mode: 0=don't persist, 1=persist while app runs, 2=persist until revoked
PERSIST_UNTIL_REVOKED = 2

# Milliseconds between characters. Injecting a whole utterance at once outruns
# some applications' input handling, so keys are spaced out.
KEY_INTERVAL_MS = 1


def _uint(value: int) -> QDBusArgument:
    """Force uint32. See note (2) in the module docstring."""
    return QDBusArgument(value, QMetaType.Type.UInt.value)


# =============================================================================
# Character to X11 Keysym mapping
# See: https://www.cl.cam.ac.uk/~mgk25/ucs/keysyms.txt
# =============================================================================

CHAR_TO_KEYSYM = {
    # Lowercase letters (XK_a - XK_z)
    'a': 0x0061, 'b': 0x0062, 'c': 0x0063, 'd': 0x0064, 'e': 0x0065,
    'f': 0x0066, 'g': 0x0067, 'h': 0x0068, 'i': 0x0069, 'j': 0x006a,
    'k': 0x006b, 'l': 0x006c, 'm': 0x006d, 'n': 0x006e, 'o': 0x006f,
    'p': 0x0070, 'q': 0x0071, 'r': 0x0072, 's': 0x0073, 't': 0x0074,
    'u': 0x0075, 'v': 0x0076, 'w': 0x0077, 'x': 0x0078, 'y': 0x0079,
    'z': 0x007a,

    # Uppercase letters (XK_A - XK_Z)
    'A': 0x0041, 'B': 0x0042, 'C': 0x0043, 'D': 0x0044, 'E': 0x0045,
    'F': 0x0046, 'G': 0x0047, 'H': 0x0048, 'I': 0x0049, 'J': 0x004a,
    'K': 0x004b, 'L': 0x004c, 'M': 0x004d, 'N': 0x004e, 'O': 0x004f,
    'P': 0x0050, 'Q': 0x0051, 'R': 0x0052, 'S': 0x0053, 'T': 0x0054,
    'U': 0x0055, 'V': 0x0056, 'W': 0x0057, 'X': 0x0058, 'Y': 0x0059,
    'Z': 0x005a,

    # Numbers (XK_0 - XK_9)
    '0': 0x0030, '1': 0x0031, '2': 0x0032, '3': 0x0033, '4': 0x0034,
    '5': 0x0035, '6': 0x0036, '7': 0x0037, '8': 0x0038, '9': 0x0039,

    # Common punctuation and symbols
    ' ': 0x0020,   # space
    '!': 0x0021,   # exclam
    '"': 0x0022,   # quotedbl
    '#': 0x0023,   # numbersign
    '$': 0x0024,   # dollar
    '%': 0x0025,   # percent
    '&': 0x0026,   # ampersand
    "'": 0x0027,   # apostrophe
    '(': 0x0028,   # parenleft
    ')': 0x0029,   # parenright
    '*': 0x002a,   # asterisk
    '+': 0x002b,   # plus
    ',': 0x002c,   # comma
    '-': 0x002d,   # minus
    '.': 0x002e,   # period
    '/': 0x002f,   # slash
    ':': 0x003a,   # colon
    ';': 0x003b,   # semicolon
    '<': 0x003c,   # less
    '=': 0x003d,   # equal
    '>': 0x003e,   # greater
    '?': 0x003f,   # question
    '@': 0x0040,   # at
    '[': 0x005b,   # bracketleft
    '\\': 0x005c,  # backslash
    ']': 0x005d,   # bracketright
    '^': 0x005e,   # asciicircum
    '_': 0x005f,   # underscore
    '`': 0x0060,   # grave
    '{': 0x007b,   # braceleft
    '|': 0x007c,   # bar
    '}': 0x007d,   # braceright
    '~': 0x007e,   # asciitilde
    '\n': 0xff0d,  # Return key
    '\t': 0xff09,  # Tab key
}


class KeyboardInjector(QObject):
    """Owns the portal session and types text through it.

    Session persistence uses the portal's v2 restore-token feature: the token
    returned by Start is saved to config so subsequent runs skip the permission
    dialog entirely. A refused or stale token is cleared so the next attempt
    prompts fresh rather than failing forever.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bus = QDBusConnection.sessionBus()
        self.session_handle: str | None = None
        self.pending_text: str | None = None
        self._pending_callback: Callable[[], None] | None = None
        self._subscription: tuple | None = None
        self._initializing = False
        self._initialized = False
        self._init_callback: Callable[[bool], None] | None = None
        # Watchers must outlive the call: a QDBusPendingCallWatcher that gets
        # garbage collected never emits finished.
        self._watchers: list[QDBusPendingCallWatcher] = []

    # -- plumbing ----------------------------------------------------------

    def _generate_token(self) -> str:
        """Generate a unique token for portal requests."""
        return "voicetyper_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=16))

    def _get_request_path(self, token: str) -> str:
        """The DBus request object path the portal will answer on."""
        sender = self.bus.baseService().replace(".", "_").replace(":", "")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _subscribe(self, request_path: str, slot) -> None:
        """Listen for the Response signal before making the matching call."""
        args = (BUS_NAME, request_path, REQUEST_IFACE, "Response")
        if not self.bus.connect(*args, slot):
            log.error(f"Failed to subscribe to {request_path}")
        self._subscription = (args, slot)

    def _unsubscribe(self) -> None:
        if self._subscription:
            args, slot = self._subscription
            self.bus.disconnect(*args, slot)
            self._subscription = None

    def _call_async(self, method: str, arguments: list, label: str) -> None:
        """Fire a portal method call; the real answer arrives via Response."""
        msg = QDBusMessage.createMethodCall(BUS_NAME, OBJ_PATH, REMOTE_DESKTOP_IFACE, method)
        msg.setArguments(arguments)
        watcher = QDBusPendingCallWatcher(self.bus.asyncCall(msg), self)

        def finished(w: QDBusPendingCallWatcher) -> None:
            reply = QDBusPendingReply(w)
            if reply.isError():
                error = reply.error()
                log.error(f"{label} call failed: {error.name()}: {error.message()}")
                self._fail()
            if w in self._watchers:
                self._watchers.remove(w)

        watcher.finished.connect(finished)
        self._watchers.append(watcher)

    def _fail(self) -> None:
        """Abandon initialization, releasing anything waiting on it."""
        self._unsubscribe()
        self._initializing = False
        self._initialized = False
        self._flush_pending()
        if self._init_callback:
            self._init_callback(False)
            self._init_callback = None

    def _flush_pending(self) -> None:
        """Release a queued utterance so its completion callback still runs."""
        if self.pending_text:
            callback = self._pending_callback
            self.pending_text = None
            self._pending_callback = None
            if callback:
                callback()

    # -- step 1: CreateSession --------------------------------------------

    def initialize(self, callback: Callable[[bool], None] | None = None) -> None:
        """Set up the portal session, triggering a permission dialog if needed."""
        if self._initialized:
            if callback:
                callback(True)
            return

        if self._initializing:
            return

        if not self.bus.isConnected():
            log.error("No DBus session bus available")
            if callback:
                callback(False)
            return

        self._initializing = True
        self._init_callback = callback
        log.info("Initializing Remote Desktop portal session...")

        token = self._generate_token()
        self._subscribe(self._get_request_path(token), self._on_create_session_response)
        self._call_async(
            "CreateSession",
            [{"handle_token": token, "session_handle_token": self._generate_token()}],
            "CreateSession",
        )

    @pyqtSlot(QDBusMessage)
    def _on_create_session_response(self, message: QDBusMessage) -> None:
        self._unsubscribe()

        response, results = message.arguments()
        if response != 0:
            log.error(f"CreateSession failed with response code: {response}")
            self._fail()
            return

        self.session_handle = results.get("session_handle")
        if not self.session_handle:
            log.error("No session_handle in CreateSession response")
            self._fail()
            return

        log.info(f"Session created: {self.session_handle}")
        self._select_devices()

    # -- step 2: SelectDevices --------------------------------------------

    def _select_devices(self) -> None:
        token = self._generate_token()
        self._subscribe(self._get_request_path(token), self._on_select_devices_response)

        options = {
            "handle_token": token,
            "types": _uint(DEVICE_TYPE_KEYBOARD),
            "persist_mode": _uint(PERSIST_UNTIL_REVOKED),
        }

        saved_token = load_config().get("portal_restore_token")
        if saved_token:
            log.debug("Using saved restore token to skip permission dialog")
            options["restore_token"] = saved_token

        self._call_async(
            "SelectDevices",
            [QDBusObjectPath(self.session_handle), options],
            "SelectDevices",
        )

    @pyqtSlot(QDBusMessage)
    def _on_select_devices_response(self, message: QDBusMessage) -> None:
        self._unsubscribe()

        response, _results = message.arguments()
        if response != 0:
            log.error(f"SelectDevices failed with response code: {response}")
            self._fail()
            return

        self._start_session()

    # -- step 3: Start -----------------------------------------------------

    def _start_session(self) -> None:
        """Start the session. This is the step that can raise the dialog."""
        token = self._generate_token()
        self._subscribe(self._get_request_path(token), self._on_start_session_response)
        self._call_async(
            "Start",
            [QDBusObjectPath(self.session_handle), "", {"handle_token": token}],
            "Start",
        )

    @pyqtSlot(QDBusMessage)
    def _on_start_session_response(self, message: QDBusMessage) -> None:
        self._unsubscribe()

        response, results = message.arguments()
        if response != 0:
            if response == 1:
                log.warning("User cancelled the Remote Desktop permission dialog")
            else:
                log.error(f"Start session failed with response code: {response}")
            # Either way the saved token is no longer trustworthy; drop it so
            # the next attempt prompts fresh instead of failing silently.
            self._clear_restore_token()
            self._fail()
            return

        # Save the restore token so future runs skip the permission dialog.
        new_token = results.get("restore_token")
        if new_token:
            config = load_config()
            config["portal_restore_token"] = new_token
            save_config(config)
            log.info("Saved portal restore token - future runs won't need permission dialog")

        log.info("Remote Desktop session started successfully")
        self._initialized = True
        self._initializing = False

        if self._init_callback:
            self._init_callback(True)
            self._init_callback = None

        if self.pending_text:
            text = self.pending_text
            callback = self._pending_callback
            self.pending_text = None
            self._pending_callback = None
            self._start_typing(text, callback)

    def _clear_restore_token(self) -> None:
        config = load_config()
        if config.get("portal_restore_token"):
            config["portal_restore_token"] = None
            save_config(config)
            log.debug("Cleared saved portal restore token")

    # -- typing ------------------------------------------------------------

    def type_text(self, text: str, on_finished: Callable[[], None] | None = None) -> None:
        """Type `text` as keystrokes, initializing the session if necessary."""
        if not self._initialized:
            log.warning("Keyboard injector not initialized, queuing text")
            self.pending_text = text
            self._pending_callback = on_finished
            if not self._initializing:
                self.initialize()
            return

        self._start_typing(text, on_finished)

    def _start_typing(self, text: str, on_finished: Callable[[], None] | None) -> None:
        """Send `text` one character at a time, spaced by KEY_INTERVAL_MS."""
        if not text:
            if on_finished:
                on_finished()
            return

        def type_char_at_index(index: int) -> None:
            if index >= len(text):
                if on_finished:
                    on_finished()
                return

            char = text[index]
            keysym = CHAR_TO_KEYSYM.get(char)
            if keysym is None:
                # Unicode keysyms cover anything not in the table.
                keysym = 0x01000000 | ord(char)

            self._send_key(keysym, pressed=True)
            self._send_key(keysym, pressed=False)

            QTimer.singleShot(KEY_INTERVAL_MS, lambda: type_char_at_index(index + 1))

        QTimer.singleShot(0, lambda: type_char_at_index(0))

    def _send_key(self, keysym: int, pressed: bool) -> None:
        """Send one key event, waiting for the portal to acknowledge it.

        This call is deliberately BLOCKING. Sending fire-and-forget with
        send() is measurably faster and measurably wrong: without the
        round-trip as back-pressure, key events race ahead of the compositor
        and arrive scrambled and truncated -- "and so my fellow americans "
        was received as "and sy fo mellow ".
        """
        if not self.session_handle:
            log.error("No session handle, cannot send key")
            return

        msg = QDBusMessage.createMethodCall(
            BUS_NAME, OBJ_PATH, REMOTE_DESKTOP_IFACE, "NotifyKeyboardKeysym"
        )
        # Signature is (oa{sv}iu): keysym is signed, state is UNSIGNED.
        msg.setArguments([
            QDBusObjectPath(self.session_handle),
            {},  # options (empty)
            keysym,
            _uint(1 if pressed else 0),
        ])
        reply = self.bus.call(msg)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            log.error(f"Failed to send key {hex(keysym)} (pressed={pressed}): "
                      f"{reply.errorName()}: {reply.errorMessage()}")

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        """Close the Remote Desktop session."""
        if self.session_handle and self.bus.isConnected():
            msg = QDBusMessage.createMethodCall(
                BUS_NAME, self.session_handle, "org.freedesktop.portal.Session", "Close"
            )
            self.bus.call(msg)
            log.info("Remote Desktop session closed")

        self._unsubscribe()
        self._watchers.clear()
        self.session_handle = None
        self._initialized = False


# =============================================================================
# Module-level access
# =============================================================================

_keyboard_injector: KeyboardInjector | None = None


def get_keyboard_injector() -> KeyboardInjector:
    """Get or create the global keyboard injector instance."""
    global _keyboard_injector
    if _keyboard_injector is None:
        _keyboard_injector = KeyboardInjector()
    return _keyboard_injector


def close_keyboard_injector() -> None:
    """Tear down the global injector, if one was created."""
    global _keyboard_injector
    if _keyboard_injector:
        _keyboard_injector.close()
        _keyboard_injector = None


def type_text(text: str, on_finished: Callable[[], None] | None = None) -> None:
    """Type text via the portal. Triggers a permission dialog on first use."""
    print(f"\n🎯 TRANSCRIBED: {len(text)} characters\n")
    get_keyboard_injector().type_text(text, on_finished=on_finished)

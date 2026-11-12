# VoxEnter - AI Coding Instructions

## Project Overview
VoxEnter provides **system-wide voice typing for Linux**: it listens to the microphone, transcribes speech locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and types the text into whatever application has keyboard focus.

**Philosophy**: Framework-light. PyQt6 + plain Python, no build step. Everything Python comes from PyPI as prebuilt wheels via `uv`.

## Architecture

```
voice_typer/ (PyQt6)
    │
    ▼
sounddevice (48kHz) → RMS silence detection → resample to 16kHz → normalize
    │
    ▼
faster-whisper, in-process (base.en, int8, CPU; model loaded once)
    │
    ▼
XDG Remote Desktop Portal over QtDBus → keystrokes into the focused app
```

## Developer Commands

```bash
./run.sh     # Launch the control window from the checkout
./build.sh   # Build dist/voxenter_<version>_<arch>.deb, then offer to install it
```

`build.sh` is a wrapper around `packaging/build-deb.sh`, which does the building and smoke-tests the staged tree before packing it.

## Code Conventions

- **Section markers**: `# ====` blocks organize each module
- **Paths**: Always use `Path` from pathlib
- **Logging**: Writes to `voice_typer.log` beside the package (overwritten each run); when that directory isn't writable, as in the installed `/usr/lib/voxenter`, to `~/.local/state/voxenter/voice_typer.log`
- **Config file**: `~/.config/voxenter.yaml` stores user preferences (microphone, silence threshold, auto-adjust sensitivity, auto-off timeout, window width) and the portal restore token
- **Device selection**: dropdown in the Settings dialog lets user pick microphone; saved to config file
- **UI shape**: the main window is one row -- mic checkbox and a gear -- and never resizes itself. The user can drag its width (default 400, saved as `window_width` in the config file on close); its height is fixed to the row. Settings open in a separate modeless dialog (`settings_dialog.py`). There is no in-window close button; the title bar's is the same size and a few pixels away
- **Window chrome**: every window draws `windowchrome`'s title bar and 2px border instead of the system's, and the mic checkbox's enlarged indicator comes from the same library -- a library of ours in its own repository. See the entries under "Deliberate non-obvious choices" below
- **Every window is framed**: each top-level window and dialog the app opens gets `windowchrome.apply_frame()` before it is first shown -- `apply_frame(self)` in the constructor of a window or `QDialog` subclass, `apply_frame(box)` before `exec()` on a hand-built `QMessageBox`. Message boxes go through `voice_typer/messages.py` (`messages.warning(...)` and siblings, same arguments and results as `QMessageBox`'s static functions), never the static `QMessageBox.warning()` and friends, which build and run their box in one call and leave nothing to frame. File pickers (`QFileDialog.get…()`), and colour or font pickers if the desktop supplies them, stay native: they belong to another process and cannot be framed, and switching them to Qt's own dialogs to frame them is not worth it. The Remote Desktop permission prompt is the portal's own window, for the same reason
- **Module layout**: `voice_typer/` holds `__main__.py` (startup), `window.py` (the control window), `settings_dialog.py` (the Settings dialog), `messages.py` (framed message boxes), `audio.py`, `transcribe.py`, `keyboard.py` and `config.py`
- **Thread bridge**: the PortAudio callback and whisper worker threads reach the UI through `pyqtSignal`, never by touching widgets directly

## Deliberate non-obvious choices - do not "fix" these

- **The config file also holds `portal_restore_token`.** Renaming `voxenter.yaml` would orphan it and force users through the Remote Desktop permission dialog again.
- **One shared `WhisperModel`, loaded once.** `get_model()` in `transcribe.py` loads it under a lock; `preload_model()` starts that on a background thread at launch so the window appears immediately. Worker threads share the model, and CTranslate2's single default worker makes overlapping `transcribe()` calls queue rather than collide.
- **`faster_whisper` is imported lazily, inside `get_model()`.** It pulls in CTranslate2 and onnxruntime, and importing them at module scope would delay the window. It also keeps `transcribe.py` cheap to import for headless tests.
- **`get_model()` tries `local_files_only=True` first.** Without it huggingface_hub contacts the Hub on every launch even when the model is cached. Only a missing model falls through to a download.
- **`vad_filter=False`.** Silence detection already happened in `audio.py`; faster-whisper's own VAD would second-guess the tuned state machine.
- **Audio goes to faster-whisper as a numpy array**, mono float32 at 16kHz. There are no temp WAV files and no ffmpeg dependency.
- **CPU only, int8.** CTranslate2's GPU path is CUDA-only, so there is no GPU option on Intel/AMD graphics.
- **`_send_key` blocks on `bus.call()` instead of using fire-and-forget `bus.send()`.** The round-trip is load-bearing back-pressure: without it, key events outrun the compositor and arrive scrambled and truncated.
- **uint32 D-Bus arguments must be `QDBusArgument(v, QMetaType.Type.UInt)`.** A plain Python int marshals as int32 and the portal rejects the message. The rejection is only logged, so getting this wrong shows up as nothing being typed.
- **`a{sv}` options are plain Python dicts.** Wrapping the values in `QDBusVariant` crashes xdg-desktop-portal outright.
- **The RMS silence-detection state machine in `audio.py` is tuned**, not arbitrary. Changing the constants or the branch structure causes missed or spurious utterances.
- **The main window must never resize itself.** That is why Settings lives in `settings_dialog.py` rather than an expanding panel. On Wayland, mutter replays a stale size in the configure that comes with focus changes, and a window that had resized itself got that stale size applied, which used to make the widgets overlap. Only the user changes the window's size, by dragging the frame.
- **The window's minimum width is the controls', not the title's.** The frame's title bar elides the title once the window is too narrow for it. That is accepted: the default width of 400 shows the whole title, and a user who drags it narrower can see what they are doing.
- **The main window's title bar has minimize and close, but no maximize.** Its height is pinned to the title bar plus one row of controls, so maximizing could only stretch that row across the screen. `apply_frame(self, maximize=False)` in `window.py`; without the button, double-clicking the title bar does nothing either. The Settings dialog and message boxes get a close button only, the frame's default for a `QDialog`.
- **The settings dialog never touches the config file or the recorder.** It validates input and emits signals; `window.py` applies them. Most settings have live side effects (the threshold reaches a running recorder, a device change stops the mic, calibration pauses it), and the window is the only thing that owns both. Calibration lives in the window for the same reason.
- **The icon buttons are painted white once, at construction, and never change.** An icon is a pixmap and does not follow the stylesheet's `color` rule, so it has to be painted by hand -- but one fixed color is all this window needs, so it does not track the phase. Only the `-symbolic` theme icons are painted; the full-color fallbacks silhouette into a blob, so they are used as they ship.
- **`on_processing_phase_changed` repolishes the mic checkbox as well as the window.** A repolish reaches exactly one widget, and the rule that flips the checkbox text black for the white phase lives on the checkbox.
- **`sounddevice` is imported lazily**, so a missing PortAudio produces the friendly dependency message instead of an import traceback.
- **`windowchrome` is a sibling library.** `[tool.uv.sources]` in `pyproject.toml` resolves it at `../windowchrome`. It has to be there or `uv run` fails with an unresolved path dependency. This app takes two things from it: the window frame (`apply_frame()`, `windowchrome/README.md` §13) and the check-box indicator (`apply_checkboxes()`, §12, the entry below). Neither has a setup call or an ordering rule beyond framing a window before it is first shown. The app has no scroll areas, so it does not use `apply_scrollbars()`; if one is added, pass it `handle="#777777"` (the GTK theme's handle gray, as Start Menu does) through a named constant beside the stylesheet code. Do not edit `windowchrome` from here: all the apps share it.
- **The mic checkbox's enlarged indicator is `windowchrome.apply_checkboxes()`, not a stylesheet rule.** It is the shared component the other apps use, so they all enlarge a check box the same way. It has to be a `QProxyStyle` applied to the widget rather than a rule in the sheet -- `windowchrome/README.md` §12 has the reasoning -- so do not move it into `build_stylesheet()` when tidying that function. The font size stays in the sheet, which is what a stylesheet is good for.
- **Checking the mic does not recolor its label.** The window background is already reporting the pipeline's state in three colors chosen for the job, and a fourth color on the text would compete with it. The label keeps the theme's foreground in every state -- white on a dark desktop, black on a light one -- and the label keeps its normal weight too; the check mark alone shows the state. Do not pin it to a hex value: the 'typing' phase paints the window white, so a hard-coded white label would disappear into it, which is the very thing the `phase="typing"` rule exists to prevent.
- **The title bar and border are `windowchrome`'s, drawn inside the window, and only through `apply_frame()`.** The system title bar on a dark GNOME desktop is as dark as the window and sets nothing apart. The frame makes the window frameless and draws its own bar and border as child widgets in the window's contents margins, so the app's layout lands below the title bar by itself. Three rules follow. Never call `setContentsMargins()` on a framed window itself -- the frame owns those margins and overwrites them on the next resize; margins go on its *layout*. Do not wrap the window in anything to paint a border: the phase colors live on the plain toplevel `QWidget#voiceTyperWindow`, and a wrapper would take the window's identity away from it. And never hold a reference from a frame part back to its window -- no subclassing `TitleBar`/`WindowFrame`, no connecting frame buttons to window slots (§13 explains the crash). Pass no colors: the defaults (`FrameColors()`) are the shared look. Do not reintroduce coloring of the *system* title bar either; that relied on undocumented decoration-plugin internals and leaked into unrelated widgets. Expected, not bugs: dragging a modal dialog moves the main window too (GNOME's `attach-modal-dialogs`); right-clicking the title bar opens no window menu (Alt+Space does, including Always on Top); there is no drop shadow.
- **The phase color is the window's palette, not a stylesheet background, and the window carries no stylesheet.** The frame paints the window's background itself, from the `Window` palette role, on a child widget that covers the whole window (it is what rounds the corners). A stylesheet `background` on the window is painted underneath that child and never seen -- the colors went missing exactly that way when the frame arrived. And a widget that carries a stylesheet stops passing its palette down to its children, the frame's included, so `build_stylesheet()` is set on the checkbox and the gear, not on the window. Its selectors still name `#voiceTyperWindow[phase=...]` as an ancestor, which works wherever the sheet is set. `on_processing_phase_changed` sets a palette with only `Window` in it, or a default-constructed one for idle, which hands the window back to the theme.

- **The `.deb` takes PyQt6 from the distribution and vendors everything else.** faster-whisper, CTranslate2, onnxruntime and PyAV have no Debian packages, so `packaging/build-deb.sh` installs the `uv.lock` versions (minus PyQt6 and windowchrome) as wheels for `/usr/bin/python3` into `/usr/lib/voxenter/vendor`. That is why the package is architecture-specific and depends on `python3 (>= X.Y), python3 (<< X.Y+1)`: the wheels hold compiled extensions for that one Python. The launcher runs `python3 -I` with `vendor/` ahead of dist-packages, so the locked versions win over any distribution copy.

## Silence Detection Config

```python
# DEFAULT_SILENCE_THRESHOLD lives in config.py, the rest in audio.py.
# The threshold is also user-editable at runtime in the Settings dialog.
DEFAULT_SILENCE_THRESHOLD = 0.005  # RMS threshold
SILENCE_DURATION_S = 1.0           # Seconds of silence → transcribe
MIN_AUDIO_DURATION_S = 0.5         # Skip very short clips
```

## Common Modifications

**Change Whisper model**: Update `MODEL_NAME` in `voice_typer/transcribe.py` (any faster-whisper model name, e.g. `small.en`, `large-v3-turbo`). The new model downloads on the next launch.

**Tune for quiet mics**: Lower the silence threshold in the Settings dialog, check RMS in `voice_typer.log`

## Dependencies

- System, for running from the checkout: `libportaudio2`, `libxcb-cursor0`, `libxkbcommon-x11-0` (Ubuntu/Debian names; README lists the `apt` command). The `.deb` depends on what it needs, so `apt` brings it along
- Python (via pyproject.toml/uv): `PyQt6`, `sounddevice`, `numpy`, `PyYAML`, `faster-whisper`, and `windowchrome` -- the last of which is **not on PyPI**: it is resolved by path from `../windowchrome`
- Keyboard injection: XDG Remote Desktop Portal via QtDBus (part of PyQt6; no PyGObject)

## Note to AI Agents

Do not ever commit code to the 'git' repo yourself, unless the developer asks you to.


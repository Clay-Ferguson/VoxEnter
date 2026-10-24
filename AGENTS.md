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
./setup.sh   # First-time: system packages, model download, desktop entry
./run.sh     # Launch the control window
```

## Code Conventions

- **Section markers**: `# ====` blocks organize each module
- **Paths**: Always use `Path` from pathlib
- **Logging**: Writes to `voice_typer.log` beside the package (overwritten each run)
- **Config file**: `~/.config/voxenter.yaml` stores user preferences (microphone, silence threshold, auto-off timeout, window width) and the portal restore token
- **Device selection**: dropdown in the Settings dialog lets user pick microphone; saved to config file
- **UI shape**: the main window is one row -- mic checkbox and a gear -- and never resizes itself. The user can drag its width (default 400, saved as `window_width` in the config file on close); its height is fixed to the row. Settings open in a separate modeless dialog (`settings_dialog.py`). There is no in-window close button; the title bar's is the same size and a few pixels away
- **Window chrome**: the title bar and window frame are the platform's own, unstyled. The mic checkbox's enlarged indicator comes from `windowchrome`, a library of ours in its own repository -- see the entries under "Deliberate non-obvious choices" below
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
- **The window's minimum width is the controls', not the title's.** On GNOME Wayland, Qt's adwaita decoration clips the title without eliding it once the window is narrower than about 340px. That is accepted: the default width of 400 shows the whole title, and a user who drags it narrower can see what they are doing.
- **The settings dialog never touches the config file or the recorder.** It validates input and emits signals; `window.py` applies them. Two of the three settings have live side effects (the threshold reaches a running recorder, a device change stops the mic), and the window is the only thing that owns both.
- **The icon buttons are painted white once, at construction, and never change.** An icon is a pixmap and does not follow the stylesheet's `color` rule, so it has to be painted by hand -- but one fixed color is all this window needs, so it does not track the phase. Only the `-symbolic` theme icons are painted; the full-color fallbacks silhouette into a blob, so they are used as they ship.
- **`on_processing_phase_changed` repolishes the mic checkbox as well as the window.** A repolish reaches exactly one widget, and the rule that flips the checkbox text black for the white phase lives on the checkbox.
- **`sounddevice` is imported lazily**, so a missing PortAudio produces the friendly dependency message instead of an import traceback.
- **`windowchrome` is a sibling library.** `[tool.uv.sources]` in `pyproject.toml` resolves it at `../windowchrome`. It has to be there or `uv run` fails with an unresolved path dependency. The only thing this app takes from it is the entry below; it has no setup call and no ordering rule.
- **The mic checkbox's enlarged indicator is `windowchrome.apply_checkboxes()`, not a stylesheet rule.** It is the shared component the other apps use, so they all enlarge a check box the same way. It has to be a `QProxyStyle` applied to the widget rather than a rule in the sheet -- `windowchrome/README.md` §12 has the reasoning -- so do not move it into `build_stylesheet()` when tidying that function. The font size stays in the sheet, which is what a stylesheet is good for.
- **Checking the mic does not recolor its label.** The window background is already reporting the pipeline's state in three colors chosen for the job, and a fourth color on the text would compete with it. The label keeps the theme's foreground in every state -- white on a dark desktop, black on a light one -- and the label keeps its normal weight too; the check mark alone shows the state. Do not pin it to a hex value: the 'typing' phase paints the window white, so a hard-coded white label would disappear into it, which is the very thing the `phase="typing"` rule exists to prevent.
- **The title bar and window frame are the platform's own, and deliberately unstyled.** Coloring them on Wayland meant relying on undocumented Qt decoration-plugin internals and leaked into unrelated widgets. Do not reintroduce title-bar or frame coloring, and do not wrap the window in anything to paint a border: the phase colors live on the plain toplevel `QWidget#voiceTyperWindow`.

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

- System: `portaudio19-dev`, `libxcb-cursor0`, `libxkbcommon-x11-0` (Ubuntu/Debian names; `./setup.sh` installs them)
- Python (via pyproject.toml/uv): `PyQt6`, `sounddevice`, `numpy`, `PyYAML`, `faster-whisper`, and `windowchrome` -- the last of which is **not on PyPI**: it is resolved by path from `../windowchrome`
- Keyboard injection: XDG Remote Desktop Portal via QtDBus (part of PyQt6; no PyGObject)

## Note to AI Agents

Do not ever commit code to the 'git' repo yourself. This is always only done by human developers.

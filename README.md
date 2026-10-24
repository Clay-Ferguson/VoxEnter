# VoxEnter: Speech-to-Text Desktop App 🎤

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](voice_typer/)
[![PyQt6](https://img.shields.io/badge/PyQt-6-41CD52?logo=qt&logoColor=white)](voice_typer/window.py)
[![faster-whisper](https://img.shields.io/badge/faster--whisper-Local_STT-purple)](https://github.com/SYSTRAN/faster-whisper)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.md)

A lightweight Linux desktop application that provides system-wide voice-to-text input. Speak naturally and your words appear wherever your cursor is focused - in any application! This app runs as a tiny window that you keep open while while you work. The window has a Speech-to-Text checkbox. Whenever you click this checkbox, anything you speak gets immediately typed into whatever app has the keyboard focus.

## How It Works

1. **Select Microphone** - click the gear to open Settings and pick your mic
2. **Click Microphone Checkbox** - when the checkbox is checked anything you speak will be typed into wherever your edit cursor is, in any application system wide.
3. **Speak naturally** - after 1 second of silence, your speech is transcribed
4. **Text is typed** wherever your cursor is focused

The window background reports what the pipeline is doing, so you can dictate without looking away from what you are typing into:

| Color | Meaning |
|-------|---------|
| default | idle, listening |
| 🟢 green | speech detected |
| 🟠 orange | transcribing with whisper |
| ⬜ white | typing the text out |

## Speech Recognition

Transcription runs entirely on your machine with **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**, using OpenAI's English-only `base.en` Whisper model. It runs on the CPU with int8 quantization, so no GPU is needed, and the model stays loaded in memory, so each utterance costs only the transcription itself.

faster-whisper installs from PyPI as prebuilt wheels: there is nothing to compile. The model (~145MB) is downloaded once from Hugging Face and cached under `~/.cache/huggingface`. It is fetched the first time the app starts, in the background while the window comes up. After that it is loaded straight from the cache, and the app makes no network requests at all.

## Running from the checkout

The Debian package (see [Installing](#installing)) is the way to install VoxEnter. To run it straight from this checkout instead — while working on it, say — you need [uv](https://docs.astral.sh/uv/) for the Python environment:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

and the system libraries the PyPI wheels link against: PortAudio, and the X/xkb libraries Qt's platform plugin needs (`libxcb-cursor0` in particular; without it Qt fails with "could not load the Qt platform plugin xcb"):

```bash
sudo apt install libportaudio2 libxcb-cursor0 libxkbcommon-x11-0
```

There is no virtualenv to create by hand: `run.sh` lets uv build and refresh the environment on every launch.

#### The `windowchrome` checkout

The mic check box's enlarged indicator comes from **[windowchrome](https://github.com/Clay-Ferguson/windowchrome)**, a small reusable PyQt6 library kept in its own repository so several apps can share the same look. It is **not on PyPI**: `pyproject.toml` resolves it by path, at `../windowchrome`, so the checkout belongs beside this one:

```bash
cd ..                      # the directory holding VoxEnter/
git clone https://github.com/Clay-Ferguson/windowchrome.git
```

giving:

```
projects/
├── VoxEnter/                <- you are here
└── windowchrome/          <- must sit beside VoxEnter
```

If it is missing, `./run.sh` fails immediately with an unresolved path dependency rather than with anything subtle. The checkout is used in place — `uv` installs it editable, so there is nothing to build and an edit there is live here on the next run.

### Running

```bash
./run.sh
```

…which is equivalent to:

```bash
uv run python -m voice_typer
```

## Installing

```bash
./build.sh
```

`build.sh` runs `packaging/build-deb.sh`, which builds `dist/voxenter_<version>_<arch>.deb`, then asks whether to install it with `sudo apt install --reinstall` (default no). To do the two steps by hand:

```bash
packaging/build-deb.sh
sudo apt install --reinstall ./dist/voxenter_0.1.0_amd64.deb
```

`--reinstall` matters when installing over an earlier build: a rebuild keeps the version from `pyproject.toml`, and apt skips a `.deb` whose version is already installed without looking at its contents.

The package installs a `voxenter` command and a menu entry, and runs the app from `/usr/lib/voxenter` on the system's `python3`. PyQt6 comes from the distribution's `python3-pyqt6`, and PortAudio from `libportaudio2`; `apt` installs both. faster-whisper and the native libraries under it aren't packaged by Debian or Ubuntu, so they are bundled: the exact versions in `uv.lock` are installed as prebuilt wheels for the build machine's `/usr/bin/python3`. That makes the package large (about 80MB, 420MB installed), architecture-specific, and tied to that Python's minor version — build it on the kind of system you are installing it on. The whisper model is not in the package; it is downloaded on first launch as described above. Installed, the app logs to `~/.local/state/voxenter/voice_typer.log`.

Building needs `dpkg-deb`, `uv`, and the `windowchrome` sibling checkout, whose source is copied into the package. The Maintainer field comes from your `git config user.name` and `user.email`; override it with `VOXENTER_MAINTAINER="Name <email>"`.

If you installed the desktop entry with the old `setup.sh`, remove `~/.local/share/applications/com.voxenter.voicetyper.desktop`: it takes precedence over the package's and keeps launching the checkout.

### Adding dependencies

```bash
uv add <package>
```

This updates `pyproject.toml` and `uv.lock`.

## Code Layout

The Qt-free logic is deliberately kept separate from the GUI, so the parts that are tied to Linux are also the parts that are easiest to test headlessly:

| Module | Responsibility | Qt? |
|--------|----------------|-----|
| `voice_typer/config.py` | YAML settings, audio-device enumeration | no |
| `voice_typer/transcribe.py` | resample, normalize, run faster-whisper, clean up output | no |
| `voice_typer/audio.py` | recording + RMS silence detection | signals only |
| `voice_typer/keyboard.py` | XDG Remote Desktop Portal keystroke injection (QtDBus) | D-Bus only |
| `voice_typer/window.py` | the control window | yes |
| `voice_typer/settings_dialog.py` | the settings dialog (widgets + validation only) | yes |
| `voice_typer/__main__.py` | dependency checks and startup | yes |

## Troubleshooting & Tips

Troubleshooting and notes are [here](docs/TROUBLESHOOTING.md)

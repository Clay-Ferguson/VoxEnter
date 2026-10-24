# VoxEnter: Voice Typer - PyQt6 Desktop App 🎤

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](voice_typer/)
[![PyQt6](https://img.shields.io/badge/PyQt-6-41CD52?logo=qt&logoColor=white)](voice_typer/window.py)
[![faster-whisper](https://img.shields.io/badge/faster--whisper-Local_STT-purple)](https://github.com/SYSTRAN/faster-whisper)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.md)

A lightweight Linux desktop application that provides system-wide voice-to-text input. Speak naturally and your words appear wherever your cursor is focused - in any application!

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

faster-whisper installs from PyPI as prebuilt wheels: there is nothing to compile. The model (~145MB) is downloaded once from Hugging Face and cached under `~/.cache/huggingface`. `./setup.sh` downloads it ahead of time; otherwise it is fetched the first time the app starts. After that it is loaded straight from the cache, and the app makes no network requests at all.

## Running

This project uses [uv](https://docs.astral.sh/uv/) for Python environment management.

### First-time setup

Install uv (if not already installed):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install the system dependencies (PortAudio, and the X/xkb libraries Qt's platform plugin needs), download the whisper model, and install the desktop entry:

```bash
./setup.sh
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

#!/usr/bin/env bash
# Phonon launcher.
#
# uv builds/refreshes the virtualenv from pyproject.toml on every run, so there
# is no install step. Everything Python -- PyQt6 and faster-whisper included --
# comes from PyPI as prebuilt wheels.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Launched from the desktop entry there is no terminal, so an error printed to
# stderr is never seen and the click just appears to do nothing. Mirror it to a
# desktop notification in that case.
fail() {
  echo "Error: $1" >&2
  if [ ! -t 2 ] && command -v notify-send >/dev/null 2>&1; then
    notify-send --app-name=Phonon "Phonon failed to start" "$1" || true
  fi
  exit 1
}

# The desktop session's PATH does not necessarily include ~/.local/bin, where
# the uv installer puts uv: GNOME does not always source ~/.profile, which is
# what normally adds it. So check uv's standard install locations ourselves.
if ! command -v uv >/dev/null 2>&1; then
  for dir in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -x "$dir/uv" ]; then
      PATH="$dir:$PATH"
      break
    fi
  done
fi

if ! command -v uv >/dev/null 2>&1; then
  fail "uv is required but was not found in PATH. Install it from https://docs.astral.sh/uv/ or via: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

exec uv run --directory "${HERE}" python -m voice_typer "$@"

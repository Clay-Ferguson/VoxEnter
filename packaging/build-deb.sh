#!/usr/bin/env bash
# Build a Debian package (.deb) of VoxEnter.
#
# Usage:  packaging/build-deb.sh          (from anywhere)
# Output: dist/voxenter_<version>_<arch>.deb   (dist/ at the top of the checkout)
# Then:   sudo apt install --reinstall ./dist/voxenter_<version>_<arch>.deb
#         (--reinstall, or apt skips a rebuild whose version is already installed)
#
# Built by hand with dpkg-deb rather than with debhelper: VoxEnter is plain
# Python with no build step, so all the package has to do is put files in the
# right places and name its dependencies.
#
# Unlike a pure-PyQt app, VoxEnter can't take everything from the distribution:
# faster-whisper and the native libraries under it (CTranslate2, onnxruntime,
# PyAV, tokenizers) aren't packaged by Debian or Ubuntu. So the package splits
# its Python dependencies in two:
#
#   - PyQt6 comes from the distribution (python3-pyqt6), which brings Qt and
#     QtDBus with it and keeps the desktop integration the distribution's own.
#   - Everything else is installed from the versions pinned in uv.lock, as
#     prebuilt wheels for the build machine's /usr/bin/python3, into a private
#     vendor/ directory. Those wheels hold compiled extensions, so the package
#     is architecture-specific and pinned to that Python's minor version.
#
# windowchrome isn't packaged anywhere, so its source is copied in from the
# sibling checkout.
#
# Installed layout:
#   /usr/bin/voxenter                                 launcher
#   /usr/lib/voxenter/voice_typer/                    the app
#   /usr/lib/voxenter/windowchrome/                   copy of ../windowchrome
#   /usr/lib/voxenter/vendor/                         faster-whisper & co., from uv.lock
#   /usr/lib/voxenter/voxenter-logo.png               window and menu icon
#   /usr/share/applications/com.voxenter.voicetyper.desktop   menu entry
#
# The desktop entry is named after APP_ID (voice_typer/__init__.py), which
# __main__ passes to setDesktopFileName(); renaming one without the other
# breaks the icon in the dock.
#
# The whisper model is not in the package. It is per-user data, cached under
# ~/.cache/huggingface, and the app downloads it the first time it starts.
set -euo pipefail

# Every directory the package creates must be 755 and every file 644 or 755,
# whatever the builder's own umask is (Ubuntu's default 002 would otherwise
# leave /usr, /usr/lib and friends group-writable in the package).
umask 022

# HERE is packaging/, which holds what only the package needs (this script and
# the desktop entry); ROOT is the checkout, which holds the app.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname -- "$HERE")"
WINDOWCHROME="$ROOT/../windowchrome/windowchrome"
DIST="$ROOT/dist"
PACKAGE=voxenter
APP_ID=com.voxenter.voicetyper
PYTHON=/usr/bin/python3

die() {
  echo "Error: $*" >&2
  exit 1
}

command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb not found (it is part of dpkg, on every Debian-based system)."
command -v uv >/dev/null 2>&1 || die "uv not found; it installs the pinned dependencies. See https://docs.astral.sh/uv/"
[ -x "$PYTHON" ] || die "$PYTHON not found; the package runs on it (apt install python3)."

VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/pyproject.toml" | head -n 1)"
[ -n "$VERSION" ] || die "could not read the version from pyproject.toml."

ARCH="$(dpkg --print-architecture)"

# The vendored wheels are built for exactly this Python, so the package
# depends on its minor version and no other.
PYVER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PYNEXT="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1] + 1}")')"

[ -f "$WINDOWCHROME/__init__.py" ] || die "windowchrome not found at $WINDOWCHROME.
Clone it beside the VoxEnter checkout:  git clone https://github.com/Clay-Ferguson/windowchrome.git ../windowchrome"

# The Maintainer field is whoever builds the package, taken from git unless
# VOXENTER_MAINTAINER="Name <email>" says otherwise.
if [ -z "${VOXENTER_MAINTAINER:-}" ]; then
  name="$(git -C "$ROOT" config user.name || true)"
  email="$(git -C "$ROOT" config user.email || true)"
  [ -n "$name" ] && [ -n "$email" ] || die "set git user.name and user.email, or VOXENTER_MAINTAINER=\"Name <email>\"."
  VOXENTER_MAINTAINER="$name <$email>"
fi

STAGE="$DIST/${PACKAGE}_${VERSION}_${ARCH}"
DEB="$DIST/${PACKAGE}_${VERSION}_${ARCH}.deb"
VENDOR_BUILD="$DIST/vendor-build"
LIB="$STAGE/usr/lib/$PACKAGE"
rm -rf "$STAGE" "$DEB" "$VENDOR_BUILD"

# -- files -------------------------------------------------------------------

install -d -m 755 \
  "$LIB" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/doc/$PACKAGE" \
  "$STAGE/DEBIAN"

# Copy every file of a directory tree into the stage: files 644, and the
# directories `install -D` creates for them 755 (the umask above). Whole trees
# rather than a glob per directory, so a new module ships without anyone having
# to remember a line here. __pycache__ is left behind on purpose and compiled
# fresh by postinst. Shared libraries are fine at 644: the loader maps them, it
# doesn't execute them.
copy_tree() {
  local src="$1" dest="$2" file
  while IFS= read -r -d '' file; do
    install -D -m 644 "$src/$file" "$dest/$file"
  done < <(cd "$src" && find . -name __pycache__ -prune -o -type f -print0)
}

copy_tree "$ROOT/voice_typer" "$LIB/voice_typer"
copy_tree "$WINDOWCHROME" "$LIB/windowchrome"

# The exact versions in uv.lock, minus what the package gets elsewhere: PyQt6
# (and the Qt and sip wheels under it) from the distribution, windowchrome
# from the copy above. --only-binary because a dependency with no wheel for
# this Python would otherwise be compiled here, quietly, from source.
echo "Installing the pinned dependencies for Python $PYVER..."
uv export --project "$ROOT" --frozen --no-hashes --no-emit-project \
  --no-emit-package windowchrome \
  --no-emit-package pyqt6 \
  --no-emit-package pyqt6-qt6 \
  --no-emit-package pyqt6-sip \
  --output-file "$DIST/vendor-requirements.txt" >/dev/null
uv pip install --quiet --python "$PYTHON" --target "$VENDOR_BUILD" \
  --no-deps --only-binary :all: --no-config \
  -r "$DIST/vendor-requirements.txt" \
  || die "could not install the pinned dependencies for Python $PYVER (above)."
rm -f "$DIST/vendor-requirements.txt"
# Console scripts (huggingface-cli, tqdm, ...) nobody runs from here.
rm -rf "$VENDOR_BUILD/bin"
copy_tree "$VENDOR_BUILD" "$LIB/vendor"
rm -rf "$VENDOR_BUILD"

# __main__ loads the window icon from beside the package, so it ships there,
# and the desktop entry points at the same file.
install -m 644 "$ROOT/voxenter-logo.png" "$LIB/voxenter-logo.png"
install -m 644 "$ROOT/LICENSE.md" "$STAGE/usr/share/doc/$PACKAGE/copyright"

# -I (isolated mode) keeps PYTHONPATH and ~/.local site-packages out, so the app
# always runs against the distribution's python3-pyqt6 and the vendored
# dependencies rather than a pip-installed copy that happens to be lying
# around. vendor/ goes ahead of the system's dist-packages, so the versions
# from uv.lock win over any distribution copy of numpy and friends.
cat > "$STAGE/usr/bin/$PACKAGE" <<'EOF'
#!/bin/sh
# VoxEnter launcher, installed by the voxenter package.
exec /usr/bin/python3 -I -c 'import sys; sys.path[:0] = ["/usr/lib/voxenter", "/usr/lib/voxenter/vendor"]; from voice_typer.__main__ import main; sys.exit(main())' "$@"
EOF
chmod 755 "$STAGE/usr/bin/$PACKAGE"

# The desktop entry, as checked in. Its comment lines only describe the file,
# so they are dropped.
sed -e '/^#/d' "$HERE/$APP_ID.desktop" > "$STAGE/usr/share/applications/$APP_ID.desktop"
chmod 644 "$STAGE/usr/share/applications/$APP_ID.desktop"

# -- smoke test ----------------------------------------------------------------

# A file the copy missed, or a wheel that doesn't load on this Python, installs
# cleanly and fails on first launch, so the staged tree is checked before it is
# packed, with the launcher's own interpreter and flags (-I, and the same path
# order) plus -B so nothing is written into the stage:
#
#   - every import inside the staged packages names a file that was staged;
#   - every app module imports, where this python3 has PyQt6, and otherwise
#     the Qt-free ones;
#   - faster-whisper imports, which loads CTranslate2, onnxruntime, PyAV and
#     tokenizers, the compiled parts most likely not to match.
/usr/bin/python3 -I -B - "$LIB" <<'PY' || die "the staged files failed the smoke test (above); nothing was packaged."
import ast
import importlib
import pathlib
import sys

lib = pathlib.Path(sys.argv[1])
sys.path[:0] = [str(lib), str(lib / "vendor")]
problems = []


def is_module(path):
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


for package in ("voice_typer", "windowchrome"):
    for source in sorted((lib / package).rglob("*.py")):
        where = source.relative_to(lib)
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = source.parent
                for _ in range(node.level - 1):
                    base = base.parent
                if node.module:
                    if not is_module(base.joinpath(*node.module.split("."))):
                        problems.append(f"{where}: from {'.' * node.level}{node.module}")
                elif not (base / "__init__.py").is_file():
                    problems.append(f"{where}: from {'.' * node.level} (no package)")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top in ("voice_typer", "windowchrome") and not is_module(lib.joinpath(*name.split("."))):
                        problems.append(f"{where}: import {name}")

# __main__ is left out of the imports: importing it starts logging to a file.
try:
    import PyQt6.QtWidgets  # noqa: F401
    modules = [p.stem for p in (lib / "voice_typer").glob("*.py") if p.stem not in ("__init__", "__main__")]
    scope = "every module"
except ImportError:
    modules = ["config", "transcribe"]
    scope = "the Qt-free modules (no PyQt6 for this python3)"
for name in ["faster_whisper"] + [f"voice_typer.{m}" for m in sorted(modules)]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        problems.append(f"import {name}: {exc!r}")

if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(1)
print(f"Smoke test passed: imports resolve, faster-whisper loads, {scope} import.")
PY

# -- package metadata --------------------------------------------------------

# Compiled as root at install time, since a user running VoxEnter can't write
# __pycache__ under /usr/lib; without it every launch compiles in memory, and
# vendor/ is large enough for that to show. The menu-cache refresh is what
# makes the entry appear without a logout; it is absent on a bare system and
# harmless when it is.
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  python3 -m compileall -q /usr/lib/voxenter >/dev/null 2>&1 || true
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
EOF

# Removes what postinst compiled, so dpkg can remove the directories it
# installed (it only deletes files it knows about). Runs before upgrades too;
# the new version's postinst compiles again.
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/voxenter -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"

# libportaudio2 is hard: sounddevice can't open a microphone without it.
# xdg-desktop-portal is what types the text, but every desktop that can run it
# already has it, and qt6-wayland is only the native Wayland backend (Qt falls
# back to XWayland without it), so both are recommendations.
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3 (>= $PYVER), python3 (<< $PYNEXT), python3-pyqt6, libportaudio2
Recommends: xdg-desktop-portal, qt6-wayland
Maintainer: $VOXENTER_MAINTAINER
Installed-Size: $INSTALLED_SIZE
Homepage: https://github.com/Clay-Ferguson/VoxEnter
Description: system-wide voice typing, transcribed locally
 VoxEnter listens to the microphone, transcribes speech on your own machine
 with faster-whisper, and types the text into whatever application has
 keyboard focus, through the XDG Remote Desktop portal. It runs as a small
 window with a single check box: while it is checked, what you say is typed.
 .
 The whisper model (about 145MB) is downloaded on first launch and cached
 under ~/.cache/huggingface; after that nothing leaves the machine.
EOF

# -- build -------------------------------------------------------------------

# --root-owner-group: files owned by root without needing fakeroot.
# -Zxz: every dpkg can read xz; Ubuntu's default zstd is not readable by
# older Debian dpkg releases.
echo "Packing (this compresses a few hundred MB, so it takes a minute)..."
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
rm -rf "$STAGE"

echo "Built $DEB"
echo ""
echo "Install:  sudo apt install --reinstall $DEB"
echo "Remove:   sudo apt remove $PACKAGE"
# A leftover from the old setup.sh, which this package replaced. Its entry in
# ~/.local/share/applications takes precedence over the package's, so it would
# keep launching the checkout after the package is installed.
if [ -f "$HOME/.local/share/applications/$APP_ID.desktop" ]; then
  echo ""
  echo "Note: a per-user launcher in ~/.local/share/applications (left by the"
  echo "old setup.sh) hides the package's. Remove it with:"
  echo "  rm ~/.local/share/applications/$APP_ID.desktop"
fi

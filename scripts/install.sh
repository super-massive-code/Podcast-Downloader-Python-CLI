#!/usr/bin/env bash
# Build the standalone binary and put `podcast` on your PATH.
#
# Works from a fresh clone: creates .venv and installs the build
# dependencies first if they aren't there yet.
#
# Usage: scripts/install.sh
#   PODCAST_BIN_DIR=~/.local/bin scripts/install.sh   # install without sudo
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
BIN_DIR="${PODCAST_BIN_DIR:-/usr/local/bin}"
TARGET="$BIN_DIR/podcast"
SOURCE="$REPO_DIR/dist/podcast"

is_new_enough() {
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 13))' 2>/dev/null
}

find_python() {
    local candidate
    for candidate in python3.14 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && is_new_enough "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if [ -x "$VENV/bin/python" ]; then
    if ! is_new_enough "$VENV/bin/python"; then
        echo "error: $VENV was made with Python older than 3.13." >&2
        echo "Delete it (rm -rf .venv) and run this script again." >&2
        exit 1
    fi
else
    if ! PYTHON="$(find_python)"; then
        echo "error: Python 3.13 or newer is needed and none was found." >&2
        echo "  macOS:         brew install python@3.13" >&2
        echo "  Debian/Ubuntu: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update" >&2
        echo "                 sudo apt install python3.13 python3.13-venv" >&2
        exit 1
    fi
    echo "Creating .venv with $("$PYTHON" --version)..."
    "$PYTHON" -m venv "$VENV"
fi

echo "Installing build dependencies..."
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$REPO_DIR/requirements-dev.txt"

echo "Building..."
make -C "$REPO_DIR" build

if [ ! -x "$SOURCE" ]; then
    echo "error: build did not produce $SOURCE" >&2
    exit 1
fi

if [ ! -d "$BIN_DIR" ]; then
    echo "error: $BIN_DIR does not exist" >&2
    exit 1
fi

echo "Linking $TARGET -> $SOURCE"
if [ -w "$BIN_DIR" ]; then
    ln -sf "$SOURCE" "$TARGET"
else
    sudo ln -sf "$SOURCE" "$TARGET"
fi

echo "Installed. Run 'podcast' from any terminal."
echo "(It's a symlink, so future 'make build' runs update it automatically.)"

#!/usr/bin/env bash
# Collapse launcher for Linux (and macOS).
#   ./run.sh          start the bot (creates .venv and installs dependencies on first run)
#   ./run.sh update   reinstall dependencies, e.g. after pulling new code
#   ./run.sh genkey   print a new MASTER_KEY for .env
# For a VPS, deploy/install-ubuntu.sh sets this up as a systemd service instead.
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
    for candidate in python3.13 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            echo "$candidate"; return 0
        fi
    done
    return 1
}

install_deps() {
    echo "Installing dependencies..."
    .venv/bin/python -m pip install --upgrade pip --quiet
    .venv/bin/python -m pip install -e . --quiet
}

if [ ! -x .venv/bin/python ]; then
    PYTHON=$(find_python) || { echo "Python 3.12 or newer is required (see README.md)." >&2; exit 1; }
    echo "Creating virtual environment with $PYTHON..."
    "$PYTHON" -m venv .venv || { echo "venv failed. On Ubuntu: sudo apt install python3-venv" >&2; exit 1; }
    install_deps
fi

case "${1:-}" in
    update) install_deps; echo "Dependencies updated."; exit 0 ;;
    genkey) exec .venv/bin/python -m banbot genkey ;;
    "") ;;
    *) echo "usage: $0 [update|genkey]" >&2; exit 2 ;;
esac

if [ ! -f .env ]; then
    echo "No .env file found. Copy .env.example to .env and fill it in first:" >&2
    echo "    cp .env.example .env" >&2
    exit 1
fi

echo "Starting Collapse. Press Ctrl+C to stop."
exec .venv/bin/python -m banbot

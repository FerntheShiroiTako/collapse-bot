#!/usr/bin/env bash
# Set Collapse up as a systemd service on an Ubuntu VPS (22.04 or newer).
#
# Run from the bot/ folder as the user the bot should run as (not root):
#     ./deploy/install-ubuntu.sh
#
# It installs Python 3.12 if the system doesn't have it, creates .venv, installs dependencies, and
# installs + starts a systemd service named "collapse" that restarts the bot if it crashes and starts
# it again after a reboot. Safe to re-run after pulling new code: it reinstalls dependencies and
# restarts the service.
set -euo pipefail
cd "$(dirname "$0")/.."
BOT_DIR=$(pwd)
RUN_USER=$(id -un)
SERVICE=collapse

if [ "$RUN_USER" = "root" ]; then
    echo "Run this as the (non-root) user the bot should run as; it uses sudo where needed." >&2
    exit 1
fi

# ---- Python 3.12+
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null &&
   ! command -v python3.12 >/dev/null 2>&1; then
    echo "Installing Python 3.12 (this Ubuntu ships an older python3)..."
    sudo apt-get update
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update
    sudo apt-get install -y python3.12 python3.12-venv
else
    # Ubuntu's python3 lacks venv support until python3-venv (or python3.X-venv) is installed.
    PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    python3 -c 'import ensurepip' 2>/dev/null || sudo apt-get install -y "python${PYV}-venv"
fi

# ---- venv + dependencies (run.sh does both)
./run.sh update

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    KEY=$(.venv/bin/python -m banbot genkey)
    sed -i "s|^MASTER_KEY=.*|MASTER_KEY=${KEY}|" .env
    echo
    echo "Created .env with a fresh MASTER_KEY. Add your DISCORD_TOKEN to it:"
    echo "    nano ${BOT_DIR}/.env"
    echo "then run this script again to start the service."
    exit 0
fi
chmod 600 .env

# ---- systemd service
sed -e "s|@BOT_DIR@|${BOT_DIR}|g" -e "s|@USER@|${RUN_USER}|g" deploy/collapse.service \
    | sudo tee /etc/systemd/system/${SERVICE}.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE} >/dev/null
sudo systemctl restart ${SERVICE}

echo
echo "Collapse is running as the '${SERVICE}' service."
echo "  status:  systemctl status ${SERVICE}"
echo "  logs:    journalctl -u ${SERVICE} -f"
echo "  stop:    sudo systemctl stop ${SERVICE}"

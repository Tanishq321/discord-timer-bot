#!/usr/bin/env bash
# One-time setup on a Linux server (Ubuntu/Debian recommended; Oracle Linux/Fedora also work).
# Installs Python deps and registers the bot as a systemd service that
# starts on boot and restarts itself if it ever crashes.
#
# Usage (from inside the project folder):   bash deploy/setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=timer-bot
RUN_USER="$(id -un)"

echo "==> Installing Python"
if command -v apt-get >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip
elif command -v dnf >/dev/null; then
  sudo dnf install -y python3 python3-pip
else
  echo "Unsupported OS: install python3 + venv yourself, then rerun." >&2
  exit 1
fi

echo "==> Creating virtual environment"
cd "$APP_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ] || grep -q "paste-your-bot-token-here" .env; then
  [ -f .env ] || cp .env.example .env
  echo
  echo "!! Put your bot token in $APP_DIR/.env  (e.g.  nano .env)  then run this script again."
  exit 1
fi
chmod 600 .env

echo "==> Registering systemd service '$SERVICE'"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<UNIT
[Unit]
Description=Discord timer bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE
sleep 3
sudo systemctl --no-pager status $SERVICE | head -n 12
echo
echo "Done. The bot now runs 24/7 and starts again after reboots."
echo "  Live logs:  journalctl -u $SERVICE -f"
echo "  Restart:    sudo systemctl restart $SERVICE"
echo "  Update:     git pull && sudo systemctl restart $SERVICE"

#!/usr/bin/env bash
# shelldon — Raspberry Pi setup. Installs deps + a systemd service that autostarts
# shelldon (Telegram brain + E-Ink face) and restarts it on failure/boot.
#
# Idempotent — safe to re-run. Run it from a clone of the repo:
#   git clone https://github.com/elliotboney/shelldon.git ~/shelldon
#   cd ~/shelldon && ./deploy/setup-pi.sh
#
# It detects a Pi by the SPI device; on a non-Pi box it skips the E-Ink deps and
# leaves the display off (headless / CLI). Edit .env (the brain + bot token) before
# starting the service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$REPO"

echo "╔══════════════════════════════════════════╗"
echo "║   shelldon — Pi setup                     ║"
echo "╚══════════════════════════════════════════╝"
echo "repo=$REPO  user=$USER_NAME"

# --- 1. uv (the package manager / runner) -----------------------------------
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "[1/5] installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
echo "[1/5] uv: $("$UV" --version)"

# --- 2. python deps (locked) -------------------------------------------------
echo "[2/5] uv sync --locked…"
"$UV" sync --locked

# --- 3. E-Ink display deps (Pi only) ----------------------------------------
DISPLAY_ENV=""
if [ -e /dev/spidev0.0 ]; then
  echo "[3/5] Pi detected — installing E-Ink display deps (pillow/spidev/gpiozero/lgpio)…"
  sudo apt-get update -qq
  sudo apt-get install -y swig liblgpio-dev fonts-unifont >/dev/null
  "$UV" pip install pillow spidev gpiozero lgpio rpi-lgpio
  DISPLAY_ENV=$'Environment=SHELLDON_DISPLAY=waveshare\nEnvironment=GPIOZERO_PIN_FACTORY=lgpio'
else
  echo "[3/5] no /dev/spidev0.0 — headless box, skipping E-Ink deps (display stays off)"
fi

# --- 4. .env -----------------------------------------------------------------
if [ ! -f "$REPO/.env" ]; then
  cp "$REPO/.env.example" "$REPO/.env"
  echo "[4/5] created .env from .env.example — ⚠️  EDIT IT before starting:"
  echo "        GLM_API_KEY, SHELLDON_TELEGRAM_BOT_TOKEN, ALLOWED_USERS"
else
  echo "[4/5] .env already present"
fi

# --- 5. systemd service ------------------------------------------------------
echo "[5/5] installing systemd service…"
sudo tee /etc/systemd/system/shelldon.service >/dev/null <<UNIT
[Unit]
Description=shelldon — an E-Ink AI pet
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO
EnvironmentFile=$REPO/.env
Environment=SHELLDON_TRANSPORT=telegram
$DISPLAY_ENV
ExecStart=$UV run python -m shelldon
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
# Pi Zero 2W guardrail (512MB): shelldon's fork-server keeps RAM flat, but cap hard.
MemoryMax=400M
MemoryHigh=350M

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable shelldon.service

echo ""
echo "✅ shelldon installed. Next:"
echo "   1. edit $REPO/.env  (GLM_API_KEY + SHELLDON_TELEGRAM_BOT_TOKEN + ALLOWED_USERS)"
echo "   2. sudo systemctl start shelldon"
echo "   3. journalctl -u shelldon -f      # watch it"
echo ""
echo "   (stop: sudo systemctl stop shelldon | disable autostart: sudo systemctl disable shelldon)"

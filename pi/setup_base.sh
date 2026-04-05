#!/usr/bin/env bash
#
# Fermentoscope - Raspberry Pi setup (headless/base, web-only)
#
# Target: fresh Raspberry Pi OS Lite on a Raspberry Pi Zero 2 W
# (or any other model with WiFi). This variant runs the web UI only,
# no LCD required.
#
# Usage (run as user pi, from a fresh install with internet access):
#   curl -fsSL https://raw.githubusercontent.com/michalbrennek/fermentoscope/main/pi/setup_base.sh | bash
# Or:
#   git clone https://github.com/michalbrennek/fermentoscope.git
#   cd fermentoscope/pi && bash setup_base.sh
#
set -euo pipefail

REPO_URL="https://github.com/michalbrennek/fermentoscope.git"
INSTALL_DIR="/home/pi/fermentoscope"
PI_USER="pi"

echo "==============================================="
echo " Fermentoscope Base (web-only) Setup"
echo "==============================================="

# --- Basic checks ------------------------------------------------------------
if [ "$(id -un)" != "$PI_USER" ]; then
    echo "Please run as user '$PI_USER'."
    exit 1
fi

if ! command -v sudo >/dev/null; then
    echo "sudo is required"
    exit 1
fi

# --- Prompt for ESP32 URL ----------------------------------------------------
read -rp "ESP32 sensor URL [http://sourdough.local:8080/]: " ESP32_URL
ESP32_URL="${ESP32_URL:-http://sourdough.local:8080/}"

# --- Update system & install packages ---------------------------------------
echo ""
echo "[1/7] Updating package lists..."
sudo apt-get update -qq

echo "[2/7] Installing system packages..."
sudo apt-get install -y --no-install-recommends \
    git python3 python3-pil python3-pip python3-requests \
    avahi-daemon avahi-utils libnss-mdns \
    bluez rfkill \
    openssl ca-certificates

# --- Configure BLE fallback --------------------------------------------------
# Install bleak + enable the onboard Bluetooth adapter so the backend can
# fall back to the Feather's BLE advertisement when HTTP is unreachable
# (e.g. on an Android hotspot with client isolation). The fallback is
# strictly optional - if this step fails the backend still runs HTTP-only.
echo "[3/7] Configuring BLE fallback..."
sudo pip install --break-system-packages --quiet bleak || \
    echo "  (bleak install failed - BLE fallback will be disabled at runtime)"

if rfkill list bluetooth 2>/dev/null | grep -q Bluetooth; then
    sudo rfkill unblock bluetooth
    # Make bluetoothd auto-power hci0 on boot so bleak finds a powered adapter
    if [ -f /etc/bluetooth/main.conf ]; then
        sudo sed -i 's/^#AutoEnable=true/AutoEnable=true/' /etc/bluetooth/main.conf
    fi
    sudo systemctl enable --now bluetooth.service 2>/dev/null || true
    echo "  Bluetooth adapter unblocked and AutoEnable=true"
else
    echo "  (no Bluetooth hardware detected - BLE fallback disabled)"
fi

# --- Set hostname to fermentoscope ------------------------------------------
echo "[4/7] Setting hostname to 'fermentoscope'..."
CURRENT_HOST="$(hostname)"
if [ "$CURRENT_HOST" != "fermentoscope" ]; then
    sudo hostnamectl set-hostname fermentoscope
    sudo sed -i "s/127\.0\.1\.1\s\+${CURRENT_HOST}/127.0.1.1\tfermentoscope/" /etc/hosts || true
    if ! grep -q '127.0.1.1' /etc/hosts; then
        echo "127.0.1.1 fermentoscope" | sudo tee -a /etc/hosts >/dev/null
    fi
fi
sudo systemctl enable --now avahi-daemon

# --- Clone or update repo ---------------------------------------------------
echo "[5/7] Fetching project files..."
if [ -d "$INSTALL_DIR" ]; then
    (cd "$INSTALL_DIR" && git pull --quiet)
else
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi

# --- Create systemd service -------------------------------------------------
echo "[6/7] Installing systemd service..."
sudo tee /etc/systemd/system/fermentoscope.service >/dev/null <<EOF
[Unit]
Description=Fermentoscope sourdough monitor (base)
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=FERMENTOSCOPE_ESP32_URL=${ESP32_URL}
Environment=FERMENTOSCOPE_DB=/home/pi/fermentoscope.db
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/pi/fermentoscope_server.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now fermentoscope.service

# --- Wait for service to come up --------------------------------------------
echo "[7/7] Starting service..."
sleep 4

if systemctl is-active --quiet fermentoscope.service; then
    STATUS="OK"
else
    STATUS="NOT RUNNING - check: sudo journalctl -u fermentoscope.service"
fi

# --- Print summary ----------------------------------------------------------
IP_ADDR="$(hostname -I | awk '{print $1}')"
cat <<EOF

================================================
 Fermentoscope is installed!

 Service:  fermentoscope.service ($STATUS)
 Web UI:   https://fermentoscope.local/
           https://${IP_ADDR}/
 ESP32:    ${ESP32_URL}

 Note: the HTTPS certificate is self-signed.
 Your browser will show a warning - accept it.

 Useful commands:
   sudo systemctl status fermentoscope
   sudo journalctl -u fermentoscope -f
   sudo systemctl restart fermentoscope
================================================
EOF

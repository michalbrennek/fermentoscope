#!/usr/bin/env bash
#
# Fermentoscope - Raspberry Pi setup (LCD variant)
#
# Target: fresh Raspberry Pi OS Lite on a Raspberry Pi Zero 2 W with the
# Waveshare Zero-to-3B adapter board and the Waveshare 3.5" RPi LCD (A)
# (SKU 9904, ILI9486 + XPT2046 touch).
#
# This script configures the LCD and touchscreen via device-tree overlays
# (no third-party kernel drivers needed), installs the Fermentoscope
# service, and runs the LCD display + web UI together.
#
# Usage (run as user pi, from a fresh install with internet access):
#   curl -fsSL https://raw.githubusercontent.com/michalbrennek/fermentoscope/main/pi/setup_lcd.sh | bash
# Or:
#   git clone https://github.com/michalbrennek/fermentoscope.git
#   cd fermentoscope/pi && bash setup_lcd.sh
#
set -euo pipefail

REPO_URL="https://github.com/michalbrennek/fermentoscope.git"
INSTALL_DIR="/home/pi/fermentoscope"
PI_USER="pi"
BOOT_CONFIG="/boot/firmware/config.txt"
BOOT_CMDLINE="/boot/firmware/cmdline.txt"

echo "==============================================="
echo " Fermentoscope LCD Setup"
echo " Waveshare 3.5\" RPi LCD (A) SKU 9904"
echo "==============================================="

if [ "$(id -un)" != "$PI_USER" ]; then
    echo "Please run as user '$PI_USER'."
    exit 1
fi

read -rp "ESP32 sensor URL [http://sourdough.local:8080/]: " ESP32_URL
ESP32_URL="${ESP32_URL:-http://sourdough.local:8080/}"

# --- System update & packages ------------------------------------------------
echo ""
echo "[1/8] Updating package lists..."
sudo apt-get update -qq

echo "[2/8] Installing system packages..."
sudo apt-get install -y --no-install-recommends \
    git python3 python3-pil python3-evdev python3-requests \
    avahi-daemon avahi-utils libnss-mdns \
    fonts-terminus libts-bin evtest \
    openssl ca-certificates

# --- Configure LCD overlays --------------------------------------------------
echo "[3/8] Configuring Waveshare 3.5\" LCD (ILI9486 + XPT2046 touch)..."

# Use /boot/firmware/config.txt on Bookworm, fallback to /boot/config.txt
if [ ! -f "$BOOT_CONFIG" ]; then
    BOOT_CONFIG="/boot/config.txt"
    BOOT_CMDLINE="/boot/cmdline.txt"
fi

if ! grep -q "Fermentoscope LCD" "$BOOT_CONFIG"; then
    sudo tee -a "$BOOT_CONFIG" >/dev/null <<'EOF'

# Fermentoscope LCD - Waveshare 3.5" RPi LCD (A) SKU 9904
dtparam=spi=on
dtoverlay=fbtft,spi0-0,piscreen,dc_pin=24,reset_pin=25,speed=16000000,rotate=90,fps=30
dtoverlay=ads7846,cs=1,penirq=17,penirq_pull=2,speed=1000000,xohms=150,swapxy=1
EOF
fi

# Silence kernel console on the framebuffer
if ! grep -q "fbcon=map:99" "$BOOT_CMDLINE"; then
    sudo sed -i 's/\(rootwait\)/\1 loglevel=0 fbcon=map:99 vt.global_cursor_default=0/' "$BOOT_CMDLINE"
fi

# Disable tty getty on the framebuffer console
sudo systemctl disable getty@tty1.service 2>/dev/null || true
echo 'kernel.printk = 0 4 1 3' | sudo tee /etc/sysctl.d/99-fermentoscope-silence.conf >/dev/null

# --- Set hostname ------------------------------------------------------------
echo "[4/8] Setting hostname to 'fermentoscope'..."
CURRENT_HOST="$(hostname)"
if [ "$CURRENT_HOST" != "fermentoscope" ]; then
    sudo hostnamectl set-hostname fermentoscope
    sudo sed -i "s/127\.0\.1\.1\s\+${CURRENT_HOST}/127.0.1.1\tfermentoscope/" /etc/hosts || true
    if ! grep -q '127.0.1.1' /etc/hosts; then
        echo "127.0.1.1 fermentoscope" | sudo tee -a /etc/hosts >/dev/null
    fi
fi
sudo systemctl enable --now avahi-daemon

# --- Fetch project -----------------------------------------------------------
echo "[5/8] Fetching project files..."
if [ -d "$INSTALL_DIR" ]; then
    (cd "$INSTALL_DIR" && git pull --quiet)
else
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi

# --- Touchscreen calibration (default values from Waveshare docs) -----------
echo "[6/8] Installing default touchscreen calibration..."
if [ ! -f /etc/pointercal ]; then
    echo '-8417 49 33293492 45 5631 -1385986 65536 480 320 0' | sudo tee /etc/pointercal >/dev/null
fi

# --- Create systemd service --------------------------------------------------
echo "[7/8] Installing systemd service..."
sudo tee /etc/systemd/system/fermentoscope.service >/dev/null <<EOF
[Unit]
Description=Fermentoscope sourdough monitor (LCD variant)
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=FERMENTOSCOPE_ESP32_URL=${ESP32_URL}
Environment=FERMENTOSCOPE_DB=/home/pi/fermentoscope.db
Environment=PYTHONPATH=${INSTALL_DIR}/pi
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/pi/fermentoscope_lcd.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fermentoscope.service

# --- Done --------------------------------------------------------------------
echo "[8/8] Setup complete."

cat <<EOF

================================================
 Fermentoscope (LCD variant) installed.

 IMPORTANT: a reboot is required for the LCD
 overlays to take effect.

 After reboot, the LCD will start automatically
 and the web UI will be available at:

   https://fermentoscope.local/
   https://$(hostname -I | awk '{print $1}')/

 ESP32: ${ESP32_URL}

 To reboot now:
   sudo reboot
================================================
EOF

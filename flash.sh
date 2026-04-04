#!/usr/bin/env bash
set -euo pipefail

# Fermentoscope - Flash setup script
# Flashes CircuitPython + sensor code to Adafruit ESP32 Feather V2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FEATHER_DIR="$SCRIPT_DIR/feather"
CP_VERSION="10.1.4"
CP_URL="https://downloads.circuitpython.org/bin/adafruit_feather_esp32_v2/en_GB/adafruit-circuitpython-adafruit_feather_esp32_v2-en_GB-${CP_VERSION}.bin"
CP_BIN="/tmp/circuitpython-feather.bin"

echo "==================================="
echo " Fermentoscope Setup"
echo "==================================="
echo ""

# Collect WiFi credentials
read -rp "WiFi SSID: " WIFI_SSID
read -rsp "WiFi Password: " WIFI_PASS
echo ""
read -rp "mDNS hostname [sourdough]: " HOSTNAME
HOSTNAME="${HOSTNAME:-sourdough}"
read -rp "HTTP port [8080]: " PORT
PORT="${PORT:-8080}"
echo ""

# Detect or ask for serial port
if command -v esptool.py &>/dev/null; then
    ESPTOOL="esptool.py"
elif command -v esptool &>/dev/null; then
    ESPTOOL="esptool"
else
    echo "esptool not found. Install with: pip install esptool"
    exit 1
fi

# Auto-detect port
DETECTED=""
for p in /dev/ttyUSB* /dev/ttyACM* /dev/tty.usbserial* /dev/tty.usbmodem*; do
    [ -e "$p" ] && DETECTED="$p" && break
done

if [ -n "$DETECTED" ]; then
    read -rp "Serial port [$DETECTED]: " COM_PORT
    COM_PORT="${COM_PORT:-$DETECTED}"
else
    read -rp "Serial port (e.g. /dev/ttyUSB0 or COM7): " COM_PORT
fi

echo ""
echo "Configuration:"
echo "  SSID:     $WIFI_SSID"
echo "  Hostname: ${HOSTNAME}.local"
echo "  Port:     $COM_PORT"
echo ""

# Step 1: Flash CircuitPython
read -rp "Flash CircuitPython firmware? (y/N): " FLASH_CP
if [[ "$FLASH_CP" =~ ^[Yy] ]]; then
    if [ ! -f "$CP_BIN" ]; then
        echo "Downloading CircuitPython ${CP_VERSION}..."
        curl -L -o "$CP_BIN" "$CP_URL"
    fi
    echo "Erasing flash..."
    $ESPTOOL --chip esp32 --port "$COM_PORT" --baud 115200 erase_flash
    echo "Flashing CircuitPython..."
    $ESPTOOL --chip esp32 --port "$COM_PORT" --baud 115200 write_flash -z 0x0 "$CP_BIN"
    echo "Waiting for board to reboot..."
    sleep 10
fi

# Step 2: Deploy code via serial REPL
echo ""
echo "Deploying Fermentoscope code..."

python3 - "$COM_PORT" "$WIFI_SSID" "$WIFI_PASS" "$HOSTNAME" "$PORT" "$FEATHER_DIR" << 'PYTHON_DEPLOY'
import sys, serial, time, os

com_port = sys.argv[1]
ssid = sys.argv[2]
password = sys.argv[3]
hostname = sys.argv[4]
port = sys.argv[5]
feather_dir = sys.argv[6]

def connect():
    p = serial.Serial(com_port, 115200, timeout=5)
    time.sleep(0.5)
    for _ in range(5):
        p.write(b'\x03')
        time.sleep(0.3)
    p.write(b' ')
    time.sleep(2)
    p.write(b'\r\n')
    time.sleep(1)
    p.read(p.in_waiting)
    return p

def cmd(p, c, wait=0.5):
    p.write((c + '\r\n').encode())
    time.sleep(wait)
    return p.read(p.in_waiting).decode(errors='replace')

def deploy_file(p, local_path, remote_path):
    with open(local_path) as f:
        content = f.read()
    hexdata = content.encode().hex()
    cmd(p, 'h=""')
    for i in range(0, len(hexdata), 200):
        cmd(p, f'h+="{hexdata[i:i+200]}"', 0.15)
    r = cmd(p, f"ff=open('{remote_path}','w');ff.write(bytes.fromhex(h).decode());ff.close();print('OK',len(h)//2)", 2)
    if 'OK' in r:
        size = content.encode().__len__()
        print(f"  {remote_path} ({size} bytes)")
    else:
        print(f"  FAILED: {remote_path}")
        print(r)

port_obj = connect()

# Verify REPL
port_obj.write(b'print(777)\r\n')
time.sleep(1)
r = port_obj.read(port_obj.in_waiting).decode(errors='replace')
if '777' not in r:
    print("ERROR: Cannot reach CircuitPython REPL")
    print("Try pressing RESET on the board and run again.")
    sys.exit(1)

print("Connected to CircuitPython REPL")

# Disable auto-reload during deployment
cmd(port_obj, 'import supervisor; supervisor.runtime.autoreload = False')

# Create lib directory
cmd(port_obj, "import os")
cmd(port_obj, "try: os.mkdir('/lib')\nexcept: pass", 0.5)

# Deploy settings.toml (with credentials)
settings = f'CIRCUITPY_WIFI_SSID = "{ssid}"\n'
settings += f'CIRCUITPY_WIFI_PASSWORD = "{password}"\n'
settings += f'CIRCUITPY_WEB_API_PASSWORD = "fermentoscope"\n'
settings += f'FERMENTOSCOPE_HOSTNAME = "{hostname}"\n'
settings += f'FERMENTOSCOPE_PORT = "{port}"\n'

hexdata = settings.encode().hex()
cmd(port_obj, 'h=""')
for i in range(0, len(hexdata), 200):
    cmd(port_obj, f'h+="{hexdata[i:i+200]}"', 0.15)
cmd(port_obj, "ff=open('/settings.toml','w');ff.write(bytes.fromhex(h).decode());ff.close();print('OK')", 2)
print("  /settings.toml (credentials)")

# Deploy code files
deploy_file(port_obj, os.path.join(feather_dir, 'lib', 'scd4x.py'), '/lib/scd4x.py')
deploy_file(port_obj, os.path.join(feather_dir, 'lib', 'vl53l4cx.py'), '/lib/vl53l4cx.py')
deploy_file(port_obj, os.path.join(feather_dir, 'code.py'), '/code.py')

print("\nDeploy complete. Resetting board...")
port_obj.write(b'\x04')
time.sleep(15)
output = port_obj.read(port_obj.in_waiting).decode(errors='replace')

if hostname in output or 'mDNS' in output:
    print(f"Board is running! Access at http://{hostname}.local:{port}/")
else:
    print("Board output:")
    print(output[-300:])

port_obj.close()
PYTHON_DEPLOY

echo ""
echo "==================================="
echo " Fermentoscope ready!"
echo ""
echo " Sensor endpoint: http://${HOSTNAME}.local:${PORT}/"
echo " Reset the board to recalibrate the distance sensor."
echo "==================================="

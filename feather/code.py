"""Fermentoscope - Sourdough fermentation monitor.

Reads CO2, temperature, humidity (SCD41) and dough rise height (VL53L4CX)
from sensors connected via I2C/STEMMA QT. Serves live JSON data over HTTP
on port 8080 and registers as sourdough.local via mDNS.

On boot/reset the ToF distance sensor calibrates against the jar bottom.
"""

import board, busio, wifi, socketpool, json, time, analogio, mdns, os, supervisor, struct
from scd4x import SCD4X
from vl53l4cx import VL53L4CX

# Optional BLE fallback - broadcast sensor data as manufacturer data in BLE
# advertisements so devices can still read it when WiFi/mDNS is unavailable
# (e.g. on Android hotspots with client isolation).
try:
    import _bleio
    HAS_BLE = True
except ImportError:
    HAS_BLE = False

BLE_COMPANY_ID = 0xFFFF  # test/unassigned company ID

# WiFi credentials from settings.toml
SSID = os.getenv("CIRCUITPY_WIFI_SSID")
PASS = os.getenv("CIRCUITPY_WIFI_PASSWORD")
HOSTNAME = os.getenv("FERMENTOSCOPE_HOSTNAME", "sourdough")
PORT = int(os.getenv("FERMENTOSCOPE_PORT", "8080"))

wifi.radio.connect(SSID, PASS)
IP = str(wifi.radio.ipv4_address)
print(IP)


def pack_ble_payload(d):
    """Pack sensor readings into a compact binary payload for BLE adv.
    Format (little-endian, 16 bytes total):
        H  co2 (ppm, 0..65535)
        h  temp * 100 (int16, -327.68..327.67 C)
        B  humidity (0..100 %)
        H  dist (mm, 0..65535)
        H  rise (mm, 0..65535)
        H  baseline (mm, 0..65535)
        B  vbat * 50 (3.0..4.2V mapped to 0..60)
        I  uptime (seconds)
    """
    vbat_byte = max(0, min(255, int((d.get("vbat", 0) - 3.0) * 50)))
    return struct.pack("<HhBHHHBI",
                       min(65535, int(d.get("co2", 0))),
                       int(d.get("temp", 0) * 100),
                       min(255, int(d.get("hum", 0))),
                       min(65535, int(d.get("dist", 0))),
                       min(65535, int(d.get("rise", 0))),
                       min(65535, int(d.get("base", 0))),
                       vbat_byte,
                       int(d.get("uptime", 0)) & 0xFFFFFFFF)


def build_ble_adv(payload):
    """Build a raw BLE advertisement with flags + manufacturer data."""
    # AD structure 1: Flags (LE general discoverable, BR/EDR not supported)
    flags = bytes([0x02, 0x01, 0x06])
    # AD structure 2: Manufacturer specific data (0xFF) with company ID + payload
    mfr_data = struct.pack("<H", BLE_COMPANY_ID) + payload
    mfr = bytes([len(mfr_data) + 1, 0xFF]) + mfr_data
    return flags + mfr


ble_adapter = None
if HAS_BLE:
    try:
        ble_adapter = _bleio.adapter
        ble_adapter.enabled = True
        ble_adapter.name = HOSTNAME
        print(f"BLE: {ble_adapter.address}")
    except Exception as e:
        print(f"BLE init failed: {e}")
        ble_adapter = None

md = mdns.Server(wifi.radio)
md.hostname = HOSTNAME
print(f"mDNS: {HOSTNAME}.local")

i2c = busio.I2C(board.SCL, board.SDA, frequency=100000)
while not i2c.try_lock():
    pass

scd = SCD4X(i2c)
scd.stop()
time.sleep(1)
scd.start()

tof = VL53L4CX(i2c)
baseline = tof.calibrate()
print(f"Baseline: {baseline}mm")
tof.start()

bat_pin = analogio.AnalogIn(board.VOLTAGE_MONITOR)

data = {
    "co2": 0,
    "temp": 0.0,
    "hum": 0.0,
    "dist": 0,
    "rise": 0.0,
    "base": baseline,
    "vbat": 0.0,
    "usb": False,
    "uptime": 0,
    "ip": IP,
    "host": HOSTNAME,
}

pool = socketpool.SocketPool(wifi.radio)
srv = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
srv.setsockopt(pool.SOL_SOCKET, pool.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(2)
srv.settimeout(1)
print(f"HTTP :{PORT} ready")

CRLF = chr(13) + chr(10)
lr = 0

while True:
    n = time.monotonic()

    if not wifi.radio.connected:
        try:
            wifi.radio.connect(SSID, PASS)
            print("WiFi reconnected")
        except Exception:
            pass

    if n - lr > 5:
        lr = n
        try:
            if scd.data_ready():
                c, t, h = scd.read()
                data["co2"] = c
                data["temp"] = round(t, 1)
                data["hum"] = round(h, 1)
            if tof.data_ready():
                d = tof.read()
                data["dist"] = d
                data["rise"] = max(0, baseline - d)
            data["vbat"] = round(bat_pin.value * 3.3 / 65535 * 2, 2)
            data["usb"] = supervisor.runtime.usb_connected
            data["uptime"] = int(n)

            # Refresh BLE advertisement with latest sensor values
            if ble_adapter:
                try:
                    adv = build_ble_adv(pack_ble_payload(data))
                    if ble_adapter.advertising:
                        ble_adapter.stop_advertising()
                    ble_adapter.start_advertising(adv, interval=1.0)
                except Exception as ble_err:
                    print(f"BLE adv err: {ble_err}")
        except Exception as e:
            print(e)

    try:
        conn, addr = srv.accept()
        buf = bytearray(256)
        conn.recv_into(buf)
        body = json.dumps(data)
        hd = "HTTP/1.1 200 OK" + CRLF + "Content-Type: application/json" + CRLF + CRLF
        conn.send(hd.encode())
        conn.send(body.encode())
        conn.close()
    except OSError:
        pass

"""Fermentoscope - Sourdough fermentation monitor.

Reads CO2, temperature, humidity (SCD41) and dough rise height (VL53L4CX)
from sensors connected via I2C/STEMMA QT. Serves live JSON data over HTTP
on port 8080 and registers as sourdough.local via mDNS.

On boot/reset the ToF distance sensor calibrates against the jar bottom.
"""

import board, busio, wifi, socketpool, json, time, analogio, mdns, os
from scd4x import SCD4X
from vl53l4cx import VL53L4CX

# WiFi credentials from settings.toml
SSID = os.getenv("CIRCUITPY_WIFI_SSID")
PASS = os.getenv("CIRCUITPY_WIFI_PASSWORD")
HOSTNAME = os.getenv("FERMENTOSCOPE_HOSTNAME", "sourdough")
PORT = int(os.getenv("FERMENTOSCOPE_PORT", "8080"))

wifi.radio.connect(SSID, PASS)
print(str(wifi.radio.ipv4_address))

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

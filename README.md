# Fermentoscope

A real-time sourdough fermentation monitor that tracks CO2, temperature, humidity, and dough rise height using an ESP32 microcontroller and environmental sensors.

## What it does

The Fermentoscope sits on top of your fermentation jar and continuously measures:

- **CO2 concentration** (ppm) — direct indicator of yeast activity
- **Temperature** (°C) — affects fermentation speed
- **Relative humidity** (%)
- **Dough rise height** (mm) — tracks how much the dough has risen from baseline

## Architecture

```
  ┌─────────────────┐     WiFi       ┌─────────────────────┐
  │   ESP32 Feather │ ─────────────→ │   Raspberry Pi      │
  │  + SCD41 + ToF  │  HTTP + mDNS   │  fermentoscope.local│
  │ sourdough.local │                │  - SQLite log       │
  └─────────────────┘                │  - HTTPS web UI     │
                                     │  - (optional) LCD   │
                                     └──────────┬──────────┘
                                                │ HTTPS
                                                ▼
                                     Phone / laptop / tablet
```

- **ESP32 Feather V2** runs CircuitPython, reads the sensors, and serves live JSON at `http://sourdough.local:8080/`.
- **Raspberry Pi Zero 2 W** (or any Pi) polls the ESP32, stores readings in SQLite, and hosts a lightweight HTTPS web UI reachable at `https://fermentoscope.local/`.
- The Pi has two variants: **base** (headless, web-only) and **LCD** (adds a Waveshare 3.5" touchscreen display).

### Detecting recalibration events

When you add flour to the starter, the dough level changes. Pressing **RESET** on the ESP32 recalibrates the baseline distance. The Pi detects this (via ESP32 uptime reset or baseline change) and asks — on the web UI and/or LCD — whether this is a **new start** (cumulative rise resets to 0) or you're **adding flour** (previous rise is preserved in the cumulative total).

## Hardware

### Controller

| Component | Part | Notes |
|-----------|------|-------|
| Microcontroller | [Adafruit ESP32 Feather V2](https://www.adafruit.com/product/5400) | WiFi, I2C, STEMMA QT, battery charging |
| Battery | Akyga Li-Po 3.7V 2200mAh (AKY0393) | JST-PH connector, soldered Feather-compatible leads |

### Sensors

| Sensor | Part | Measures | Interface |
|--------|------|----------|-----------|
| [SparkFun CO2 Humidity and Temperature Sensor - SCD41 (Qwiic)](https://www.sparkfun.com/sparkfun-co2-humidity-and-temperature-sensor-scd41-qwiic.html) | SPF-23483 | CO2, temperature, humidity | I2C 0x62 |
| [Adafruit VL53L4CX Time of Flight Distance Sensor](https://www.adafruit.com/product/5425) | ADA-5425 | Distance (dough rise) | I2C 0x29 |

### Wiring

All sensors connect via STEMMA QT / Qwiic cables — no soldering required. Daisy-chain from the Feather's STEMMA QT port through both sensors.

## Software

Runs [CircuitPython](https://circuitpython.org/) 10.x on the ESP32 Feather V2. Includes minimal, dependency-free drivers for both sensors.

### Firmware structure

```
feather/
├── code.py              # Main application
└── lib/
    ├── scd4x.py         # SCD41 CO2/temp/humidity driver
    └── vl53l4cx.py      # VL53L4CX time-of-flight distance driver
```

## Feather controls and indicators

### Buttons

The ESP32 Feather V2 has two small tactile buttons on the top edge:

| Button | Label | Function |
|--------|-------|----------|
| **RESET** | `RST` | Reboots the ESP32. Press this to recalibrate the distance sensor (the baseline is re-measured on every boot). |
| **BOOT** | `BOOT` / `USER` | Used to enter the ROM bootloader for flashing. Normally unused in operation. To enter bootloader mode: hold `BOOT`, press and release `RST`, then release `BOOT`. |

### Turning on and off

The Feather V2 has **no dedicated power switch**. To turn it on/off:

- **Turn ON**: connect USB-C power or plug in the LiPo battery. The board boots automatically.
- **Turn OFF**: unplug USB-C **and** disconnect the battery. Pressing `RST` only reboots — it does not power the board off.
- Optional: splice a small slide switch into one of the battery leads if you want a hardware on/off.

### LEDs

The Feather has three user-visible LEDs:

| LED | Location | Meaning |
|-----|----------|---------|
| **Orange CHG** | Right of USB-C | Battery charge status from the BQ24074 charger chip |
| **Red D13** | Near GPIO13 | User LED (currently unused by Fermentoscope code) |
| **NeoPixel** | RGB LED near the USB-C | CircuitPython status and optional user indication |

#### Orange CHG LED (battery charging)

| Pattern | Meaning |
|---------|---------|
| **Off** | Not charging — no USB power, or battery already full |
| **Solid on** | Actively charging the LiPo |
| **Slow blink (~1 Hz)** | Fault — battery too hot/cold, cell damaged, timeout, or deeply discharged. Unplug USB for a few seconds and try again. A deeply discharged LiPo may trickle-charge (blinking) for a while before switching to solid-on fast charge. |

#### NeoPixel (CircuitPython status)

| Pattern | Meaning |
|---------|---------|
| **Green** | Boot successful, `code.py` running |
| **Fading white / rainbow** | Startup / REPL waiting |
| **Red flashes** | Python exception — count the flashes to identify the error: 1 = generic, 2 = syntax error, 3 = IndentationError, etc. Check the serial console for the traceback |
| **Yellow** | Safe mode — CircuitPython booted without running `code.py`. Usually after a crash. Reset with USB to recover |
| **Yellow flashes** | Safe mode with error code — count of flashes indicates the reason (brown-out, hard crash, watchdog, etc.) |
| **Off** | Deep sleep, or NeoPixel turned off by code |

## Raspberry Pi setup

The Pi polls the ESP32, logs everything to SQLite, and hosts the HTTPS web UI. Two one-shot setup scripts are provided — pick one depending on whether you have the Waveshare LCD.

### Base variant — headless, web UI only

Fresh **Raspberry Pi OS Lite** install (tested on a **Pi Zero 2 W**), default user `pi`. After first boot and internet access:

```bash
curl -fsSL https://raw.githubusercontent.com/michalbrennek/fermentoscope/main/pi/setup_base.sh | bash
```

The script will:
1. Install Python, Pillow, avahi-daemon, openssl
2. Set the hostname to `fermentoscope` (so mDNS publishes `fermentoscope.local`)
3. Clone this repo into `/home/pi/fermentoscope`
4. Generate a self-signed TLS certificate
5. Install and enable the `fermentoscope.service` systemd unit

When it finishes, open `https://fermentoscope.local/` from any device on the same WiFi. The browser will warn about the self-signed certificate — accept it.

### LCD variant — Waveshare 3.5" touchscreen

Additional hardware (all soldered / plug-and-play, no modifications):

| Component | Part |
|-----------|------|
| Adapter | [Waveshare Zero-to-3B adapter board](https://www.waveshare.com/zero-to-3b-adapter.htm) — mounts the Pi Zero 2 W in a Pi 3 Model B footprint so that 40-pin GPIO HATs fit |
| Display | [Waveshare 3.5" RPi LCD (A), SKU 9904](https://www.waveshare.com/3.5inch-rpi-lcd-a.htm) — ILI9486 display + XPT2046 resistive touch, plugs onto the 40-pin header |

From a fresh **Raspberry Pi OS Lite** install:

```bash
curl -fsSL https://raw.githubusercontent.com/michalbrennek/fermentoscope/main/pi/setup_lcd.sh | bash
sudo reboot
```

The script does everything the base variant does, plus:
1. Enables SPI and configures the `fbtft/piscreen` and `ads7846` device-tree overlays in `/boot/firmware/config.txt`
2. Silences kernel console output on the framebuffer
3. Installs a default touchscreen calibration (`/etc/pointercal`)
4. Runs the LCD display + web UI together

A reboot is required the first time so the LCD overlays take effect.

On the LCD you get:
- **Values view** — CO2, Temp, RH, Rise at the top, combined 4-parameter plot at the bottom
- **Touch a value** → full-screen detail plot with a large back button
- **Touch the back button** → returns to values view
- **Calibration dialog** — full-screen when the ESP32 baseline changes, with "NEW START" and "ADDING FLOUR" buttons

### Using the web UI

Whichever variant you install, the web UI is always available at `https://fermentoscope.local/`:

- Top row: four big colored sensor tiles — CO2 (red), Temp (yellow), RH (blue), Rise (green)
- Below: combined plot of all four parameters over the last 24 h
- **Click a tile** → single big plot for that parameter with a back button
- **Click the combined plot** → 2×2 grid of large individual plots with a back button
- When the ESP32 is recalibrated a modal appears asking "NEW START" or "ADDING FLOUR"

### Changing where the ESP32 lives

By default the Pi polls `http://sourdough.local:8080/`. Both setup scripts prompt for this on install. You can change it later by editing the `Environment=FERMENTOSCOPE_ESP32_URL=...` line in `/etc/systemd/system/fermentoscope.service` and running:

```bash
sudo systemctl daemon-reload && sudo systemctl restart fermentoscope
```

## Setup — flashing the Feather

### Prerequisites

- Python 3 with `pyserial` and `esptool`: `pip install pyserial esptool`
- USB cable connected to the Feather

### Flash and configure

```bash
./flash.sh
```

The script will:
1. Ask for your WiFi SSID and password
2. Optionally flash CircuitPython firmware
3. Deploy the sensor code and drivers
4. Configure mDNS hostname

### Recalibrate distance sensor

Press the **RESET** button on the Feather. On boot, it takes 5 distance readings and averages them as the baseline. Point the sensor at the jar bottom before resetting.

## API

Once running, the Feather serves JSON at `http://sourdough.local:8080/`:

```json
{
  "co2": 1250,
  "temp": 24.3,
  "hum": 68.2,
  "dist": 142,
  "rise": 33,
  "base": 175,
  "vbat": 3.92,
  "usb": true,
  "uptime": 3600
}
```

| Field | Unit | Description |
|-------|------|-------------|
| `co2` | ppm | CO2 concentration (0–40000) |
| `temp` | °C | Ambient temperature |
| `hum` | % | Relative humidity |
| `dist` | mm | Current distance to dough surface |
| `rise` | mm | How much dough has risen from baseline (`base - dist`) |
| `base` | mm | Baseline distance (calibrated on reset) |
| `vbat` | V | Battery voltage (3.2–4.2V) |
| `usb` | bool | USB data cable connected (only true when plugged into a computer, not a wall charger) |
| `uptime` | s | Seconds since last boot/reset — use this to detect a recalibration event |

## BLE fallback

The Feather also broadcasts the latest sensor reading as a BLE advertisement, independently of the WiFi path. This exists because WiFi + mDNS isn't always reachable — Android hotspots in particular enable **client isolation** by default, which lets the ESP32 join the hotspot but blocks HTTP between devices on the same SSID. BLE sidesteps the AP entirely: any phone, laptop, or other Pi within range can read the sensor values directly.

The advertisement uses standard BLE manufacturer-specific data. The hostname from `FERMENTOSCOPE_HOSTNAME` (default `sourdough`) is broadcast as the Complete Local Name in a **scan response** packet (the 31 B adv packet itself is full of Flags + Manufacturer Data), so any *active* BLE scanner — bleak, nRF Connect, system Bluetooth — sees the name via the normal `SCAN_REQ`/`SCAN_RSP` flow. The sensor payload sits in a Manufacturer Specific AD with company ID `0xFFFF` (Bluetooth SIG's reserved "for testing" value). Name-based matching is the recommended way to filter; `tools/ble_scan.py` and the Pi backend also fall back to `company_id == 0xFFFF AND len(payload) == 16` for passive scans or scanners that haven't merged the scan response yet.

### Payload layout (16 bytes, little-endian)

| Offset | Type | Field | Meaning |
|-------:|------|-------|---------|
| 0 | `uint16` | `co2` | CO2 concentration in ppm |
| 2 | `int16`  | `temp × 100` | Temperature in hundredths of °C (so 24.30 → `2430`) |
| 4 | `uint8`  | `hum` | Relative humidity in % (0–100) |
| 5 | `uint16` | `dist` | Current distance to dough surface in mm |
| 7 | `uint16` | `rise` | Dough rise from baseline in mm |
| 9 | `uint16` | `base` | Calibrated baseline distance in mm |
| 11 | `uint8` | `vbat × 50` | Battery voltage: `3.0 V + vbat_byte / 50` (3.0–4.2 V → 0–60) |
| 12 | `uint32` | `uptime` | Seconds since the last ESP32 reset |

Advertisements are refreshed every ~5 seconds with the latest values.

### Reference scanner

A standalone Python scanner is provided at [`tools/ble_scan.py`](tools/ble_scan.py). It uses [`bleak`](https://github.com/hbldh/bleak) (cross-platform BLE) and matches the payload format byte-for-byte with the Feather firmware:

```bash
pip install bleak
python3 tools/ble_scan.py            # scan continuously
python3 tools/ble_scan.py --once     # exit after first decode
python3 tools/ble_scan.py --name mydough   # custom hostname
```

Example output:

```
[21:14:52] 00:4B:12:BE:B7:F8 rssi=-48dBm  co2=1891ppm temp=27.5°C hum=39% rise=0mm base=29mm vbat=4.22V uptime=2740s
```

### Pi backend uses it automatically

When the Pi backend can't reach the ESP32 over HTTP (e.g. the hotspot-with-client-isolation case), it falls back to the BLE cache maintained by a background scanner in `pi/fermentoscope_server.py`. The fallback is optional — install `bleak` on the Pi to enable it:

```bash
pip install bleak
sudo systemctl restart fermentoscope
```

If `bleak` isn't installed the Pi runs exactly as before (HTTP-only); nothing breaks.

## Acknowledgments

### Hardware
- [Adafruit](https://www.adafruit.com/) — ESP32 Feather V2, VL53L4CX breakout, CircuitPython, STEMMA QT ecosystem
- [SparkFun](https://www.sparkfun.com/) — SCD41 Qwiic breakout
- [Sensirion](https://sensirion.com/) — SCD4x sensor and [datasheet](https://sensirion.com/media/documents/48C4B7FB/67FE0194/CD_DS_SCD4x_Datasheet_D1.pdf)
- [STMicroelectronics](https://www.st.com/) — VL53L4CX sensor and register documentation

### Software
- [CircuitPython](https://circuitpython.org/) — Python runtime for microcontrollers
- [Adafruit CircuitPython VL53L1X](https://github.com/adafruit/Adafruit_CircuitPython_VL53L1X) (MIT) — the VL53L4CX driver's initialization sequence is derived from this library by Carter Nelson / Adafruit Industries, which is itself based on ST's VL53L1X Ultra Lite Driver
- [Adafruit CircuitPython SCD4X](https://github.com/adafruit/Adafruit_CircuitPython_SCD4X) (MIT) — the SCD41 driver is inspired by this library by ladyada / Adafruit Industries

## About the drivers

The sensor drivers in `feather/lib/` are minimal, dependency-free rewrites designed to run on the ESP32's limited memory without needing the Adafruit library bundle.

**`vl53l4cx.py`** — The 91-byte initialization sequence is from [Adafruit's CircuitPython VL53L1X library](https://github.com/adafruit/Adafruit_CircuitPython_VL53L1X) (MIT, Carter Nelson / Adafruit Industries), originally ported from ST's VL53L1X Ultra Lite Driver. Changes from the original:
- Adapted for VL53L4CX (model ID 0xEB instead of 0xEA)
- Inverted interrupt polarity on `data_ready()` to match VL53L4CX behavior
- Uses raw I2C (`writeto_then_readfrom`) instead of Adafruit's `bus_device` abstraction
- Added `calibrate()` for baseline distance measurement and `rise()` for tracking dough height

**`scd4x.py`** — I2C command codes and temperature/humidity conversion formulas are from the [Sensirion SCD4x datasheet](https://sensirion.com/media/documents/48C4B7FB/67FE0194/CD_DS_SCD4x_Datasheet_D1.pdf). The code structure is inspired by [Adafruit's CircuitPython SCD4X library](https://github.com/adafruit/Adafruit_CircuitPython_SCD4X) (MIT, ladyada / Adafruit Industries), rewritten as a single-file driver without `adafruit_bus_device` and `adafruit_register` dependencies.

## License

MIT

# Fermentoscope

A real-time sourdough fermentation monitor that tracks CO2, temperature, humidity, and dough rise height using an ESP32 microcontroller and environmental sensors.

## What it does

The Fermentoscope sits on top of your fermentation jar and continuously measures:

- **CO2 concentration** (ppm) — direct indicator of yeast activity
- **Temperature** (°C) — affects fermentation speed
- **Relative humidity** (%)
- **Dough rise height** (mm) — tracks how much the dough has risen from baseline

Data is served as JSON over HTTP on your local network via mDNS (`sourdough.local`).

## Hardware

### Controller

| Component | Part | Notes |
|-----------|------|-------|
| Microcontroller | [Adafruit ESP32 Feather V2](https://www.adafruit.com/product/5400) | WiFi, I2C, STEMMA QT, battery charging |
| Battery | Akyga Li-Po 3.7V 2200mAh (AKY0393) | JST-PH connector, soldered Feather-compatible leads |

### Sensors

| Sensor | Part | Measures | Interface |
|--------|------|----------|-----------|
| [Sensirion SCD41](https://www.sparkfun.com/sparkfun-co2-humidity-and-temperature-sensor-scd41-qwiic.html) | SparkFun SCD41 Qwiic (SPF-23483) | CO2, temperature, humidity | I2C 0x62 |
| [ST VL53L4CX](https://www.adafruit.com/product/5425) | Adafruit VL53L4CX ToF (ADA-5425) | Distance (dough rise) | I2C 0x29 |

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

## Setup

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
  "vbat": 3.92
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
| `vbat` | V | Battery voltage (3.2–4.2V, >4.1V = charging) |

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

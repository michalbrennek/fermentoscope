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

- [Adafruit](https://www.adafruit.com/) — ESP32 Feather V2, VL53L4CX breakout, CircuitPython, STEMMA QT ecosystem
- [SparkFun](https://www.sparkfun.com/) — SCD41 Qwiic breakout
- [Sensirion](https://sensirion.com/) — SCD4x sensor and datasheet
- [STMicroelectronics](https://www.st.com/) — VL53L4CX sensor and register documentation
- [CircuitPython](https://circuitpython.org/) — Python runtime for microcontrollers

## License

MIT

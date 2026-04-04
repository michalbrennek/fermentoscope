"""Minimal SCD41 (SCD4x) driver for CircuitPython. No dependencies.

I2C command codes and conversion formulas from the Sensirion SCD4x datasheet:
  https://sensirion.com/media/documents/48C4B7FB/67FE0194/CD_DS_SCD4x_Datasheet_D1.pdf

Inspired by the Adafruit CircuitPython SCD4X library (MIT License):
  https://github.com/adafruit/Adafruit_CircuitPython_SCD4X
  Copyright (c) 2021 ladyada for Adafruit Industries
"""

import time


class SCD4X:
    def __init__(self, i2c):
        self.i2c = i2c
        self.addr = 0x62
        self._buf = bytearray(2)
        self.co2 = 0
        self.temperature = 0.0
        self.humidity = 0.0

    def _cmd(self, cmd):
        self._buf[0] = (cmd >> 8) & 0xFF
        self._buf[1] = cmd & 0xFF
        self.i2c.writeto(self.addr, self._buf)

    def start(self):
        self._cmd(0x21B1)

    def stop(self):
        self._cmd(0x3F86)
        time.sleep(0.5)

    def data_ready(self):
        self._cmd(0xE4B8)
        time.sleep(0.001)
        buf = bytearray(3)
        self.i2c.readfrom_into(self.addr, buf)
        return (buf[0] << 8 | buf[1]) & 0x07FF != 0

    def read(self):
        self._cmd(0xEC05)
        time.sleep(0.001)
        buf = bytearray(9)
        self.i2c.readfrom_into(self.addr, buf)
        self.co2 = buf[0] << 8 | buf[1]
        raw_t = buf[3] << 8 | buf[4]
        raw_h = buf[6] << 8 | buf[7]
        self.temperature = -45 + 175 * (raw_t / 65535.0)
        self.humidity = 100 * (raw_h / 65535.0)
        return self.co2, self.temperature, self.humidity

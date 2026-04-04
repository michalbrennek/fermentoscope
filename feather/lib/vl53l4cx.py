"""Minimal VL53L4CX ToF distance sensor driver for CircuitPython. No dependencies."""

import struct
import time

_INIT = bytes([
    0x00, 0x00, 0x00, 0x01, 0x02, 0x00, 0x02, 0x08,
    0x00, 0x08, 0x10, 0x01, 0x01, 0x00, 0x00, 0x00,
    0x00, 0xFF, 0x00, 0x0F, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x20, 0x0B, 0x00, 0x00, 0x02, 0x0A, 0x21,
    0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0xC8,
    0x00, 0x00, 0x38, 0xFF, 0x01, 0x00, 0x08, 0x00,
    0x00, 0x01, 0xCC, 0x0F, 0x01, 0xF1, 0x0D, 0x01,
    0x68, 0x00, 0x80, 0x08, 0xB8, 0x00, 0x00, 0x00,
    0x00, 0x0F, 0x89, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x0F, 0x0D, 0x0E, 0x0E, 0x00,
    0x00, 0x02, 0xC7, 0xFF, 0x9B, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x00,
])


class VL53L4CX:
    def __init__(self, i2c, addr=0x29):
        self.i2c = i2c
        self.addr = addr
        self.distance = 0
        self.baseline = None
        r = bytearray(3)
        i2c.writeto_then_readfrom(addr, bytes([0x01, 0x0F]), r)
        if r[0] != 0xEB:
            raise RuntimeError("Wrong sensor ID")
        i2c.writeto(addr, bytes([0x00, 0x2D]) + _INIT)
        i2c.writeto(addr, bytes([0x00, 0x87, 0x40]))
        for _ in range(100):
            s = bytearray(1)
            i2c.writeto_then_readfrom(addr, bytes([0x00, 0x31]), s)
            if s[0] & 0x01 == 1:
                break
            time.sleep(0.01)
        i2c.writeto(addr, bytes([0x00, 0x86, 0x01]))
        i2c.writeto(addr, bytes([0x00, 0x87, 0x00]))
        i2c.writeto(addr, bytes([0x00, 0x08, 0x09]))
        i2c.writeto(addr, bytes([0x00, 0x0B, 0x00]))

    def start(self):
        self.i2c.writeto(self.addr, bytes([0x00, 0x87, 0x40]))

    def stop(self):
        self.i2c.writeto(self.addr, bytes([0x00, 0x87, 0x00]))

    def clear(self):
        self.i2c.writeto(self.addr, bytes([0x00, 0x86, 0x01]))

    def data_ready(self):
        s = bytearray(1)
        self.i2c.writeto_then_readfrom(self.addr, bytes([0x00, 0x31]), s)
        return s[0] & 0x01 == 1

    def read(self):
        d = bytearray(2)
        self.i2c.writeto_then_readfrom(self.addr, bytes([0x00, 0x96]), d)
        self.distance = struct.unpack(">H", d)[0]
        self.clear()
        return self.distance

    def calibrate(self):
        readings = []
        self.start()
        for _ in range(5):
            for _ in range(100):
                if self.data_ready():
                    break
                time.sleep(0.02)
            readings.append(self.read())
        self.baseline = sum(readings) // len(readings)
        return self.baseline

    def rise(self):
        if self.baseline is None:
            return 0
        return max(0, self.baseline - self.distance)

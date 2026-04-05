#!/usr/bin/env python3
"""Reference BLE scanner for Fermentoscope sensor advertisements.

Listens for BLE advertisements from devices whose local name matches
``--name`` (default ``sourdough``, matching ``FERMENTOSCOPE_HOSTNAME``
set in feather/code.py) and decodes the 16-byte manufacturer-specific
data payload broadcast by the Feather.

The BLE fallback exists for cases where HTTP / mDNS is unavailable —
most commonly Android hotspots with client isolation enabled. The Pi
backend uses the same decoder for its own fallback (see
``pi/fermentoscope_server.py``). This script is the standalone
reference: no Pi, no backend, just ``bleak`` and a scanner.

Requires bleak: ``pip install bleak``

Usage:
    python3 tools/ble_scan.py                  # scan continuously
    python3 tools/ble_scan.py --once           # exit after first decode
    python3 tools/ble_scan.py --name mydough   # match a custom hostname
    python3 tools/ble_scan.py --duration 30    # scan for 30 seconds
"""

import argparse
import asyncio
import struct
import sys
from datetime import datetime

try:
    from bleak import BleakScanner
except ImportError:
    sys.exit("bleak not installed. Run: pip install bleak")

# Must match feather/code.py pack_ble_payload()
PAYLOAD_FMT = "<HhBHHHBI"
PAYLOAD_LEN = struct.calcsize(PAYLOAD_FMT)  # 16 bytes
BLE_COMPANY_ID = 0xFFFF  # Bluetooth SIG "for testing" - filter by name too


def decode_payload(mfr_data: bytes):
    """Decode 16-byte manufacturer data into a sensor reading dict.

    Returns None if the payload length doesn't match the expected 16 B.
    """
    if len(mfr_data) != PAYLOAD_LEN:
        return None
    co2, temp100, hum, dist, rise, baseline, vbat50, uptime = struct.unpack(
        PAYLOAD_FMT, mfr_data)
    return {
        "co2": co2,
        "temp": temp100 / 100.0,
        "hum": hum,
        "dist": dist,
        "rise": rise,
        "base": baseline,
        "vbat": round(3.0 + vbat50 / 50.0, 2),
        "uptime": uptime,
    }


async def run(target_name: str, once: bool, duration):
    found = asyncio.Event()

    def on_detect(device, adv):
        if adv.local_name != target_name:
            return
        mfr = adv.manufacturer_data.get(BLE_COMPANY_ID)
        if not mfr:
            return
        decoded = decode_payload(bytes(mfr))
        if decoded is None:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {device.address} rssi={adv.rssi}dBm  "
              f"co2={decoded['co2']}ppm temp={decoded['temp']}°C "
              f"hum={decoded['hum']}% rise={decoded['rise']}mm "
              f"base={decoded['base']}mm vbat={decoded['vbat']}V "
              f"uptime={decoded['uptime']}s")
        if once:
            found.set()

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        if once:
            try:
                await asyncio.wait_for(found.wait(), timeout=duration or 30.0)
            except asyncio.TimeoutError:
                print(f"No advertiser named {target_name!r} found within "
                      f"{duration or 30.0}s", file=sys.stderr)
                sys.exit(1)
        else:
            await asyncio.sleep(duration if duration else 10 ** 9)
    finally:
        await scanner.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Fermentoscope BLE reference scanner")
    parser.add_argument("--name", default="sourdough",
                        help="advertiser local name to match "
                             "(default: sourdough)")
    parser.add_argument("--once", action="store_true",
                        help="exit after the first decoded reading")
    parser.add_argument("--duration", type=float, default=None,
                        help="scan duration in seconds "
                             "(default: forever, or 30 with --once)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.name, args.once, args.duration))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

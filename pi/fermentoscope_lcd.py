#!/usr/bin/env python3
"""Fermentoscope LCD variant.

Runs everything the base server does plus renders the sensor display on
a Waveshare 3.5" RPi LCD (A) (SKU 9904) attached to a Raspberry Pi via
the 40-pin GPIO header (ILI9486 + XPT2046 touch).

Views (touch to cycle):
  1. Values (top) + combined plot (bottom)
  2. Full-screen detail plot of one parameter (touch a value to enter)

The calibration dialog appears as a full-screen overlay when a baseline
change or ESP32 reset is detected.
"""

import select
import struct
import threading
import time
from collections import deque
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

import fermentoscope_server as srv

try:
    import evdev
    HAS_TOUCH = True
except ImportError:
    HAS_TOUCH = False

# --- LCD configuration -------------------------------------------------------

FB_DEVICE = "/dev/fb0"
FB_WIDTH = 480
FB_HEIGHT = 320
TOP_H = 120
BOT_H = 200
HISTORY_MAX = 8640  # 24h at 10s
HISTORY_LOAD_HOURS = 24

BG = (13, 17, 23)
WHITE = (230, 237, 243)
GREEN = (0, 248, 0)
YELLOW = (248, 248, 0)
RED = (248, 0, 0)
DIM = (100, 110, 120)
GRID = (30, 38, 45)
PLOT_C = {"co2": RED, "temp": YELLOW, "hum": (0, 120, 248), "rise": GREEN}
FONT = "/usr/share/fonts/truetype/terminus/TerminusTTF-4.46.0.ttf"

# Touch calibration (from /etc/pointercal)
CAL_A, CAL_B, CAL_C = -8417, 49, 33293492
CAL_D, CAL_E, CAL_F = 45, 5631, -1385986
CAL_S = 65536

# View state
history = deque(maxlen=HISTORY_MAX)
_chg_cache = {"state": "Discharging"}
view = "plots"  # "plots" or "detail:KEY"
ZONES = [(0, 120, "co2"), (120, 240, "temp"),
         (240, 360, "hum"), (360, 480, "rise")]
ZONE_LABELS = {"co2": "CO2 (ppm)", "temp": "Temp (\u00b0C)",
               "hum": "RH (%)", "rise": "Rise (mm)"}


# --- Helpers -----------------------------------------------------------------

def fonts():
    try:
        return (ImageFont.truetype(FONT, 36),
                ImageFont.truetype(FONT, 20),
                ImageFont.truetype(FONT, 16))
    except (OSError, IOError):
        f = ImageFont.load_default()
        return f, f, f


def nice_scale(vmin, vmax, ticks=4):
    import math
    if vmin == vmax:
        vmax = vmin + 1
    raw_range = vmax - vmin
    raw_step = raw_range / ticks
    mag = 10 ** math.floor(math.log10(raw_step))
    norm = raw_step / mag
    if norm <= 1:
        ns = 1
    elif norm <= 2:
        ns = 2
    elif norm <= 2.5:
        ns = 2.5
    elif norm <= 5:
        ns = 5
    else:
        ns = 10
    step = ns * mag
    return (math.floor(vmin / step) * step,
            math.ceil(vmax / step) * step, step)


def bat_pct(v):
    if v <= 3.2:
        return 0
    if v >= 4.2:
        return 100
    return int((v - 3.2) / 1.0 * 100)


def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?.?.?.?"


# --- Framebuffer -------------------------------------------------------------

def to_rgb565(img):
    px = img.load()
    w, h = img.size
    buf = bytearray(w * h * 2)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            struct.pack_into("<H", buf, i,
                             ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
            i += 2
    return bytes(buf)


def write_fb(img, offset_y=0):
    raw = to_rgb565(img)
    with open(FB_DEVICE, "r+b") as fb:
        fb.seek(offset_y * FB_WIDTH * 2)
        fb.write(raw)


# --- Rendering ---------------------------------------------------------------

def _draw_ble_badge(draw):
    """Draw a small cyan 'BT' indicator in the top-right corner of the
    current render. Called by render_values() when the most recent
    sensor reading came from the BLE fallback path. Cyan (0,248,248)
    is the brightest blue-ish primary in RGB565 and reads cleanly on
    the ILI9486; no background fill to stay unobtrusive."""
    _, _, fs = fonts()
    draw.text((FB_WIDTH - 22, 4), "BT", fill=(0, 248, 248), font=fs)


def render_values(last_data, sensor_online, sess, hist=()):
    """Top 120px: 4 big sensor values + battery/network status."""
    img = Image.new("RGB", (FB_WIDTH, TOP_H), BG)
    draw = ImageDraw.Draw(img)
    fb, _, fs = fonts()

    d = last_data or {}
    co2 = d.get("co2", 0)
    temp = d.get("temp", 0.0)
    hum = d.get("hum", 0.0)
    rise = d.get("rise", 0.0)
    vbat = d.get("vbat", 0.0)

    cols = [5, 125, 250, 375]
    draw.text((cols[0], 4), f"{co2}", fill=PLOT_C["co2"], font=fb)
    draw.text((cols[0], 58), "CO2 (ppm)", fill=DIM, font=fs)
    draw.text((cols[1], 4), f"{temp:.1f}", fill=PLOT_C["temp"], font=fb)
    draw.text((cols[1], 58), "Temp (\u00b0C)", fill=DIM, font=fs)
    draw.text((cols[2], 4), f"{hum:.0f}", fill=PLOT_C["hum"], font=fb)
    draw.text((cols[2], 58), "RH (%)", fill=DIM, font=fs)
    draw.text((cols[3], 4), f"{int(rise)}", fill=PLOT_C["rise"], font=fb)
    cum = sess.get("cumulative_rise", 0) if sess else 0
    if cum > 0:
        draw.text((cols[3], 58), f"Rise (mm) +{int(cum + rise)}",
                  fill=DIM, font=fs)
    else:
        draw.text((cols[3], 58), "Rise (mm)", fill=DIM, font=fs)

    if sensor_online:
        bp = bat_pct(vbat)
        chg = _chg_cache.get("state", "Discharging")
        if len(hist) >= 5:
            window = min(len(hist), 30)
            recent_v = [h.get("vbat", 0) for h in list(hist)[-window:]]
            v_min, v_max = min(recent_v), max(recent_v)
            if d.get("usb") or vbat > 4.10:
                if v_max - v_min < 0.02 and vbat >= 4.15:
                    chg = "Full"
                else:
                    chg = "Charging"
            elif v_max - vbat > 0.02:
                chg = "Discharging"
            elif vbat - v_min > 0.02:
                chg = "Charging"
        elif d.get("usb") or vbat > 4.10:
            chg = "Charging"
        _chg_cache["state"] = chg
        bc = GREEN if bp > 50 else YELLOW if bp > 20 else RED
        draw.text((5, 80), f"{chg}:{bp}% {vbat:.2f}V", fill=bc, font=fs)
        draw.text((230, 80), "Online", fill=GREEN, font=fs)
    else:
        draw.text((5, 80), "Offline", fill=RED, font=fs)

    pi_ip = get_local_ip()
    draw.text((5, 100), f"IP: {pi_ip}  fermentoscope.local",
              fill=DIM, font=fs)

    # BT indicator - only shown when the current reading came from BLE
    if d.get("_source") == "ble":
        _draw_ble_badge(draw)

    draw.line([(0, TOP_H - 1), (FB_WIDTH, TOP_H - 1)], fill=(48, 54, 61))
    return img


def render_combined(hist):
    """Bottom 200px: all 4 parameters overlaid with time ticks."""
    img = Image.new("RGB", (FB_WIDTH, BOT_H), BG)
    draw = ImageDraw.Draw(img)
    _, _, fs = fonts()

    if len(hist) < 2:
        draw.text((10, 80), "Collecting data...", fill=DIM, font=fs)
        return img

    pl, pr, pt, pb = 50, FB_WIDTH - 50, 4, BOT_H - 18
    pw, ph = pr - pl, pb - pt

    grid_step = ph // 4
    for i in range(5):
        y = pt + int(i * ph / 4)
        draw.line([(pl, y), (pr, y)], fill=GRID)
    for x in range(pl, pr + 1, grid_step):
        draw.line([(x, pt), (x, pb)], fill=GRID)

    times = [h.get("_ts", 0) for h in hist]
    t0, t1 = times[0], times[-1]
    elapsed = max(1, t1 - t0)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        ago = int(elapsed * (1.0 - frac))
        x = pl + int(frac * pw)
        if ago == 0:
            lbl = "now"
        elif ago < 3600:
            lbl = f"-{ago // 60}m"
        else:
            lbl = f"-{ago // 3600}h"
        tw = 5 * len(lbl)
        draw.text((x - tw // 2, pb + 2), lbl, fill=DIM, font=fs)

    keys = ["co2", "temp", "hum", "rise"]
    for key in keys:
        vals = [h.get(key, 0) or 0 for h in hist]
        ns_min, ns_max, _ = nice_scale(min(vals), max(vals))
        pts = []
        for i, v in enumerate(vals):
            x = pl + int(i * pw / (len(vals) - 1))
            y = pb - int((v - ns_min) * ph / (ns_max - ns_min))
            pts.append((x, y))
        if len(pts) > 1:
            draw.line(pts, fill=PLOT_C[key], width=2)

    # Side scale labels
    co2_vals = [h.get("co2", 0) or 0 for h in hist]
    c_min, c_max, _ = nice_scale(min(co2_vals), max(co2_vals))
    r_vals = [h.get("rise", 0) or 0 for h in hist]
    r_min, r_max, _ = nice_scale(min(r_vals), max(r_vals))
    t_vals = [h.get("temp", 0) or 0 for h in hist]
    t_min, t_max, _ = nice_scale(min(t_vals), max(t_vals))
    h_vals = [h.get("hum", 0) or 0 for h in hist]
    h_min, h_max, _ = nice_scale(min(h_vals), max(h_vals))

    draw.text((0, pt), f"{c_max:.0f}", fill=PLOT_C["co2"], font=fs)
    draw.text((0, pb - 14), f"{c_min:.0f}", fill=PLOT_C["co2"], font=fs)
    draw.text((0, pt + 16), f"{r_max:.0f}", fill=PLOT_C["rise"], font=fs)
    draw.text((0, pb - 28), f"{r_min:.0f}", fill=PLOT_C["rise"], font=fs)
    draw.text((pr + 4, pt), f"{t_max:.1f}", fill=PLOT_C["temp"], font=fs)
    draw.text((pr + 4, pb - 14), f"{t_min:.1f}", fill=PLOT_C["temp"], font=fs)
    draw.text((pr + 4, pt + 16), f"{h_max:.0f}", fill=PLOT_C["hum"], font=fs)
    draw.text((pr + 4, pb - 28), f"{h_min:.0f}", fill=PLOT_C["hum"], font=fs)
    return img


def render_detail(key, hist):
    """Full-screen plot with Back button and current value."""
    label = ZONE_LABELS[key]
    color = PLOT_C[key]
    img = Image.new("RGB", (FB_WIDTH, FB_HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    fb, fm, fs = fonts()

    bx, by, bsz = FB_WIDTH - 70, 5, 60
    draw.rectangle([(bx, by), (bx + bsz, by + bsz)],
                   fill=(40, 46, 55), outline=DIM)
    draw.text((bx + 6, by + 18), "Back", fill=WHITE, font=fm)

    draw.text((4, 4), label, fill=DIM, font=fs)

    if len(hist) < 2:
        draw.text((4, 30), "Collecting data...", fill=DIM, font=fm)
        return img

    vals = [h.get(key, 0) or 0 for h in hist]
    times = [h.get("_ts", 0) for h in hist]
    vmin = min(vals)
    vmax = max(vals)
    if vmin == vmax:
        vmax = vmin + 1
    ns_min, ns_max, step = nice_scale(vmin, vmax)

    cur = vals[-1]
    cv = f"{cur:.1f}" if isinstance(cur, float) else f"{cur}"
    draw.text((bx, by + bsz + 8), cv, fill=color, font=fm)

    pl, pr, pt, pb = 60, FB_WIDTH - 10, 40, FB_HEIGHT - 50
    pw, ph = pr - pl, pb - pt

    ticks = max(1, round((ns_max - ns_min) / step))
    for i in range(ticks + 1):
        y = pt + int(i * ph / ticks)
        draw.line([(pl, y), (pr, y)], fill=GRID)
        v = ns_max - (ns_max - ns_min) * i / ticks
        lbl = f"{v:.0f}" if step >= 1 else f"{v:.1f}"
        draw.text((2, y - 6), lbl, fill=DIM, font=fs)

    elapsed = times[-1] - times[0]
    if elapsed > 0:
        draw.text((pl, pb + 8), f"-{int(elapsed / 60)}m", fill=DIM, font=fs)
        draw.text((pr - 25, pb + 8), "now", fill=DIM, font=fs)

    pts = []
    for i, v in enumerate(vals):
        x = pl + int(i * pw / (len(vals) - 1))
        y = pb - int((v - ns_min) * ph / (ns_max - ns_min))
        pts.append((x, y))
    if len(pts) > 1:
        draw.line(pts, fill=color, width=2)
    return img


def render_dialog(new_baseline, sess, last_rise):
    img = Image.new("RGB", (FB_WIDTH, FB_HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    fb, fm, _ = fonts()

    last_cum = sess.get("cumulative_rise", 0) if sess else 0

    draw.text((80, 10), "CALIBRATION", fill=WHITE, font=fb)
    draw.text((40, 50), f"New baseline: {new_baseline}mm", fill=DIM, font=fm)
    if last_cum + last_rise > 0:
        draw.text((40, 75),
                  f"Previous total rise: {int(last_cum + last_rise)}mm",
                  fill=DIM, font=fm)

    btn_y, btn_h, btn_w = 130, 110, 220
    draw.rectangle([(10, btn_y), (10 + btn_w, btn_y + btn_h)],
                   fill=(40, 80, 40), outline=GREEN, width=3)
    draw.text((35, btn_y + 20), "NEW", fill=WHITE, font=fb)
    draw.text((25, btn_y + 60), "START", fill=WHITE, font=fb)

    draw.rectangle([(250, btn_y), (250 + btn_w, btn_y + btn_h)],
                   fill=(80, 60, 20), outline=(248, 200, 0), width=3)
    draw.text((290, btn_y + 20), "ADDING", fill=WHITE, font=fb)
    draw.text((305, btn_y + 60), "FLOUR", fill=WHITE, font=fb)
    return img


# --- Touch -------------------------------------------------------------------

def touch_to_screen(rx, ry):
    sx = (CAL_A * rx + CAL_B * ry + CAL_C) // CAL_S
    sy = (CAL_D * rx + CAL_E * ry + CAL_F) // CAL_S
    return (max(0, min(sx, FB_WIDTH - 1)),
            max(0, min(sy, FB_HEIGHT - 1)))


def find_touch():
    if not HAS_TOUCH:
        return None
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if "7846" in dev.name or "touch" in dev.name.lower():
            return dev
    return None


# --- LCD main loop -----------------------------------------------------------

def refresh_screen():
    with srv.state_lock:
        last_data = srv.state["last_data"]
        sensor_online = srv.state["sensor_online"]
        sess = srv.state["current_session"] or {}
        pending = srv.state["pending_baseline"]
    last_rise = (last_data or {}).get("rise", 0)

    if pending is not None:
        write_fb(render_dialog(pending, sess, last_rise), 0)
        return

    if view.startswith("detail:"):
        key = view.split(":")[1]
        write_fb(render_detail(key, list(history)), 0)
    else:
        write_fb(render_values(last_data, sensor_online, sess, history), 0)
        write_fb(render_combined(list(history)), TOP_H)


def lcd_loop():
    global view
    print("LCD display manager starting...")
    td = find_touch()
    if td:
        print(f"Touch: {td.name}")
        td.grab()
    else:
        print("No touch device")

    last_refresh = 0
    rx, ry = 0, 0

    # Load initial history from SQLite via server module
    for row in srv.db_history(HISTORY_LOAD_HOURS):
        history.append({
            "_ts": row["ts"], "co2": row["co2"], "temp": row["temp"],
            "hum": row["hum"], "dist": row["dist"], "rise": row["rise"],
            "vbat": row["vbat"],
        })
    print(f"Loaded {len(history)} readings")

    while True:
        now = time.time()

        # Pull latest from polled state into history
        with srv.state_lock:
            d = srv.state["last_data"]
        if d:
            # Avoid duplicates: only add if ts differs
            if not history or history[-1].get("_ts", 0) != int(now // 10) * 10:
                entry = dict(d)
                entry["_ts"] = now
                history.append(entry)

        # Periodic refresh
        if now - last_refresh > 5:
            last_refresh = now
            refresh_screen()

        # Touch handling
        if td:
            while True:
                r, _, _ = select.select([td.fd], [], [], 0)
                if not r:
                    break
                for ev in td.read():
                    if ev.type == evdev.ecodes.EV_ABS:
                        if ev.code == evdev.ecodes.ABS_X:
                            rx = ev.value
                        elif ev.code == evdev.ecodes.ABS_Y:
                            ry = ev.value
                    elif (ev.type == evdev.ecodes.EV_KEY
                          and ev.code == evdev.ecodes.BTN_TOUCH
                          and ev.value == 0):
                        sx, sy = touch_to_screen(rx, ry)
                        with srv.state_lock:
                            pending = srv.state["pending_baseline"]
                            sess = srv.state["current_session"]
                        print(f"Touch ({sx},{sy}) view={view} pending={bool(pending)}")

                        if pending is not None:
                            # Calibration dialog
                            if 130 <= sy <= 240:
                                action = None
                                if 10 <= sx <= 230:
                                    action = "start"
                                elif 250 <= sx <= 470:
                                    action = "flour"
                                if action:
                                    last_rise = (srv.state["last_data"] or {}).get("rise", 0)
                                    prev_cum = sess.get("cumulative_rise", 0) if sess else 0
                                    if action == "start":
                                        srv.db_new_session(pending, 0, "start")
                                    else:
                                        srv.db_new_session(pending, prev_cum + last_rise, "flour")
                                    with srv.state_lock:
                                        srv.state["current_session"] = srv.db_get_session()
                                        srv.state["pending_baseline"] = None
                                    refresh_screen()
                        elif view == "plots" and sy < TOP_H:
                            for zl, zr, key in ZONES:
                                if zl <= sx < zr:
                                    view = f"detail:{key}"
                                    break
                            refresh_screen()
                        elif view.startswith("detail:"):
                            view = "plots"
                            refresh_screen()

        time.sleep(0.05)


def main():
    print("Fermentoscope (LCD variant) starting...")
    srv.db_init()
    with srv.state_lock:
        srv.state["current_session"] = srv.db_get_session()

    # Start poller and web server in background
    threading.Thread(target=srv.poller, daemon=True).start()
    threading.Thread(target=srv.start_server, daemon=True).start()

    # LCD loop runs in main thread
    lcd_loop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fermentoscope base server (headless, web-only).

Polls the ESP32 sensor node over HTTP, persists readings to SQLite, and
serves a web UI over HTTPS (with HTTP fallback). Detects ESP32 resets
(recalibration events) and prompts the user via a web modal to choose
between "New Start" and "Adding Flour", preserving cumulative rise.

Runs on a Raspberry Pi (tested on Pi Zero 2 W). No LCD required.
"""

import json
import os
import sqlite3
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Optional BLE fallback: if bleak isn't installed, the Pi runs HTTP-only
# exactly as before. Enable by running `pip install bleak`.
try:
    from bleak import BleakScanner
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

# --- Configuration -----------------------------------------------------------

ESP32_URL = os.environ.get("FERMENTOSCOPE_ESP32_URL",
                           "http://sourdough.local:8080/")
DB_PATH = os.environ.get("FERMENTOSCOPE_DB", "/home/pi/fermentoscope.db")
CERT_DIR = Path(os.environ.get("FERMENTOSCOPE_CERT_DIR",
                               "/home/pi/.fermentoscope_cert"))
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
HTTP_PORT = int(os.environ.get("FERMENTOSCOPE_HTTP_PORT", "80"))
HTTPS_PORT = int(os.environ.get("FERMENTOSCOPE_HTTPS_PORT", "443"))
POLL_INTERVAL = 10  # seconds between sensor polls

# BLE fallback configuration - matches feather/code.py
BLE_NAME = os.environ.get("FERMENTOSCOPE_BLE_NAME", "sourdough")
BLE_COMPANY_ID = 0xFFFF
BLE_PAYLOAD_FMT = "<HhBHHHBI"
BLE_PAYLOAD_LEN = struct.calcsize(BLE_PAYLOAD_FMT)  # 16
BLE_CACHE_TTL = 15  # seconds a cached BLE reading is considered fresh

# Shared BLE cache updated by the scanner thread, read by fetch_sensors()
_ble_lock = threading.Lock()
_ble_cache = {"data": None, "ts": 0.0}

# Shared state (updated by polling thread, read by HTTP handlers)
state_lock = threading.Lock()
state = {
    "last_data": None,
    "sensor_online": False,
    "current_session": None,
    "pending_baseline": None,   # set when calibration is detected
    "last_uptime_seen": 0,
}


# --- SQLite ------------------------------------------------------------------

def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            ts   INTEGER NOT NULL,
            co2  INTEGER,
            temp REAL,
            hum  REAL,
            dist INTEGER,
            rise REAL,
            vbat REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts INTEGER NOT NULL,
            baseline INTEGER NOT NULL,
            cumulative_rise REAL DEFAULT 0,
            event TEXT DEFAULT 'start',
            last_uptime INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def db_insert_reading(data, ts):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO readings (ts, co2, temp, hum, dist, rise, vbat) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(ts),
             int(data.get("co2", 0)),
             float(data.get("temp", 0)),
             float(data.get("hum", 0)),
             int(data.get("dist", 0)),
             float(data.get("rise", 0)),
             float(data.get("vbat", 0))))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB insert error: {e}")


def db_history(hours):
    cutoff = int(time.time()) - hours * 3600
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT ts, co2, temp, hum, dist, rise, vbat FROM readings "
        "WHERE ts >= ? ORDER BY ts ASC", (cutoff,))
    rows = [{"ts": r[0], "co2": r[1], "temp": r[2], "hum": r[3],
             "dist": r[4], "rise": r[5], "vbat": r[6]}
            for r in cur.fetchall()]
    conn.close()
    return rows


def db_get_session():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT id, start_ts, baseline, cumulative_rise, event, last_uptime "
        "FROM sessions ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "start_ts": row[1], "baseline": row[2],
                "cumulative_rise": row[3], "event": row[4],
                "last_uptime": row[5] or 0}
    return None


def db_new_session(baseline, cumulative_rise, event):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO sessions (start_ts, baseline, cumulative_rise, event, last_uptime) "
        "VALUES (?, ?, ?, ?, 0)",
        (int(time.time()), int(baseline), float(cumulative_rise), event))
    conn.commit()
    conn.close()


def db_update_session_uptime(session_id, uptime):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE sessions SET last_uptime = ? WHERE id = ?",
                     (int(uptime), session_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB uptime update error: {e}")


# --- ESP32 polling -----------------------------------------------------------

def fetch_sensors():
    """Fetch a sensor reading from the Feather.

    Tries HTTP first (the fastest and richest path). If HTTP fails across
    all three attempts - typically because WiFi/mDNS is unreachable or an
    AP has enabled client isolation - falls back to whatever the BLE
    scanner last decoded (if BLE fallback is enabled and the cache is
    fresh). Returns None if both paths fail.
    """
    # HTTP first
    for _ in range(3):
        try:
            req = urllib.request.Request(
                ESP32_URL, headers={"Accept-Encoding": "identity"})
            return json.loads(
                urllib.request.urlopen(req, timeout=3).read().decode())
        except Exception:
            time.sleep(1)
    # HTTP exhausted - try the BLE cache
    return fetch_sensors_ble()


def _ble_decode(mfr_data):
    """Decode the 16-byte BLE manufacturer data payload from the Feather.

    Matches the pack_ble_payload() format in feather/code.py. Returns a
    dict with the same keys as the HTTP JSON endpoint (minus 'usb' and
    'host' which aren't broadcast over BLE) or None on malformed input.
    """
    if len(mfr_data) != BLE_PAYLOAD_LEN:
        return None
    try:
        co2, temp100, hum, dist, rise, baseline, vbat50, uptime = struct.unpack(
            BLE_PAYLOAD_FMT, mfr_data)
    except Exception:
        return None
    return {
        "co2": co2,
        "temp": temp100 / 100.0,
        "hum": float(hum),
        "dist": dist,
        "rise": float(rise),
        "base": baseline,
        "vbat": round(3.0 + vbat50 / 50.0, 2),
        "uptime": uptime,
    }


def fetch_sensors_ble():
    """Return the last decoded BLE payload if fresh, else None."""
    with _ble_lock:
        data = _ble_cache["data"]
        ts = _ble_cache["ts"]
    if data is None or time.time() - ts > BLE_CACHE_TTL:
        return None
    return dict(data)


def _ble_runner():
    """Run a BleakScanner forever in this thread's own asyncio loop.

    The detection callback decodes matching adverts into _ble_cache. The
    poller's fetch_sensors_ble() reads that cache with an age check.
    """
    import asyncio

    async def _main():
        def _on_detect(_device, adv):
            if adv.local_name != BLE_NAME:
                return
            mfr = adv.manufacturer_data.get(BLE_COMPANY_ID)
            if not mfr:
                return
            decoded = _ble_decode(bytes(mfr))
            if decoded is None:
                return
            with _ble_lock:
                _ble_cache["data"] = decoded
                _ble_cache["ts"] = time.time()

        scanner = BleakScanner(detection_callback=_on_detect)
        await scanner.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await scanner.stop()

    try:
        asyncio.run(_main())
    except Exception as e:
        print(f"BLE scanner thread exited: {e}")


def start_ble_scanner():
    """Start the background BLE scanner if bleak is available."""
    if not HAS_BLEAK:
        print("bleak not installed - BLE fallback disabled "
              "(run: pip install bleak to enable)")
        return
    threading.Thread(target=_ble_runner, daemon=True,
                     name="ble-scanner").start()
    print(f"BLE fallback scanner running (target name: {BLE_NAME!r})")


def poller():
    """Background thread: polls ESP32 every POLL_INTERVAL seconds."""
    while True:
        try:
            data = fetch_sensors()
            now = time.time()
            with state_lock:
                if data:
                    state["sensor_online"] = True
                    state["last_data"] = data
                    db_insert_reading(data, now)

                    sess = state["current_session"]
                    new_base = data.get("base", 0)
                    up = data.get("uptime", 0)

                    if sess is None and new_base > 0:
                        # First ever session
                        db_new_session(new_base, 0, "start")
                        state["current_session"] = db_get_session()

                    elif sess and new_base > 0:
                        baseline_diff = abs(new_base - sess["baseline"]) > 2
                        uptime_drop = (sess["last_uptime"] > 0 and
                                       up < sess["last_uptime"] - 5)
                        if ((baseline_diff or uptime_drop)
                                and state["pending_baseline"] is None):
                            state["pending_baseline"] = new_base
                            print(f"Calibration detected: "
                                  f"{sess['baseline']}->{new_base}, "
                                  f"uptime {sess['last_uptime']}->{up}")

                        # Update last seen uptime
                        if up > 0 and sess:
                            sess["last_uptime"] = up
                            db_update_session_uptime(sess["id"], up)
                else:
                    state["sensor_online"] = False
        except Exception as e:
            print(f"Poller error: {e}")
        time.sleep(POLL_INTERVAL)


# --- HTTPS self-signed cert --------------------------------------------------

def ensure_cert():
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", "3650", "-nodes",
        "-subj", "/CN=fermentoscope.local"
    ], check=True, capture_output=True)


# --- Web UI ------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Fermentoscope</title>
<style>
:root{--bg:#0d1117;--fg:#e6edf3;--dim:#656d76;--border:#30363d;
 --co2:#f83c00;--temp:#f8f800;--hum:#0078f8;--rise:#00f800;}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,sans-serif;
 padding:12px;user-select:none;overflow:hidden}
#view-main,#view-detail,#view-all{height:100%;display:flex;flex-direction:column}
.hidden{display:none!important}
h1{font-size:13px;color:var(--dim);font-weight:400;margin-bottom:8px;flex-shrink:0}
.status{display:flex;gap:16px;font-size:12px;color:var(--dim);margin-bottom:10px;flex-shrink:0}
.status .online{color:var(--rise)}
.status .offline{color:var(--co2)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;flex-shrink:0}
.cell{background:#161b22;border:1px solid var(--border);border-radius:8px;
 padding:14px;cursor:pointer;transition:border-color .15s}
.cell:hover{border-color:var(--fg)}
.val{font-size:clamp(24px,5vw,42px);font-weight:700;
 font-family:'Courier New',monospace;line-height:1}
.lbl{font-size:11px;color:var(--dim);margin-top:6px;text-transform:uppercase;letter-spacing:.05em}
.co2 .val{color:var(--co2)}
.temp .val{color:var(--temp)}
.hum .val{color:var(--hum)}
.rise .val{color:var(--rise)}
.plots{background:#161b22;border:1px solid var(--border);border-radius:8px;
 padding:8px;cursor:pointer;flex:1;min-height:0;display:flex}
.plots canvas{flex:1;width:100%;height:100%}
.back{display:inline-block;background:#21262d;border:1px solid var(--border);
 border-radius:6px;padding:10px 18px;color:var(--fg);cursor:pointer;
 font-size:14px;text-decoration:none;margin-bottom:8px;flex-shrink:0;align-self:flex-start}
.back:hover{border-color:var(--fg)}
#view-detail canvas,#view-all>.big-grid{flex:1;min-height:0}
#view-detail canvas{width:100%;height:100%}
.big-grid{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:8px}
.big-grid>div{background:#161b22;border:1px solid var(--border);border-radius:8px;
 padding:6px;min-height:0;display:flex}
.big-grid canvas{flex:1;width:100%;height:100%}
@media(max-width:640px){.grid{grid-template-columns:repeat(2,1fr)}
 .big-grid{grid-template-columns:1fr;grid-template-rows:repeat(4,1fr)}}
/* Calibration modal */
#modal{position:fixed;inset:0;background:rgba(0,0,0,0.85);
 display:none;align-items:center;justify-content:center;z-index:100}
#modal.active{display:flex}
.mdl{background:#161b22;border:2px solid var(--rise);border-radius:12px;
 padding:24px;max-width:420px;width:90%;text-align:center}
.mdl h2{color:var(--rise);margin-bottom:12px}
.mdl p{color:var(--dim);margin-bottom:20px}
.mdl-btns{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.mdl-btns button{padding:16px;border:none;border-radius:8px;font-size:14px;
 font-weight:bold;cursor:pointer;color:#fff}
.btn-start{background:#1a6428}
.btn-flour{background:#855c12}
</style>
</head>
<body>

<div id="view-main">
 <h1>FERMENTOSCOPE <span id="status-line"></span></h1>
 <div class="status">
  <span>Battery: <span id="bat">-</span></span>
  <span id="conn" class="online">Online</span>
  <span id="session-info"></span>
 </div>
 <div class="grid">
  <div class="cell co2" onclick="showDetail('co2')"><div class="val" id="co2">-</div><div class="lbl">CO2 (ppm)</div></div>
  <div class="cell temp" onclick="showDetail('temp')"><div class="val" id="temp">-</div><div class="lbl">Temp (°C)</div></div>
  <div class="cell hum" onclick="showDetail('hum')"><div class="val" id="hum">-</div><div class="lbl">RH (%)</div></div>
  <div class="cell rise" onclick="showDetail('rise')"><div class="val" id="rise">-</div><div class="lbl">Rise (mm)</div></div>
 </div>
 <div class="plots" onclick="showAll()"><canvas id="combined"></canvas></div>
</div>

<div id="view-detail" class="hidden">
 <a class="back" onclick="showMain()">← Back</a>
 <h1 id="detail-title"></h1>
 <canvas id="detail-canvas"></canvas>
</div>

<div id="view-all" class="hidden">
 <a class="back" onclick="showMain()">← Back</a>
 <h1>All Parameters</h1>
 <div class="big-grid">
  <div><canvas id="all-co2"></canvas></div>
  <div><canvas id="all-temp"></canvas></div>
  <div><canvas id="all-hum"></canvas></div>
  <div><canvas id="all-rise"></canvas></div>
 </div>
</div>

<div id="modal">
 <div class="mdl">
  <h2>CALIBRATION</h2>
  <p id="mdl-info">Baseline changed</p>
  <div class="mdl-btns">
   <button class="btn-start" onclick="calibrate('start')">NEW<br>START</button>
   <button class="btn-flour" onclick="calibrate('flour')">ADDING<br>FLOUR</button>
  </div>
 </div>
</div>

<script>
const COLORS = {co2:'#f83c00',temp:'#f8f800',hum:'#0078f8',rise:'#00f800'};
const LABELS = {co2:'CO2 (ppm)',temp:'Temp (°C)',hum:'RH (%)',rise:'Rise (mm)'};

function fmt(v,key){
 if(v==null) return '-';
 if(key==='co2'||key==='rise'||key==='dist'||key==='base') return Math.round(v);
 return v.toFixed(1);
}

function niceScale(vmin,vmax,ticks=4){
 if(vmin===vmax) vmax = vmin+1;
 const range = vmax-vmin;
 const rawStep = range/ticks;
 const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
 const norm = rawStep/mag;
 let niceStep;
 if(norm<=1) niceStep=1;
 else if(norm<=2) niceStep=2;
 else if(norm<=2.5) niceStep=2.5;
 else if(norm<=5) niceStep=5;
 else niceStep=10;
 const step = niceStep*mag;
 return {min:Math.floor(vmin/step)*step, max:Math.ceil(vmax/step)*step, step};
}

function sizeCanvas(cv){
 const r = cv.getBoundingClientRect();
 const dpr = window.devicePixelRatio||1;
 cv.width = r.width*dpr;
 cv.height = r.height*dpr;
 cv.getContext('2d').scale(dpr,dpr);
 return {w:r.width, h:r.height};
}

function drawPlot(cv, data, key, color){
 const {w, h} = sizeCanvas(cv);
 const ctx = cv.getContext('2d');
 ctx.clearRect(0,0,w,h);
 if(!data||data.length<2){
  ctx.fillStyle='#656d76';ctx.font='14px system-ui';
  ctx.fillText('Collecting data...',20,40);return;
 }
 const pad=55;
 const vals = data.map(d=>d[key]).filter(v=>v!=null);
 if(vals.length<2) return;
 const ns = niceScale(Math.min(...vals),Math.max(...vals));
 const times = data.map(d=>d.ts);
 const t0=times[0],t1=times[times.length-1];
 const elapsed = Math.max(1,t1-t0);
 // grid
 ctx.strokeStyle='#30363d';ctx.lineWidth=1;
 const ticks = Math.round((ns.max-ns.min)/ns.step);
 for(let i=0;i<=ticks;i++){
  const y = pad+(h-2*pad)*i/ticks;
  ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-pad/2,y);ctx.stroke();
 }
 for(let i=0;i<=6;i++){
  const x = pad+(w-pad*1.5)*i/6;
  ctx.beginPath();ctx.moveTo(x,pad);ctx.lineTo(x,h-pad);ctx.stroke();
 }
 // y labels
 ctx.fillStyle='#8b949e';ctx.font='11px system-ui';ctx.textAlign='right';
 for(let i=0;i<=ticks;i++){
  const y = pad+(h-2*pad)*i/ticks;
  const v = ns.max-(ns.max-ns.min)*i/ticks;
  ctx.fillText(v.toFixed(ns.step<1?1:0),pad-5,y+4);
 }
 // x labels
 ctx.textAlign='center';
 for(let i=0;i<=6;i++){
  const frac=i/6;
  const ago=Math.round(elapsed*(1-frac));
  const x = pad+(w-pad*1.5)*frac;
  let lbl=ago===0?'now':ago<3600?`-${Math.round(ago/60)}m`:`-${(ago/3600).toFixed(1)}h`;
  ctx.fillText(lbl,x,h-pad+16);
 }
 // plot
 ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();
 data.forEach((d,i)=>{
  if(d[key]==null) return;
  const x = pad+(w-pad*1.5)*(d.ts-t0)/elapsed;
  const y = pad+(h-2*pad)*(ns.max-d[key])/(ns.max-ns.min);
  if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
 });
 ctx.stroke();
 // title
 ctx.fillStyle=color;ctx.font='bold 14px system-ui';ctx.textAlign='left';
 ctx.fillText(LABELS[key]+': '+fmt(vals[vals.length-1],key),pad,24);
}

function drawCombined(data){
 const cv = document.getElementById('combined');
 const {w, h} = sizeCanvas(cv);
 const ctx = cv.getContext('2d');
 ctx.clearRect(0,0,w,h);
 if(!data||data.length<2) return;
 const pad=30;
 // grid
 ctx.strokeStyle='#30363d';ctx.lineWidth=1;
 for(let i=0;i<=4;i++){
  const y = pad+(h-2*pad)*i/4;
  ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-pad,y);ctx.stroke();
 }
 for(let i=0;i<=6;i++){
  const x = pad+(w-2*pad)*i/6;
  ctx.beginPath();ctx.moveTo(x,pad);ctx.lineTo(x,h-pad);ctx.stroke();
 }
 const keys=['co2','temp','hum','rise'];
 const t0=data[0].ts,t1=data[data.length-1].ts;
 const elapsed = Math.max(1,t1-t0);
 keys.forEach(k=>{
  const vals = data.map(d=>d[k]).filter(v=>v!=null);
  if(vals.length<2) return;
  const ns = niceScale(Math.min(...vals),Math.max(...vals));
  ctx.strokeStyle=COLORS[k];ctx.lineWidth=2;ctx.beginPath();
  let started=false;
  data.forEach(d=>{
   if(d[k]==null) return;
   const x = pad+(w-2*pad)*(d.ts-t0)/elapsed;
   const y = pad+(h-2*pad)*(ns.max-d[k])/(ns.max-ns.min);
   if(!started){ctx.moveTo(x,y);started=true;} else ctx.lineTo(x,y);
  });
  ctx.stroke();
 });
 // time labels
 ctx.fillStyle='#8b949e';ctx.font='11px system-ui';ctx.textAlign='center';
 for(let i=0;i<=6;i++){
  const frac=i/6;
  const ago=Math.round(elapsed*(1-frac));
  const x = pad+(w-2*pad)*frac;
  let lbl=ago===0?'now':ago<3600?`-${Math.round(ago/60)}m`:`-${(ago/3600).toFixed(1)}h`;
  ctx.fillText(lbl,x,h-pad+16);
 }
}

async function fetchData(){
 try{
  const r = await fetch('/api/data');
  return await r.json();
 }catch(e){return null}
}
async function fetchHistory(hours){
 try{
  const r = await fetch('/api/history?hours='+hours);
  return await r.json();
 }catch(e){return []}
}
async function fetchSession(){
 try{
  const r = await fetch('/api/session');
  return await r.json();
 }catch(e){return {}}
}

let currentView = 'main';
let detailKey = null;

async function refresh(){
 const d = await fetchData();
 if(d && Object.keys(d).length){
  document.getElementById('co2').textContent = fmt(d.co2,'co2');
  document.getElementById('temp').textContent = fmt(d.temp,'temp');
  document.getElementById('hum').textContent = fmt(d.hum,'hum');
  document.getElementById('rise').textContent = fmt(d.rise,'rise');
  document.getElementById('bat').textContent = d.vbat ? d.vbat.toFixed(2)+'V' : '-';
  document.getElementById('conn').textContent = 'Online';
  document.getElementById('conn').className = 'online';
 }else{
  document.getElementById('conn').textContent = 'Offline';
  document.getElementById('conn').className = 'offline';
 }

 const sess = await fetchSession();
 if(sess.pending){
  showModal(sess.pending_baseline, sess.cumulative_rise||0, sess.last_rise||0);
 }else{
  hideModal();
  if(sess.cumulative_rise>0){
   document.getElementById('session-info').textContent =
    'Cumulative: '+Math.round(sess.cumulative_rise)+'mm';
  }
 }

 const hist = await fetchHistory(24);
 if(currentView==='main') drawCombined(hist);
 else if(currentView==='detail' && detailKey){
  drawPlot(document.getElementById('detail-canvas'), hist, detailKey, COLORS[detailKey]);
 }else if(currentView==='all'){
  ['co2','temp','hum','rise'].forEach(k=>
   drawPlot(document.getElementById('all-'+k), hist, k, COLORS[k]));
 }
}

function showMain(){
 currentView='main';
 document.getElementById('view-main').classList.remove('hidden');
 document.getElementById('view-detail').classList.add('hidden');
 document.getElementById('view-all').classList.add('hidden');
 refresh();
}
function showDetail(key){
 currentView='detail';detailKey=key;
 document.getElementById('view-main').classList.add('hidden');
 document.getElementById('view-detail').classList.remove('hidden');
 document.getElementById('view-all').classList.add('hidden');
 document.getElementById('detail-title').textContent=LABELS[key];
 refresh();
}
function showAll(){
 currentView='all';
 document.getElementById('view-main').classList.add('hidden');
 document.getElementById('view-detail').classList.add('hidden');
 document.getElementById('view-all').classList.remove('hidden');
 refresh();
}

function showModal(baseline, cumRise, lastRise){
 document.getElementById('mdl-info').textContent =
  `New baseline: ${baseline}mm. Previous total: ${Math.round(cumRise+lastRise)}mm`;
 document.getElementById('modal').classList.add('active');
}
function hideModal(){
 document.getElementById('modal').classList.remove('active');
}
async function calibrate(action){
 await fetch('/api/calibrate?action='+action,{method:'POST'});
 hideModal();
 refresh();
}

window.addEventListener('resize',()=>refresh());
refresh();
setInterval(refresh,10000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX_HTML)
            return
        if path == "/api/data":
            with state_lock:
                d = state["last_data"] or {}
            self._send(200, "application/json", json.dumps(d))
            return
        if path == "/api/history":
            hours = 24
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    if k == "hours":
                        try:
                            hours = int(v)
                        except Exception:
                            pass
            self._send(200, "application/json", json.dumps(db_history(hours)))
            return
        if path == "/api/session":
            with state_lock:
                sess = state["current_session"] or {}
                pending = state["pending_baseline"]
                last_rise = (state["last_data"] or {}).get("rise", 0)
            resp = {
                "pending": pending is not None,
                "pending_baseline": pending,
                "cumulative_rise": sess.get("cumulative_rise", 0),
                "baseline": sess.get("baseline", 0),
                "last_rise": last_rise,
            }
            self._send(200, "application/json", json.dumps(resp))
            return
        self._send(404, "text/plain", "not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/calibrate":
            action = None
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    if k == "action":
                        action = v
            with state_lock:
                pending = state["pending_baseline"]
                sess = state["current_session"]
            if pending and action in ("start", "flour"):
                if action == "start":
                    db_new_session(pending, 0, "start")
                else:
                    last_rise = (state["last_data"] or {}).get("rise", 0)
                    prev_cum = sess.get("cumulative_rise", 0) if sess else 0
                    db_new_session(pending, prev_cum + last_rise, "flour")
                with state_lock:
                    state["current_session"] = db_get_session()
                    state["pending_baseline"] = None
                self._send(200, "application/json", '{"ok":true}')
                return
            self._send(400, "application/json", '{"error":"bad request"}')
            return
        self._send(404, "text/plain", "not found")


# --- Entry point -------------------------------------------------------------

def start_server():
    try:
        ensure_cert()
        server = HTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"HTTPS server on :{HTTPS_PORT}")
        server.serve_forever()
    except Exception as e:
        print(f"HTTPS failed ({e}), starting HTTP on :{HTTP_PORT}")
        HTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


def main():
    print(f"Fermentoscope server starting")
    print(f"  ESP32:  {ESP32_URL}")
    print(f"  DB:     {DB_PATH}")
    db_init()
    with state_lock:
        state["current_session"] = db_get_session()
    start_ble_scanner()
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    start_server()


if __name__ == "__main__":
    main()

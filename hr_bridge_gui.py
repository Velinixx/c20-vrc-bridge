#!/usr/bin/env python3
"""C20 Smartwatch → VRChat OSC Heart Rate Bridge — GUI Edition."""
import asyncio
import json
import math
import os
import platform
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, colorchooser as tkc
from typing import Optional

from bleak import BleakClient, BleakScanner
from pythonosc.udp_client import SimpleUDPClient

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

# media detection
try:
    import winrt.windows.media.control as wmc
    HAS_WINRT = True
except ImportError:
    HAS_WINRT = False

# Pear Desktop API support
import json
import urllib.request
HAS_PEAR = True  # urllib is built-in
PEAR_DEFAULT_PORT = 26538

# BLE
BLE_HR_MEASURE = "00002a37-0000-1000-8000-00805f9b34fb"
BLE_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
BLE_FEE2_OUT = "0000fee2-0000-1000-8000-00805f9b34fb"
BLE_FEE3_IN = "0000fee3-0000-1000-8000-00805f9b34fb"
CMD_START_DYNAMIC_HR = 104
CMD_TRIGGER_HR = 109
CMD_SET_HR_INTERVAL = 31
CMD_SET_QUICK_VIEW = 24
IS_LINUX = platform.system() == "Linux"
DEFAULT_ADDR = "96:D6:AF:D0:2B:6E"
DEFAULT_TEMPLATE = "❤️ {bpm} BPM  🔋 {battery}%"


def make_packet(cmd, payload=bytes()):
    data = bytearray([0xFE, 0xEA, 0x10, 0x00, cmd]) + payload
    data[3] = len(data)
    return bytes(data)


if HAS_WINRT:
    async def get_winrt_media():
        try:
            session = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            s = session.get_current_session()
            if s is None:
                return None
            info = await s.try_get_media_properties_async()
            result = {"title": info.title or "", "artist": info.artist or ""}
            try:
                tl = s.get_timeline_properties()
                p = tl.position
                e = tl.end_time
                # winrt TimeSpan: .duration is 100ns ticks, or use total_seconds if available
                try:
                    result["position"] = max(0, int(p.total_seconds()))
                    result["duration"] = max(0, int(e.total_seconds()))
                except:
                    result["position"] = max(0, int(p.duration / 1e7))
                    result["duration"] = max(0, int(e.duration / 1e7))
            except:
                pass
            return result
        except:
            return None
else:
    async def get_winrt_media():
        return None

def get_pear_media_sync(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/song", timeout=3) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read())
            if not data:
                return None
            author = (data.get("author") or {}).get("name", "") if isinstance(data.get("author"), dict) else (data.get("artist") or "")
            return {
                "title": data.get("title", "") or "",
                "artist": author or "",
                "position": data.get("elapsedSeconds", 0) or 0,
                "duration": data.get("songDuration", 0) or 0,
                "isPaused": data.get("isPaused", False) or False,
            }
    except Exception:
        return None


def pear_seek_to(port, seconds):
    """Seek Pear Desktop to a specific position in seconds."""
    try:
        body = json.dumps({"seconds": seconds}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/seek-to",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3):
            pass
        return True
    except Exception:
        return False


# ── System Stats ──────────────────────────────────────────────
def get_system_stats():
    """Returns {cpu (int), ram (int %), ram_gb (float)} or None."""
    try:
        if IS_LINUX:
            # CPU
            with open("/proc/stat") as f:
                vals = list(map(int, f.readline().split()[1:5]))
            total = sum(vals)
            idle = vals[3]
            # Mem
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            mem_total = int([l for l in lines if "MemTotal" in l][0].split()[1])
            mem_avail = int([l for l in lines if "MemAvailable" in l][0].split()[1])
            ram = int((mem_total - mem_avail) / mem_total * 100)
            ram_gb = round((mem_total - mem_avail) / 1_048_576, 1)
            # For CPU we need two samples; return 0 on first call, caller caches
            return {"cpu": total, "idle": idle, "ram": ram, "ram_gb": ram_gb}
        else:
            import ctypes
            kernel = ctypes.windll.kernel32
            # CPU
            idle, kernel_t, user_t = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
            kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel_t), ctypes.byref(user_t))
            total = idle.value + kernel_t.value + user_t.value
            # RAM
            mem = ctypes.c_ulonglong()
            kernel32 = ctypes.windll.kernel32
            kernel32.GlobalMemoryStatusEx.c_ulonglong = ctypes.c_ulonglong
            buf = ctypes.create_string_buffer(64)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(buf))
            # MEMORYSTATUSEX layout: 8 bytes dwLength, 4 dwMemoryLoad, 8 ullTotalPhys, 8 ullAvailPhys ...
            mem_load = int.from_bytes(buf[8:12], "little")
            total_phys = int.from_bytes(buf[12:20], "little")
            avail_phys = int.from_bytes(buf[20:28], "little")
            ram_gb = round((total_phys - avail_phys) / (1024**3), 1)
            return {"cpu": total, "idle": idle.value, "ram": mem_load, "ram_gb": ram_gb}
    except:
        return None

def get_cpu_percent(stats):
    """Convert raw cpu ticks to a 0-100 percentage given previous sample."""
    if not stats:
        return 0
    prev = getattr(get_cpu_percent, "_prev", None)
    if not prev:
        get_cpu_percent._prev = stats
        return 0
    total_delta = (stats["cpu"] - prev["cpu"]) or 1
    idle_delta = stats["idle"] - prev["idle"]
    get_cpu_percent._prev = stats
    return int((1 - idle_delta / total_delta) * 100)

# ── Bridge Engine ───────────────────────────────────────────
class HRBridge:
    def __init__(self, address, template, log_cb, show_hr=True, show_battery=True, show_media=False, show_status=False, show_extremes=True, show_system_stats=False, status_text="", poll_interval=3, keepalive_interval=30, osc_host="127.0.0.1", osc_port=9000, media_source="none", pear_port=PEAR_DEFAULT_PORT, hr_source="ble", hyperate_id="", hyperate_key=""):
        self.address = address
        self.template = template
        self.log = log_cb
        self.show_hr = show_hr
        self.show_battery = show_battery
        self.show_media = show_media
        self.show_status = show_status
        self.show_extremes = show_extremes
        self.show_system_stats = show_system_stats
        self.status_text = status_text
        self.poll_interval = poll_interval
        self.keepalive_interval = keepalive_interval
        self.media_source = media_source
        self.pear_port = pear_port
        self.hr_source = hr_source
        self.hyperate_id = hyperate_id
        self.hyperate_key = hyperate_key
        self.osc = SimpleUDPClient(osc_host, osc_port)
        self.bpm = 0
        self.hr_min = 999
        self.hr_max = 0
        self.battery = 0
        self.running = False
        self.song = ""
        self.artist = ""
        self.media_position = 0
        self.media_duration = 0
        self.media_is_paused = False
        self._client = None
        self.bpm_history = []
        self.cpu = 0
        self.ram = 0
        self.ram_gb = 0.0
        self._sys_stats = None
        self._pulse = 0.0
        self.paused = False

    def log_msg(self, msg):
        self.log(msg)

    def _build_chatbox(self):
        if self.show_status and self.status_text.strip():
            return self.status_text.strip()
        text = self.template
        text = text.replace("{bpm}", str(self.bpm))
        text = text.replace("{hr_min}", str(self.hr_min if self.hr_min != 999 else self.bpm))
        text = text.replace("{hr_max}", str(self.hr_max))
        text = text.replace("{battery}", str(self.battery))
        if self.show_media:
            media_parts = []
            if self.song:
                media_parts.append(self.song)
            if self.artist:
                media_parts.append(self.artist)
            media_str = " — ".join(media_parts) if media_parts else ""
            text = text.replace("{song}", media_str).replace("{artist}", self.artist).replace("{title}", self.song)
            # media progress
            dur = self.media_duration
            pos = self.media_position
            if dur > 0:
                prog = pos / dur
                text = text.replace("{media_progress}", f"{prog:.2f}")
            else:
                text = text.replace("{media_progress}", "0")
            text = text.replace("{media_position}", f"{pos // 60:02d}:{pos % 60:02d}")
            text = text.replace("{media_duration}", f"{dur // 60:02d}:{dur % 60:02d}")
            text = text.replace("{media_is_paused}", str(self.media_is_paused).lower())
        else:
            text = text.replace("{song}", "").replace("{artist}", "").replace("{title}", "")
            text = text.replace("{media_progress}", "0").replace("{media_position}", "00:00").replace("{media_duration}", "00:00").replace("{media_is_paused}", "false")
        if self.show_system_stats:
            text = text.replace("{cpu}", str(self.cpu))
            text = text.replace("{ram}", str(self.ram))
            text = text.replace("{ram_gb}", f"{self.ram_gb:.1f}")
        else:
            text = text.replace("{cpu}", "0").replace("{ram}", "0").replace("{ram_gb}", "0.0")
        return " ".join(text.split()).strip()

    def _log_line(self):
        parts = []
        if self.show_hr:
            parts.append(f"\u2764\ufe0f {self.bpm} BPM")
        if self.show_extremes and self.hr_max > 0:
            parts.append(f"\U0001f7e2 {self.hr_min}\u2194{self.hr_max}")
        if self.show_battery:
            parts.append(f"\U0001f50b {self.battery}%")
        if self.show_media and (self.song or self.artist):
            parts.append(f"\U0001f3b5 {self.song or self.artist}")
        if self.show_system_stats:
            parts.append(f"\U0001f5a5 {self.cpu}% \U0001f4be {self.ram}%")
        self.log_msg("  " + "  ".join(parts))

    def send_osc(self):
        chat = self._build_chatbox()
        self.osc.send_message("/avatar/parameters/isHRConnected", not self.paused)
        self.osc.send_message("/avatar/parameters/HR", int(self.bpm))
        self.osc.send_message("/avatar/parameters/floatHR", min(self.bpm / 255.0, 1.0))
        self.osc.send_message("/avatar/parameters/HRBattery", self.battery)
        self.osc.send_message("/avatar/parameters/HRBatteryFloat", self.battery / 100.0)
        self.osc.send_message("/avatar/parameters/HRMin", int(self.hr_min if self.hr_min != 999 else self.bpm))
        self.osc.send_message("/avatar/parameters/HRMax", int(self.hr_max))
        if self.show_media:
            dur = self.media_duration
            if dur > 0:
                self.osc.send_message("/avatar/parameters/MediaProgress", min(self.media_position / dur, 1.0))
            else:
                self.osc.send_message("/avatar/parameters/MediaProgress", 0.0)
            self.osc.send_message("/avatar/parameters/MediaIsPaused", self.media_is_paused)
        if self.show_system_stats:
            self.osc.send_message("/avatar/parameters/CPU", self.cpu)
            self.osc.send_message("/avatar/parameters/CPUFloat", self.cpu / 100.0)
            self.osc.send_message("/avatar/parameters/RAM", self.ram)
            self.osc.send_message("/avatar/parameters/RAMFloat", self.ram / 100.0)
            self.osc.send_message("/avatar/parameters/RAMGB", self.ram_gb)
        if chat and not self.paused:
            self.osc.send_message("/chatbox/input", [chat, True])
        self._log_line()

    def on_hr(self, _h, data):
        if len(data) < 2:
            return
        flags = data[0]
        bpm = data[2] if (flags & 1) else data[1]
        if 20 <= bpm <= 250:
            self._update_bpm(bpm)

    def on_fee3(self, _h, data):
        if len(data) < 5 or data[0] != 0xFE or data[1] != 0xEA:
            return
        cmd = data[4]
        if cmd == CMD_TRIGGER_HR and len(data) >= 6:
            bpm = data[5]
            if 20 <= bpm <= 250:
                self._update_bpm(bpm)

    def _update_bpm(self, bpm):
        self.bpm = bpm
        self._pulse = 1.0
        self.bpm_history.append(bpm)
        if len(self.bpm_history) > 180:
            self.bpm_history = self.bpm_history[-180:]
        if bpm < self.hr_min:
            self.hr_min = bpm
        if bpm > self.hr_max:
            self.hr_max = bpm
        if self.show_system_stats:
            self._fetch_system_stats()
        self.send_osc()

    def _fetch_system_stats(self):
        stats = get_system_stats()
        if stats:
            self.cpu = get_cpu_percent(stats)
            self.ram = stats["ram"]
            self.ram_gb = stats["ram_gb"]

    async def _cache_linux(self):
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "connect", self.address,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()

    async def run_once(self):
        if IS_LINUX:
            self.log_msg("\U0001f504 Caching\u2026")
            await self._cache_linux()
        self.reset_hr_extremes()
        self.log_msg(f"\U0001f4e1 Connecting to {self.address}\u2026")
        kwargs = {"timeout": 20.0}
        if IS_LINUX:
            kwargs["dangerous_use_bleak_cache"] = True
        async with BleakClient(self.address, **kwargs) as client:
            self._client = client
            self.log_msg("  \u2705 Connected")
            try:
                batt = await client.read_gatt_char(BLE_BATTERY)
                self.battery = batt[0]
                self.log_msg(f"  \U0001f50b {self.battery}%")
            except:
                pass
            await client.start_notify(BLE_FEE3_IN, self.on_fee3)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_SET_HR_INTERVAL, bytes([0x01])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_TRIGGER_HR, bytes([0x00])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_SET_QUICK_VIEW, bytes([0x01])), response=False)
            await client.start_notify(BLE_HR_MEASURE, self.on_hr)
            self.log_msg("  \u2705 Streaming!")
            last_notify = asyncio.get_event_loop().time()
            last_keepalive = last_notify
            poll_count = 0
            while self.running and client.is_connected:
                await asyncio.sleep(self.poll_interval)
                now = asyncio.get_event_loop().time()
                if now - last_notify >= 2:
                    await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_TRIGGER_HR, bytes([0x00])), response=False)
                if now - last_keepalive >= self.keepalive_interval:
                    await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False)
                    last_keepalive = now
                poll_count += 1
                if poll_count >= 3:
                    await client.write_gatt_char(BLE_FEE2_OUT, make_packet(47, bytes([])), response=False)
                    poll_count = 0
                if self.show_media and self.media_source != "none":
                    media = await self._fetch_media()
                    if media:
                        self.song = media.get("title", "")
                        self.artist = media.get("artist", "")
                        self.media_position = media.get("position", 0)
                        self.media_duration = media.get("duration", 0)
                        self.media_is_paused = media.get("isPaused", False)
            self.log_msg("  \u26a0\ufe0f Disconnected")

    async def run_hyperate(self):
        if not HAS_WS:
            self.log_msg("  \u274c websockets library not installed")
            return
        self.reset_hr_extremes()
        url = f"wss://app.hyperate.io/ws/{self.hyperate_id}?token={self.hyperate_key}"
        self.log_msg(f"\U0001f4e1 Connecting to HypeRate\u2026")
        async with websockets.connect(url) as ws:
            self.log_msg("  \u2705 Connected to HypeRate")
            await ws.send(json.dumps({"topic": f"hr:{self.hyperate_id}", "event": "phx_join", "payload": {}, "ref": "1"}))
            last_media = 0
            while self.running:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=35))
                except asyncio.TimeoutError:
                    try:
                        await ws.send(json.dumps({"event": "ping", "payload": {"timestamp": asyncio.get_event_loop().time()}}))
                    except:
                        pass
                    continue
                if msg.get("event") == "phx_reply":
                    self.log_msg("  ✅ Joined HypeRate channel")
                elif msg.get("event") == "hr_update":
                    bpm = msg.get("payload", {}).get("hr", 0)
                    if 20 <= bpm <= 250:
                        self._update_bpm(bpm)
                elif msg.get("event") == "phx_close":
                    self.log_msg("  \u26a0\ufe0f HypeRate channel closed")
                    break
                if self.show_media and self.media_source != "none":
                    now = asyncio.get_event_loop().time()
                    if now - last_media >= 5:
                        media = await self._fetch_media()
                        if media:
                            self.song = media.get("title", "")
                            self.artist = media.get("artist", "")
                            self.media_position = media.get("position", 0)
                            self.media_duration = media.get("duration", 0)
                            self.media_is_paused = media.get("isPaused", False)
                        last_media = now
            self.log_msg("  \u26a0\ufe0f Disconnected from HypeRate")

    async def run_forever(self):
        runner = self.run_hyperate if self.hr_source == "hyperate" else self.run_once
        while self.running:
            try:
                await runner()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log_msg(f"  \u26a0\ufe0f {e}")
            if self.running:
                self.log_msg("  \U0001f504 Reconnecting in 5s\u2026")
                await asyncio.sleep(5)

    def start(self):
        self.running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self.run_forever())

    async def _fetch_media(self):
        if self.media_source == "winrt":
            return await get_winrt_media()
        elif self.media_source == "pear":
            return await asyncio.to_thread(get_pear_media_sync, self.pear_port)
        return None

    def reset_hr_extremes(self):
        self.hr_min = 999
        self.hr_max = 0
        self.bpm = 0
        self.log_msg("  \U0001f504 HR extremes reset")

    def stop(self):
        self.running = False
        if self._client and self._client.is_connected:
            asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.log_msg("  \u23f9\ufe0f Stopped")


# ── Config ──────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_config.json")

DEFAULT_PRESETS = {
    "Minimal": "\u2764\ufe0f {bpm} BPM",
    "Full Stats": "\u2764\ufe0f {bpm} BPM  \U0001f7e2 {hr_min}\u2194{hr_max}  \U0001f50b {battery}%  \U0001f3b5 {title}",
    "Velinix": "\u2764\ufe0f BPM:{bpm} Highest:{hr_max} Lowest:{hr_min} \U0001f3b5 Listening to:{title} by {artist}",
}

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[save_config] Failed to save config: {e}", flush=True)


# ── HR History ────────────────────────────────────────────────
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hr_history.json")

def load_history():
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except:
        return []

def save_history(entries):
    with open(HISTORY_PATH, "w") as f:
        json.dump(entries, f, indent=2)

def record_history(bpm, hr_min, hr_max):
    import datetime
    entries = load_history()
    entries.append({
        "ts": datetime.datetime.now().isoformat(),
        "bpm": bpm,
        "min": hr_min,
        "max": hr_max,
    })
    if len(entries) > 50000:
        entries = entries[-25000:]
    save_history(entries)


# ── GUI ─────────────────────────────────────────────────────
# ── Constants ──────────────────────────────────────────────────
BLANK_EGG_SECRETS = ("boihanny", "sr4 series")

# ── Color Palette (Magic Chatbox inspired) ─────────────────────
BG_DARK     = "#1a1a2e"
BG_MID      = "#232244"
BG_CARD     = "#2d2b55"
BG_INPUT    = "#1e1e3a"
ACCENT      = "#7c5cbf"
ACCENT_LIGHT = "#9d7de0"
TEXT_WHITE  = "#f0eefe"
TEXT_GRAY   = "#a8a0c8"
SUCCESS     = "#4ade80"
DANGER      = "#ef4444"
DEFAULT_GRADIENT = {
    "type": "linear",
    "angle": 180,
    "stops": [
        {"color": "#1a1a2e", "position": 0},
        {"color": "#7c5cbf", "position": 100},
    ],
}
GRADIENT_CFG = dict(DEFAULT_GRADIENT)

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

def _lerp_color(a, b, t):
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    return _rgb_to_hex(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t)

def _gradient_color(t, stops):
    """Sample a multi-stop gradient at t (0–1). stops = [{color, position}, …] with pos 0–100."""
    if not stops:
        return "#000000"
    if len(stops) == 1:
        return stops[0]["color"]
    s = sorted(stops, key=lambda x: x["position"])
    if t <= s[0]["position"] / 100:
        return s[0]["color"]
    if t >= s[-1]["position"] / 100:
        return s[-1]["color"]
    for i in range(len(s) - 1):
        p0 = s[i]["position"] / 100
        p1 = s[i + 1]["position"] / 100
        if p0 <= t <= p1:
            lt = (t - p0) / (p1 - p0)
            return _lerp_color(s[i]["color"], s[i + 1]["color"], lt)
    return s[-1]["color"]

def _gradient_css(grad):
    """Build CSS gradient string from config dict."""
    if not grad or not grad.get("stops"):
        return "none"
    stops = sorted(grad["stops"], key=lambda x: x["position"])
    parts = [f"{s['color']} {s['position']}%" for s in stops]
    if grad.get("type") == "radial":
        return f"radial-gradient(circle at center, {', '.join(parts)})"
    else:
        angle = grad.get("angle", 180)
        return f"linear-gradient({angle}deg, {', '.join(parts)})"

def apply_palette(cfg):
    global BG_DARK, BG_MID, BG_CARD, BG_INPUT, ACCENT, ACCENT_LIGHT, TEXT_WHITE, TEXT_GRAY, GRADIENT_CFG
    m = {"bg_dark": "BG_DARK", "bg_mid": "BG_MID", "bg_card": "BG_CARD", "bg_input": "BG_INPUT",
         "accent": "ACCENT", "accent_light": "ACCENT_LIGHT", "text_white": "TEXT_WHITE", "text_gray": "TEXT_GRAY"}
    for key, gname in m.items():
        val = cfg.get(f"color_{key}")
        if val:
            globals()[gname] = val
    g = cfg.get("gradient")
    if g and isinstance(g, dict):
        GRADIENT_CFG.clear()
        GRADIENT_CFG.update(g)

def setup_style():
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(".", background=BG_DARK, foreground=TEXT_WHITE, fieldbackground=BG_INPUT)
    style.configure("TFrame", background=BG_DARK)
    style.configure("TLabel", background=BG_DARK, foreground=TEXT_WHITE)
    style.configure("TLabelframe", background=BG_DARK, foreground=ACCENT_LIGHT, fieldbackground=BG_CARD)
    style.configure("TLabelframe.Label", background=BG_DARK, foreground=ACCENT_LIGHT)
    style.configure("TEntry", fieldbackground=BG_INPUT, foreground=TEXT_WHITE, insertcolor=TEXT_WHITE)
    style.configure("TSpinbox", fieldbackground=BG_INPUT, foreground=TEXT_WHITE, arrowcolor=TEXT_WHITE)
    style.map("TEntry", fieldbackground=[("focus", BG_INPUT)])
    style.configure("TCheckbutton", background=BG_DARK, foreground=TEXT_WHITE)
    style.map("TCheckbutton", background=[("active", BG_MID)])
    style.configure("TButton", background=ACCENT, foreground=TEXT_WHITE, bordercolor=ACCENT, focuscolor="none")
    style.map("TButton", background=[("active", ACCENT_LIGHT), ("pressed", "#5a3d99")])
    style.configure("TSidebar.TButton", background=BG_MID, foreground=TEXT_GRAY, borderwidth=0, focuscolor="none")
    style.map("TSidebar.TButton", background=[("active", BG_CARD), ("selected", ACCENT)])
    style.configure("Success.TButton", background=SUCCESS, foreground="#000000")
    style.map("Success.TButton", background=[("active", "#6ee7a0")])
    style.configure("Danger.TButton", background=DANGER, foreground=TEXT_WHITE)
    style.configure("green.Horizontal.TProgressbar", background=ACCENT, troughcolor=BG_MID, bordercolor=BG_CARD, lightcolor=ACCENT_LIGHT, darkcolor=ACCENT)
    style.map("Danger.TButton", background=[("active", "#f87171")])
    style.configure("TScrollbar", background=BG_MID, troughcolor=BG_DARK, bordercolor=BG_MID, arrowcolor=TEXT_GRAY)


class Page(tk.Frame):
    """Base page with a card-style container."""
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG_DARK, **kw)


class CollapsibleCard(tk.Frame):
    """A card with a clickable header that toggles visibility of the body."""
    def __init__(self, parent, title, collapsed=False, expand=False):
        super().__init__(parent, bg=BG_CARD, padx=10, pady=8)
        self._collapsed = collapsed
        hdr = tk.Frame(self, bg=BG_CARD)
        hdr.pack(fill="x")
        arrow = "\u25b6" if collapsed else "\u25bc"
        self.arrow_lbl = tk.Label(hdr, text=arrow, bg=BG_CARD, fg=TEXT_GRAY,
                                  font=("", 8), cursor="hand2")
        self.arrow_lbl.pack(side="left")
        title_lbl = tk.Label(hdr, text=title, bg=BG_CARD, fg=ACCENT_LIGHT,
                             font=("", 9, "bold"), anchor="w", cursor="hand2")
        title_lbl.pack(side="left", padx=(4, 0))
        for w in (hdr, self.arrow_lbl, title_lbl):
            w.bind("<Button-1>", lambda e: self._toggle())
        self.body = tk.Frame(self, bg=BG_CARD)
        if not collapsed:
            kw = {"fill": "both", "expand": True, "pady": (6, 0)} if expand else {"fill": "x", "pady": (6, 0)}
            self.body.pack(**kw)

    def _toggle(self):
        if self._collapsed:
            self.body.pack(fill="x", pady=(6, 0))
            self.arrow_lbl.config(text="\u25bc")
        else:
            self.body.pack_forget()
            self.arrow_lbl.config(text="\u25b6")
        self._collapsed = not self._collapsed


class ToolTip:
    """Hover tooltip for any widget."""
    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._id = None
        self._tw = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)

    def _schedule(self, event=None):
        self._id = self.widget.after(self.delay, self._show)

    def _show(self):
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tw = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", bg="#2d2b55", fg="#f0eefe",
                 font=("", 8), padx=8, pady=5).pack()

    def _hide(self, event=None):
        if self._id:
            self.widget.after_cancel(self._id)
            self._id = None
        if self._tw:
            self._tw.destroy()
            self._tw = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        cfg = load_config()
        apply_palette(cfg)

        self.title("C20 HR Bridge")
        self.configure(bg=BG_DARK)
        self.minsize(520, 400)
        setup_style()

        self.bridge = None
        self.egg_dev = False
        self._autostarted_this_session = False

        # ── top bar ─────────────────────────────────────────
        top = tk.Frame(self, bg=BG_MID, height=48)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="C20  →  VRChat Bridge", bg=BG_MID,
                 fg=ACCENT_LIGHT, font=("", 11, "bold")).pack(side="left", padx=14, pady=10)

        # ── body canvas (gradient background) ────────────────
        body = tk.Canvas(self, highlightthickness=0, bg=BG_DARK)
        body.pack(fill="both", expand=True)
        self._body_canvas = body

        # sidebar (canvas window)
        sidebar = tk.Frame(body, bg=BG_MID, width=52)
        self._sidebar_frame = sidebar
        self._sidebar_wid = self._body_canvas.create_window(0, 0, window=sidebar, anchor="nw")

        icons = ["\u2764", "\u2699", "\u2630"]
        tips  = ["Status", "Features", "Log"]
        self.nav_btns = []
        for i, (ico, tip) in enumerate(zip(icons, tips)):
            btn = tk.Button(sidebar, text=ico, font=("", 16), bg=BG_MID, fg=TEXT_GRAY,
                            bd=0, activebackground=BG_CARD, activeforeground=ACCENT_LIGHT,
                            cursor="hand2", relief="flat")
            btn.pack(pady=(12 if i == 0 else 4, 4))
            self.nav_btns.append(btn)

        # content area (canvas window)
        content = tk.Frame(body, bg=BG_DARK, padx=14, pady=10)
        self._content_frame = content
        self._content_wid = self._body_canvas.create_window(52, 0, window=content, anchor="nw")

        # bind resize to reposition windows + redraw gradient
        body.bind("<Configure>", self._on_body_resize)
        # force initial redraw after layout
        self.after(20, self._on_body_resize)

        # ── pages ────────────────────────────────────────────
        self._pages = []

        p0 = Page(content)
        self._pages.append(p0)

        p1 = Page(content)
        self._pages.append(p1)

        p2 = Page(content)
        self._pages.append(p2)

        self._build_status_page(p0, cfg)
        self._build_features_page(p1, cfg)
        self._build_log_page(p2)

        for p in self._pages:
            p.pack(fill="both", expand=True)

        # wire nav
        for i, btn in enumerate(self.nav_btns):
            btn.config(command=lambda idx=i: self._show_page(idx))

        # ── egg toggle initial state after all pages exist ──
        self._on_egg_toggle()

        # ── BlankEgg config restore ──
        if cfg.get("blank_egg", False):
            self.egg_dev = True
            self._show_dev()

        self._show_page(0)

        # ── status bar ────────────────────────────────────
        self._status_bar = tk.Frame(self, bg=BG_MID, height=24)
        self._status_bar.pack(fill="x")
        self._status_bar.pack_propagate(False)
        self._status_label = tk.Label(self._status_bar, text="\u25cf Idle", anchor="w",
                                      bg=BG_MID, fg=TEXT_GRAY, font=("", 8))
        self._status_label.pack(side="left", padx=8)
        self._status_osc = tk.Label(self._status_bar, text="OSC: --", anchor="e",
                                    bg=BG_MID, fg=TEXT_GRAY, font=("", 8))
        self._status_osc.pack(side="right", padx=8)
        self._update_statusbar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── start VR auto-start monitor ──
        self.after(5000, self._check_vrchat)

    # ── page switching ────────────────────────────────────────
    def _show_page(self, idx):
        for i, p in enumerate(self._pages):
            p.pack_forget() if i != idx else p.pack(fill="both", expand=True)
        for i, btn in enumerate(self.nav_btns):
            btn.config(bg=BG_CARD if i == idx else BG_MID, fg=ACCENT_LIGHT if i == idx else TEXT_GRAY)

    def _card(self, parent, text):
        c = CollapsibleCard(parent, text)
        c.pack(fill="x", pady=(0, 6))
        return c.body

    def _qbtn(self, parent, text):
        """Add a ? label with a hover tooltip."""
        lbl = tk.Label(parent, text="?", bg=parent.cget("bg") or BG_CARD,
                       fg=TEXT_GRAY, font=("", 7, "bold"), cursor="question_arrow")
        lbl.pack(side="left", padx=(2, 0))
        ToolTip(lbl, text)
        return lbl

    # ── page: status ──────────────────────────────────────────
    def _build_status_page(self, page, cfg):
        self._build_bpm_display(page)
        # media seek bar
        self._seek_frame = tk.Frame(page, bg=BG_CARD, padx=10, pady=4)
        self._seek_frame.pack(fill="x", pady=(0, 2))
        self._seek_lbl = tk.Label(self._seek_frame, text="\U0001f3b5  —:— / —:—",
                                  bg=BG_CARD, fg=TEXT_GRAY, font=("", 8))
        self._seek_lbl.pack(side="left")
        self._seek_bar = ttk.Progressbar(self._seek_frame, value=0, length=180,
                                         mode="determinate", style="green.Horizontal.TProgressbar")
        self._seek_bar.pack(side="left", padx=(8, 4), fill="x", expand=True)
        self._seek_btn = tk.Button(self._seek_frame, text="\u25b6\ufe0f", font=("", 9),
                                   bg=BG_MID, fg=TEXT_WHITE, bd=0, padx=6, cursor="hand2",
                                   activebackground=BG_CARD, command=self._pear_seek_popup)
        self._seek_btn.pack(side="left")
        self._seek_frame.pack_forget()
        # address
        card = self._card(page, "Watch")
        addr_row = tk.Frame(card, bg=BG_CARD)
        addr_row.pack(fill="x", pady=(6, 0))
        tk.Label(addr_row, text="BLE Address", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        self._qbtn(addr_row, "Watch Bluetooth MAC address\nExample: 96:D6:AF:D0:2B:6E\nTap watch screen first to wake it")
        self.addr = tk.StringVar(value=cfg.get("address", DEFAULT_ADDR))
        tk.Entry(addr_row, textvariable=self.addr, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                 bd=0, highlightthickness=1, highlightbackground=BG_MID, highlightcolor=ACCENT, width=24).pack(side="right")

        # template
        card2 = self._card(page, "Chatbox Format")
        self.template = tk.StringVar(value=cfg.get("template", DEFAULT_TEMPLATE))
        self.template.trace_add("write", self._live_sync)
        self._template_entry = tk.Entry(card2, textvariable=self.template, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                                        bd=0, highlightthickness=1, highlightbackground=BG_MID, highlightcolor=ACCENT, width=44)
        self._template_entry.pack(fill="x", pady=(6, 2))
        vars_row = tk.Frame(card2, bg=BG_CARD)
        vars_row.pack(fill="x")
        tk.Label(vars_row, text="{bpm} {hr_min} {hr_max} {battery} {cpu} {ram} {ram_gb} {song} {artist} {title} {media_progress} {media_position} {media_duration} {media_is_paused}",
                 bg=BG_CARD, fg=TEXT_GRAY, font=("", 7)).pack(side="left")
        self._qbtn(vars_row, "Variables you can use:\n{bpm} - heart rate\n{hr_min} / {hr_max} - min/max\n{battery} - battery %\n{cpu} - CPU usage %\n{ram} - RAM usage %\n{ram_gb} - RAM used (GB)\n{song} / {artist} / {title} - media\n{media_progress} - 0.00-1.00 position\n{media_position} - mm:ss\n{media_duration} - mm:ss\n{media_is_paused} - true/false\nExample: ❤ {bpm} BPM | 🔋 {battery}%")

        # template builder buttons
        btn_row = tk.Frame(card2, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(2, 0))
        for t in ("{bpm}", "{hr_min}", "{hr_max}", "{battery}", "{cpu}", "{ram}", "{ram_gb}", "{song}", "{artist}", "{title}", "{media_progress}", "{media_position}", "{media_duration}", "{media_is_paused}"):
            b = tk.Button(btn_row, text=t, font=("Consolas", 7), bg=BG_MID, fg=TEXT_WHITE,
                          bd=0, padx=4, cursor="hand2", command=lambda t=t: self._insert_template(t))
            b.pack(side="left", padx=(0, 2))
        # emoji buttons
        emoji_row = tk.Frame(card2, bg=BG_CARD)
        emoji_row.pack(fill="x", pady=(2, 4))
        for em in ("❤️", "💙", "💚", "💜", "🖤", "🔥", "🎵", "🎶", "🔋", "⚡"):
            b = tk.Button(emoji_row, text=em, font=("", 8), bg=BG_MID, fg=TEXT_WHITE,
                          bd=0, padx=3, cursor="hand2", command=lambda e=em: self._insert_template(e))
            b.pack(side="left", padx=(0, 2))
        # presets
        presets_row = tk.Frame(card2, bg=BG_CARD)
        presets_row.pack(fill="x", pady=(2, 0))
        tk.Label(presets_row, text="Preset:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        presets = self._load_presets(cfg)
        self._preset_var = tk.StringVar()
        self._preset_menu = tk.OptionMenu(presets_row, self._preset_var, "")
        self._preset_menu.config(bg=BG_MID, fg=TEXT_WHITE, bd=0, highlightthickness=0, activebackground=BG_CARD, width=18)
        self._preset_menu["menu"].config(bg=BG_MID, fg=TEXT_WHITE)
        self._preset_menu.pack(side="left", padx=(6, 4))
        self._rebuild_preset_menu(presets)
        tk.Button(presets_row, text="\u25b6 Load", font=("", 8), bg=BG_MID, fg=TEXT_WHITE, bd=0, padx=8,
                  cursor="hand2", command=self._load_preset).pack(side="left", padx=(0, 4))
        tk.Button(presets_row, text="\u2795 Save", font=("", 8), bg=BG_MID, fg=TEXT_WHITE, bd=0, padx=8,
                  cursor="hand2", command=self._save_preset).pack(side="left", padx=(0, 4))
        tk.Button(presets_row, text="\u2716 Delete", font=("", 8), bg=BG_MID, fg=TEXT_GRAY, bd=0, padx=6,
                  cursor="hand2", command=self._delete_preset).pack(side="left")
        self._mirror_egg_var = tk.BooleanVar(value=cfg.get("mirror_egg", False))
        tk.Checkbutton(card2, text="Mirror to egg", variable=self._mirror_egg_var,
                       bg=BG_CARD, fg=TEXT_GRAY, selectcolor=BG_INPUT,
                       activebackground=BG_CARD, activeforeground=TEXT_WHITE,
                       font=("", 7), cursor="hand2").pack(anchor="w")

        # egg mode
        self.egg_frame = tk.Frame(page, bg=BG_DARK)
        self.egg_frame.pack(fill="x", pady=(0, 0))
        self.egg_txt = tk.StringVar(value=cfg.get("egg_text", ""))
        self.egg_txt.trace_add("write", self._live_sync)
        self._egg_entry = tk.Entry(self.egg_frame, textvariable=self.egg_txt, bg=BG_INPUT, fg=TEXT_WHITE,
                                   insertbackground=TEXT_WHITE, bd=0, highlightthickness=1,
                                   highlightbackground=BG_MID, highlightcolor=ACCENT, width=44)
        self._egg_entry.pack(side="left", padx=(0, 6))
        self._qbtn(self.egg_frame, "Custom short text for Egg Mode\nReplaces the template entirely\nSent as-is to chatbox")
        tk.Label(self.egg_frame, text="Egg text", bg=BG_DARK, fg=TEXT_GRAY, font=("", 7)).pack(side="left")

        # start / stop / pause
        btn_frame = tk.Frame(page, bg=BG_DARK)
        btn_frame.pack(fill="x", pady=(10, 0))
        self.btn = tk.Button(btn_frame, text="\u25b6  Start", font=("", 10, "bold"),
                             bg=SUCCESS, fg="#000", bd=0, padx=18, pady=4,
                             activebackground="#6ee7a0", cursor="hand2",
                             command=self._toggle)
        self.btn.pack(side="left")
        self._pause_btn = tk.Button(btn_frame, text="\u23f8  Pause", font=("", 10, "bold"),
                                    bg=BG_MID, fg=TEXT_WHITE, bd=0, padx=14, pady=4,
                                    activebackground=BG_CARD, cursor="hand2",
                                    command=self._toggle_pause)
        self._pause_btn.pack(side="left", padx=(8, 0))
        self._pause_btn.pack_forget()

        # dev panel
        self._dev_build(page, cfg)

    # ── page: features ────────────────────────────────────────
    def _build_features_page(self, page, cfg):
        toggles_card = CollapsibleCard(page, "Toggles")
        toggles_card.pack(fill="x", pady=(0, 6))
        body = toggles_card.body

        toggles_data = [
            ("\u2764  Heart Rate", "hr", True, "Stream heart rate to VRChat\nOSC: /avatar/parameters/HR"),
            ("\U0001f50b  Battery", "battery", True, "Stream watch battery level\nOSC: /avatar/parameters/HRBattery"),
            ("\U0001f5a5  System Stats", "system_stats", False, "Stream CPU & RAM usage\nOSC: /avatar/parameters/CPU, /RAM, /RAMGB"),
            ("\U0001f3b5  Media Info", "media", False, "Stream current song info\nSources: winrt (Windows) / Pear Desktop"),
            ("\U0001f7e2  Min / Max HR", "extremes", True, "Track min & max heart rate\nauto-resets on reconnection"),
            ("\U0001f95a  Egg Mode", "egg", False, "Replace template with custom\nshort text (chatbox pillar style)"),
        ]
        self._toggles_vars = {}
        for i, (label, key, default, tip) in enumerate(toggles_data):
            var = tk.BooleanVar(value=cfg.get(key, default))
            self._toggles_vars[key] = var
            if key != "egg":
                var.trace_add("write", self._live_sync)
            f = tk.Frame(body, bg=BG_CARD)
            f.pack(side="left", padx=(0, 8), pady=2)
            cb = tk.Checkbutton(f, text=label, variable=var,
                                bg=BG_CARD, fg=TEXT_WHITE, selectcolor=BG_INPUT,
                                activebackground=BG_CARD, activeforeground=TEXT_WHITE,
                                font=("", 9), cursor="hand2")
            cb.pack(side="left")
            self._qbtn(f, tip)
            if key == "egg":
                cb.config(command=self._on_egg_toggle)

        self.chk_hr = self._toggles_vars["hr"]
        self.chk_batt = self._toggles_vars["battery"]
        self.chk_sysstats = self._toggles_vars["system_stats"]
        self.chk_media = self._toggles_vars["media"]
        self.chk_extremes = self._toggles_vars["extremes"]
        self.chk_egg = self._toggles_vars["egg"]

        f = tk.Frame(body, bg=BG_CARD)
        f.pack(side="left", padx=(0, 8), pady=2)
        self._autostart_var = tk.BooleanVar(value=cfg.get("autostart_vrchat", False))
        self._autostart_var.trace_add("write", self._live_sync)
        tk.Checkbutton(f, text="\U0001f3ae  Auto-start with VRChat", variable=self._autostart_var,
                       bg=BG_CARD, fg=TEXT_WHITE, selectcolor=BG_INPUT,
                       activebackground=BG_CARD, activeforeground=TEXT_WHITE,
                       font=("", 9), cursor="hand2").pack(side="left")
        self._qbtn(f, "Auto-start bridge when VRChat\nor SteamVR process is detected")

        # media source
        src_row = tk.Frame(body, bg=BG_CARD)
        src_row.pack(fill="x", pady=(4, 0))
        tk.Label(src_row, text="Media Source:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        self._qbtn(src_row, "Source for media info\nnone = off\nwinrt = Windows Runtime (Windows only)\npear = Pear Desktop API")
        self.media_source = tk.StringVar(value=cfg.get("media_source", "none"))
        self.media_source.trace_add("write", self._live_sync)
        sources = ["none", "winrt", "pear"]
        src_menu = tk.OptionMenu(src_row, self.media_source, *sources)
        src_menu.config(bg=BG_MID, fg=TEXT_WHITE, bd=0, highlightthickness=0, activebackground=BG_CARD)
        src_menu["menu"].config(bg=BG_MID, fg=TEXT_WHITE)
        src_menu.pack(side="left", padx=(6, 0))

        # pear port
        tk.Label(src_row, text="Port:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left", padx=(10, 0))
        self._qbtn(src_row, "Pear Desktop API port\nDefault: 26538")
        self.pear_port = tk.IntVar(value=cfg.get("pear_port", PEAR_DEFAULT_PORT))
        self.pear_port.trace_add("write", self._live_sync)
        tk.Spinbox(src_row, from_=1024, to=65535, textvariable=self.pear_port, width=6,
                   bg=BG_INPUT, fg=TEXT_WHITE, bd=0, highlightthickness=1,
                   highlightbackground=BG_MID, highlightcolor=ACCENT, buttonbackground=BG_MID).pack(side="left", padx=(4, 0))

        if not HAS_WINRT:
            tk.Label(src_row, text="(winrt unavailable)", bg=BG_CARD, fg=TEXT_GRAY, font=("", 7)).pack(side="left", padx=(6, 0))

        # reset extremes
        reset_btn = tk.Button(body, text="Reset Min/Max", font=("", 8),
                              bg=BG_MID, fg=TEXT_GRAY, bd=0, padx=10, pady=2,
                              activebackground=BG_CARD, activeforeground=ACCENT_LIGHT,
                              cursor="hand2", command=self._reset_extremes)
        reset_btn.pack(anchor="w", pady=(6, 0))

        # ── HR source ───────────────────────────────────────────
        hr_card = CollapsibleCard(page, "Heart Rate Source")
        hr_card.pack(fill="x", pady=(0, 6))
        hr_body = hr_card.body

        tk.Label(hr_body, text="Source:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        self._qbtn(hr_body, "Heart rate source\nble = C20 watch via Bluetooth\nhyperate = HypeRate.io WebSocket")
        self.hr_source = tk.StringVar(value=cfg.get("hr_source", "ble"))
        self.hr_source.trace_add("write", self._toggle_hr_fields)
        self.hr_source.trace_add("write", self._live_sync)
        hr_sources = ["ble"] + (["hyperate"] if HAS_WS else [])
        hr_menu = tk.OptionMenu(hr_body, self.hr_source, *hr_sources)
        hr_menu.config(bg=BG_MID, fg=TEXT_WHITE, bd=0, highlightthickness=0, activebackground=BG_CARD)
        hr_menu["menu"].config(bg=BG_MID, fg=TEXT_WHITE)
        hr_menu.pack(side="left", padx=(6, 0))

        # HypeRate fields (hidden when BLE selected)
        self.hr_id_frame = tk.Frame(hr_body, bg=BG_CARD)
        self.hr_id_frame.pack(fill="x", pady=(2, 0))
        tk.Label(self.hr_id_frame, text="Device ID:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        self._qbtn(self.hr_id_frame, "HypeRate device ID\nget from hyperate.io dashboard")
        self.hyperate_id = tk.StringVar(value=cfg.get("hyperate_id", ""))
        self.hyperate_id.trace_add("write", self._live_sync)
        tk.Entry(self.hr_id_frame, textvariable=self.hyperate_id, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                 bd=0, highlightthickness=1, highlightbackground=BG_MID, highlightcolor=ACCENT, width=18).pack(side="left", padx=(4, 0))

        self.hr_key_frame = tk.Frame(hr_body, bg=BG_CARD)
        self.hr_key_frame.pack(fill="x", pady=(2, 0))
        tk.Label(self.hr_key_frame, text="API Key:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        self._qbtn(self.hr_key_frame, "HypeRate API key\nget from hyperate.io/api")
        self.hyperate_key = tk.StringVar(value=cfg.get("hyperate_key", ""))
        self.hyperate_key.trace_add("write", self._live_sync)
        tk.Entry(self.hr_key_frame, textvariable=self.hyperate_key, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                 bd=0, highlightthickness=1, highlightbackground=BG_MID, highlightcolor=ACCENT, width=28, show="*").pack(side="left", padx=(4, 0))

        self._toggle_hr_fields()

        self._theme_card(page, cfg)

        hist_btn = tk.Button(page, text="\U0001f4ca  HR History", font=("", 8),
                             bg=BG_MID, fg=TEXT_GRAY, bd=0, padx=10, pady=3,
                             activebackground=BG_CARD, activeforeground=ACCENT_LIGHT,
                             cursor="hand2", command=self._show_history)
        hist_btn.pack(anchor="w", pady=(4, 0))

    # ── page: log ─────────────────────────────────────────────
    def _build_log_page(self, page):
        card = tk.Frame(page, bg=BG_CARD, padx=10, pady=8)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="Activity Log", bg=BG_CARD, fg=ACCENT_LIGHT, font=("", 9, "bold"), anchor="w").pack(fill="x")
        self.log = tk.Text(card, bg=BG_INPUT, fg=TEXT_GRAY, font=("Consolas", 9),
                           bd=0, highlightthickness=1, highlightbackground=BG_MID,
                           highlightcolor=ACCENT, wrap="word")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.log.insert("end", "Ready \u2014 press Start to begin.\n")
        self.log.config(state="disabled")

    # ── dev panel ─────────────────────────────────────────────
    def _dev_build(self, page, cfg):
        self.dev_frame = tk.Frame(page, bg=BG_CARD, padx=10, pady=6)
        self.dev_frame.pack(fill="x", pady=(6, 0))
        self.dev_frame.pack_forget()  # hidden until unlocked

        tk.Label(self.dev_frame, text="\u2699 Dev Options", bg=BG_CARD, fg=ACCENT_LIGHT,
                 font=("", 9, "bold")).pack(anchor="w")

        grid = tk.Frame(self.dev_frame, bg=BG_CARD)
        grid.pack(fill="x", pady=(4, 0))

        fields = [
            ("Poll (s)", "poll_interval", 3, "How often to read the watch (seconds)"),
            ("Keepalive (s)", "keepalive_interval", 30, "How often to send keepalive (seconds)"),
            ("OSC Host", "osc_host", "127.0.0.1", "VRChat OSC host IP\nDefault: 127.0.0.1"),
            ("Port", "osc_port", 9000, "VRChat OSC port\nDefault: 9000"),
        ]
        self._dev_vars = {}
        for i, (label, key, default, tip) in enumerate(fields):
            f = tk.Frame(grid, bg=BG_CARD)
            f.grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 4))
            tk.Label(f, text=label, bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
            self._qbtn(f, tip)
            if isinstance(default, int):
                var = tk.IntVar(value=cfg.get(key, default))
                w = tk.Spinbox(grid, from_=5 if "keepalive" in key else 1,
                               to=120 if "keepalive" in key else (10 if "poll" in key else 65535),
                               textvariable=var, bg=BG_INPUT, fg=TEXT_WHITE, bd=0,
                               highlightthickness=1, highlightbackground=BG_MID,
                               highlightcolor=ACCENT, width=6, buttonbackground=BG_MID)
            elif isinstance(default, float):
                var = tk.DoubleVar(value=cfg.get(key, default))
                w = tk.Spinbox(grid, from_=1, to=10, increment=0.5, textvariable=var,
                               bg=BG_INPUT, fg=TEXT_WHITE, bd=0, highlightthickness=1,
                               highlightbackground=BG_MID, highlightcolor=ACCENT, width=6,
                               buttonbackground=BG_MID)
            else:
                var = tk.StringVar(value=cfg.get(key, default))
                w = tk.Entry(grid, textvariable=var, bg=BG_INPUT, fg=TEXT_WHITE, bd=0,
                             highlightthickness=1, highlightbackground=BG_MID,
                             highlightcolor=ACCENT, width=14)
            self._dev_vars[key] = var
            w.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 16), pady=2)

    def _show_dev(self):
        self.dev_frame.pack(fill="x", pady=(6, 0))

    # ── presets ────────────────────────────────────────────────
    def _load_presets(self, cfg):
        saved = cfg.get("presets", {})
        merged = dict(DEFAULT_PRESETS)
        merged.update(saved)
        return merged

    def _rebuild_preset_menu(self, presets=None):
        if presets is None:
            presets = self._load_presets(load_config())
        menu = self._preset_menu["menu"]
        menu.delete(0, "end")
        names = list(presets.keys())
        if not names:
            names = [""]
        for name in names:
            menu.add_command(label=name, command=lambda n=name: self._preset_var.set(n))
        self._preset_var.set(names[0] if names[0] else "")

    def _load_preset(self):
        name = self._preset_var.get()
        if not name:
            return
        presets = self._load_presets(load_config())
        if name in presets:
            self.template.set(presets[name])

    def _save_preset(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("Save Preset", "Preset name:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        cfg = load_config()
        presets = cfg.get("presets", {})
        presets[name] = self.template.get()
        cfg["presets"] = presets
        save_config(cfg)
        self._rebuild_preset_menu(self._load_presets(cfg))
        self._preset_var.set(name)

    def _delete_preset(self):
        name = self._preset_var.get()
        if not name:
            return
        if name in DEFAULT_PRESETS:
            self._write_log(f"  \u26a0\ufe0f Cannot delete default preset '{name}'")
            return
        from tkinter import messagebox
        if not messagebox.askyesno("Delete Preset", f"Delete '{name}'?", parent=self):
            return
        cfg = load_config()
        presets = cfg.get("presets", {})
        presets.pop(name, None)
        cfg["presets"] = presets
        save_config(cfg)
        self._rebuild_preset_menu(self._load_presets(cfg))

    # ── helpers ───────────────────────────────────────────────
    def _live_sync(self, *_):
        """Save config + sync template/toggles to running bridge instantly."""
        if not hasattr(self, "template"):
            return
        if not hasattr(self, "chk_hr") or not hasattr(self, "chk_batt"):
            return
        try:
            save_config(self._gather_config())
        except Exception as e:
            print(f"[live_sync] save error: {e}", flush=True)
        if self.bridge and self.bridge.running:
            self.bridge.template = self.template.get()
            self.bridge.status_text = self.egg_txt.get()
            if hasattr(self, "chk_hr"):
                self.bridge.show_hr = self.chk_hr.get()
                self.bridge.show_battery = self.chk_batt.get()
                self.bridge.show_system_stats = self.chk_sysstats.get()
                self.bridge.show_media = self.chk_media.get()
                self.bridge.show_extremes = self.chk_extremes.get()
                self.bridge.show_status = self.chk_egg.get()

    def _toggle_hr_fields(self, *_):
        show = self.hr_source.get() == "hyperate"
        for f in (self.hr_id_frame, self.hr_key_frame):
            if show:
                f.pack(fill="x", pady=(2, 0))
            else:
                f.pack_forget()

    def _reset_extremes(self):
        if self.bridge:
            self.bridge.reset_hr_extremes()

    def _check_blank_egg(self, _event=None):
        txt = self.egg_txt.get().strip().lower()
        if txt in BLANK_EGG_SECRETS and not self.egg_dev:
            self.egg_dev = True
            self._show_dev()
            self._write_log("  \U0001f3eb BlankEgg Dev Mode unlocked!")
            from tkinter import messagebox
            messagebox.showinfo("\U0001f3eb Egg", "u found the dev egggmoooodeee go to dev options")

    def _on_egg_toggle(self):
        mode = self.chk_egg.get()
        state = "normal" if mode else "disabled"
        self._egg_entry.configure(state=state)
        self._template_entry.configure(state="disabled" if mode else "normal")
        self._live_sync()

    def _insert_template(self, text):
        """Insert text at cursor in template entry; mirror to egg if toggled."""
        self._template_entry.insert("insert", text)
        if hasattr(self, "_mirror_egg_var") and self._mirror_egg_var.get():
            self._egg_entry.insert("insert", text)

    # ── polished BPM display ──────────────────────────────────
    def _build_bpm_display(self, page):
        self._disp_frame = tk.Frame(page, bg=BG_CARD, padx=10, pady=8)
        self._disp_frame.pack(fill="x", pady=(0, 6))
        self._bpm_canvas = tk.Canvas(self._disp_frame, height=110, bg=BG_CARD,
                                     highlightthickness=0, cursor="hand2")
        self._bpm_canvas.pack(fill="x")
        self._graph_canvas = tk.Canvas(self._disp_frame, height=60, bg=BG_CARD,
                                       highlightthickness=0)
        self._graph_canvas.pack(fill="x", pady=(2, 0))
        self._update_display()

    def _update_display(self):
        def _format(n):
            return str(int(n)) if n < 999 else "—"
        pw = self._bpm_canvas.winfo_width() or 400
        ph = 110
        self._bpm_canvas.delete("all")
        if self.bridge and self.bridge.running:
            bpm = self.bridge.bpm
            pulse = getattr(self.bridge, "_pulse", 0)
            if pulse > 0:
                self.bridge._pulse = max(0, pulse - 0.04)
            connected = bpm > 0
        else:
            bpm = 0
            pulse = 0
            connected = False

        # background
        self._bpm_canvas.create_rectangle(0, 0, pw, ph, fill=BG_CARD, outline="")

        # pulsing heart outline
        heart_size = 28 + (pulse * 12 if connected else 0)
        hx, hy = 40, ph // 2
        self._bpm_canvas.create_text(hx, hy, text="\u2764",
                                     fill=ACCENT_LIGHT if connected else TEXT_GRAY,
                                     font=("", int(heart_size)), anchor="center")

        # big BPM number
        color = ACCENT_LIGHT if connected else TEXT_GRAY
        self._bpm_canvas.create_text(pw // 2, ph // 2 - 4, text=_format(bpm),
                                     fill=color, font=("", 40, "bold"), anchor="center")

        # BPM label
        self._bpm_canvas.create_text(pw // 2, ph // 2 + 28, text="BPM",
                                     fill=TEXT_GRAY, font=("", 10), anchor="center")

        # min / max
        if self.bridge:
            self._bpm_canvas.create_text(pw - 14, ph // 2 - 12, text=f"\u25bc {_format(self.bridge.hr_min if self.bridge.hr_min != 999 else 0)}",
                                         fill=SUCCESS, font=("", 9), anchor="e")
            self._bpm_canvas.create_text(pw - 14, ph // 2 + 10, text=f"\u25b2 {_format(self.bridge.hr_max)}",
                                         fill=DANGER, font=("", 9), anchor="e")

        # ── graph ──
        gw = self._graph_canvas.winfo_width() or 400
        gh = 60
        self._graph_canvas.delete("all")
        self._graph_canvas.create_rectangle(0, 0, gw, gh, fill=BG_CARD, outline="")
        if self.bridge and len(self.bridge.bpm_history) >= 2:
            hist = self.bridge.bpm_history[-60:]
            mn = min(hist) - 5
            mx = max(hist) + 5
            if mx - mn < 10:
                mx = mn + 10
            pts = []
            for i, v in enumerate(hist):
                x = (i / (len(hist) - 1)) * gw
                y = gh - ((v - mn) / (mx - mn)) * (gh - 14) - 7
                pts.extend([x, y])
            if len(pts) >= 4:
                self._graph_canvas.create_line(pts, fill=ACCENT, width=2, smooth=True)
            # fill under line
            if len(pts) >= 2:
                fill_pts = [pts[0], pts[1], gw, gh, 0, gh]
                self._graph_canvas.create_polygon(fill_pts, fill=ACCENT, stipple="gray25", outline="")
        else:
            self._graph_canvas.create_text(gw // 2, gh // 2, text="Waiting for HR data\u2026",
                                           fill=TEXT_GRAY, font=("", 8))

        self.after(350, self._update_display)
        self._update_seek_bar()

    def _update_seek_bar(self):
        if not self.bridge:
            return
        src = self.media_source.get() if hasattr(self, "media_source") else "none"
        if src == "pear" and self.bridge.show_media:
            self._seek_frame.pack(fill="x", pady=(0, 2))
            dur = self.bridge.media_duration
            pos = self.bridge.media_position
            if dur > 0:
                prog = min(pos / dur, 1.0)
                pos_s = f"{int(pos // 60):02d}:{int(pos % 60):02d}"
                dur_s = f"{int(dur // 60):02d}:{int(dur % 60):02d}"
            else:
                prog = 0
                pos_s = "--:--"
                dur_s = "--:--"
            self._seek_lbl.config(text=f"\U0001f3b5  {pos_s} / {dur_s}")
            self._seek_bar["value"] = prog * 100
        else:
            self._seek_frame.pack_forget()

    def _pear_seek_popup(self):
        win = tk.Toplevel(self)
        win.title("Seek")
        win.configure(bg=BG_DARK)
        win.geometry("260x100")
        tk.Label(win, text="Seek to position (mm:ss):", bg=BG_DARK, fg=TEXT_WHITE, font=("", 9)).pack(pady=(12, 4))
        var = tk.StringVar()
        e = tk.Entry(win, textvariable=var, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE, bd=0, highlightthickness=1, highlightbackground=BG_MID, width=12)
        e.pack()
        e.focus()
        def do_seek():
            txt = var.get().strip()
            try:
                if ":" in txt:
                    m, s = txt.split(":")
                    secs = int(m) * 60 + int(s)
                else:
                    secs = int(txt)
                if self.bridge and self.bridge.media_source == "pear":
                    ok = pear_seek_to(self.bridge.pear_port, secs)
                    if ok:
                        self.bridge.media_position = secs
            except:
                pass
            win.destroy()
        tk.Button(win, text="Seek", command=do_seek, bg=ACCENT, fg=TEXT_WHITE, bd=0, padx=12, cursor="hand2").pack(pady=(6, 0))
        win.bind("<Return>", lambda e: do_seek())

    def _update_statusbar(self):
        running = self.bridge and self.bridge.running
        bpm = self.bridge.bpm if self.bridge else 0
        src = self.hr_source.get() if hasattr(self, "hr_source") else "ble"
        paused = self.bridge.paused if self.bridge else False
        if running and paused:
            self._status_label.config(text=f"\u23f8 Paused  \u2764\ufe0f {bpm}", fg="#eab308")
        elif running and bpm > 0:
            self._status_label.config(text=f"\u25cf Connected  \u2764\ufe0f {bpm}", fg=ACCENT_LIGHT)
        elif running:
            self._status_label.config(text="\u25cb Connecting\u2026", fg="yellow")
        else:
            self._status_label.config(text="\u25cf Idle", fg=TEXT_GRAY)
        if hasattr(self, "_dev_vars") and "osc_host" in self._dev_vars and "osc_port" in self._dev_vars:
            self._status_osc.config(text=f"OSC: {self._dev_vars['osc_host'].get()}:{self._dev_vars['osc_port'].get()}  |  HR: {src}")
        self.after(1000, self._update_statusbar)

    def _show_history(self):
        from datetime import datetime, date
        entries = load_history()
        if not entries:
            tk.messagebox.showinfo("HR History", "No history recorded yet.", parent=self)
            return
        # group by day
        days = {}
        for e in entries:
            d = datetime.fromisoformat(e["ts"]).strftime("%Y-%m-%d")
            days.setdefault(d, {"min": 999, "max": 0})
            days[d]["min"] = min(days[d]["min"], e["min"])
            days[d]["max"] = max(days[d]["max"], e["max"])
        win = tk.Toplevel(self)
        win.title("HR History")
        win.configure(bg=BG_DARK)
        win.geometry("500x400")
        win.minsize(400, 250)
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        for label, period_fn in [
            ("Daily", lambda: sorted(days.items(), reverse=True)[:60]),
            ("Weekly", lambda: self._group_history(entries, "%Y-W%W")),
            ("Monthly", lambda: self._group_history(entries, "%Y-%m")),
            ("Yearly", lambda: self._group_history(entries, "%Y")),
        ]:
            tab = tk.Frame(nb, bg=BG_DARK)
            nb.add(tab, text=label)
            data = period_fn()
            if not data:
                tk.Label(tab, text="No data", bg=BG_DARK, fg=TEXT_GRAY).pack(pady=20)
                continue
            text = tk.Text(tab, bg=BG_INPUT, fg=TEXT_WHITE, font=("Consolas", 9),
                           bd=0, highlightthickness=0, wrap="none")
            scroll = tk.Scrollbar(tab, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            text.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            text.insert("end", f"{'Period':<16} {'Min':>4}  {'Max':>4}\n")
            text.insert("end", "─" * 28 + "\n")
            for period, v in data:
                text.insert("end", f"{period:<16} {v['min']:>4}  {v['max']:>4}\n")
            text.config(state="disabled")

    def _group_history(self, entries, fmt):
        from datetime import datetime
        groups = {}
        for e in entries:
            k = datetime.fromisoformat(e["ts"]).strftime(fmt)
            groups.setdefault(k, {"min": 999, "max": 0})
            groups[k]["min"] = min(groups[k]["min"], e["min"])
            groups[k]["max"] = max(groups[k]["max"], e["max"])
        return sorted(groups.items(), reverse=True)

    # ── VRChat auto-start ─────────────────────────────────────
    @staticmethod
    def _is_vrchat_running():
        targets = ("VRChat", "vrchat", "vrmonitor", "steamvr")
        try:
            if IS_LINUX:
                out = os.popen("ps aux 2>/dev/null").read()
            else:
                out = os.popen("tasklist 2>nul").read()
            return any(t.lower() in out.lower() for t in targets)
        except:
            return False

    def _check_vrchat(self):
        if not getattr(self, "_autostart_var", None) or not self._autostart_var.get():
            self.after(10000, self._check_vrchat)
            return
        if self._is_vrchat_running():
            if not self.bridge or not self.bridge.running:
                if not getattr(self, "_autostarted_this_session", False):
                    self._autostarted_this_session = True
                    self._write_log("  \U0001f3ae VRChat/SteamVR detected, auto-starting\u2026")
                    self._toggle()
        else:
            self._autostarted_this_session = False
        self.after(10000, self._check_vrchat)

    # ── theme helpers ──────────────────────────────────────────
    def _on_body_resize(self, _event=None):
        c = self._body_canvas
        w = c.winfo_width() or 600
        h = c.winfo_height() or 400
        # reposition sidebar (fixed width)
        sw = self._sidebar_frame.winfo_reqwidth() or 52
        cw = max(0, w - sw)
        ch = max(0, h)
        c.coords(self._sidebar_wid, 0, 0)
        c.itemconfig(self._sidebar_wid, width=sw, height=ch)
        c.coords(self._content_wid, sw, 0)
        c.itemconfig(self._content_wid, width=cw, height=ch)
        # redraw gradient
        self._redraw_gradient(w, h)

    def _redraw_gradient(self, w, h):
        c = self._body_canvas
        c.delete("gradient")
        grad = GRADIENT_CFG
        if not grad.get("stops") or len(grad["stops"]) < 2:
            c.create_rectangle(0, 0, w, h, fill=BG_DARK, outline="", tags="gradient")
            return
        steps = 80
        gh = h
        stops = sorted(grad["stops"], key=lambda x: x["position"])
        if grad.get("type") == "radial":
            cx, cy = w / 2, h / 2
            r_max = max(w, h) / 2
            for i in range(steps):
                t = i / steps
                color = _gradient_color(t, stops)
                r = r_max * (1 - (steps - i) / steps)
                c.create_oval(cx - r, cy - r, cx + r, cy + r,
                              fill=color, outline="", tags="gradient")
        else:
            angle = grad.get("angle", 180)
            rad = math.radians(angle)
            if abs(rad % math.pi) < 0.01:
                for i in range(steps):
                    t = i / steps
                    color = _gradient_color(t, stops)
                    y1 = int(gh * t)
                    y2 = int(gh * (i + 1) / steps)
                    c.create_rectangle(0, y1, w, y2, fill=color, outline="", tags="gradient")
            else:
                diag = math.hypot(w, h)
                for i in range(steps):
                    t = i / steps
                    color = _gradient_color(t, stops)
                    cx, cy = w / 2, h / 2
                    offset = (t - 0.5) * diag
                    dx = offset * math.cos(rad)
                    dy = offset * math.sin(rad)
                    x0 = cx + dx - math.cos(rad + math.pi / 2) * diag / 2
                    y0 = cy + dy - math.sin(rad + math.pi / 2) * diag / 2
                    x1 = cx + dx + math.cos(rad + math.pi / 2) * diag / 2
                    y1 = cy + dy + math.sin(rad + math.pi / 2) * diag / 2
                    c.create_polygon(x0, y0, x1, y1, fill=color, outline="", tags="gradient")
        c.tag_lower("gradient")

    def _pick_color(self, var, btn):
        hex_color = var.get()
        result = tkc.askcolor(color=hex_color, title="Pick a color", parent=self)
        if result and result[1]:
            var.set(result[1])
            btn.configure(bg=result[1])

    def _theme_card(self, page, cfg):
        card = CollapsibleCard(page, "\U0001f3a8  Theme", collapsed=True)
        card.pack(fill="x", pady=(0, 6))
        body = card.body

        fallback = {"bg_dark": BG_DARK, "bg_mid": BG_MID, "bg_card": BG_CARD, "bg_input": BG_INPUT,
                     "accent": ACCENT, "accent_light": ACCENT_LIGHT, "text_white": TEXT_WHITE, "text_gray": TEXT_GRAY}
        color_keys = [
            ("bg_dark",     "Background"), ("bg_mid", "Mid BG"),
            ("bg_card",     "Card"),        ("bg_input", "Input"),
            ("accent",      "Accent"),      ("accent_light", "Accent Lt"),
            ("text_white",  "Text"),        ("text_gray", "Text Dim"),
        ]
        # wrap swatches in a frame so grid doesn't conflict with pack
        swatch_frame = tk.Frame(body, bg=BG_CARD)
        swatch_frame.pack(fill="x", pady=(4, 0))
        self._color_vars = {}
        for col, (cfg_key, label) in enumerate(color_keys):
            var = tk.StringVar(value=cfg.get(f"color_{cfg_key}", fallback[cfg_key]))
            var.trace_add("write", self._live_sync)
            self._color_vars[cfg_key] = var
            f = tk.Frame(swatch_frame, bg=BG_CARD)
            f.grid(row=0, column=col, padx=(0, 8), sticky="w")
            btn = tk.Button(f, text="  ", bg=var.get(), bd=1, relief="solid",
                            width=3, cursor="hand2", command=lambda: None)
            btn.pack(side="left")
            tk.Label(f, text=label, bg=BG_CARD, fg=TEXT_GRAY, font=("", 7)).pack(side="left", padx=(3, 0))
        for col, (cfg_key, _) in enumerate(color_keys):
            var = self._color_vars[cfg_key]
            f = swatch_frame.grid_slaves(row=0, column=col)[0]
            btn = f.winfo_children()[0]
            btn.config(command=lambda v=var, b=btn: self._pick_color(v, b))

        # ── Gradient Editor ──
        grad_section = tk.Frame(body, bg=BG_CARD, padx=4, pady=4)
        grad_section.pack(fill="x", pady=(4, 0))
        tk.Label(grad_section, text="Gradient:", bg=BG_CARD, fg=TEXT_GRAY,
                 font=("", 8, "bold")).pack(anchor="w")

        grad = cfg.get("gradient", dict(DEFAULT_GRADIENT))
        self._grad_stops = [dict(s) for s in grad.get("stops", [{"color": "#1a1a2e", "position": 0}, {"color": "#7c5cbf", "position": 100}])]
        self._grad_type = tk.StringVar(value=grad.get("type", "linear"))
        self._grad_angle = tk.IntVar(value=grad.get("angle", 180))

        # type + angle row
        def _on_grad_change(*_):
            self._grad_dirty = True
        ta_row = tk.Frame(grad_section, bg=BG_CARD)
        ta_row.pack(fill="x", pady=(2, 0))
        for gval, glabel in [("linear", "Linear"), ("radial", "Radial")]:
            rb = tk.Radiobutton(ta_row, text=glabel, variable=self._grad_type,
                                value=gval, bg=BG_CARD, fg=TEXT_WHITE, selectcolor=BG_INPUT,
                                activebackground=BG_CARD, activeforeground=TEXT_WHITE,
                                font=("", 8), cursor="hand2", command=_on_grad_change)
            rb.pack(side="left", padx=(0, 6))
        tk.Label(ta_row, text="Angle:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left", padx=(10, 2))
        tk.Spinbox(ta_row, from_=0, to=360, textvariable=self._grad_angle, width=4,
                   bg=BG_INPUT, fg=TEXT_WHITE, bd=0, highlightthickness=1,
                   highlightbackground=BG_MID, highlightcolor=ACCENT, buttonbackground=BG_MID,
                   command=_on_grad_change).pack(side="left")

        # color stops
        stops_frame = tk.Frame(grad_section, bg=BG_CARD)
        stops_frame.pack(fill="x", pady=(2, 0))
        self._grad_stops_ui = []
        def _rebuild_stops():
            for w in self._grad_stops_ui:
                for child in w.winfo_children():
                    child.destroy()
                w.destroy()
            self._grad_stops_ui.clear()
            for i, s in enumerate(self._grad_stops):
                sf = tk.Frame(stops_frame, bg=BG_CARD)
                sf.pack(fill="x", pady=(1, 0))
                self._grad_stops_ui.append(sf)
                color_var = tk.StringVar(value=s["color"])
                pos_var = tk.IntVar(value=s["position"])
                def make_setters(idx, cv, pv):
                    def set_color():
                        result = tkc.askcolor(color=cv.get(), title="Stop Color", parent=self)
                        if result and result[1]:
                            cv.set(result[1])
                            self._grad_stops[idx]["color"] = result[1]
                            self._apply_gradient()
                    def set_pos(*_):
                        self._grad_stops[idx]["position"] = pv.get()
                        self._apply_gradient()
                    pv.trace_add("write", lambda *_: (_on_grad_change(), self._apply_gradient()))
                    return set_color, set_pos
                set_color, set_pos = make_setters(i, color_var, pos_var)
                swatch = tk.Button(sf, text="  ", bg=s["color"], bd=1, relief="solid",
                                   width=2, cursor="hand2", command=set_color)
                swatch.pack(side="left")
                tk.Label(sf, text="Pos:", bg=BG_CARD, fg=TEXT_GRAY, font=("", 7)).pack(side="left", padx=(3, 0))
                tk.Spinbox(sf, from_=0, to=100, textvariable=pos_var, width=4,
                           bg=BG_INPUT, fg=TEXT_WHITE, bd=0, highlightthickness=1,
                           highlightbackground=BG_MID, highlightcolor=ACCENT, buttonbackground=BG_MID).pack(side="left")
                if len(self._grad_stops) > 2:
                    def make_del(idx):
                        def do_del():
                            self._grad_stops.pop(idx)
                            _rebuild_stops()
                            self._apply_gradient()
                        return do_del
                    tk.Button(sf, text="\u2716", font=("", 7), bg=BG_MID, fg=TEXT_GRAY, bd=0,
                              padx=3, cursor="hand2", command=make_del(i)).pack(side="left", padx=(4, 0))
        _rebuild_stops()

        # add stop + reset buttons
        btn_row = tk.Frame(grad_section, bg=BG_CARD)
        btn_row.pack(fill="x", pady=(2, 0))
        tk.Button(btn_row, text="+ Add Stop", font=("", 8), bg=BG_MID, fg=TEXT_WHITE, bd=0, padx=8,
                  cursor="hand2", command=lambda: (
                      self._grad_stops.append({"color": "#7c5cbf", "position": 50}),
                      _rebuild_stops(), self._apply_gradient()
                  )).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="\U0001f441 Preview", font=("", 8), bg=BG_MID, fg=TEXT_WHITE, bd=0, padx=8,
                  cursor="hand2", command=self._gradient_preview_new).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Reset", font=("", 8), bg=BG_MID, fg=TEXT_GRAY, bd=0, padx=8,
                  cursor="hand2", command=lambda: (
                      setattr(self, "_grad_stops", [dict(s) for s in DEFAULT_GRADIENT["stops"]]),
                      self._grad_type.set(DEFAULT_GRADIENT["type"]),
                      self._grad_angle.set(DEFAULT_GRADIENT["angle"]),
                      _rebuild_stops(), self._apply_gradient()
                  )).pack(side="left")

        self._grad_dirty = False
        self._apply_gradient = self._make_gradient_applier()
        # live preview label
        self._grad_preview_lbl = tk.Label(grad_section, text="\u25cf Gradient active", bg=BG_CARD,
                                          fg=SUCCESS, font=("", 7))
        self._grad_preview_lbl.pack(anchor="w", pady=(2, 0))

    def _make_gradient_applier(self):
        def apply():
            grad = {
                "type": self._grad_type.get(),
                "angle": self._grad_angle.get(),
                "stops": [{"color": s["color"], "position": s["position"]} for s in self._grad_stops],
            }
            global GRADIENT_CFG
            GRADIENT_CFG.clear()
            GRADIENT_CFG.update(grad)
            self._live_sync()
            w = self._body_canvas.winfo_width() or 600
            h = self._body_canvas.winfo_height() or 400
            self._redraw_gradient(w, h)
            # tint page backgrounds from gradient
            stops = sorted(grad.get("stops", []), key=lambda x: x["position"])
            if stops:
                mid = _gradient_color(0.5, stops)
                for p in self._pages:
                    p.configure(bg=mid)
                self._content_frame.configure(bg=mid)
            css = _gradient_css(grad)
            if css != "none":
                self._grad_preview_lbl.config(text=f"\u25cf {css[:50]}\u2026" if len(css) > 50 else f"\u25cf {css}", fg=SUCCESS)
            else:
                self._grad_preview_lbl.config(text="\u25cf Gradient active", fg=SUCCESS)
        return apply

    def _gradient_preview_new(self):
        top = tk.Toplevel(self)
        top.title("Gradient Preview")
        top.geometry("360x280")
        top.configure(bg=BG_DARK)
        top.resizable(False, False)
        grad = {
            "type": self._grad_type.get(),
            "angle": self._grad_angle.get(),
            "stops": [{"color": s["color"], "position": s["position"]} for s in self._grad_stops],
        }
        canvas = tk.Canvas(top, width=340, height=180, highlightthickness=0, bg=BG_DARK)
        canvas.pack(padx=10, pady=(10, 4))
        steps = 100
        cw, ch = 340, 180
        if grad["type"] == "radial":
            cx, cy = cw / 2, ch / 2
            r_max = max(cw, ch) / 2
            for i in range(steps):
                t = i / steps
                color = _gradient_color(t, sorted(grad["stops"], key=lambda x: x["position"]))
                r = r_max * (1 - (steps - i) / steps)
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")
        else:
            angle = grad.get("angle", 180)
            rad = math.radians(angle)
            diag = math.hypot(cw, ch)
            for i in range(steps):
                t = i / steps
                color = _gradient_color(t, sorted(grad["stops"], key=lambda x: x["position"]))
                cx, cy = cw / 2, ch / 2
                offset = (t - 0.5) * diag
                dx = offset * math.cos(rad)
                dy = offset * math.sin(rad)
                x0 = cx + dx - math.cos(rad + math.pi / 2) * diag / 2
                y0 = cy + dy - math.sin(rad + math.pi / 2) * diag / 2
                x1 = cx + dx + math.cos(rad + math.pi / 2) * diag / 2
                y1 = cy + dy + math.sin(rad + math.pi / 2) * diag / 2
                canvas.create_polygon(x0, y0, x1, y1, fill=color, outline="")
        canvas.create_text(cw // 2, ch // 2, text="C20  →  VRChat Bridge",
                           fill="white", font=("", 11, "bold"))
        css = _gradient_css(grad)
        info = tk.Label(top, text=css, bg=BG_DARK, fg=TEXT_GRAY, font=("Consolas", 7), wraplength=340)
        info.pack(padx=10)
        tk.Button(top, text="Close", bg=BG_MID, fg=TEXT_WHITE,
                  bd=0, cursor="hand2", command=top.destroy).pack(pady=4)

    def _write_log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def write_log(self, msg):
        self.after(0, self._write_log, msg)

    # ── config gathering ────────────────────────────────────────
    def _gather_config(self):
        """Collect all current settings into a flat dict for saving."""
        d = {
            "address": self.addr.get(),
            "template": self.template.get(),
            "hr": self.chk_hr.get(),
            "battery": self.chk_batt.get(),
            "system_stats": self.chk_sysstats.get(),
            "media": self.chk_media.get(),
            "media_source": self.media_source.get(),
            "pear_port": self.pear_port.get(),
            "hr_source": self.hr_source.get(),
            "hyperate_id": self.hyperate_id.get(),
            "hyperate_key": self.hyperate_key.get(),
            "extremes": self.chk_extremes.get(),
            "egg": self.chk_egg.get(),
            "egg_text": self.egg_txt.get(),
            "blank_egg": self.egg_dev,
            "mirror_egg": self._mirror_egg_var.get() if hasattr(self, "_mirror_egg_var") else False,
            "autostart_vrchat": self._autostart_var.get() if hasattr(self, "_autostart_var") else False,
            "poll_interval": self._dev_vars["poll_interval"].get() if self.egg_dev else 3,
            "keepalive_interval": self._dev_vars["keepalive_interval"].get() if self.egg_dev else 30,
            "osc_host": self._dev_vars["osc_host"].get() if self.egg_dev else "127.0.0.1",
            "osc_port": self._dev_vars["osc_port"].get() if self.egg_dev else 9000,
            "color_bg_dark": self._color_vars["bg_dark"].get(),
            "color_bg_mid": self._color_vars["bg_mid"].get(),
            "color_bg_card": self._color_vars["bg_card"].get(),
            "color_bg_input": self._color_vars["bg_input"].get(),
            "color_accent": self._color_vars["accent"].get(),
            "color_accent_light": self._color_vars["accent_light"].get(),
            "color_text_white": self._color_vars["text_white"].get(),
            "color_text_gray": self._color_vars["text_gray"].get(),
            "gradient": dict(GRADIENT_CFG),
            "presets": load_config().get("presets", {}),
        }
        if self.bridge and self.bridge.running and self.bridge.hr_max > 0:
            record_history(self.bridge.bpm, self.bridge.hr_min, self.bridge.hr_max)
        return d

    # ── toggle bridge ─────────────────────────────────────────
    def _toggle(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
            self.btn.config(text="\u25b6  Start", bg=SUCCESS)
            self._pause_btn.pack_forget()
            self._pause_btn.config(text="\u23f8  Pause", bg=BG_MID, fg=TEXT_WHITE)
            save_config(self._gather_config())
        else:
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")
            poll = self._dev_vars["poll_interval"].get() if self.egg_dev else 3
            ka = self._dev_vars["keepalive_interval"].get() if self.egg_dev else 30
            host = self._dev_vars["osc_host"].get() if self.egg_dev else "127.0.0.1"
            port = self._dev_vars["osc_port"].get() if self.egg_dev else 9000
            self.bridge = HRBridge(
                address=self.addr.get(),
                template=self.template.get(),
                log_cb=self.write_log,
                show_hr=self.chk_hr.get(),
                show_battery=self.chk_batt.get(),
                show_system_stats=self.chk_sysstats.get(),
                show_media=self.chk_media.get(),
                media_source=self.media_source.get(),
                pear_port=self.pear_port.get(),
                hr_source=self.hr_source.get(),
                hyperate_id=self.hyperate_id.get(),
                hyperate_key=self.hyperate_key.get(),
                show_extremes=self.chk_extremes.get(),
                show_status=self.chk_egg.get(),
                status_text=self.egg_txt.get(),
                poll_interval=poll,
                keepalive_interval=ka,
                osc_host=host,
                osc_port=port,
            )
            self.bridge.start()
            self.btn.config(text="\u25a0  Stop", bg=DANGER)
            self._pause_btn.pack(side="left", padx=(8, 0))

    def _toggle_pause(self):
        if not self.bridge or not self.bridge.running:
            return
        self.bridge.paused = not self.bridge.paused
        if self.bridge.paused:
            self._pause_btn.config(text="\u25b6  Resume", bg="#eab308", fg="#000")
        else:
            self._pause_btn.config(text="\u23f8  Pause", bg=BG_MID, fg=TEXT_WHITE)

    def _on_close(self):
        try:
            save_config(self._gather_config())
        except Exception as e:
            print(f"[_on_close] save error: {e}", flush=True)
        if self.bridge and self.bridge.running:
            self.bridge.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

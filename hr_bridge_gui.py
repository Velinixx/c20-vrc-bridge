#!/usr/bin/env python3
"""C20 Smartwatch → VRChat OSC Heart Rate Bridge — GUI Edition."""
import asyncio
import json
import os
import platform
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import Optional

from bleak import BleakClient, BleakScanner
from pythonosc.udp_client import SimpleUDPClient

# media detection (Windows only)
try:
    import winrt.windows.media.control as wmc
    HAS_MEDIA = True
except ImportError:
    HAS_MEDIA = False

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


if HAS_MEDIA:
    async def get_media_info():
        try:
            session = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            s = session.get_current_session()
            if s is None:
                return None
            info = await s.try_get_media_properties_async()
            return {"title": info.title, "artist": info.artist}
        except:
            return None
else:
    async def get_media_info():
        return None


# ── Bridge Engine ───────────────────────────────────────────
class HRBridge:
    def __init__(self, address, template, log_cb, show_hr=True, show_battery=True, show_media=False, show_status=False, status_text="", poll_interval=3, keepalive_interval=30, osc_host="127.0.0.1", osc_port=9000):
        self.address = address
        self.template = template
        self.log = log_cb
        self.show_hr = show_hr
        self.show_battery = show_battery
        self.show_media = show_media
        self.show_status = show_status
        self.status_text = status_text
        self.poll_interval = poll_interval
        self.keepalive_interval = keepalive_interval
        self.osc = SimpleUDPClient(osc_host, osc_port)
        self.bpm = 0
        self.battery = 0
        self.running = False
        self.song = ""
        self.artist = ""
        self._client = None

    def log_msg(self, msg):
        self.log(msg)

    def _build_chatbox(self):
        if self.show_status and self.status_text.strip():
            return self.status_text.strip()
        text = self.template
        text = text.replace("{bpm}", str(self.bpm))
        text = text.replace("{battery}", str(self.battery))
        if self.show_media:
            media_parts = []
            if self.song:
                media_parts.append(self.song)
            if self.artist:
                media_parts.append(self.artist)
            media_str = " — ".join(media_parts) if media_parts else ""
            text = text.replace("{song}", media_str).replace("{artist}", self.artist).replace("{title}", self.song)
        else:
            text = text.replace("{song}", "").replace("{artist}", "").replace("{title}", "")
        return " ".join(text.split()).strip()

    def _log_line(self):
        parts = []
        if self.show_hr:
            parts.append(f"\u2764\ufe0f {self.bpm} BPM")
        if self.show_battery:
            parts.append(f"\U0001f50b {self.battery}%")
        if self.show_media and (self.song or self.artist):
            parts.append(f"\U0001f3b5 {self.song or self.artist}")
        self.log_msg("  " + "  ".join(parts))

    def send_osc(self):
        chat = self._build_chatbox()
        self.osc.send_message("/avatar/parameters/isHRConnected", True)
        self.osc.send_message("/avatar/parameters/HR", int(self.bpm))
        self.osc.send_message("/avatar/parameters/floatHR", min(self.bpm / 255.0, 1.0))
        self.osc.send_message("/avatar/parameters/HRBattery", self.battery)
        self.osc.send_message("/avatar/parameters/HRBatteryFloat", self.battery / 100.0)
        if chat:
            self.osc.send_message("/chatbox/input", [chat, True])
        self._log_line()

    def on_hr(self, _h, data):
        if len(data) < 2:
            return
        flags = data[0]
        bpm = data[2] if (flags & 1) else data[1]
        if 20 <= bpm <= 250:
            self.bpm = bpm
            self.send_osc()

    def on_fee3(self, _h, data):
        if len(data) < 5 or data[0] != 0xFE or data[1] != 0xEA:
            return
        cmd = data[4]
        if cmd == CMD_TRIGGER_HR and len(data) >= 6:
            bpm = data[5]
            if 20 <= bpm <= 250:
                self.bpm = bpm
                self.send_osc()

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
                if self.show_media and HAS_MEDIA:
                    media = await get_media_info()
                    if media:
                        self.song = media.get("title", "")
                        self.artist = media.get("artist", "")
            self.log_msg("  \u26a0\ufe0f Disconnected")

    async def run_forever(self):
        while self.running:
            try:
                await self.run_once()
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

    def stop(self):
        self.running = False
        if self._client and self._client.is_connected:
            asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.log_msg("  \u23f9\ufe0f Stopped")


# ── Config ──────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f)


# ── GUI ─────────────────────────────────────────────────────
BLANK_EGG_SECRETS = ("boihanny", "sr4 series")

class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=12)
        self.root = root
        self.root.title("C20 HR Bridge")
        self.root.resizable(False, False)
        self.bridge = None
        self.egg_dev = False
        cfg = load_config()
        self._build(cfg)
        self.grid()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── build ────────────────────────────────────────────────
    def _build(self, cfg):
        # title
        ttk.Label(self, text="C20 \u2192 VRChat Heart Rate Bridge", font=("", 11, "bold")).grid(row=0, column=0, columnspan=3, pady=(0, 8))

        # ── address row ──
        ttk.Label(self, text="Watch Address:").grid(row=1, column=0, sticky="w", pady=2)
        self.addr = tk.StringVar(value=cfg.get("address", DEFAULT_ADDR))
        ttk.Entry(self, textvariable=self.addr, width=22).grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=2)

        # ── toggles ──
        toggles = ttk.LabelFrame(self, text="Features", padding=8)
        toggles.grid(row=2, column=0, columnspan=3, sticky="ew", pady=6)

        self.chk_hr = tk.BooleanVar(value=cfg.get("hr", True))
        ttk.Checkbutton(toggles, text="Heart Rate", variable=self.chk_hr).grid(row=0, column=0, sticky="w", padx=4)

        self.chk_batt = tk.BooleanVar(value=cfg.get("battery", True))
        ttk.Checkbutton(toggles, text="Battery", variable=self.chk_batt).grid(row=0, column=1, sticky="w", padx=4)

        self.chk_media = tk.BooleanVar(value=cfg.get("media", False))
        ttk.Checkbutton(toggles, text="Media Info", variable=self.chk_media).grid(row=0, column=2, sticky="w", padx=4)
        if not HAS_MEDIA:
            ttk.Label(toggles, text="(Win only)", font=("", 8), foreground="gray").grid(row=0, column=3, padx=(0, 4))

        self.chk_egg = tk.BooleanVar(value=cfg.get("egg", False))
        ttk.Checkbutton(toggles, text="Egg Mode", variable=self.chk_egg, command=self._on_egg_toggle).grid(row=0, column=4, sticky="w", padx=4)

        # ── egg text / template ──
        self.egg_frame = ttk.Frame(self)
        self.egg_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Label(self.egg_frame, text="Egg Text:", font=("", 8)).grid(row=0, column=0, sticky="w")
        self.egg_txt = tk.StringVar(value=cfg.get("egg_text", ""))
        egg_entry = ttk.Entry(self.egg_frame, textvariable=self.egg_txt, width=40)
        egg_entry.grid(row=0, column=1, padx=6, sticky="ew")
        egg_entry.bind("<KeyRelease>", self._check_blank_egg)
        ttk.Label(self.egg_frame, text="(short = tiny chatbox)", font=("", 7), foreground="gray").grid(row=0, column=2, padx=(0, 4))

        self.template_frame = ttk.Frame(self)
        self.template_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Label(self.template_frame, text="Chatbox Format:", font=("", 8)).grid(row=0, column=0, sticky="w")
        self.template = tk.StringVar(value=cfg.get("template", DEFAULT_TEMPLATE))
        ttk.Entry(self.template_frame, textvariable=self.template, width=40).grid(row=0, column=1, padx=6, sticky="ew")

        # placeholder hint
        ttk.Label(self, text="Placeholders: {bpm} {battery} {song} {artist} {title}", font=("", 7), foreground="gray").grid(row=5, column=0, columnspan=3, sticky="w")

        # ── dev frame (hidden until egg dev unlocked) ──
        self.dev_frame = ttk.LabelFrame(self, text="\u2699 Dev Options", padding=8)
        self.dev_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=4)
        self.dev_frame.grid_remove()

        # restore blank egg dev mode from config
        if cfg.get("blank_egg", False):
            self.egg_dev = True
            self.dev_frame.grid()

        self._on_egg_toggle()

        ttk.Label(self.dev_frame, text="Poll Interval (s):", font=("", 8)).grid(row=0, column=0, sticky="w")
        self.dev_poll = tk.DoubleVar(value=cfg.get("poll_interval", 3))
        ttk.Spinbox(self.dev_frame, from_=1, to=10, increment=0.5, textvariable=self.dev_poll, width=5).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(self.dev_frame, text="Keepalive (s):", font=("", 8)).grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.dev_keepalive = tk.IntVar(value=cfg.get("keepalive_interval", 30))
        ttk.Spinbox(self.dev_frame, from_=5, to=120, increment=5, textvariable=self.dev_keepalive, width=5).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(self.dev_frame, text="OSC Host:", font=("", 8)).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.dev_osc_host = tk.StringVar(value=cfg.get("osc_host", "127.0.0.1"))
        ttk.Entry(self.dev_frame, textvariable=self.dev_osc_host, width=15).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))

        ttk.Label(self.dev_frame, text="Port:", font=("", 8)).grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(4, 0))
        self.dev_osc_port = tk.IntVar(value=cfg.get("osc_port", 9000))
        ttk.Spinbox(self.dev_frame, from_=1024, to=65535, textvariable=self.dev_osc_port, width=6).grid(row=1, column=3, sticky="w", padx=4, pady=(4, 0))

        # ── start/stop ──
        btn_row = ttk.Frame(self)
        btn_row.grid(row=7, column=0, columnspan=3, pady=8)
        self.btn = ttk.Button(btn_row, text="\u25b6 Start", command=self._toggle)
        self.btn.pack(side="left", padx=4)

        # ── log ──
        self.log = scrolledtext.ScrolledText(self, width=62, height=14, font=("Consolas", 9))
        self.log.grid(row=8, column=0, columnspan=3)
        self.log.insert("end", "Ready \u2014 press Start to begin.\n")
        self.log.see("end")

    def _check_blank_egg(self, _event=None):
        txt = self.egg_txt.get().strip().lower()
        if txt in BLANK_EGG_SECRETS and not self.egg_dev:
            self.egg_dev = True
            self.dev_frame.grid()
            self.write_log("  \U0001f3eb BlankEgg Dev Mode unlocked!")
            from tkinter import messagebox
            messagebox.showinfo("\U0001f3eb Egg", "u found the dev egggmoooodeee go to dev options")
        elif txt not in BLANK_EGG_SECRETS and self.egg_dev:
            pass

    def _on_egg_toggle(self):
        mode = self.chk_egg.get()
        state = "normal" if mode else "disabled"
        for child in self.egg_frame.winfo_children():
            child.configure(state=state)
        for child in self.template_frame.winfo_children():
            child.configure(state="disabled" if mode else "normal")
        if mode and self.egg_dev:
            self.dev_frame.grid()
        elif not mode:
            pass

    # ── logging ──────────────────────────────────────────────
    def write_log(self, msg):
        self.root.after(0, self._insert_log, msg)

    def _insert_log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    # ── toggle ───────────────────────────────────────────────
    def _toggle(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
            self.btn.config(text="\u25b6 Start")
            save_config({
                "address": self.addr.get(),
                "template": self.template.get(),
                "hr": self.chk_hr.get(),
                "battery": self.chk_batt.get(),
                "media": self.chk_media.get(),
                "egg": self.chk_egg.get(),
                "egg_text": self.egg_txt.get(),
                "blank_egg": self.egg_dev,
                "poll_interval": self.dev_poll.get() if self.egg_dev else 3,
                "keepalive_interval": self.dev_keepalive.get() if self.egg_dev else 30,
                "osc_host": self.dev_osc_host.get() if self.egg_dev else "127.0.0.1",
                "osc_port": self.dev_osc_port.get() if self.egg_dev else 9000,
            })
        else:
            self.log.delete("1.0", "end")
            poll = self.dev_poll.get() if self.egg_dev else 3
            ka = self.dev_keepalive.get() if self.egg_dev else 30
            host = self.dev_osc_host.get() if self.egg_dev else "127.0.0.1"
            port = self.dev_osc_port.get() if self.egg_dev else 9000
            self.bridge = HRBridge(
                address=self.addr.get(),
                template=self.template.get(),
                log_cb=self.write_log,
                show_hr=self.chk_hr.get(),
                show_battery=self.chk_batt.get(),
                show_media=self.chk_media.get(),
                show_status=self.chk_egg.get(),
                status_text=self.egg_txt.get(),
                poll_interval=poll,
                keepalive_interval=ka,
                osc_host=host,
                osc_port=port,
            )
            self.bridge.start()
            self.btn.config(text="\u25a0 Stop")

    def _on_close(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

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
    def __init__(self, address, template, log_cb, show_hr=True, show_battery=True, show_media=False, show_status=False, show_extremes=True, status_text="", poll_interval=3, keepalive_interval=30, osc_host="127.0.0.1", osc_port=9000):
        self.address = address
        self.template = template
        self.log = log_cb
        self.show_hr = show_hr
        self.show_battery = show_battery
        self.show_media = show_media
        self.show_status = show_status
        self.show_extremes = show_extremes
        self.status_text = status_text
        self.poll_interval = poll_interval
        self.keepalive_interval = keepalive_interval
        self.osc = SimpleUDPClient(osc_host, osc_port)
        self.bpm = 0
        self.hr_min = 999
        self.hr_max = 0
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
        else:
            text = text.replace("{song}", "").replace("{artist}", "").replace("{title}", "")
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
        self.log_msg("  " + "  ".join(parts))

    def send_osc(self):
        chat = self._build_chatbox()
        self.osc.send_message("/avatar/parameters/isHRConnected", True)
        self.osc.send_message("/avatar/parameters/HR", int(self.bpm))
        self.osc.send_message("/avatar/parameters/floatHR", min(self.bpm / 255.0, 1.0))
        self.osc.send_message("/avatar/parameters/HRBattery", self.battery)
        self.osc.send_message("/avatar/parameters/HRBatteryFloat", self.battery / 100.0)
        self.osc.send_message("/avatar/parameters/HRMin", int(self.hr_min if self.hr_min != 999 else self.bpm))
        self.osc.send_message("/avatar/parameters/HRMax", int(self.hr_max))
        if chat:
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
        if bpm < self.hr_min:
            self.hr_min = bpm
        if bpm > self.hr_max:
            self.hr_max = bpm
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
    style.map("Danger.TButton", background=[("active", "#f87171")])
    style.configure("TScrollbar", background=BG_MID, troughcolor=BG_DARK, bordercolor=BG_MID, arrowcolor=TEXT_GRAY)


class Page(tk.Frame):
    """Base page with a card-style container."""
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG_DARK, **kw)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("C20 HR Bridge")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        setup_style()

        self.bridge = None
        self.egg_dev = False
        cfg = load_config()

        # ── top bar ──────────────────────────────────────────
        top = tk.Frame(self, bg=BG_MID, height=48)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="C20  →  VRChat Bridge", bg=BG_MID, fg=ACCENT_LIGHT, font=("", 11, "bold")).pack(side="left", padx=14, pady=10)

        # ── body: sidebar + content ──────────────────────────
        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill="both", expand=True)

        # sidebar
        sidebar = tk.Frame(body, bg=BG_MID, width=52)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        icons = ["\u2764", "\u2699", "\u2630"]
        tips  = ["Status", "Features", "Log"]
        self.nav_btns = []
        for i, (ico, tip) in enumerate(zip(icons, tips)):
            btn = tk.Button(sidebar, text=ico, font=("", 16), bg=BG_MID, fg=TEXT_GRAY,
                            bd=0, activebackground=BG_CARD, activeforeground=ACCENT_LIGHT,
                            cursor="hand2", relief="flat")
            btn.pack(pady=(12 if i == 0 else 4, 4))
            self.nav_btns.append(btn)

        # content area
        content = tk.Frame(body, bg=BG_DARK, padx=14, pady=10)
        content.pack(side="right", fill="both", expand=True)

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

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── page switching ────────────────────────────────────────
    def _show_page(self, idx):
        for i, p in enumerate(self._pages):
            p.pack_forget() if i != idx else p.pack(fill="both", expand=True)
        for i, btn in enumerate(self.nav_btns):
            btn.config(bg=BG_CARD if i == idx else BG_MID, fg=ACCENT_LIGHT if i == idx else TEXT_GRAY)

    def _card(self, parent, text):
        f = tk.Frame(parent, bg=BG_CARD, padx=10, pady=8)
        tk.Label(f, text=text, bg=BG_CARD, fg=ACCENT_LIGHT, font=("", 9, "bold"), anchor="w").pack(fill="x")
        return f

    # ── page: status ──────────────────────────────────────────
    def _build_status_page(self, page, cfg):
        # address
        card = self._card(page, "Watch")
        addr_row = tk.Frame(card, bg=BG_CARD)
        addr_row.pack(fill="x", pady=(6, 0))
        tk.Label(addr_row, text="BLE Address", bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).pack(side="left")
        self.addr = tk.StringVar(value=cfg.get("address", DEFAULT_ADDR))
        tk.Entry(addr_row, textvariable=self.addr, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                 bd=0, highlightthickness=1, highlightbackground=BG_MID, highlightcolor=ACCENT, width=24).pack(side="right")

        # template
        card2 = self._card(page, "Chatbox Format")
        self.template = tk.StringVar(value=cfg.get("template", DEFAULT_TEMPLATE))
        self._template_entry = tk.Entry(card2, textvariable=self.template, bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                                        bd=0, highlightthickness=1, highlightbackground=BG_MID, highlightcolor=ACCENT, width=44)
        self._template_entry.pack(fill="x", pady=(6, 2))
        tk.Label(card2, text="{bpm} {hr_min} {hr_max} {battery} {song} {artist} {title}",
                 bg=BG_CARD, fg=TEXT_GRAY, font=("", 7)).pack(anchor="w")

        # egg mode
        self.egg_frame = tk.Frame(page, bg=BG_DARK)
        self.egg_frame.pack(fill="x", pady=(0, 0))
        self.egg_txt = tk.StringVar(value=cfg.get("egg_text", ""))
        self._egg_entry = tk.Entry(self.egg_frame, textvariable=self.egg_txt, bg=BG_INPUT, fg=TEXT_WHITE,
                                   insertbackground=TEXT_WHITE, bd=0, highlightthickness=1,
                                   highlightbackground=BG_MID, highlightcolor=ACCENT, width=44)
        self._egg_entry.pack(side="left", padx=(0, 6))
        self._egg_entry.bind("<KeyRelease>", self._check_blank_egg)
        tk.Label(self.egg_frame, text="Egg text", bg=BG_DARK, fg=TEXT_GRAY, font=("", 7)).pack(side="left")

        # start / stop
        btn_frame = tk.Frame(page, bg=BG_DARK)
        btn_frame.pack(fill="x", pady=(10, 0))
        self.btn = tk.Button(btn_frame, text="\u25b6  Start", font=("", 10, "bold"),
                             bg=SUCCESS, fg="#000", bd=0, padx=18, pady=4,
                             activebackground="#6ee7a0", cursor="hand2",
                             command=self._toggle)
        self.btn.pack(side="left")

        # dev panel
        self._dev_build(page, cfg)

    # ── page: features ────────────────────────────────────────
    def _build_features_page(self, page, cfg):
        card = tk.Frame(page, bg=BG_CARD, padx=10, pady=8)
        card.pack(fill="x")
        tk.Label(card, text="Toggles", bg=BG_CARD, fg=ACCENT_LIGHT, font=("", 9, "bold"), anchor="w").pack(fill="x")

        body = tk.Frame(card, bg=BG_CARD)
        body.pack(fill="x", pady=(6, 0))

        toggles_data = [
            ("\u2764  Heart Rate", "hr", True),
            ("\U0001f50b  Battery", "battery", True),
            ("\U0001f3b5  Media Info", "media", False),
            ("\U0001f7e2  Min / Max HR", "extremes", True),
            ("\U0001f95a  Egg Mode", "egg", False),
        ]
        self._toggles_vars = {}
        for i, (label, key, default) in enumerate(toggles_data):
            var = tk.BooleanVar(value=cfg.get(key, default))
            self._toggles_vars[key] = var
            cb = tk.Checkbutton(body, text=label, variable=var,
                                bg=BG_CARD, fg=TEXT_WHITE, selectcolor=BG_INPUT,
                                activebackground=BG_CARD, activeforeground=TEXT_WHITE,
                                font=("", 9), cursor="hand2")
            cb.pack(side="left", padx=(0, 12), pady=2)
            if key == "egg":
                cb.config(command=self._on_egg_toggle)

        self.chk_hr = self._toggles_vars["hr"]
        self.chk_batt = self._toggles_vars["battery"]
        self.chk_media = self._toggles_vars["media"]
        self.chk_extremes = self._toggles_vars["extremes"]
        self.chk_egg = self._toggles_vars["egg"]

        if not HAS_MEDIA:
            tk.Label(body, text="(Win only)", bg=BG_CARD, fg=TEXT_GRAY, font=("", 7)).pack(side="left")

        # reset extremes
        reset_btn = tk.Button(card, text="Reset Min/Max", font=("", 8),
                              bg=BG_MID, fg=TEXT_GRAY, bd=0, padx=10, pady=2,
                              activebackground=BG_CARD, activeforeground=ACCENT_LIGHT,
                              cursor="hand2", command=self._reset_extremes)
        reset_btn.pack(anchor="w", pady=(6, 0))

    # ── page: log ─────────────────────────────────────────────
    def _build_log_page(self, page):
        card = self._card(page, "Activity Log")
        self.log = tk.Text(card, bg=BG_INPUT, fg=TEXT_GRAY, font=("Consolas", 9),
                           bd=0, highlightthickness=1, highlightbackground=BG_MID,
                           highlightcolor=ACCENT, height=18, width=56, wrap="word")
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
            ("Poll (s)", "poll_interval", 3),
            ("Keepalive (s)", "keepalive_interval", 30),
            ("OSC Host", "osc_host", "127.0.0.1"),
            ("Port", "osc_port", 9000),
        ]
        self._dev_vars = {}
        for i, (label, key, default) in enumerate(fields):
            tk.Label(grid, text=label, bg=BG_CARD, fg=TEXT_GRAY, font=("", 8)).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 4))
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

    # ── helpers ───────────────────────────────────────────────
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

    def _write_log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def write_log(self, msg):
        self.after(0, self._write_log, msg)

    # ── toggle bridge ─────────────────────────────────────────
    def _toggle(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
            self.btn.config(text="\u25b6  Start", bg=SUCCESS)
            save_config({
                "address": self.addr.get(),
                "template": self.template.get(),
                "hr": self.chk_hr.get(),
                "battery": self.chk_batt.get(),
                "media": self.chk_media.get(),
                "extremes": self.chk_extremes.get(),
                "egg": self.chk_egg.get(),
                "egg_text": self.egg_txt.get(),
                "blank_egg": self.egg_dev,
                "poll_interval": self._dev_vars["poll_interval"].get() if self.egg_dev else 3,
                "keepalive_interval": self._dev_vars["keepalive_interval"].get() if self.egg_dev else 30,
                "osc_host": self._dev_vars["osc_host"].get() if self.egg_dev else "127.0.0.1",
                "osc_port": self._dev_vars["osc_port"].get() if self.egg_dev else 9000,
            })
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
                show_media=self.chk_media.get(),
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

    def _on_close(self):
        if self.bridge and self.bridge.running:
            self.bridge.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

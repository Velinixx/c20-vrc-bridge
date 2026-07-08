#!/usr/bin/env python3
"""C20 Smartwatch -> VRChat OSC Heart Rate Bridge (cross-platform).
Connects to a C20 smartwatch via BLE and streams heart rate to VRChat via OSC.

Windows:  python hr_bridge.py
Linux:    python hr_bridge.py [address]
"""
import asyncio
import sys
import platform
import struct
from bleak import BleakClient, BleakScanner
from pythonosc.udp_client import SimpleUDPClient

# BLE UUIDs for the C20 watch
BLE_HR_MEASURE = "00002a37-0000-1000-8000-00805f9b34fb"
BLE_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
BLE_FEE2_OUT = "0000fee2-0000-1000-8000-00805f9b34fb"
BLE_FEE3_IN = "0000fee3-0000-1000-8000-00805f9b34fb"

# MOYOUNG V2 commands for the C20
CMD_START_DYNAMIC_HR = 104  # 0x68
CMD_TRIGGER_HR = 109        # 0x6D
CMD_SET_HR_INTERVAL = 31    # 0x1F

OSC_IP = "127.0.0.1"
OSC_PORT = 9000
POLL_SECONDS = 3
IDLE_TIMEOUT = 2
KEEPALIVE_INTERVAL = 30

IS_LINUX = platform.system() == "Linux"


def make_packet(cmd, payload=bytes()):
    """Build MOYOUNG V2 packet: FE EA 10 <len> <cmd> <payload>"""
    data = bytearray([0xFE, 0xEA, 0x10, 0x00, cmd]) + payload
    data[3] = len(data)
    return bytes(data)


class HRBridge:
    def __init__(self, address):
        self.address = address
        self.osc = SimpleUDPClient(OSC_IP, OSC_PORT)
        self.bpm = 0
        self.battery = 0
        self.last_notify = 0
        self.client = None

    # ── OSC Output ──────────────────────────────────────────

    def send_osc(self):
        bpm, batt = self.bpm, self.battery
        self.osc.send_message("/avatar/parameters/isHRConnected", True)
        self.osc.send_message("/avatar/parameters/HR", int(bpm))
        self.osc.send_message("/avatar/parameters/floatHR", min(bpm / 255.0, 1.0))
        self.osc.send_message("/avatar/parameters/HRBattery", batt)
        self.osc.send_message("/avatar/parameters/HRBatteryFloat", batt / 100.0)
        self.osc.send_message("/chatbox/input", [f"❤️ {bpm} BPM", True])
        print(f"  ❤️ {bpm} BPM  🔋 {batt}%", flush=True)

    # ── BLE Handlers ────────────────────────────────────────

    def on_hr(self, _handle, data):
        if len(data) < 2:
            return
        flags = data[0]
        bpm = data[2] if (flags & 1) else data[1]
        if 20 <= bpm <= 250:
            self.bpm = bpm
            self.last_notify = asyncio.get_event_loop().time()
            self.send_osc()

    def on_fee3(self, _handle, data):
        if len(data) < 5 or data[0] != 0xFE or data[1] != 0xEA:
            return
        cmd = data[4]
        if cmd == CMD_TRIGGER_HR and len(data) >= 6:
            bpm = data[5]
            if 20 <= bpm <= 250:
                self.bpm = bpm
                self.send_osc()

    # ── BLE Connection ──────────────────────────────────────

    async def cache_services_linux(self):
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "connect", self.address,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()

    def get_connect_kwargs(self):
        if IS_LINUX:
            return {"timeout": 20.0, "dangerous_use_bleak_cache": True}
        return {"timeout": 20.0}

    async def run_once(self):
        if IS_LINUX:
            print("🔄 Caching services...", flush=True)
            await self.cache_services_linux()

        print(f"📡 Connecting to {self.address}...", flush=True)
        async with BleakClient(self.address, **self.get_connect_kwargs()) as client:
            self.client = client
            print(f"  ✅ Connected!", flush=True)

            # Read battery
            try:
                batt = await client.read_gatt_char(BLE_BATTERY)
                self.battery = batt[0]
                print(f"  🔋 {self.battery}%", flush=True)
            except Exception:
                pass

            # Initialize MOYOUNG protocol on FEE2/FEE3
            await client.start_notify(BLE_FEE3_IN, self.on_fee3)
            await client.write_gatt_char(
                BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False
            )
            await asyncio.sleep(0.3)
            await client.write_gatt_char(
                BLE_FEE2_OUT, make_packet(CMD_SET_HR_INTERVAL, bytes([0x01])), response=False
            )
            await client.write_gatt_char(
                BLE_FEE2_OUT, make_packet(CMD_TRIGGER_HR, bytes([0x00])), response=False
            )
            print(f"  ✅ MOYOUNG initialized", flush=True)

            # Subscribe to standard BLE Heart Rate notifications
            await client.start_notify(BLE_HR_MEASURE, self.on_hr)
            print(f"  ✅ Subscribed to HR notifications", flush=True)
            print(f"\n  ✨ Streaming HR! Press Ctrl+C to stop.\n", flush=True)

            self.last_notify = asyncio.get_event_loop().time()
            last_keepalive = self.last_notify
            while client.is_connected:
                await asyncio.sleep(POLL_SECONDS)
                now = asyncio.get_event_loop().time()
                idle = now - self.last_notify
                if idle >= IDLE_TIMEOUT:
                    pkt = make_packet(CMD_TRIGGER_HR, bytes([0x00]))
                    await client.write_gatt_char(BLE_FEE2_OUT, pkt, response=False)
                if now - last_keepalive >= KEEPALIVE_INTERVAL:
                    await client.write_gatt_char(
                        BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False
                    )
                    last_keepalive = now

    async def run_forever(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"  ⚠️ {e}", flush=True)
            print("  🔄 Reconnecting in 5s...", flush=True)
            await asyncio.sleep(5)


async def find_watch():
    print("🔍 Scanning for C20 watch...", flush=True)
    devices = await BleakScanner.discover(timeout=15, return_adv=True)
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        if "c 20" in name.lower() or "c20" in name.lower():
            print(f"  Found C20 at {addr}", flush=True)
            return addr
    print("  C20 not found! Make sure the watch is awake.", flush=True)
    return None


DEFAULT_ADDR = "96:D6:AF:D0:2B:6E"


async def main():
    bridge = HRBridge(DEFAULT_ADDR)
    await bridge.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye!", flush=True)
    except Exception as e:
        print(f"\n⚠️ Error: {e}", flush=True)
    input("\nPress Enter to exit...")

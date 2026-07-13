#!/usr/bin/env python3
"""C20 Smartwatch -> VRChat OSC Heart Rate Bridge (cross-platform).
Connects to a C20 smartwatch via BLE and streams heart rate to VRChat via OSC.
Also serves HR data over TCP for MagicChatbox integration.

Windows:  python hr_bridge.py
Linux:    python hr_bridge.py [address]
"""
import asyncio
import sys
import platform
import struct
import json
from bleak import BleakClient, BleakScanner
from pythonosc.udp_client import SimpleUDPClient

# BLE UUIDs for the C20 watch
BLE_HR_MEASURE = "00002a37-0000-1000-8000-00805f9b34fb"
BLE_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
BLE_FEE2_OUT = "0000fee2-0000-1000-8000-00805f9b34fb"
BLE_FEE3_IN = "0000fee3-0000-1000-8000-00805f9b34fb"

# MOYOUNG V2 commands
CMD_START_DYNAMIC_HR = 104
CMD_TRIGGER_HR = 109
CMD_SET_HR_INTERVAL = 31

OSC_IP = "127.0.0.1"
OSC_PORT = 9000
TCP_PORT = 9876
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
    def __init__(self, address, show_system_stats=False):
        self.address = address
        self.osc = SimpleUDPClient(OSC_IP, OSC_PORT)
        self.bpm = 0
        self.battery = 0
        self.last_notify = 0
        self.client = None
        self.show_system_stats = show_system_stats
        self.tcp_clients = []  # list of writer streams
        self.tcp_server = None

    # ── TCP Server for MCB ──────────────────────────────────

    async def start_tcp_server(self):
        """Serve HR data as JSON over TCP for MagicChatbox C20 module."""
        self.tcp_server = await asyncio.start_server(
            self._handle_tcp_client, "127.0.0.1", TCP_PORT
        )
        print(f"  🖥️ TCP server listening on 127.0.0.1:{TCP_PORT}", flush=True)

    async def _handle_tcp_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        print(f"  🔌 MCB connected from {addr}", flush=True)
        self.tcp_clients.append(writer)
        try:
            # Keep connection open, send HR updates as they happen
            # The client can also send commands (empty line = ping)
            while True:
                data = await reader.readline()
                if not data:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.tcp_clients.remove(writer)
            print(f"  🔌 MCB disconnected", flush=True)
            try:
                writer.close()
            except:
                pass

    async def tcp_broadcast(self):
        """Send current HR data to all connected TCP clients."""
        if not self.tcp_clients:
            return
        msg = json.dumps({
            "bpm": self.bpm,
            "battery": self.battery,
            "connected": True,
        }) + "\n"
        dead = []
        for w in self.tcp_clients:
            try:
                w.write(msg.encode())
                await w.drain()
            except:
                dead.append(w)
        for w in dead:
            try:
                self.tcp_clients.remove(w)
                w.close()
            except:
                pass

    # ── System Stats ────────────────────────────────────────

    @staticmethod
    def get_system_stats():
        if IS_LINUX:
            with open("/proc/stat") as f:
                vals = list(map(int, f.readline().split()[1:5]))
            total = sum(vals)
            idle = vals[3]
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            mem_total = int([l for l in lines if "MemTotal" in l][0].split()[1])
            mem_avail = int([l for l in lines if "MemAvailable" in l][0].split()[1])
            ram = int((mem_total - mem_avail) / mem_total * 100)
            ram_gb = round((mem_total - mem_avail) / 1_048_576, 1)
            return {"cpu": total, "idle": idle, "ram": ram, "ram_gb": ram_gb}
        else:
            import ctypes
            kernel = ctypes.windll.kernel32
            idle, kernel_t, user_t = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
            kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel_t), ctypes.byref(user_t))
            total = idle.value + kernel_t.value + user_t.value
            mem = ctypes.create_string_buffer(128)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            mem_load = int.from_bytes(mem[8:12], "little")
            total_phys = int.from_bytes(mem[12:20], "little")
            avail_phys = int.from_bytes(mem[20:28], "little")
            ram_gb = round((total_phys - avail_phys) / (1024**3), 1)
            return {"cpu": total, "idle": idle.value, "ram": mem_load, "ram_gb": ram_gb}

    @staticmethod
    def get_cpu_percent(stats):
        prev = getattr(HRBridge.get_cpu_percent, "_prev", None)
        if not prev:
            HRBridge.get_cpu_percent._prev = stats
            return 0
        total_delta = (stats["cpu"] - prev["cpu"]) or 1
        idle_delta = stats["idle"] - prev["idle"]
        HRBridge.get_cpu_percent._prev = stats
        return int((1 - idle_delta / total_delta) * 100)

    # ── OSC Output ──────────────────────────────────────────

    def send_osc(self):
        bpm, batt = self.bpm, self.battery
        self.osc.send_message("/avatar/parameters/isHRConnected", True)
        self.osc.send_message("/avatar/parameters/HR", int(bpm))
        self.osc.send_message("/avatar/parameters/floatHR", min(bpm / 255.0, 1.0))
        self.osc.send_message("/avatar/parameters/HRBattery", batt)
        self.osc.send_message("/avatar/parameters/HRBatteryFloat", batt / 100.0)
        if self.show_system_stats:
            stats = self.get_system_stats()
            if stats:
                cpu = self.get_cpu_percent(stats)
                ram = stats["ram"]
                self.osc.send_message("/avatar/parameters/CPU", cpu)
                self.osc.send_message("/avatar/parameters/CPUFloat", cpu / 100.0)
                self.osc.send_message("/avatar/parameters/RAM", ram)
                self.osc.send_message("/avatar/parameters/RAMFloat", ram / 100.0)
                self.osc.send_message("/avatar/parameters/RAMGB", stats["ram_gb"])
        self.osc.send_message("/chatbox/input", [f"❤️ {bpm} BPM  🔋 {batt}%", True])
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
            asyncio.ensure_future(self.tcp_broadcast())

    def on_fee3(self, _handle, data):
        if len(data) < 5 or data[0] != 0xFE or data[1] != 0xEA:
            return
        cmd = data[4]
        if cmd == CMD_TRIGGER_HR and len(data) >= 6:
            bpm = data[5]
            if 20 <= bpm <= 250:
                self.bpm = bpm
                self.send_osc()
                asyncio.ensure_future(self.tcp_broadcast())

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

            # Initialize MOYOUNG protocol
            await client.start_notify(BLE_FEE3_IN, self.on_fee3)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_START_DYNAMIC_HR, bytes([0x00])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_SET_HR_INTERVAL, bytes([0x01])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(CMD_TRIGGER_HR, bytes([0x00])), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(BLE_FEE2_OUT, make_packet(24, bytes([0x01])), response=False)
            print(f"  ✅ MOYOUNG initialized", flush=True)

            await client.start_notify(BLE_HR_MEASURE, self.on_hr)
            print(f"  ✅ Subscribed to HR notifications", flush=True)
            print(f"\n  ✨ Streaming HR! Press Ctrl+C to stop.\n", flush=True)

            await self.tcp_broadcast()

            self.last_notify = asyncio.get_event_loop().time()
            last_keepalive = self.last_notify
            keepalive_count = 0
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
                keepalive_count += 1
                if keepalive_count >= 3:
                    await client.write_gatt_char(
                        BLE_FEE2_OUT, make_packet(47, bytes([])), response=False
                    )
                    keepalive_count = 0

            # Notify TCP clients about disconnect
            msg = json.dumps({"bpm": 0, "battery": 0, "connected": False}) + "\n"
            for w in self.tcp_clients:
                try:
                    w.write(msg.encode())
                    await w.drain()
                except:
                    pass

    async def run_forever(self):
        # Start TCP server before connecting
        await self.start_tcp_server()
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

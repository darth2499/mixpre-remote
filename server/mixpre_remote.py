#!/usr/bin/env python3
"""
MixPre Remote - bridge running on a Raspberry Pi Zero 2 W plugged into a
Sound Devices MixPre II's USB-A port.

  Phone/laptop --(Web Bluetooth or Wi-Fi WebSocket)--> Pi --(USB)--> MixPre
                                                         (keyboard + nanoKONTROL2 MIDI)

Run with --fake to test the web page on any computer without a Pi or MixPre.
"""
import argparse
import asyncio
import collections
import json
import logging
import os
import re
import secrets
import socket
import time
from pathlib import Path

from aiohttp import web, WSMsgType

from mixpre_net import Net

APP_DIR = Path(__file__).resolve().parent
try:   # set by the update bundle / image build
    VERSION = json.loads((APP_DIR / "BUILD.json").read_text()).get("version", "dev")
except (OSError, ValueError):
    VERSION = "0.3.0-dev"
WEB_DIR = APP_DIR / "web"
GADGET_SH = APP_DIR / "gadget.sh"
AP_IP = "192.168.4.1"

# BLE UUIDs (16-bit-style service UUID keeps the advertisement small so the name fits)
SERVICE_UUID = "00004d50-0000-1000-8000-00805f9b34fb"
CMD_UUID = "4d500001-6d69-7870-7265-72656d6f7465"
STATE_UUID = "4d500002-6d69-7870-7265-72656d6f7465"
LEVELS_UUID = "4d500003-6d69-7870-7265-72656d6f7465"
LOG_UUID = "4d500004-6d69-7870-7265-72656d6f7465"
JSON_UUID = "4d500005-6d69-7870-7265-72656d6f7465"  # chunked JSON (Wi-Fi, remote access)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

MODES = ["keyboard", "midi", "both"]

# nanoKONTROL2 default CC map (what the MixPre expects from a real unit)
CC_FADER = range(0, 8)
CC_KNOB = range(16, 24)
CC_SOLO = range(32, 40)
CC_MUTE = range(48, 56)
CC_ARM = range(64, 72)
CC_TRANSPORT = {41: 0, 42: 1, 43: 2, 44: 3, 45: 4, 46: 5, 60: 6, 61: 7}  # play stop rew ff rec cycle set mark<

DEFAULT_CONFIG = {
    "name": "",
    "wifi_password": "mixpreremote",
    "wifi_channel": 6,
    "hotspot_mode": "local",      # local = phones keep their internet; captive = page pops up on join
    "wifi_auto_join": True,
    "relay_url": "",              # e.g. https://mixpre-relay.<you>.workers.dev
    "remote_code": "",            # generated on first start
    "update_repo": "",            # GitHub "owner/repo" to get app updates from
    "update_token": "",           # only for private repos (read-only token)
    "update_auto": False,         # install updates at start-up before first use
    "mode": "both",
    "midi_channel": 1,
    "verbose_log": False,
    # Replies to SysEx the MixPre may send while "configuring" the controller.
    # "??" = any byte (captured as $1, $2...), "4?" = high-nibble match, "*" = rest of message.
    # "SCENE" = echo back the last scene the MixPre uploaded.
    "sysex_replies": [
        {"name": "Universal Device Inquiry", "match": "F0 7E ?? 06 01 F7",
         "reply": "F0 7E 00 06 02 42 13 01 00 00 01 00 01 00 F7"},
        {"name": "Korg Search Device", "match": "F0 42 50 00 ?? F7",
         "reply": "F0 42 50 01 00 $1 13 01 00 00 01 00 01 00 F7"},
        {"name": "Scene upload to controller", "match": "F0 42 4? 00 01 13 00 7F *",
         "reply": "F0 42 40 00 01 13 00 5F 23 00 F7", "store_scene": True},
        {"name": "Scene write request", "match": "F0 42 4? 00 01 13 00 1F 11 *",
         "reply": "F0 42 40 00 01 13 00 5F 21 00 F7"},
        {"name": "Scene dump request", "match": "F0 42 4? 00 01 13 00 1F 10 *",
         "reply": "SCENE"},
    ],
}

log = logging.getLogger("mixpre")


def hexs(data):
    return " ".join(f"{b:02X}" for b in data)


# ---------------------------------------------------------------- keyboard
ASCII_HID = {}
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    ASCII_HID[c] = (0, 0x04 + i)
    ASCII_HID[c.upper()] = (0x02, 0x04 + i)
for i, c in enumerate("1234567890"):
    ASCII_HID[c] = (0, 0x1E + i)
for i, c in enumerate("!@#$%^&*()"):
    ASCII_HID[c] = (0x02, 0x1E + i)
ASCII_HID.update({
    " ": (0, 0x2C), "-": (0, 0x2D), "_": (0x02, 0x2D), "=": (0, 0x2E), "+": (0x02, 0x2E),
    "[": (0, 0x2F), "]": (0, 0x30), ";": (0, 0x33), ":": (0x02, 0x33), "'": (0, 0x34),
    '"': (0x02, 0x34), ",": (0, 0x36), ".": (0, 0x37), "/": (0, 0x38), "?": (0x02, 0x38),
})


class Keyboard:
    def __init__(self, fake, emit_log):
        self.fake = fake
        self.emit_log = emit_log
        self.fd = None
        self.lock = asyncio.Lock()
        self.held = None          # (mods, keys)
        self.held_until = 0.0

    @property
    def available(self):
        return self.fake or os.path.exists("/dev/hidg0")

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    async def report(self, mods, keys):
        keys = [k for k in keys if k][:6]
        data = bytes([mods & 0xFF, 0] + keys + [0] * (6 - len(keys)))
        if self.fake:
            if mods or keys:
                self.emit_log(f"KEY  mods={mods:02X} keys={hexs(keys)}")
            return True
        deadline = time.monotonic() + 0.3
        while True:
            if self.fd is None:
                try:
                    self.fd = os.open("/dev/hidg0", os.O_RDWR | os.O_NONBLOCK)
                except OSError:
                    return False
            try:
                os.write(self.fd, data)
                return True
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return False
                await asyncio.sleep(0.004)
            except OSError:  # host not connected (ESHUTDOWN) etc.
                self.close()
                return False

    async def tap(self, mods, keys, hold=0.05):
        async with self.lock:
            ok = await self.report(mods, keys)
            await asyncio.sleep(hold)
            await self.report(0, [])
            await asyncio.sleep(0.02)
            if not ok:
                self.emit_log("KEY  not delivered (keyboard not connected to MixPre?)")
            return ok

    async def hold(self, mods, keys):
        self.held_until = time.monotonic() + 1.2
        if self.held != (mods, tuple(keys)):
            async with self.lock:
                self.held = (mods, tuple(keys))
                await self.report(mods, keys)

    async def release(self):
        if self.held is not None:
            async with self.lock:
                self.held = None
                await self.report(0, [])

    async def watchdog(self):
        if self.held is not None and time.monotonic() > self.held_until:
            await self.release()

    async def type_text(self, text):
        for ch in text:
            if ch == "\n":
                await self.tap(0, [0x28])
            elif ch in ASCII_HID:
                m, k = ASCII_HID[ch]
                await self.tap(m, [k], hold=0.03)


# ---------------------------------------------------------------- MIDI
class Midi:
    def __init__(self, loop, fake, on_message, emit_log):
        self.loop = loop
        self.fake = fake
        self.on_message = on_message
        self.emit_log = emit_log
        self.fd = None
        self.dev = None
        self.status = None
        self.buf = []
        self.sysex = None

    @staticmethod
    def find_device():
        try:
            txt = Path("/proc/asound/cards").read_text()
        except OSError:
            return None
        for line in txt.splitlines():
            m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]:\s*(.*)", line)
            if not m:
                continue
            idx, cid, rest = m.group(1), m.group(2).strip().lower(), m.group(3).lower()
            if "f_midi" in rest or cid.startswith("nanokontrol"):
                dev = f"/dev/snd/midiC{idx}D0"
                if os.path.exists(dev):
                    return dev
        return None

    @property
    def available(self):
        return self.fake or self.fd is not None

    def open(self):
        if self.fake or self.fd is not None:
            return self.fake or True
        dev = self.find_device()
        if not dev:
            return False
        try:
            self.fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            return False
        self.dev = dev
        self.loop.add_reader(self.fd, self._readable)
        self.emit_log(f"MIDI opened {dev}")
        return True

    def close(self):
        if self.fd is not None:
            try:
                self.loop.remove_reader(self.fd)
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _readable(self):
        try:
            data = os.read(self.fd, 1024)
        except BlockingIOError:
            return
        except OSError:
            self.close()
            return
        for b in data:
            self._parse(b)

    def _parse(self, b):
        if b >= 0xF8:            # realtime
            return
        if b == 0xF0:
            self.sysex = [b]
            return
        if self.sysex is not None:
            if b == 0xF7:
                msg, self.sysex = self.sysex + [b], None
                self.on_message(bytes(msg))
                return
            if b < 0x80:
                if len(self.sysex) < 8192:
                    self.sysex.append(b)
                return
            self.sysex = None    # aborted by a new status byte
        if b >= 0x80:
            self.status = b if b < 0xF0 else None
            self.buf = []
            return
        if self.status is None:
            return
        self.buf.append(b)
        need = 1 if (self.status & 0xF0) in (0xC0, 0xD0) else 2
        if len(self.buf) == need:
            self.on_message(bytes([self.status] + self.buf))
            self.buf = []

    async def send(self, data):
        if self.fake:
            self.loop.call_later(0.03, self._fake_mixpre, bytes(data))
            return True
        if self.fd is None and not self.open():
            return False
        deadline = time.monotonic() + 0.3
        view = bytes(data)
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return False
                await asyncio.sleep(0.003)
            except OSError:
                self.close()
                return False
        return True

    # In --fake mode, pretend to be a MixPre that lights the controller LEDs.
    fake_leds = {}

    def _fake_mixpre(self, data):
        if len(data) == 3 and (data[0] & 0xF0) == 0xB0 and data[2] > 0:
            cc = data[1]
            if cc in CC_SOLO or cc in CC_MUTE or cc in CC_ARM or cc == 45:
                on = not self.fake_leds.get(cc, False)
                self.fake_leds[cc] = on
                self.on_message(bytes([0xB0, cc, 127 if on else 0]))
            elif cc in (41, 42):
                self.fake_leds[41] = cc == 41
                self.on_message(bytes([0xB0, 41, 127 if cc == 41 else 0]))
                self.on_message(bytes([0xB0, 42, 127 if cc == 42 else 0]))
                if cc == 42 and self.fake_leds.get(45):
                    self.fake_leds[45] = False
                    self.on_message(bytes([0xB0, 45, 0]))


# ---------------------------------------------------------------- SysEx
def parse_pattern(p):
    return p.upper().split()


def sysex_match(pattern, msg):
    caps = []
    toks = parse_pattern(pattern)
    for i, t in enumerate(toks):
        if t == "*":
            return caps
        if i >= len(msg):
            return None
        b = msg[i]
        if t == "??":
            caps.append(b)
        elif len(t) == 2 and t[1] == "?":
            if (b >> 4) != int(t[0], 16):
                return None
        elif b != int(t, 16):
            return None
    return caps if len(toks) == len(msg) else None


# ---------------------------------------------------------------- BLE
class Ble:
    def __init__(self, bridge):
        self.bridge = bridge
        self.server = None

    async def start(self, name):
        try:
            from bless import (BlessServer, GATTAttributePermissions as Perm,
                               GATTCharacteristicProperties as Prop)
        except Exception as e:  # noqa
            log.warning("Bluetooth disabled (bless not available: %s)", e)
            return
        try:
            self.server = BlessServer(name=name, loop=asyncio.get_running_loop())
            self.server.read_request_func = self._read
            self.server.write_request_func = self._write
            await self.server.add_new_service(SERVICE_UUID)
            await self.server.add_new_characteristic(
                SERVICE_UUID, CMD_UUID, Prop.write | Prop.write_without_response,
                bytearray(b"\x00"), Perm.writeable)
            b = self.bridge
            for uuid, val in ((STATE_UUID, b.state_packet()), (LEVELS_UUID, b.levels_packet()),
                              (LOG_UUID, b"ready"), (JSON_UUID, b"\x00")):
                await self.server.add_new_characteristic(
                    SERVICE_UUID, uuid, Prop.read | Prop.notify, bytearray(val), Perm.readable)
            await self.server.start()
            log.info("Bluetooth advertising as %s", name)
        except Exception as e:  # noqa
            log.exception("Bluetooth failed to start: %s", e)
            self.server = None

    def _read(self, characteristic, **kwargs):
        return characteristic.value

    def _write(self, characteristic, value, **kwargs):
        if str(characteristic.uuid).lower() == CMD_UUID:
            data = bytes(value)
            if data[:1] == b"\x40":
                self.bridge.loop.call_soon_threadsafe(self._chunk_in, data)
            else:
                self.bridge.loop.call_soon_threadsafe(self.bridge.enqueue, data)

    # JSON over BLE is split into frames: [0x40, msg_id, index, total] + up to 16 bytes
    _rx = {}
    _tx_id = 0

    def _chunk_in(self, frame):
        if len(frame) < 4:
            return
        mid, idx, total = frame[1], frame[2], frame[3]
        parts = self._rx.setdefault(mid, {})
        parts[idx] = frame[4:]
        if len(parts) == total:
            data = b"".join(parts[i] for i in range(total) if i in parts)
            del self._rx[mid]
            asyncio.ensure_future(self.bridge.handle_text(data.decode("utf-8", "ignore"), None))

    async def send_json(self, text):
        if not self.server:
            return
        data = text.encode()
        chunks = [data[i:i + 16] for i in range(0, len(data), 16)] or [b""]
        if len(chunks) > 255:
            return
        Ble._tx_id = (Ble._tx_id + 1) & 0xFF
        for i, c in enumerate(chunks):
            self.notify(JSON_UUID, bytes([0x40, Ble._tx_id, i, len(chunks)]) + c)
            await asyncio.sleep(0.008)

    def notify(self, uuid, data):
        if not self.server:
            return
        try:
            c = self.server.get_characteristic(uuid)
            c.value = bytearray(data)
            self.server.update_value(SERVICE_UUID, uuid)
        except Exception as e:  # noqa
            log.debug("notify failed: %s", e)


# ---------------------------------------------------------------- clients
class WsClient:
    kind = "ws"

    def __init__(self, ws):
        self.ws = ws
        self.id = secrets.token_hex(4)

    async def send_text(self, text):
        await self.ws.send_str(text)


# ---------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, args):
        self.args = args
        self.fake = args.fake
        self.loop = None
        self.config_path = self._find_config()
        self.config = self._load_config()
        self.clients = set()
        self.logs = collections.deque(maxlen=150)
        self.queue = asyncio.Queue()
        self.arm = self.mute = self.solo = self.transport = 0
        self.levels = [255] * 16          # 255 = unknown
        self.held_cc = {}                 # cc -> expiry
        self.scene = None
        self.usb_configured = False
        self.gadget_mode = None
        self._bcast_pending = False
        self.kb = None
        self.midi = None
        self.ble = Ble(self)
        self.net = None
        self.cloud = None
        self.updater = None
        self.cmd_count = 0
        self.started_at = time.monotonic()
        self.version = VERSION
        self._net_pending = False
        self._last_hello = None
        if not self.config.get("remote_code"):
            self.config["remote_code"] = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            self.save_config()

    # ---- config
    def _find_config(self):
        if self.args.config:
            return Path(self.args.config)
        if self.fake:
            return APP_DIR / "mixpre-remote.json"
        for p in (Path("/boot/firmware/mixpre-remote.json"), Path("/boot/mixpre-remote.json")):
            if p.parent.exists():
                return p
        return APP_DIR / "mixpre-remote.json"

    def _load_config(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            cfg.update(json.loads(self.config_path.read_text()))
        except (OSError, ValueError):
            pass
        if cfg.get("mode") not in MODES:
            cfg["mode"] = "both"
        return cfg

    def save_config(self):
        try:
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.config, indent=2))
            os.replace(tmp, self.config_path)
            os.sync()
        except OSError as e:
            self.emit_log(f"Could not save config: {e}")

    @property
    def name(self):
        if self.config.get("name"):
            return self.config["name"][:20]
        try:
            serial = Path("/sys/firmware/devicetree/base/serial-number").read_text().strip("\x00\n")
            return f"MixPre-Remote-{serial[-4:].upper()}"
        except OSError:
            return "MixPre-Remote" + ("-DEV" if self.fake else "")

    @property
    def mode(self):
        return self.config["mode"]

    @property
    def code(self):
        return self.config.get("remote_code", "")

    # ---- logging
    def emit_log(self, line):
        line = f"{time.strftime('%H:%M:%S')} {line}"
        log.info(line)
        self.logs.append(line)
        msg = json.dumps({"type": "log", "line": line})
        for c in list(self.clients):
            asyncio.ensure_future(self._send(c, msg))
        self.ble.notify(LOG_UUID, line[9:].encode()[:180])

    # ---- state
    def state_packet(self):
        flags = (1 if self.midi and self.midi.available else 0) | \
                (2 if self.kb and self.kb.available else 0) | \
                (4 if self.usb_configured else 0)
        return bytes([1, MODES.index(self.mode), flags,
                      self.arm & 0xFF, self.arm >> 8, self.mute & 0xFF, self.mute >> 8,
                      self.solo & 0xFF, self.solo >> 8, self.transport & 0xFF])

    def levels_packet(self):
        return bytes(self.levels)

    def state_json(self):
        return {
            "type": "state", "version": VERSION, "name": self.name, "mode": self.mode,
            "midi": bool(self.midi and self.midi.available),
            "hid": bool(self.kb and self.kb.available),
            "usb": self.usb_configured,
            "arm": self.arm, "mute": self.mute, "solo": self.solo,
            "transport": self.transport, "levels": self.levels,
        }

    def broadcast(self):
        if self._bcast_pending:
            return
        self._bcast_pending = True
        self.loop.call_later(0.02, self._do_broadcast)

    def _do_broadcast(self):
        self._bcast_pending = False
        msg = json.dumps(self.state_json())
        self.send_all(msg)
        self.ble.notify(STATE_UUID, self.state_packet())
        self.ble.notify(LEVELS_UUID, self.levels_packet())

    def send_all(self, msg, relay=True):
        for c in list(self.clients):
            asyncio.ensure_future(self._send(c, msg))
        if relay and self.cloud:
            asyncio.ensure_future(self.cloud.broadcast(msg))

    async def _send(self, client, msg):
        try:
            await client.send_text(msg)
        except Exception:  # noqa
            self.clients.discard(client)

    # ---- network / remote-access info ("net" message)
    def net_json(self):
        n = self.net.status() if self.net else {}
        n.update({
            "type": "net", "name": self.name, "version": VERSION, "code": self.code,
            "relay_url": self.config.get("relay_url", ""),
            "cloud": bool(self.cloud and self.cloud.connected),
            "cloud_viewers": self.cloud.viewers if self.cloud else 0,
            "direct": bool(self.cloud and self.cloud.peers),
            "saved": self.config.get("wifi_recent", []),
            "update": self.updater.status_json() if self.updater else None,
        })
        return n

    def net_changed(self):
        if self._net_pending or self.loop is None:
            return
        self._net_pending = True
        self.loop.call_later(0.15, self._do_net)

    def _do_net(self):
        self._net_pending = False
        msg = json.dumps(self.net_json())
        self.send_all(msg)
        asyncio.ensure_future(self.ble.send_json(msg))
        # keep the relay's presence info (name, local address) current
        if self.cloud and self.cloud.connected and self.net:
            hello = (self.net.mode, self.net.ip, self.net.ssid, self.name)
            if hello != self._last_hello:
                self._last_hello = hello
                asyncio.ensure_future(self.cloud.hello())

    def hello_messages(self):
        return [json.dumps(self.state_json()),
                json.dumps({"type": "logs", "lines": list(self.logs)[-60:]}),
                json.dumps(self.net_json())]

    async def handle_text(self, text, client):
        try:
            obj = json.loads(text)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if obj.get("t") == "peer" and isinstance(client, WsClient):
            # Send/Listen audio signaling between browsers on the Pi's own network
            out = json.dumps({"type": "peer", "from": client.id, "d": obj.get("d")})
            for c in list(self.clients):
                if isinstance(c, WsClient) and c is not client and (not obj.get("to") or c.id == obj.get("to")):
                    asyncio.ensure_future(self._send(c, out))
            return
        await self.handle_json(obj)

    async def handle_json(self, m):
        op = m.get("op")
        net = self.net
        if op == "net_status":
            if net:
                await net.refresh()
            self.net_changed()
        elif op == "wifi_scan" and net:
            await net.scan()
        elif op == "wifi_join" and net and m.get("ssid"):
            asyncio.ensure_future(net.join(str(m["ssid"])[:32], (str(m["psk"]) if m.get("psk") else None)))
        elif op == "wifi_forget" and net and m.get("ssid"):
            await net.forget(str(m["ssid"]))
        elif op == "hotspot" and net:
            asyncio.ensure_future(net.start_hotspot("requested"))
        elif op == "hotspot_mode" and m.get("mode") in ("local", "captive"):
            self.config["hotspot_mode"] = m["mode"]
            self.save_config()
            self.emit_log(f"Hotspot mode: {m['mode']}")
            if net and net.mode == "hotspot" and not self.fake:
                net.write_dnsmasq()
                await net.run("systemctl", "restart", "mixpre-dhcp.service", timeout=15)
            self.net_changed()
        elif op == "set_relay":
            url = str(m.get("url") or "").strip()[:200]
            self.config["relay_url"] = url
            self.save_config()
            self.emit_log(f"Relay URL {'set to ' + url if url else 'cleared'}")
            if self.cloud:
                self.cloud.restart()
            self.net_changed()
        elif op == "new_code":
            self.config["remote_code"] = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            self.save_config()
            self.emit_log("New remote code generated (old one no longer works)")
            if self.cloud:
                self.cloud.restart()
            self.net_changed()
        elif op == "update_check" and self.updater:
            asyncio.ensure_future(self.updater.check(manual=True))
        elif op == "update_install" and self.updater:
            asyncio.ensure_future(self.updater.install())
        elif op == "update_rollback" and self.updater:
            asyncio.ensure_future(self.updater.rollback())
        elif op == "update_dismiss" and self.updater:
            self.updater.notice = ""
            self.net_changed()
        elif op == "update_settings":
            if "repo" in m:
                self.config["update_repo"] = str(m.get("repo") or "").strip()[:100]
            if m.get("token") is not None:
                self.config["update_token"] = str(m.get("token") or "").strip()[:200]
            if "auto" in m:
                self.config["update_auto"] = bool(m.get("auto"))
            self.save_config()
            self.emit_log("Update settings saved")
            if self.updater and ("repo" in m or m.get("token") is not None):
                asyncio.ensure_future(self.updater.check(manual=True))
            self.net_changed()
        elif op == "set_name":
            self.config["name"] = str(m.get("name") or "")[:20]
            self.save_config()
            self.emit_log("Name saved - restart the Pi to apply it to Bluetooth and the hotspot")
            self.net_changed()

    # ---- MIDI from the MixPre (LED feedback, SysEx)
    def on_midi(self, msg):
        if msg[0] == 0xF0:
            self.emit_log(f"SYSEX IN ({len(msg)}B) {hexs(msg[:48])}{' ...' if len(msg) > 48 else ''}")
            self.handle_sysex(msg)
            return
        if (msg[0] & 0xF0) == 0xB0 and len(msg) == 3:
            cc, on = msg[1], msg[2] > 0
            if self.config.get("verbose_log") or cc not in CC_FADER:
                self.emit_log(f"IN   {hexs(msg)}")

            def setbit(mask, i):
                return mask | (1 << i) if on else mask & ~(1 << i)
            if cc in CC_ARM:
                self.arm = setbit(self.arm, cc - 64)
            elif cc in CC_MUTE:
                self.mute = setbit(self.mute, cc - 48)
            elif cc in CC_SOLO:
                self.solo = setbit(self.solo, cc - 32)
            elif cc in CC_TRANSPORT:
                self.transport = setbit(self.transport, CC_TRANSPORT[cc])
            self.broadcast()
        else:
            self.emit_log(f"IN   {hexs(msg)}")

    def handle_sysex(self, msg):
        for rule in self.config.get("sysex_replies", []):
            try:
                caps = sysex_match(rule["match"], msg)
            except (KeyError, ValueError):
                continue
            if caps is None:
                continue
            if rule.get("store_scene"):
                self.scene = msg
            reply = rule.get("reply", "")
            if reply == "SCENE":
                if not self.scene:
                    self.emit_log(f"SYSEX '{rule.get('name')}' - no scene stored yet, no reply")
                    return
                out = bytes([self.scene[0], self.scene[1], 0x40]) + self.scene[3:]
            else:
                toks = []
                for t in reply.split():
                    if t.startswith("$"):
                        toks.append(caps[int(t[1:]) - 1])
                    else:
                        toks.append(int(t, 16))
                out = bytes(toks)
            self.emit_log(f"SYSEX reply '{rule.get('name')}': {hexs(out[:32])}")
            asyncio.ensure_future(self.midi.send(out))
            return
        self.emit_log("SYSEX no matching reply rule (logged above)")

    # ---- commands from the web page
    def enqueue(self, data):
        if data:
            self.queue.put_nowait(data)

    async def worker(self):
        while True:
            data = await self.queue.get()
            try:
                await self.dispatch(data)
            except Exception as e:  # noqa
                log.exception("command failed")
                self.emit_log(f"Command error: {e}")

    def cc_status(self):
        return 0xB0 | ((int(self.config.get("midi_channel", 1)) - 1) & 0x0F)

    async def send_cc(self, cc, val):
        ok = await self.midi.send(bytes([self.cc_status(), cc & 0x7F, val & 0x7F]))
        if not ok:
            self.emit_log("MIDI not delivered (MIDI mode off or MixPre not connected?)")
        return ok

    async def dispatch(self, d):
        if d and d[0] not in (0x30,):
            self.cmd_count += 1
        op = d[0]
        if op == 0x01:                                    # tap key combo
            await self.kb.tap(d[1], list(d[2:8]))
        elif op == 0x02:                                  # hold key (heartbeat)
            await self.kb.hold(d[1], list(d[2:8]))
        elif op == 0x03:
            await self.kb.release()
        elif op == 0x04:                                  # type text
            await self.kb.type_text(d[1:].decode("utf-8", "ignore"))
        elif op == 0x10 and len(d) >= 3:                  # set CC (fader/knob)
            cc, val = d[1], d[2]
            await self.send_cc(cc, val)
            if cc in CC_FADER:
                self.levels[cc] = val
            elif cc in CC_KNOB:
                self.levels[8 + cc - 16] = val
            if self.config.get("verbose_log"):
                self.emit_log(f"OUT  CC {cc} = {val}")
            self.broadcast()
        elif op == 0x11:                                  # tap button
            self.emit_log(f"OUT  button CC {d[1]}")
            await self.send_cc(d[1], 127)
            await asyncio.sleep(0.05)
            await self.send_cc(d[1], 0)
        elif op == 0x12:                                  # combo: hold d[1], tap d[2]
            self.emit_log(f"OUT  combo CC {d[1]} + {d[2]}")
            await self.send_cc(d[1], 127)
            await asyncio.sleep(0.05)
            await self.send_cc(d[2], 127)
            await asyncio.sleep(0.05)
            await self.send_cc(d[2], 0)
            await asyncio.sleep(0.05)
            await self.send_cc(d[1], 0)
        elif op == 0x13:                                  # hold button (heartbeat)
            cc = d[1]
            if cc not in self.held_cc:
                self.emit_log(f"OUT  hold CC {cc}")
                await self.send_cc(cc, 127)
            self.held_cc[cc] = time.monotonic() + 1.2
        elif op == 0x14:                                  # release button
            cc = d[1]
            if cc in self.held_cc:
                del self.held_cc[cc]
                await self.send_cc(cc, 0)
        elif op == 0x15:                                  # raw MIDI
            self.emit_log(f"OUT  raw {hexs(d[1:])}")
            await self.midi.send(d[1:])
        elif op == 0x20 and len(d) >= 2 and d[1] < len(MODES):
            await self.set_mode(MODES[d[1]])
        elif op == 0x30:
            self.broadcast()
            self.net_changed()

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.2)
            await self.kb.watchdog()
            now = time.monotonic()
            for cc, exp in list(self.held_cc.items()):
                if now > exp:
                    del self.held_cc[cc]
                    await self.send_cc(cc, 0)

    # ---- USB gadget
    async def run_gadget(self, mode):
        if self.fake:
            self.gadget_mode = mode
            return
        self.midi.close()
        self.kb.close()
        proc = await asyncio.create_subprocess_exec(
            "bash", str(GADGET_SH), mode,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        self.emit_log(out.decode().strip() or f"gadget.sh exited {proc.returncode}")
        self.gadget_mode = mode
        await asyncio.sleep(1.0)
        self.midi.open()

    async def set_mode(self, mode):
        self.emit_log(f"Switching USB mode to {mode}")
        await self.kb.release()
        self.held_cc.clear()
        self.config["mode"] = mode
        self.save_config()
        await self.run_gadget(mode)
        self.broadcast()

    async def monitor_usb(self):
        last = None
        while True:
            if self.fake:
                self.usb_configured = True
            else:
                state = ""
                try:
                    udc = os.listdir("/sys/class/udc")[0]
                    state = Path(f"/sys/class/udc/{udc}/state").read_text().strip()
                except (OSError, IndexError):
                    pass
                self.usb_configured = state == "configured"
                if self.mode != "keyboard" and self.midi.fd is None:
                    self.midi.open()
            snap = (self.usb_configured, self.midi.available, self.kb.available)
            if snap != last:
                if last is not None:
                    self.emit_log(f"USB {'connected to host' if snap[0] else 'not connected'}"
                                  f" | MIDI {'ok' if snap[1] else '-'} | keyboard {'ok' if snap[2] else '-'}")
                last = snap
                self.broadcast()
            await asyncio.sleep(1.0)

    # ---- web
    def make_app(self):
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/", self.index)
        app.router.add_get("/{tail:.*}", self.catch_all)
        return app

    def _is_ours(self, request):
        host = request.host.rsplit(":", 1)[0].strip("[]")
        return (re.fullmatch(r"[\d.]+", host) is not None or host.endswith(".local")
                or host in ("localhost", socket.gethostname()))

    async def index(self, request):
        if not self._is_ours(request):
            raise web.HTTPFound(f"http://{AP_IP}/")
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def catch_all(self, request):
        # Captive portal: any other host/path lands on the control page.
        target = "/" if self._is_ours(request) else f"http://{AP_IP}/"
        raise web.HTTPFound(target)

    async def ws_handler(self, request):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        client = WsClient(ws)
        self.clients.add(client)
        await ws.send_str(json.dumps({"type": "you", "id": client.id}))
        for text in self.hello_messages():
            await ws.send_str(text)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self.enqueue(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await self.handle_text(msg.data, client)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(client)
            left = json.dumps({"type": "peer", "from": client.id, "d": {"a": "left"}})
            for c in list(self.clients):
                if isinstance(c, WsClient):
                    asyncio.ensure_future(self._send(c, left))
        return ws

    # ---- main
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.kb = Keyboard(self.fake, self.emit_log)
        self.midi = Midi(self.loop, self.fake, self.on_midi, self.emit_log)
        current = None
        try:
            current = Path("/run/mixpre-gadget-mode").read_text().strip()
        except OSError:
            pass
        if current != self.mode:
            await self.run_gadget(self.mode)
        else:
            self.gadget_mode = current
            self.midi.open()
        runner = web.AppRunner(self.make_app())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.args.port).start()
        log.info("Web control page on port %s", self.args.port)
        asyncio.ensure_future(self.worker())
        asyncio.ensure_future(self.watchdog())
        asyncio.ensure_future(self.monitor_usb())
        if not self.args.no_ble:
            await self.ble.start(self.name)
        self.emit_log(f"MixPre Remote {VERSION} ready - {self.name} - USB mode: {self.mode}")
        # Wi-Fi status / fallback watchdog (the boot-time join is done by mixpre-net.service)
        self.net = Net(lambda: self.config, self.save_config, self.emit_log, self.fake, self.net_changed)
        if self.fake:
            await self.net.boot()
        else:
            await self.net.refresh()
        asyncio.ensure_future(self.net.watchdog())
        # Cloud relay + direct (WebRTC) connections; imported late so USB/Bluetooth come up first
        try:
            from mixpre_update import Updater
            self.updater = Updater(self)
            if not self.fake or os.environ.get("MIXPRE_BASE"):
                asyncio.ensure_future(self.updater.loop())
        except Exception as e:  # noqa
            log.warning("Updater disabled: %s", e)
        try:
            from mixpre_cloud import Cloud, HAVE_RTC
            self.cloud = Cloud(self)
            self.cloud.start()
            if not HAVE_RTC:
                log.warning("aiortc not installed: remote access will use the relay only")
        except Exception as e:  # noqa
            log.warning("Cloud link disabled: %s", e)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fake", action="store_true", help="no Pi/MixPre: simulate USB and LED feedback")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-ble", action="store_true")
    p.add_argument("--config")
    args = p.parse_args()
    if args.port is None:
        args.port = 8080 if args.fake else 80
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("aioice", "aiortc", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    bridge = Bridge(args)
    await bridge.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
MixPre Remote - Wi-Fi management for the Pi.

Two modes:
  hotspot : the Pi runs its own network "MixPre-Remote-XXXX" (192.168.4.1).
            "local" hotspot mode hands out no gateway/DNS, so phones keep using
            cellular for the internet. "captive" mode makes every site open the
            control page (auto pop-up on join) but blocks the internet.
  client  : the Pi joins another network (venue Wi-Fi, phone Personal Hotspot).
            If that fails or drops, it falls back to its own hotspot.

Also usable from the command line:  python3 mixpre_net.py boot|hotspot|scan|status
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

AP_CON = "mixpre-ap"
AP_IP = "192.168.4.1"
IFACE = "wlan0"
DNSMASQ_CONF = Path("/run/mixpre-remote/dnsmasq.conf")
BOOT_DIRS = (Path("/boot/firmware"), Path("/boot"))


def boot_config_path():
    for d in BOOT_DIRS:
        if d.exists():
            return d / "mixpre-remote.json"
    return Path(__file__).resolve().parent / "mixpre-remote.json"


def split_terse(line):
    """Split an `nmcli -t` line on unescaped colons."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def pi_suffix():
    try:
        s = Path("/sys/firmware/devicetree/base/serial-number").read_text().strip("\x00\n")
        return s[-4:].upper()
    except OSError:
        return "0000"


class Net:
    def __init__(self, get_config, save_config=None, log=print, fake=False, on_change=None):
        self.get_config = get_config
        self.save_config = save_config or (lambda: None)
        self.log = log
        self.fake = fake
        self.on_change = on_change or (lambda: None)
        self.lock = asyncio.Lock()
        self.mode = "unknown"         # hotspot | client | switching | unknown
        self.ssid = ""
        self.ip = ""
        self.connectivity = "unknown"  # full | limited | portal | none | unknown
        self.networks = []             # [{ssid, signal, secure}]
        self.message = ""
        self.lost_since = None
        self.ap_up = False
        self.ap_down_checks = 0
        self._fake_mode = "hotspot"
        self._fake_ssid = ""
        self._fake_saved = ["Studio WiFi"]

    # ------------------------------------------------------------ helpers
    @property
    def cfg(self):
        return self.get_config()

    def hotspot_ssid(self):
        return (self.cfg.get("name") or f"MixPre-Remote-{pi_suffix()}")[:32]

    def hotspot_psk(self):
        p = str(self.cfg.get("wifi_password") or "")
        return p if len(p) >= 8 else "mixpreremote"

    async def run(self, *args, timeout=40):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            return proc.returncode, out.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa
                pass
            return 124, "timeout"
        except FileNotFoundError:
            return 127, f"{args[0]} not found"

    def mac(self):
        try:
            return Path(f"/sys/class/net/{IFACE}/address").read_text().strip().upper()
        except OSError:
            return "B8:27:EB:00:00:00" if self.fake else ""

    async def lan_ips(self):
        if self.fake:
            return ["192.168.1.50"] if self._fake_mode == "client" else [AP_IP]
        rc, out = await self.run("ip", "-4", "-o", "addr", "show", "scope", "global", timeout=5)
        return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out) if rc == 0 else []

    async def saved_networks(self):
        """Client Wi-Fi profiles: [(con_name, ssid)]"""
        if self.fake:
            return [(f"mixpre-{s}", s) for s in self._fake_saved]
        rc, out = await self.run("nmcli", "-t", "-f", "NAME,TYPE", "con", "show", timeout=10)
        res = []
        for line in out.splitlines() if rc == 0 else []:
            name, typ = (split_terse(line) + [""])[:2]
            if typ != "802-11-wireless" or name == AP_CON:
                continue
            rc2, o2 = await self.run("nmcli", "-g", "802-11-wireless.ssid,802-11-wireless.mode",
                                     "con", "show", name, timeout=10)
            vals = o2.splitlines()
            if rc2 == 0 and vals and (len(vals) < 2 or vals[1] != "ap"):
                res.append((name, vals[0].replace("\\:", ":")))
        return res

    async def wait_wifi_ready(self, timeout=45):
        """Wi-Fi starts switched off (radio kill switch) and the original Zero W is slow to
        bring it up: unblock it and wait until NetworkManager says wlan0 is usable."""
        if self.fake:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.run("rfkill", "unblock", "wifi", timeout=5)
            await self.run("nmcli", "radio", "wifi", "on", timeout=10)
            rc, out = await self.run("nmcli", "-t", "-f", "DEVICE,STATE", "dev", timeout=10)
            for line in out.splitlines() if rc == 0 else []:
                dev, _, state = line.partition(":")
                if dev == IFACE and state and not state.startswith(("unavailable", "unmanaged")):
                    return True
            await asyncio.sleep(2)
        self.log("Wi-Fi: wlan0 still not ready (radio off or Wi-Fi driver not started)")
        return False

    # ------------------------------------------------------------ hotspot
    def write_dnsmasq(self):
        captive = self.cfg.get("hotspot_mode", "local") == "captive"
        lines = [
            f"interface={IFACE}", "bind-dynamic", "except-interface=lo",
            "dhcp-range=192.168.4.10,192.168.4.200,255.255.255.0,12h",
            "dhcp-authoritative", "no-resolv", "no-hosts",
            "address=/mixpre.local/192.168.4.1",
        ]
        if captive:
            # Gateway + DNS point at the Pi, every name resolves to it -> auto pop-up page
            lines += [f"dhcp-option=3,{AP_IP}", f"dhcp-option=6,{AP_IP}", f"address=/#/{AP_IP}"]
        else:
            # Local-only: no gateway, no DNS. Devices keep cellular/other internet.
            lines += ["port=0", "dhcp-option=3", "dhcp-option=6"]
        DNSMASQ_CONF.parent.mkdir(parents=True, exist_ok=True)
        DNSMASQ_CONF.write_text("\n".join(lines) + "\n")

    async def ensure_ap_profile(self):
        ch = str(self.cfg.get("wifi_channel") or 6)
        common = ["802-11-wireless.ssid", self.hotspot_ssid(), "wifi-sec.psk", self.hotspot_psk(),
                  "802-11-wireless.channel", ch, "connection.autoconnect", "no",
                  "ipv4.method", "manual", "ipv4.addresses", f"{AP_IP}/24", "ipv6.method", "disabled"]
        rc, _ = await self.run("nmcli", "-t", "con", "show", AP_CON, timeout=10)
        if rc == 0:
            await self.run("nmcli", "con", "modify", AP_CON, *common)
        else:
            await self.run("nmcli", "con", "add", "type", "wifi", "ifname", IFACE, "con-name", AP_CON,
                           "ssid", self.hotspot_ssid(), "802-11-wireless.mode", "ap",
                           "802-11-wireless.band", "bg", "wifi-sec.key-mgmt", "wpa-psk",
                           "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
                           *common)

    async def start_hotspot(self, reason=""):
        async with self.lock:
            await self._start_hotspot(reason)

    async def _start_hotspot(self, reason=""):
        self.log(f"Wi-Fi: starting hotspot {self.hotspot_ssid()}{' (' + reason + ')' if reason else ''}")
        self.mode, self.ssid, self.message = "switching", self.hotspot_ssid(), reason
        self.on_change()
        if self.fake:
            await asyncio.sleep(0.3)
            self._fake_mode = "hotspot"
        else:
            await self.wait_wifi_ready()
            await self.ensure_ap_profile()
            self.write_dnsmasq()
            for attempt in range(1, 5):
                rc, out = await self.run("nmcli", "--wait", "20", "con", "up", AP_CON, timeout=30)
                if rc == 0:
                    break
                self.log(f"Wi-Fi: hotspot attempt {attempt} failed: {out}")
                await asyncio.sleep(4)
                await self.wait_wifi_ready(timeout=20)
            await self.run("systemctl", "restart", "mixpre-dhcp.service", timeout=15)
        self.mode, self.lost_since = "hotspot", None
        await self.refresh()

    # ------------------------------------------------------------ client
    async def join(self, ssid, psk=None, timeout=30):
        """Join a network. Falls back to the hotspot on failure. Returns True on success."""
        async with self.lock:
            self.log(f"Wi-Fi: joining '{ssid}'")
            self.mode, self.ssid, self.message = "switching", ssid, f"Joining {ssid}…"
            self.on_change()
            await asyncio.sleep(1.5)   # let the reply reach the page before the hotspot drops
            ok = await self._join(ssid, psk, timeout)
            if ok:
                self.mode, self.message, self.lost_since = "client", "", None
                last = [s for s in self.cfg.get("wifi_recent", []) if s != ssid]
                self.cfg["wifi_recent"] = ([ssid] + last)[:10]
                self.save_config()
                self.log(f"Wi-Fi: connected to '{ssid}'")
                await self.refresh()
                return True
            await self._start_hotspot(f"couldn't join {ssid}")
            self.message = f"Couldn't join “{ssid}” — check the password. Back on the Pi's hotspot."
            self.on_change()
            return False

    async def _join(self, ssid, psk, timeout):
        if self.fake:
            await asyncio.sleep(1.0)
            if psk == "wrong":
                return False
            if ssid not in self._fake_saved:
                self._fake_saved.append(ssid)
            self._fake_mode, self._fake_ssid = "client", ssid
            return True
        await self.run("systemctl", "stop", "mixpre-dhcp.service", timeout=10)
        await self.run("nmcli", "con", "down", AP_CON, timeout=15)
        await asyncio.sleep(1)
        await self.run("nmcli", "dev", "wifi", "rescan", "ifname", IFACE, timeout=15)
        await asyncio.sleep(2)
        saved = dict((s, n) for n, s in await self.saved_networks())
        if ssid in saved and not psk:
            rc, out = await self.run("nmcli", "--wait", str(timeout), "con", "up", saved[ssid], timeout=timeout + 10)
        else:
            if ssid in saved:
                await self.run("nmcli", "con", "delete", saved[ssid], timeout=10)
            args = ["nmcli", "--wait", str(timeout), "dev", "wifi", "connect", ssid, "ifname", IFACE,
                    "name", f"mixpre-{ssid}"[:60]]
            if psk:
                args[7:7] = ["password", psk]   # right after the SSID
            rc, out = await self.run(*args, timeout=timeout + 10)
        if rc != 0:
            self.log(f"Wi-Fi: join failed: {out}")
            # don't keep a profile with a wrong password
            if psk:
                await self.run("nmcli", "con", "delete", f"mixpre-{ssid}"[:60], timeout=10)
            return False
        # we decide when to connect, not NetworkManager
        for name, s in await self.saved_networks():
            if s == ssid:
                await self.run("nmcli", "con", "modify", name, "connection.autoconnect", "no", timeout=10)
        return True

    async def forget(self, ssid):
        async with self.lock:
            if self.fake:
                self._fake_saved = [s for s in self._fake_saved if s != ssid]
            else:
                for name, s in await self.saved_networks():
                    if s == ssid:
                        await self.run("nmcli", "con", "delete", name, timeout=10)
            self.cfg["wifi_recent"] = [s for s in self.cfg.get("wifi_recent", []) if s != ssid]
            self.save_config()
            self.log(f"Wi-Fi: forgot '{ssid}'")
        if self.mode == "client" and self.ssid == ssid:
            await self.start_hotspot("network forgotten")
        else:
            self.on_change()

    # ------------------------------------------------------------ scan / status
    async def scan(self):
        if self.fake:
            await asyncio.sleep(0.5)
            self.networks = [{"ssid": "Studio WiFi", "signal": 82, "secure": True},
                             {"ssid": "Yuki's iPhone", "signal": 70, "secure": True},
                             {"ssid": "Hotel Guest", "signal": 45, "secure": False}]
            self.on_change()
            return self.networks
        nets = {}
        if self.mode == "hotspot":
            # NetworkManager can't scan while hosting; iw can ("ap-force")
            rc, out = await self.run("iw", "dev", IFACE, "scan", "ap-force", timeout=25)
            if rc == 0:
                cur = None
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("BSS "):
                        cur = {"ssid": "", "signal": 0, "secure": False}
                    elif cur is None:
                        continue
                    elif line.startswith("signal:"):
                        try:
                            dbm = float(line.split()[1])
                            cur["signal"] = max(0, min(100, int(2 * (dbm + 100))))
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("SSID:"):
                        cur["ssid"] = line[5:].strip()
                        if cur["ssid"]:
                            nets.setdefault(cur["ssid"], cur)
                    elif line.startswith(("RSN:", "WPA:")):
                        cur["secure"] = True
                    elif line.startswith("capability:") and "Privacy" in line:
                        cur["secure"] = True
            else:
                self.log(f"Wi-Fi: scan failed: {out[:120]}")
        else:
            rc, out = await self.run("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                                     "--rescan", "yes", timeout=25)
            for line in out.splitlines() if rc == 0 else []:
                ssid, sig, sec = (split_terse(line) + ["", "", ""])[:3]
                if ssid and ssid not in nets:
                    nets[ssid] = {"ssid": ssid, "signal": int(sig or 0), "secure": bool(sec and sec != "--")}
        own = self.hotspot_ssid()
        self.networks = sorted((n for n in nets.values() if n["ssid"] != own), key=lambda n: -n["signal"])[:20]
        self.on_change()
        return self.networks

    async def refresh(self):
        """Update mode/ssid/ip/connectivity from the system."""
        if self.fake:
            self.mode = self._fake_mode if self.mode != "switching" else self.mode
            self.ssid = self._fake_ssid if self._fake_mode == "client" else self.hotspot_ssid()
            self.ip = (await self.lan_ips() or [""])[0]
            self.connectivity = "full" if self._fake_mode == "client" else "none"
            self.on_change()
            return
        rc, out = await self.run("nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION", "dev", "show", IFACE, timeout=10)
        state = conn = ""
        for line in out.splitlines():
            k, _, v = line.partition(":")
            if k == "GENERAL.STATE":
                state = v
            elif k == "GENERAL.CONNECTION":
                conn = v
        connected = state.startswith("100")
        self.ap_up = connected and conn == AP_CON
        if self.mode != "switching":
            if connected and conn == AP_CON:
                self.mode, self.ssid = "hotspot", self.hotspot_ssid()
            elif connected and conn:
                self.mode = "client"
                ssids = dict(await self.saved_networks())
                self.ssid = ssids.get(conn, conn)
        if self.mode == "client" and not connected:
            self.lost_since = self.lost_since or time.monotonic()
        elif connected:
            self.lost_since = None
        ips = await self.lan_ips()
        self.ip = ips[0] if ips else ""
        if self.mode == "client" and connected:
            rc, out = await self.run("nmcli", "networking", "connectivity", "check", timeout=15)
            self.connectivity = out.strip() if rc == 0 and out.strip() else "unknown"
        else:
            self.connectivity = "none"
        self.on_change()

    async def watchdog(self):
        """Fall back to the hotspot if the client network is lost for 45 s."""
        while True:
            await asyncio.sleep(10)
            if self.lock.locked() or self.mode == "switching":
                continue
            try:
                await self.refresh()
            except Exception as e:  # noqa
                self.log(f"Wi-Fi: status check failed: {e}")
                continue
            if self.mode == "client" and self.lost_since and time.monotonic() - self.lost_since > 45:
                await self.start_hotspot(f"lost {self.ssid}")
            elif self.mode == "hotspot" and not self.fake:
                self.ap_down_checks = 0 if self.ap_up else self.ap_down_checks + 1
                if self.ap_down_checks >= 2:      # ~20 s without a working hotspot
                    self.ap_down_checks = 0
                    await self.start_hotspot("hotspot wasn't running - retrying")

    async def boot(self):
        """At power-on: join a remembered network that's in range, else start the hotspot."""
        if self.fake:
            await self._start_hotspot()
            return
        await self.run("rfkill", "unblock", "all", timeout=5)
        for _ in range(30):
            rc, _ = await self.run("nmcli", "-t", "general", "status", timeout=5)
            if rc == 0:
                break
            await asyncio.sleep(1)
        await self.wait_wifi_ready()
        saved = await self.saved_networks()
        for name, _ssid in saved:   # we decide; stop NM auto-joining on its own
            await self.run("nmcli", "con", "modify", name, "connection.autoconnect", "no", timeout=10)
        if saved and self.cfg.get("wifi_auto_join", True):
            await self.run("nmcli", "dev", "wifi", "rescan", "ifname", IFACE, timeout=15)
            await asyncio.sleep(3)
            rc, out = await self.run("nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", timeout=15)
            visible = set(split_terse(l)[0] for l in out.splitlines()) if rc == 0 else set()
            recent = self.cfg.get("wifi_recent", [])
            order = sorted(saved, key=lambda ns: recent.index(ns[1]) if ns[1] in recent else 99)
            for name, ssid in order:
                if ssid in visible:
                    self.log(f"Wi-Fi: trying remembered network '{ssid}'")
                    rc, out = await self.run("nmcli", "--wait", "25", "con", "up", name, timeout=35)
                    if rc == 0:
                        self.mode, self.ssid = "client", ssid
                        self.log(f"Wi-Fi: connected to '{ssid}'")
                        await self.refresh()
                        return
        await self._start_hotspot()

    def status(self):
        return {
            "mode": self.mode, "ssid": self.ssid, "ip": self.ip, "connectivity": self.connectivity,
            "hotspot_ssid": self.hotspot_ssid(), "hotspot_mode": self.cfg.get("hotspot_mode", "local"),
            "networks": self.networks, "message": self.message, "mac": self.mac(),
        }


async def _cli(cmd):
    path = boot_config_path()
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        cfg = {}

    def save():
        try:
            path.write_text(json.dumps(cfg, indent=2))
        except OSError:
            pass
    net = Net(lambda: cfg, save)
    if cmd == "boot":
        await net.boot()
    elif cmd == "hotspot":
        await net.start_hotspot("manual")
    elif cmd == "scan":
        await net.refresh()
        print(json.dumps(await net.scan(), indent=2))
        return
    await net.refresh()
    print(json.dumps(net.status(), indent=2))


if __name__ == "__main__":
    import sys
    asyncio.run(_cli(sys.argv[1] if len(sys.argv) > 1 else "status"))

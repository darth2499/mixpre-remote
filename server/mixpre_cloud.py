"""
MixPre Remote - cloud link.

When the Pi has internet it keeps one WebSocket open to the relay (Cloudflare).
The relay introduces browsers to the Pi; they then try a direct WebRTC data
channel (same local network first, then direct over the internet). If neither
works, commands and state flow through the relay itself.
"""
import asyncio
import base64
import json
import logging
import random

import aiohttp

log = logging.getLogger("mixpre.cloud")

try:
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    HAVE_RTC = True
except Exception:  # noqa
    HAVE_RTC = False

STUN = "stun:stun.cloudflare.com:3478"
MAX_PEERS = 6


class DataChannelClient:
    """A browser connected directly over WebRTC."""
    kind = "p2p"

    def __init__(self, channel):
        self.channel = channel

    async def send_text(self, text):
        if self.channel.readyState == "open":
            self.channel.send(text)


class Cloud:
    def __init__(self, bridge):
        self.bridge = bridge
        self.ws = None
        self.connected = False
        self.relay_viewers = 0
        self.viewers = 0
        self.peers = {}            # client id -> RTCPeerConnection
        self._task = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------ config
    def url(self):
        u = str(self.bridge.config.get("relay_url") or "").strip().rstrip("/")
        if not u:
            return ""
        if u.startswith("https://"):
            u = "wss://" + u[8:]
        elif u.startswith("http://"):
            u = "ws://" + u[7:]
        elif not u.startswith(("ws://", "wss://")):
            u = "wss://" + u
        return u

    def restart(self):
        """Called when relay_url or code changes."""
        self._wake.set()
        if self.ws is not None and not self.ws.closed:
            asyncio.ensure_future(self.ws.close())

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    # ------------------------------------------------------------ main loop
    async def run(self):
        backoff = 2
        while True:
            url, code = self.url(), self.bridge.code
            if not url or not code:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(f"{url}/pi?code={code}", heartbeat=None,
                                                  timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                        self.ws, self.connected, backoff = ws, True, 2
                        self.bridge.emit_log(f"Cloud: connected to relay ({url.split('//')[-1]})")
                        await self.hello()
                        pinger = asyncio.ensure_future(self._ping(ws))
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    if msg.data == "pong":
                                        continue
                                    await self.handle(msg.data)
                                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                    break
                        finally:
                            pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa  (no internet, DNS, relay down...)
                log.debug("relay connect failed: %s", e)
            if self.connected:
                self.bridge.emit_log("Cloud: disconnected from relay")
            self.ws, self.connected, self.relay_viewers, self.viewers = None, False, 0, 0
            self.bridge.net_changed()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), backoff + random.random())
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)

    async def _ping(self, ws):
        n = 0
        while not ws.closed:
            await asyncio.sleep(30)
            n += 1
            try:
                await ws.send_str("ping")
                if n % 4 == 0:          # re-announce every 2 min so the online list stays fresh
                    await self.hello()
            except Exception:  # noqa
                return

    async def send(self, obj):
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(obj))
            except Exception:  # noqa
                pass

    async def hello(self):
        n = self.bridge.net
        await self.send({"t": "hello", "name": self.bridge.name, "version": self.bridge.version,
                         "lan": await n.lan_ips() if n else [], "wifi": n.ssid if n else ""})
        self.bridge.net_changed()

    # ------------------------------------------------------------ messages from the relay
    async def handle(self, text):
        try:
            m = json.loads(text)
        except ValueError:
            return
        t = m.get("t")
        if t == "clients":
            had = self.relay_viewers
            self.relay_viewers, self.viewers = int(m.get("relay", 0)), int(m.get("total", 0))
            if m.get("joined") or (self.relay_viewers and not had):
                await self.send_full_state()
        elif t == "cmd":
            try:
                self.bridge.enqueue(base64.b64decode(m.get("d", "")))
            except (ValueError, TypeError):
                pass
        elif t == "json":
            if isinstance(m.get("d"), dict):
                await self.bridge.handle_json(m["d"])
        elif t == "offer":
            await self.on_offer(m.get("from", ""), m.get("sdp", ""))

    async def broadcast(self, text):
        """State/log JSON to viewers that are using the relay (not direct)."""
        if self.relay_viewers > 0:
            await self.send({"t": "bcast", "d": text})

    async def send_full_state(self):
        for text in self.bridge.hello_messages():
            await self.send({"t": "bcast", "d": text})

    # ------------------------------------------------------------ WebRTC
    async def on_offer(self, cid, sdp):
        if not HAVE_RTC:
            self.bridge.emit_log("Cloud: direct connection unavailable (aiortc not installed) - using relay")
            return
        if not cid or not sdp:
            return
        old = self.peers.pop(cid, None)
        if old:
            await old.close()
        while len(self.peers) >= MAX_PEERS:
            _, p = self.peers.popitem()
            await p.close()
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)]))
        self.peers[cid] = pc
        bridge = self.bridge

        @pc.on("datachannel")
        def on_datachannel(channel):
            client = DataChannelClient(channel)

            @channel.on("open")
            def _open():
                pass

            @channel.on("message")
            def on_message(message):
                if isinstance(message, (bytes, bytearray)):
                    bridge.enqueue(bytes(message))
                elif isinstance(message, str):
                    asyncio.ensure_future(bridge.handle_text(message, client))

            @channel.on("close")
            def on_close():
                bridge.clients.discard(client)

            bridge.clients.add(client)
            for text in bridge.hello_messages():
                asyncio.ensure_future(client.send_text(text))

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState == "connected":
                bridge.emit_log("Cloud: direct connection established")
            if pc.connectionState in ("failed", "closed"):
                if self.peers.get(cid) is pc:
                    del self.peers[cid]
                await pc.close()

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)   # aiortc gathers all candidates here
            await self.send({"t": "answer", "to": cid, "sdp": pc.localDescription.sdp})
        except Exception as e:  # noqa
            bridge.emit_log(f"Cloud: direct connection setup failed ({e}) - using relay")
            self.peers.pop(cid, None)
            await pc.close()

/**
 * MixPre Remote relay (Cloudflare Worker + Durable Objects)
 *
 *   wss://<relay>/pi?code=ID            a Pi stays connected here while it has internet
 *   wss://<relay>/watch?key=KEY         the control page's live list of online Pis
 *   wss://<relay>/client?code=ID&key=   the control page talking to one Pi
 *   https://<relay>/turn?key=KEY        TURN servers for the Send/Listen audio (optional)
 *
 * - Directory (one object): which Pis are online, their names, and whether a
 *   viewer is on the same network (same public IP).
 * - Room (one per Pi): introduces browser and Pi for a direct WebRTC link
 *   (local network first) and relays commands/state if a direct link fails.
 *
 * ACCESS_KEY (Cloudflare secret): if set, the list and control need this key.
 * TURN_KEY_ID + TURN_KEY_API_TOKEN (secrets, optional): Cloudflare Realtime TURN
 *   credentials so Send/Listen audio works on any network (e.g. cellular).
 * Uses the WebSocket Hibernation API, so idle connections cost nothing.
 */
import { DurableObject } from "cloudflare:workers";

const CODE_RE = /^[A-Z0-9]{6,16}$/;
const MAX_MSG = 64 * 1024;
const MAX_CLIENTS = 8;
const MAX_MSGS_PER_SEC = 60;            // per viewer, relayed commands
const STALE_MS = 6 * 60 * 1000;         // Pis re-announce every 2 min

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "access-control-allow-origin": "*" } });

function originOk(request, env) {
  if (!env.ALLOWED_ORIGINS) return true;
  const allowed = env.ALLOWED_ORIGINS.split(",").map(s => s.trim()).filter(Boolean);
  return !allowed.length || allowed.includes(request.headers.get("Origin") || "");
}
function keyOk(url, env) {
  return !env.ACCESS_KEY || url.searchParams.get("key") === env.ACCESS_KEY;
}
const directory = env => env.DIRECTORY.get(env.DIRECTORY.idFromName("main"));

const STUN = [{ urls: "stun:stun.cloudflare.com:3478" }];
async function turnServers(env) {
  if (!env.TURN_KEY_ID || !env.TURN_KEY_API_TOKEN) return STUN;
  const base = `https://rtc.live.cloudflare.com/v1/turn/keys/${env.TURN_KEY_ID}/credentials`;
  const init = {
    method: "POST",
    headers: { Authorization: `Bearer ${env.TURN_KEY_API_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ ttl: 6 * 3600 }),
  };
  try {
    let r = await fetch(`${base}/generate-ice-servers`, init);
    if (r.ok) {
      const d = await r.json();
      if (Array.isArray(d.iceServers)) return d.iceServers;
    }
    r = await fetch(`${base}/generate`, init);   // older endpoint
    if (r.ok) {
      const d = await r.json();
      if (d.iceServers) return [...STUN, d.iceServers];
    }
  } catch {}
  return STUN;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === "/" || path === "/health") {
      return new Response("MixPre relay OK\n", { headers: { "content-type": "text/plain" } });
    }
    const ws = request.headers.get("Upgrade") === "websocket";

    if (path === "/turn") {
      if (!keyOk(url, env)) return json({ error: "key" }, 401);
      return json({ iceServers: await turnServers(env) });
    }

    if (path === "/watch" || path === "/list") {
      if (!originOk(request, env)) return json({ error: "origin" }, 403);
      if (!keyOk(url, env)) return json({ error: "key" }, 401);
      if (path === "/watch" && !ws) return new Response("Expected WebSocket", { status: 426 });
      return directory(env).fetch(request);
    }
    if (path !== "/pi" && path !== "/client") return new Response("Not found", { status: 404 });
    if (!ws) return new Response("Expected WebSocket", { status: 426 });
    const code = (url.searchParams.get("code") || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!CODE_RE.test(code)) return new Response("Bad code", { status: 400 });
    if (path === "/client") {
      if (!originOk(request, env)) return new Response("Origin not allowed", { status: 403 });
      if (!keyOk(url, env)) return new Response("Bad key", { status: 401 });
    }
    return env.ROOMS.get(env.ROOMS.idFromName(code)).fetch(request);
  },
};

/* ======================================================================= Directory */
export class Directory extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  // called by Room objects (RPC)
  async update(code, info) {
    await this.ctx.storage.put("pi:" + code, { ...info, code, seen: Date.now() });
    await this.push();
  }
  async remove(code, since) {
    const cur = await this.ctx.storage.get("pi:" + code);
    if (cur && (!since || cur.since === since)) {
      await this.ctx.storage.delete("pi:" + code);
      await this.push();
    }
  }

  async entries() {
    const all = await this.ctx.storage.list({ prefix: "pi:" });
    const now = Date.now(), out = [];
    for (const [k, v] of all) {
      if (now - (v.seen || 0) > STALE_MS) { await this.ctx.storage.delete(k); continue; }
      out.push(v);
    }
    return out.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  }
  view(list, viewerIp) {
    return list.map(p => ({
      code: p.code, name: p.name, version: p.version, wifi: p.wifi, lan: p.lan, since: p.since,
      sameNetwork: !!viewerIp && p.ip === viewerIp,
    }));
  }
  async push() {
    const watchers = this.ctx.getWebSockets("watch");
    if (!watchers.length) return;
    const list = await this.entries();
    for (const w of watchers) {
      const ip = w.deserializeAttachment()?.ip || "";
      try { w.send(JSON.stringify({ t: "list", pis: this.view(list, ip) })); } catch {}
    }
  }

  async fetch(request) {
    const ip = request.headers.get("CF-Connecting-IP") || "";
    if (new URL(request.url).pathname === "/list") {
      return json({ pis: this.view(await this.entries(), ip) });
    }
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, ["watch"]);
    server.serializeAttachment({ ip });
    server.send(JSON.stringify({ t: "list", pis: this.view(await this.entries(), ip) }));
    return new Response(null, { status: 101, webSocket: client });
  }
  async webSocketMessage(ws, data) {
    if (data === "refresh") {
      const list = await this.entries();
      try { ws.send(JSON.stringify({ t: "list", pis: this.view(list, ws.deserializeAttachment()?.ip || "") })); } catch {}
    }
  }
  async webSocketClose() {}
  async webSocketError() {}
}

/* ======================================================================= Room (one Pi) */
export class Room extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  async fetch(request) {
    const url = new URL(request.url);
    const role = url.pathname === "/pi" ? "pi" : "client";
    const code = (url.searchParams.get("code") || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    const [client, server] = Object.values(new WebSocketPair());

    if (role === "pi") {
      for (const old of this.ctx.getWebSockets("pi")) {   // one Pi per ID
        try { old.close(4000, "replaced"); } catch {}
      }
      this.ctx.acceptWebSocket(server, ["pi"]);
      server.serializeAttachment({ role: "pi", code, ip: request.headers.get("CF-Connecting-IP") || "", info: {} });
    } else {
      const clients = this.ctx.getWebSockets("client");
      if (clients.length >= MAX_CLIENTS) {
        try { clients[0].close(4001, "too many viewers"); } catch {}
      }
      const id = crypto.randomUUID().slice(0, 8);
      this.ctx.acceptWebSocket(server, ["client", "c:" + id]);
      server.serializeAttachment({ role: "client", id, p2p: false, win: 0, n: 0 });
      queueMicrotask(() => {
        this.send(server, { t: "you", id });
        this.send(server, this.presence());
        this.toPi({ t: "clients", ...this.counts(), joined: id });
      });
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  // ---------------------------------------------------------------- helpers
  send(ws, obj) {
    try { ws.send(typeof obj === "string" ? obj : JSON.stringify(obj)); } catch {}
  }
  pi() {
    return this.ctx.getWebSockets("pi")[0] || null;
  }
  toPi(obj) {
    const p = this.pi();
    if (p) this.send(p, obj);
    return !!p;
  }
  toClients(obj, onlyRelay = false) {
    const msg = JSON.stringify(obj);
    for (const ws of this.ctx.getWebSockets("client")) {
      if (onlyRelay && ws.deserializeAttachment()?.p2p) continue;
      this.send(ws, msg);
    }
  }
  counts(exclude = null) {
    const all = this.ctx.getWebSockets("client").filter(ws => ws !== exclude);
    const relay = all.filter(ws => !ws.deserializeAttachment()?.p2p).length;
    return { total: all.length, relay };
  }
  presence() {
    const p = this.pi();
    const info = p ? p.deserializeAttachment()?.info || {} : {};
    return { t: "presence", online: !!p, ...info };
  }

  // ---------------------------------------------------------------- events
  async webSocketMessage(ws, data) {
    if (typeof data !== "string" || data.length > MAX_MSG) return;
    let m;
    try { m = JSON.parse(data); } catch { return; }
    const att = ws.deserializeAttachment() || {};

    if (att.role === "pi") {
      switch (m.t) {
        case "hello": {   // sent on connect and every 2 minutes
          const info = {
            name: String(m.name || "").slice(0, 40),
            version: String(m.version || "").slice(0, 20),
            lan: Array.isArray(m.lan) ? m.lan.slice(0, 4).map(String) : [],
            wifi: String(m.wifi || "").slice(0, 40),
            since: att.info?.since || Date.now(),
          };
          const changed = JSON.stringify({ ...att.info, since: 0 }) !== JSON.stringify({ ...info, since: 0 });
          ws.serializeAttachment({ ...att, info });
          if (changed) this.toClients(this.presence());
          this.send(ws, { t: "clients", ...this.counts() });
          await directory(this.env).update(att.code, { ...info, ip: att.ip });
          break;
        }
        case "answer":
        case "to": {
          const target = this.ctx.getWebSockets("c:" + m.to)[0];
          if (target) this.send(target, m.t === "answer" ? { t: "answer", sdp: m.sdp } : { t: "msg", d: m.d });
          break;
        }
        case "bcast":
          this.toClients({ t: "bcast", d: m.d }, true);
          break;
      }
      return;
    }

    // ---- viewer
    const now = Math.floor(Date.now() / 1000);
    if (att.win !== now) { att.win = now; att.n = 0; }
    att.n++;
    if (att.n > MAX_MSGS_PER_SEC && (m.t === "cmd" || m.t === "json")) {
      ws.serializeAttachment(att);
      return;
    }
    switch (m.t) {
      case "cmd":
      case "json":
      case "offer":
        if (!this.toPi({ ...m, from: att.id })) this.send(ws, { t: "presence", online: false });
        break;
      case "peer": {   // Send/Listen audio signaling between viewers
        const out = JSON.stringify({ t: "peer", from: att.id, d: m.d });
        if (m.to) {
          const target = this.ctx.getWebSockets("c:" + m.to)[0];
          if (target) this.send(target, out);
        } else {
          for (const c of this.ctx.getWebSockets("client")) if (c !== ws) this.send(c, out);
        }
        break;
      }
      case "p2p":
        att.p2p = !!m.on;
        ws.serializeAttachment(att);
        this.toPi({ t: "clients", ...this.counts() });
        break;
    }
    ws.serializeAttachment(att);
  }

  async webSocketClose(ws) {
    await this.onGone(ws);
  }
  async webSocketError(ws) {
    await this.onGone(ws);
  }
  async onGone(ws) {
    const att = ws.deserializeAttachment() || {};
    if (att.role === "pi") {
      const still = this.ctx.getWebSockets("pi").filter(p => p !== ws);
      if (!still.length) {
        this.toClients({ t: "presence", online: false });
        await directory(this.env).remove(att.code, att.info?.since);
      }
    } else {
      this.toPi({ t: "clients", ...this.counts(ws), left: att.id });
      const out = JSON.stringify({ t: "peer", from: att.id, d: { a: "left" } });
      for (const c of this.ctx.getWebSockets("client")) if (c !== ws) this.send(c, out);
    }
  }
}

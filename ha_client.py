"""Home Assistant connection for the Smart TV panel (ha_view.py).

Talks to Home Assistant's WebSocket API (/api/websocket) with only the Python
standard library: a tiny WebSocket client over a TLS socket. It loads the
rooms (areas), devices, entities and their states once, then follows every
state change live, and sends service calls (turn a light on...).

All network work happens in one background thread; the GTK side reads the
data with snapshot() and gets told about changes on the GTK main loop.

Settings live in ~/.config/smart-tv/homeassistant.conf ("key = value" lines):

  url = https://homeassistant.local:8123
  token = <long-lived access token from your HA profile, Security tab>
  verify_ssl = yes          # "no" for a self-signed certificate
  favorites = light.living_room, scene.movie   # optional first row
  hide = switch.some_thing                     # optional, never shown
"""
import base64
import json
import os
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request

from gi.repository import GLib

CONFIG = os.path.expanduser("~/.config/smart-tv/homeassistant.conf")
ICON = os.path.expanduser("~/.local/share/smart-tv/homeassistant.png")
DEFAULT_URL = "http://homeassistant.local:8123"
PING_S = 20            # keepalive ping when nothing arrives for this long
NOTIFY_MS = 80         # changes are passed to the view at most this often


def load_config():
    cfg = {"url": DEFAULT_URL, "token": "", "verify_ssl": True, "favorites": [], "hide": []}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return cfg
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (p.strip() for p in line.split("=", 1))
        value = value.strip("\"'")
        key = key.lower()
        if key == "url" and value:
            cfg["url"] = value.rstrip("/")
        elif key == "token":
            cfg["token"] = value
        elif key == "verify_ssl":
            cfg["verify_ssl"] = value.lower() not in ("no", "false", "0", "off")
        elif key in ("favorites", "hide"):
            cfg[key] = [v for v in value.replace(",", " ").split() if "." in v]
    return cfg


class AuthError(Exception):
    pass


class WebSocket:
    """Just enough of RFC 6455 for Home Assistant: text frames, ping/pong, close."""

    def __init__(self, url, verify=True, timeout=10):
        u = urllib.parse.urlsplit(url)
        secure = u.scheme in ("https", "wss")
        host = u.hostname or "localhost"
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        self.sock = sock
        self.buf = bytearray()
        self.parts = []          # fragments of a message split over frames
        self.queue = []          # messages read ahead by receive()
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path.rstrip("/") or "") + "/api/websocket"
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            self.fill()
            if len(self.buf) > 65536:
                raise ConnectionError("bad handshake")
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(status.decode(errors="replace"))

    def fileno(self):
        return self.sock.fileno()

    def tls_pending(self):
        return isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0

    def fill(self):
        data = self.sock.recv(1 << 16)
        if not data:
            raise ConnectionError("connection closed")
        self.buf += data

    def send(self, text, opcode=0x1):
        data = text.encode() if isinstance(text, str) else text
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        if n:
            k = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")
        self.sock.sendall(head + mask + data)

    def frame(self):
        """Next complete frame from the buffer as (fin, opcode, payload), or None."""
        b = self.buf
        if len(b) < 2:
            return None
        n, i = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        masked = b[1] & 0x80
        if masked:
            i += 4
        if len(b) < i + n:
            return None
        payload = bytes(b[i:i + n])
        if masked:
            mask = b[i - 4:i]
            payload = bytes(c ^ mask[j % 4] for j, c in enumerate(payload))
        fin, op = b[0] & 0x80, b[0] & 0x0F
        del b[:i + n]
        return fin, op, payload

    def messages(self):
        """Complete text messages in the buffer (answers pings on the way)."""
        out, self.queue = self.queue, []
        while True:
            f = self.frame()
            if f is None:
                return out
            fin, op, payload = f
            if op == 0x9:
                self.send(payload, 0xA)
            elif op == 0x8:
                raise ConnectionError("closed by server")
            elif op in (0x0, 0x1, 0x2):
                self.parts.append(payload)
                if fin:
                    out.append(b"".join(self.parts).decode("utf-8", "replace"))
                    self.parts = []

    def receive(self):
        """Blocking: wait for the next text message (used while logging in)."""
        while True:
            msgs = self.messages()
            if msgs:
                self.queue = msgs[1:]   # the rest comes out of messages() later
                return json.loads(msgs[0])
            self.fill()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class HAClient:
    """Live copy of the Home Assistant state. start() / stop() may be called many times."""

    def __init__(self, cfg, on_change=None):
        self.cfg = cfg
        self.on_change = on_change
        self.lock = threading.Lock()
        self.status = "idle" if cfg["token"] else "no_token"   # connecting, ready, offline, auth
        self.error = ""
        self.loaded = False
        self.areas = []            # [(area_id, name)] in Home Assistant's order
        self.devices = {}          # device_id -> area_id
        self.entities = {}         # entity_id -> {"area", "device", "hidden", "category", "icon"}
        self.states = {}           # entity_id -> state object
        self.watch = None          # entity ids the view shows; other changes don't wake it
        self.failed = []           # error messages of service calls, for the view to show
        self.outbox = []
        self.gen = 0               # bumped to stop the running thread
        self.thread = None
        self.stop_id = None
        self.notify_id = None
        self.next_id = 100
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    def key(self):
        c = self.cfg
        return c["url"], c["token"], c["verify_ssl"]

    # --- lifetime (GTK thread) ---------------------------------------------
    def start(self):
        if self.stop_id:
            GLib.source_remove(self.stop_id)
            self.stop_id = None
        if not self.cfg["token"]:
            self.status = "no_token"
            return
        if self.status == "auth":
            self.status = "idle"   # maybe the token was fixed meanwhile: try again
        if self.thread and self.thread.is_alive():
            return
        self.gen += 1
        self.thread = threading.Thread(target=self.run, args=(self.gen,), daemon=True)
        self.thread.start()

    def stop_later(self, seconds=60):
        """Stay connected a while, so opening the panel again is instant."""
        if self.stop_id:
            GLib.source_remove(self.stop_id)
        self.stop_id = GLib.timeout_add_seconds(seconds, self._stop_now)

    def _stop_now(self):
        self.stop_id = None
        self.stop()
        return False

    def stop(self):
        self.gen += 1
        self.wake()

    def wake(self):
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    # --- talking to Home Assistant (GTK thread) -----------------------------
    def call(self, domain, service, entity_id, data=None):
        with self.lock:
            self.next_id += 1
            msg = {"id": self.next_id, "type": "call_service", "domain": domain, "service": service,
                   "service_data": data or {}, "target": {"entity_id": entity_id}}
            self.outbox.append(json.dumps(msg))
        self.wake()

    def snapshot(self):
        with self.lock:
            return list(self.areas), dict(self.devices), dict(self.entities), dict(self.states)

    def take_failures(self):
        with self.lock:
            out, self.failed = self.failed, []
        return out

    # --- background thread --------------------------------------------------
    def notify(self):
        def deliver():
            self.notify_id = None
            if self.on_change:
                self.on_change()
            return False

        def schedule():
            if self.notify_id is None:
                self.notify_id = GLib.timeout_add(NOTIFY_MS, deliver)
            return False
        GLib.idle_add(schedule, priority=GLib.PRIORITY_DEFAULT)   # not starved by animations

    def set_status(self, status, error=""):
        self.status, self.error = status, error
        self.notify()

    def run(self, gen):
        delay = 2
        while gen == self.gen:
            if not self.loaded or self.status != "offline":
                self.set_status("connecting")
            try:
                self.session(gen)
                delay = 2
            except AuthError as e:
                self.set_status("auth", str(e))
                return
            except (OSError, ValueError, ConnectionError) as e:
                if gen != self.gen:
                    return
                print(f"home assistant: {e}", flush=True)
                self.set_status("offline", str(e))
            end = time.monotonic() + delay
            while gen == self.gen and time.monotonic() < end:
                select.select([self.wake_r], [], [], max(0.0, end - time.monotonic()))
                self.drain_wake()
            delay = min(30, delay * 2)

    def drain_wake(self):
        try:
            while os.read(self.wake_r, 512):
                pass
        except OSError:
            pass

    def session(self, gen):
        ws = WebSocket(self.cfg["url"], self.cfg["verify_ssl"])
        try:
            msg = ws.receive()
            if msg.get("type") != "auth_required":
                raise ConnectionError("not a Home Assistant server")
            ws.send(json.dumps({"type": "auth", "access_token": self.cfg["token"]}))
            msg = ws.receive()
            if msg.get("type") == "auth_invalid":
                raise AuthError(msg.get("message", "invalid token"))
            if msg.get("type") != "auth_ok":
                raise ConnectionError(f"unexpected answer: {msg.get('type')}")
            print(f"home assistant: connected ({msg.get('ha_version', '?')})", flush=True)
            with self.lock:
                self.outbox = []   # nothing old: a light switched minutes later would be a surprise
            # 1-4: what to show, 5: live changes after that
            asks = ["config/area_registry/list", "config/device_registry/list",
                    "config/entity_registry/list_for_display", "get_states"]
            for i, kind in enumerate(asks, 1):
                ws.send(json.dumps({"id": i, "type": kind}))
            ws.send(json.dumps({"id": 5, "type": "subscribe_events", "event_type": "state_changed"}))
            self.fetch_icon()
            got = set()
            ws.sock.settimeout(10)
            quiet = 0
            while gen == self.gen:
                for text in ws.messages():
                    self.handle(json.loads(text), got)
                if ws.tls_pending():      # decrypted data select() can't see
                    ws.fill()
                    continue
                ready, _, _ = select.select([ws, self.wake_r], [], [], PING_S)
                if not ready:
                    quiet += 1
                    if quiet > 2:
                        raise ConnectionError("no answer")
                    ws.send(json.dumps({"id": self.take_id(), "type": "ping"}))
                    continue
                if self.wake_r in ready:
                    self.drain_wake()
                    self.flush(ws)
                if ws in ready:
                    ws.fill()
                    quiet = 0
        finally:
            ws.close()

    def take_id(self):
        with self.lock:
            self.next_id += 1
            return self.next_id

    def flush(self, ws):
        with self.lock:
            out, self.outbox = self.outbox, []
        for text in out:
            ws.send(text)

    def handle(self, msg, got):
        kind = msg.get("type")
        if kind == "event":
            data = msg.get("event", {}).get("data", {})
            eid, new = data.get("entity_id"), data.get("new_state")
            with self.lock:
                if new:
                    self.states[eid] = new
                else:
                    self.states.pop(eid, None)
                watched = self.watch is None or eid in self.watch
            if watched and self.loaded:
                self.notify()
            return
        if kind != "result":
            return
        i, result = msg.get("id"), msg.get("result")
        if not msg.get("success"):
            err = (msg.get("error") or {}).get("message", "chyba")
            if i in (1, 2, 3, 4):
                raise ConnectionError(f"{i}: {err}")
            if i and i > 100:
                with self.lock:
                    self.failed.append(err)
                self.notify()
            return
        with self.lock:
            if i == 1:
                self.areas = [(a["area_id"], a.get("name") or a["area_id"]) for a in result or []]
            elif i == 2:
                self.devices = {d["id"]: d.get("area_id") for d in result or []}
            elif i == 3:
                self.entities = {}
                for e in (result or {}).get("entities", []):
                    self.entities[e["ei"]] = {"area": e.get("ai"), "device": e.get("di"),
                                              "hidden": bool(e.get("hb")),
                                              "category": e.get("ec") is not None, "icon": e.get("ic")}
            elif i == 4:
                self.states = {s["entity_id"]: s for s in result or []}
        if i in (1, 2, 3, 4):
            got.add(i)
            if got == {1, 2, 3, 4}:
                self.loaded = True
                self.set_status("ready")

    def fetch_icon(self):
        """Home Assistant's own logo for the home screen card (once)."""
        if os.path.isfile(ICON):
            return
        ctx = None
        if not self.cfg["verify_ssl"]:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(self.cfg["url"] + "/static/icons/favicon-192x192.png",
                                        timeout=5, context=ctx) as r:
                data = r.read(2_000_000)
            if data.startswith(b"\x89PNG"):
                os.makedirs(os.path.dirname(ICON), exist_ok=True)
                with open(ICON + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(ICON + ".tmp", ICON)
        except (OSError, ValueError):
            pass

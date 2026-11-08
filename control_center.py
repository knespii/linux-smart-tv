"""Control center: a TV-style overlay over the app in front with volume, mute,
audio output and media controls (play/pause, previous, next, progress).

Volume goes through PipeWire (wpctl / pactl). What's playing comes from MPRIS,
which Chrome provides for YouTube and Netflix. SmartTV turns gamepad input into
action() calls; this module knows nothing about evdev.
"""
import json
import math
import subprocess
import threading
import time
import urllib.request

import home_view as hv
from home_view import Gdk, GdkPixbuf, Gio, GLib, Gsk, Gtk, Pango, rgba, rect, rounded, radial, draw_text, draw_layout

SINK = "@DEFAULT_AUDIO_SINK@"
STEP = 5                      # volume step in %
OPEN_S, CLOSE_S = 0.35, 0.18
SMOOTH = 20.0
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ART_W, ART_H = 320, 180          # video thumbnail size
ART_FADE_S = 0.35


# --- audio (PipeWire) ------------------------------------------------------
def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def read_audio():
    """{"volume": 0..1, "muted": bool, "sinks": [{name, label, icon}], "default": name}"""
    out = {"volume": None, "muted": False, "sinks": [], "default": None}
    r = _run(["wpctl", "get-volume", SINK])
    if r and r.returncode == 0:
        parts = r.stdout.split()
        try:
            out["volume"] = float(parts[1])
        except (IndexError, ValueError):
            pass
        out["muted"] = "[MUTED]" in r.stdout
    r = _run(["pactl", "get-default-sink"])
    if r and r.returncode == 0:
        out["default"] = r.stdout.strip()
    r = _run(["pactl", "-f", "json", "list", "sinks"])
    if r and r.returncode == 0:
        try:
            for s in json.loads(r.stdout):
                out["sinks"].append(sink_info(s))
        except (ValueError, KeyError, TypeError):
            pass
    return out


def sink_info(s):
    port = (s.get("active_port") or "").lower()
    desc = s.get("description") or s.get("name", "?")
    if "headphone" in port:
        label, icon = "Sluchátka", "audio-headphones-symbolic"
    elif "hdmi" in port or "hdmi" in desc.lower() or "displayport" in port:
        label, icon = "Monitor (HDMI)", "video-display-symbolic"
    elif "speaker" in port or "analog" in port:
        label, icon = "Reproduktory", "audio-speakers-symbolic"
    elif "bluez" in s.get("name", ""):
        label, icon = desc, "audio-headphones-symbolic"
    else:
        label, icon = desc, "audio-card-symbolic"
    return {"name": s.get("name"), "label": label, "icon": icon}


def change_volume(step_percent):
    sign = "+" if step_percent > 0 else "-"
    _run(["wpctl", "set-volume", "-l", "1.0", SINK, f"{abs(step_percent)}%{sign}"])
    _run(["wpctl", "set-mute", SINK, "0"])


def toggle_mute():
    _run(["wpctl", "set-mute", SINK, "toggle"])


def set_default_sink(name):
    _run(["pactl", "set-default-sink", name])


# --- media (MPRIS) -----------------------------------------------------------
def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def find_player(pids):
    """The MPRIS player of the app in front (by process id), else any playing one.
    Returns (bus name, properties dict) or None."""
    try:
        bus = _bus()
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "ListNames", None, GLib.VariantType("(as)"),
                              Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    except GLib.Error:
        return None
    playing = None
    for name in names:
        if not name.startswith(MPRIS_PREFIX):
            continue
        try:
            pid = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)),
                                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            props = bus.call_sync(name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                                  GLib.Variant("(s)", (PLAYER_IFACE,)), GLib.VariantType("(a{sv})"),
                                  Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        except GLib.Error:
            continue
        if pid in pids:
            return name, props
        if playing is None and props.get("PlaybackStatus") == "Playing":
            playing = (name, props)
    return playing


def player_call(name, method):
    try:
        _bus().call(name, MPRIS_PATH, PLAYER_IFACE, method, None, None, Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        pass


def fmt_time(us):
    s = max(0, int(us // 1_000_000))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --- the view ----------------------------------------------------------------
class ControlCenter:
    def __init__(self, tv, app):
        self.tv = tv
        self.app = app or {}
        self.audio = {"volume": None, "muted": False, "sinks": [], "default": None}
        self.player = None           # (bus name, props)
        self.player_t = time.monotonic()
        self.shown_volume = None     # animated volume bar
        self.volume_t = -10.0        # last volume change (makes the bar glow)
        self.focus = "play"
        now = time.monotonic()
        self.open_t = now
        self.close_t = None
        self.press_t = -10.0
        self.hl = None               # animated focus highlight [x, y, w, h]
        self.targets = {}            # item -> (x, y, w, h), from the last frame
        self.hits = []
        self.tick_id = None
        self.last_frame = None
        self.poll_id = None
        self.window = None
        self.canvas = None
        self.px_factor = 1.0
        self.icon_cache = {}
        self.art_url = None          # thumbnail of what's playing (MPRIS mpris:artUrl)
        self.art = None              # its texture
        self.art_old = None          # previous one, fading out
        self.art_t = -10.0
        self.refresh()

    # --- window ----------------------------------------------------------
    def show(self):
        hv.ensure_css()
        win = Gtk.Window(title="Smart TV ovládání")
        win.set_decorated(False)
        win.add_css_class("smart-tv")
        self.canvas = hv.Canvas(self)
        win.set_child(self.canvas)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        win.add_controller(keys)
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_click)
        self.canvas.add_controller(click)
        win.connect("close-request", lambda *_: self.tv.close_control_center() or True)
        self.window = win
        self.poll_id = GLib.timeout_add(1000, self.on_poll)
        hv.cover_screen(win)   # not fullscreen, so the app stays visible behind
        win.present()
        self.kick()

    def close(self):
        if self.close_t is None:
            self.close_t = time.monotonic()
            self.kick()

    def destroy(self):
        if self.poll_id:
            GLib.source_remove(self.poll_id)
            self.poll_id = None
        if self.window:
            self.window.destroy()
            self.window = self.canvas = None
        return False

    # --- data --------------------------------------------------------------
    def refresh(self):
        """Read volume and the player in the background."""
        pids = self.tv.player_pids()

        def work():
            audio = read_audio()
            player = find_player(pids)
            GLib.idle_add(self.got_data, audio, player)
        threading.Thread(target=work, daemon=True).start()

    def got_data(self, audio, player):
        if audio["volume"] is not None:
            self.audio = audio
        self.player = player
        self.player_t = time.monotonic()
        url = (self.props().get("Metadata", {}) or {}).get("mpris:artUrl") or None
        if url != self.art_url:
            self.art_url = url
            self.load_art(url)
        if self.focus not in self.items():
            self.focus = "play" if "play" in self.items() else self.items()[0]
        self.kick()
        return False

    def load_art(self, url):
        """Load the thumbnail in the background (Chrome gives a file:// URL, Spotify https://)."""
        if not url:
            self.got_art(None, None)
            return

        def work():
            pb = None
            try:
                # decoded straight at about the size it's shown (2x for sharp HiDPI)
                if url.startswith("file://"):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(Gio.File.new_for_uri(url).get_path(),
                                                                 ART_W * 3, ART_H * 3, True)
                elif url.startswith(("https://", "http://")):
                    with urllib.request.urlopen(url, timeout=5) as r:
                        data = r.read(20_000_000)
                    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
                    pb = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, ART_W * 3, ART_H * 3, True, None)
            except (OSError, ValueError, GLib.Error):
                pb = None
            GLib.idle_add(self.got_art, url, pb)
        threading.Thread(target=work, daemon=True).start()

    def got_art(self, url, pb):
        if url != self.art_url:
            return False   # a newer thumbnail is already on its way
        tex = Gdk.Texture.new_for_pixbuf(pb) if pb else None
        if tex is not self.art:
            self.art_old, self.art, self.art_t = self.art, tex, time.monotonic()
            self.kick()
        return False

    def on_poll(self):
        self.refresh()
        if self.playing() and self.canvas:  # keep the progress bar moving
            self.kick()
        return True

    def props(self):
        return self.player[1] if self.player else {}

    def playing(self):
        return self.props().get("PlaybackStatus") == "Playing"

    def position(self):
        p = self.props()
        pos = p.get("Position", 0) or 0
        if self.playing():
            pos += (time.monotonic() - self.player_t) * 1_000_000 * (p.get("Rate", 1.0) or 1.0)
        return pos

    def items(self):
        has_player = bool(self.player)
        out = []
        if has_player and self.props().get("CanGoPrevious"):
            out.append("prev")
        out.append("play")
        if has_player and self.props().get("CanGoNext"):
            out.append("next")
        out += ["mute", "vol-", "vol+"]
        if len(self.audio["sinks"]) > 1:
            out.append("output")
        return out

    # --- actions -------------------------------------------------------------
    def action(self, name):
        if self.close_t is not None:
            return
        items = self.items()
        if name in ("left", "right"):
            i = items.index(self.focus) if self.focus in items else 0
            self.focus = items[(i + (1 if name == "right" else -1)) % len(items)]
        elif name in ("up", "down"):
            self.volume(STEP if name == "up" else -STEP)
        elif name == "activate":
            self.press_t = time.monotonic()
            self.do(self.focus)
        elif name in ("vol-", "vol+"):   # A held on a volume button
            self.press_t = time.monotonic()
            self.do(name)
        else:
            self.do(name)
        self.kick()

    def do(self, item):
        if item == "play":
            self.play_pause()
        elif item in ("prev", "next"):
            if self.player:
                player_call(self.player[0], "Previous" if item == "prev" else "Next")
                GLib.timeout_add(400, lambda: self.refresh() or False)
        elif item == "mute":
            self.audio["muted"] = not self.audio["muted"]
            threading.Thread(target=toggle_mute, daemon=True).start()
        elif item == "vol-":
            self.volume(-STEP)
        elif item == "vol+":
            self.volume(STEP)
        elif item == "output":
            self.next_output()
        elif item == "close":
            self.tv.close_control_center()

    def play_pause(self):
        if self.player:
            p = self.props()
            p["PlaybackStatus"] = "Paused" if self.playing() else "Playing"
            p["Position"] = self.position()
            self.player_t = time.monotonic()
            player_call(self.player[0], "PlayPause")
            GLib.timeout_add(500, lambda: self.refresh() or False)
        else:
            self.tv.close_control_center(then_key="space")   # no MPRIS: space in the app

    def volume(self, step):
        v = self.audio["volume"] or 0.0
        self.audio["volume"] = max(0.0, min(1.0, round(v + step / 100, 2)))
        self.audio["muted"] = False
        self.volume_t = time.monotonic()
        threading.Thread(target=change_volume, args=(step,), daemon=True).start()

    def next_output(self):
        sinks = self.audio["sinks"]
        names = [s["name"] for s in sinks]
        i = names.index(self.audio["default"]) if self.audio["default"] in names else -1
        target = names[(i + 1) % len(names)]
        self.audio["default"] = target
        threading.Thread(target=set_default_sink, args=(target,), daemon=True).start()

    # --- real keyboard / mouse -------------------------------------------------
    def on_key(self, _ctrl, keyval, _keycode, _state):
        name = Gdk.keyval_name(keyval)
        mapping = {"Left": "left", "Right": "right", "Up": "up", "Down": "down", "Return": "activate",
                   "KP_Enter": "activate", "space": "play", "m": "mute"}
        if name in ("Escape", "BackSpace"):
            self.tv.close_control_center()
        elif name in mapping:
            self.action(mapping[name])
        return True

    def on_click(self, _gesture, _n, x, y):
        for hx, hy, hw, hh, item in self.hits:
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                self.focus = item
                self.action("activate")
                break

    # --- animation -------------------------------------------------------------
    def kick(self):
        if self.canvas and self.tick_id is None:
            self.last_frame = None
            self.tick_id = self.canvas.add_tick_callback(self.on_tick)

    def on_tick(self, widget, _clock):
        now = time.monotonic()
        dt = 1 / 60 if self.last_frame is None else min(0.05, now - self.last_frame)
        self.last_frame = now
        moving = self.step(dt)
        widget.queue_draw()
        if self.close_t is not None and now - self.close_t >= CLOSE_S:
            self.tick_id = None
            GLib.idle_add(self.destroy)
            return False
        busy = (moving or now - self.open_t < OPEN_S + 0.1 or now - self.press_t < 0.25
                or now - self.art_t < ART_FADE_S + 0.05
                or now - self.volume_t < 0.8 or self.close_t is not None)
        if not busy:
            self.tick_id = None
        return busy

    def step(self, dt):
        f = 1 - math.exp(-dt * SMOOTH)
        moving = False
        target = self.targets.get(self.focus)
        if target:
            if self.hl is None:
                self.hl = list(target)
            for i in range(4):
                if abs(target[i] - self.hl[i]) > 0.3:
                    self.hl[i] += (target[i] - self.hl[i]) * f
                    moving = True
                else:
                    self.hl[i] = target[i]
        vol = self.audio["volume"]
        if vol is not None:
            if self.shown_volume is None:
                self.shown_volume = vol
            elif abs(vol - self.shown_volume) > 0.002:
                self.shown_volume += (vol - self.shown_volume) * (1 - math.exp(-dt * 16))
                moving = True
            else:
                self.shown_volume = vol
        return moving

    # --- drawing ---------------------------------------------------------------
    def icon(self, snap, name, x, y, size, color):
        px = max(8, int(round(size * self.px_factor)))
        key = (name, px)
        if key not in self.icon_cache:
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            self.icon_cache[key] = (theme.lookup_icon(name, None, px, 1, Gtk.TextDirection.NONE, 0)
                                    if theme.has_icon(name) else None)
        p = self.icon_cache[key]
        if p:
            snap.save()
            snap.translate(hv.point(x, y))
            p.snapshot_symbolic(snap, size, size, [color])
            snap.restore()

    def render(self, snap, aw, ah, factor=1):
        if aw <= 0 or ah <= 0:
            return
        now = time.monotonic()
        s = aw / hv.W
        hh = ah / s
        self.px_factor = s * factor
        self.hits = []

        t = (now - self.open_t) / OPEN_S
        fade = hv.ease_out(t)
        rise = (1 - hv.ease_out(t)) * 80
        if self.close_t is not None:
            c = hv.clamp01((now - self.close_t) / CLOSE_S)
            fade *= 1 - c
            rise = max(rise, c * 40)
        if fade < 0.999:
            snap.push_opacity(fade)

        # darken top and bottom, the video stays visible in the middle
        snap.append_linear_gradient(rect(0, 0, aw, ah * 0.3), hv.point(0, 0), hv.point(0, ah * 0.3),
                                    hv.stops((0, rgba(*hv.TINT, 0.75)), (1, rgba(*hv.TINT, 0))))
        snap.append_linear_gradient(rect(0, ah * 0.4, aw, ah * 0.6), hv.point(0, ah * 0.4), hv.point(0, ah),
                                    hv.stops((0, rgba(*hv.TINT, 0)), (0.55, rgba(*hv.TINT, 0.75)),
                                             (1, rgba(*hv.TINT, 0.95))))
        snap.save()
        snap.scale(s, s)
        self.draw_top(snap, rise)
        snap.translate(hv.point(0, rise))
        self.draw_bottom(snap, hh, now, s, rise)
        snap.restore()
        if fade < 0.999:
            snap.pop()

    def accent(self):
        return hv.ACCENTS.get(self.app.get("theme"), hv.ACCENTS["discord"])

    def draw_top(self, snap, rise):
        y = 52 - rise * 0.5
        x = hv.SIDE
        if self.app:
            name = self.app.get("name", "")
            draw_text(snap, name, x, y, 34, weight=Pango.Weight.SEMIBOLD)
            draw_text(snap, "Ovládací centrum", x, y + 46, 20, (1, 1, 1, 0.6))
        else:
            draw_text(snap, "Ovládací centrum", x, y, 34, weight=Pango.Weight.SEMIBOLD)
        draw_text(snap, time.strftime("%H:%M"), hv.W - hv.SIDE, y - 4, 44, (1, 1, 1, 0.95),
                  Pango.Weight.LIGHT, align=1.0)

    def draw_bottom(self, snap, hh, now, s, rise):
        props = self.props()
        meta = props.get("Metadata", {}) or {}
        title = meta.get("xesam:title") or self.app.get("name") or "Ovládací centrum"
        artist = meta.get("xesam:artist")
        artist = ", ".join(artist) if isinstance(artist, list) else (artist or "")
        length = meta.get("mpris:length", 0) or 0
        acc = self.accent()

        # thumbnail, then tag, title, subtitle next to it
        x = hv.SIDE
        y = hh - 400
        a = hv.ease_out((now - self.art_t) / ART_FADE_S)
        shown = a if self.art else (1 - a if self.art_old else 0.0)
        self.draw_art(snap, x, y - 10, a)
        tx = x + (ART_W + 40) * shown
        text_w = hv.W - 2 * hv.SIDE - (ART_W + 40) * shown
        if self.player:
            tag = "PŘEHRÁVÁ SE" if self.playing() else "POZASTAVENO"
        else:
            tag = "ZVUK"
        lay = hv.text_layout(tag, 17, Pango.Weight.BOLD, spacing=1.5)
        tw, th = lay.get_pixel_size()
        snap.push_rounded_clip(rounded(tx, y, tw + 28, th + 12, 8))
        snap.append_color(rgba(*acc, 0.95), rect(tx, y, tw + 28, th + 12))
        snap.pop()
        draw_layout(snap, lay, tx + 14, y + 6, rgba(1, 1, 1))
        draw_text(snap, title, tx, y + 44, 52, weight=Pango.Weight.SEMIBOLD, max_w=text_w)
        sub = artist or ("Hlasitost, zvuk a přehrávání" if not self.player else "")
        if sub:
            draw_text(snap, sub, tx, y + 114, 24, (1, 1, 1, 0.7), max_w=text_w)

        # progress bar
        by = hh - 215
        bw = hv.W - 2 * hv.SIDE
        if self.player and length > 0:
            pos = min(self.position(), length)
            snap.push_rounded_clip(rounded(x, by, bw, 6, 3))
            snap.append_color(rgba(1, 1, 1, 0.22), rect(x, by, bw, 6))
            snap.append_color(rgba(1, 1, 1, 0.95), rect(x, by, bw * pos / length, 6))
            snap.pop()
            self.dot(snap, x + bw * pos / length, by + 3, 9, rgba(1, 1, 1))
            draw_text(snap, fmt_time(length), x + bw, by + 18, 20, (1, 1, 1, 0.6), align=1.0)

        # controls row
        cy = hh - 110
        if self.player:
            draw_text(snap, fmt_time(self.position()), x, cy - 26, 44, (1, 1, 1, 0.95), Pango.Weight.LIGHT)
        targets = {}
        items = self.items()

        # volume group, right aligned
        right = hv.W - hv.SIDE
        sinks = {sn["name"]: sn for sn in self.audio["sinks"]}
        out = sinks.get(self.audio["default"])
        if "output" in items and out:
            lay = hv.text_layout(out["label"], 22, Pango.Weight.MEDIUM)
            lw, lh = lay.get_pixel_size()
            ow = lw + 88
            targets["output"] = (right - ow, cy - 32, ow, 64)
            right -= ow + 26
        targets["vol+"] = (right - 60, cy - 30, 60, 60)
        right -= 60 + 18
        bar_w = 260
        bar_x = right - bar_w
        right = bar_x - 18
        targets["vol-"] = (right - 60, cy - 30, 60, 60)
        right -= 60 + 22
        targets["mute"] = (right - 68, cy - 34, 68, 68)

        # media buttons, centered in the space between the time and the volume group
        cx = min(hv.W / 2, (hv.SIDE + 300 + right - 68) / 2)
        offsets = {"prev": -130, "play": 0, "next": 130}
        for it in ("prev", "play", "next"):
            if it in items:
                r = 46 if it == "play" else 36
                targets[it] = (cx + offsets[it] - r, cy - r, 2 * r, 2 * r)
        self.targets = targets

        # gliding focus highlight
        if self.hl is None and self.focus in targets:
            self.hl = list(targets[self.focus])
        if self.hl:
            hx, hy, hw, hh2 = self.hl
            pulse = 1.0
            tp = (now - self.press_t) / 0.22
            if tp < 1:
                pulse = 0.88 + 0.12 * hv.ease_out(tp)
            ccx, ccy = hx + hw / 2, hy + hh2 / 2
            w2, h2 = hw * 1.08 * pulse, hh2 * 1.08 * pulse
            radial(snap, ccx, ccy, max(w2, h2) * 0.95, (1, 1, 1), 0.25)
            snap.push_rounded_clip(rounded(ccx - w2 / 2, ccy - h2 / 2, w2, h2, min(w2, h2) / 2))
            snap.append_color(rgba(1, 1, 1, 0.97), rect(ccx - w2 / 2, ccy - h2 / 2, w2, h2))
            snap.pop()

        dark = rgba(*hv.TINT)
        white = rgba(1, 1, 1)

        def button(item, icon_name, size):
            bx, by_, bw_, bh_ = targets[item]
            focused = item == self.focus
            if not focused:
                self.circle_or_pill(snap, bx, by_, bw_, bh_, rgba(1, 1, 1, 0.12))
            self.icon(snap, icon_name, bx + (bw_ - size) / 2, by_ + (bh_ - size) / 2, size,
                      dark if focused else white)
            self.hits.append((bx * s, (by_ + rise) * s, bw_ * s, bh_ * s, item))

        if "prev" in targets:
            button("prev", "media-skip-backward-symbolic", 30)
        button("play", "media-playback-pause-symbolic" if self.playing() else "media-playback-start-symbolic", 38)
        if "next" in targets:
            button("next", "media-skip-forward-symbolic", 30)

        vol = self.shown_volume if self.shown_volume is not None else (self.audio["volume"] or 0)
        muted = self.audio["muted"]
        vicon = ("audio-volume-muted-symbolic" if muted or vol <= 0.001 else
                 "audio-volume-low-symbolic" if vol < 0.34 else
                 "audio-volume-medium-symbolic" if vol < 0.67 else "audio-volume-high-symbolic")
        button("mute", vicon, 32)
        button("vol-", "list-remove-symbolic", 26)
        button("vol+", "list-add-symbolic", 26)

        # volume bar
        glow = max(0.0, 1 - (now - self.volume_t) / 0.8)
        snap.push_rounded_clip(rounded(bar_x, cy - 5, bar_w, 10, 5))
        snap.append_color(rgba(1, 1, 1, 0.22), rect(bar_x, cy - 5, bar_w, 10))
        fill = rgba(1, 1, 1, 0.45) if muted else rgba(*[hv.mix(1.0, c, 0.35 * glow) for c in acc], 0.97)
        snap.append_color(fill, rect(bar_x, cy - 5, bar_w * min(1.0, vol), 10))
        snap.pop()
        if glow > 0.01:
            radial(snap, bar_x + bar_w * min(1.0, vol), cy, 40, (1, 1, 1), 0.35 * glow)
        self.dot(snap, bar_x + bar_w * min(1.0, vol), cy, 9 + 3 * glow, white)
        pct = "Ztlumeno" if muted else f"{round(vol * 100)} %"
        draw_text(snap, pct, bar_x + bar_w / 2, cy - 52, 22 + 6 * glow, (1, 1, 1, 0.85),
                  Pango.Weight.SEMIBOLD, align=0.5)

        if "output" in targets and out:
            bx, by_, bw_, bh_ = targets["output"]
            focused = self.focus == "output"
            if not focused:
                self.circle_or_pill(snap, bx, by_, bw_, bh_, rgba(1, 1, 1, 0.12))
            col = dark if focused else white
            self.icon(snap, out["icon"], bx + 22, by_ + (bh_ - 28) / 2, 28, col)
            lay = hv.text_layout(out["label"], 22, Pango.Weight.MEDIUM)
            draw_layout(snap, lay, bx + 62, by_ + (bh_ - lay.get_pixel_size()[1]) / 2, col)
            self.hits.append((bx * s, (by_ + rise) * s, bw_ * s, bh_ * s, "output"))

        self.draw_hints(snap, hh)

    def draw_art(self, snap, x, y, a):
        """Thumbnail card: cover-fit, rounded, shadow; cross-fades when the video changes,
        dims with a pause sign while paused."""
        paused = self.player and not self.playing()
        for tex, alpha, scale in ((self.art_old, 1 - a, 1.0), (self.art, a, 0.92 + 0.08 * a)):
            if tex is None or alpha < 0.01:
                continue
            w, h = ART_W * scale, ART_H * scale
            cx, cy = x + ART_W / 2, y + ART_H / 2
            bx, by = cx - w / 2, cy - h / 2
            card = rounded(bx, by, w, h, 16)
            snap.push_opacity(alpha)
            snap.append_outset_shadow(card, rgba(0, 0, 0, 0.55), 0, 14, 0, 34)
            snap.push_rounded_clip(card)
            k = max(w / tex.get_width(), h / tex.get_height())
            tw, th = tex.get_width() * k, tex.get_height() * k
            snap.append_scaled_texture(tex, Gsk.ScalingFilter.TRILINEAR, rect(cx - tw / 2, cy - th / 2, tw, th))
            if paused:
                snap.append_color(rgba(0, 0, 0, 0.40), rect(bx, by, w, h))
                self.icon(snap, "media-playback-pause-symbolic", cx - 26, cy - 26, 52, rgba(1, 1, 1, 0.95))
            snap.pop()
            snap.append_border(card, [1.5] * 4, [rgba(1, 1, 1, 0.28)] * 4)
            snap.pop()

    @staticmethod
    def circle_or_pill(snap, x, y, w, h, color):
        snap.push_rounded_clip(rounded(x, y, w, h, min(w, h) / 2))
        snap.append_color(color, rect(x, y, w, h))
        snap.pop()

    @staticmethod
    def dot(snap, cx, cy, r, color):
        snap.push_rounded_clip(rounded(cx - r, cy - r, 2 * r, 2 * r, r))
        snap.append_color(color, rect(cx - r, cy - r, 2 * r, 2 * r))
        snap.pop()

    def draw_hints(self, snap, hh):
        hints = [("◀ ▶", "vybrat"), ("A", "potvrdit"), ("▲ ▼", "hlasitost"), ("X", "pauza"),
                 ("Y", "ztlumit"), ("B", "zavřít")]
        sizes = []
        for key, label in hints:
            kw, kh = hv.text_layout(key, 16, Pango.Weight.BOLD).get_pixel_size()
            lw, _ = hv.text_layout(label, 19).get_pixel_size()
            sizes.append((max(kw + 20, kh + 10), lw))
        total = sum(bw + 10 + lw for bw, lw in sizes) + 30 * (len(hints) - 1)
        x, y = (hv.W - total) / 2, hh - 44
        dark = rgba(*hv.TINT)
        for (key, label), (bw, lw) in zip(hints, sizes):
            lay = hv.text_layout(key, 16, Pango.Weight.BOLD)
            kw, kh = lay.get_pixel_size()
            bh = kh + 8
            snap.push_rounded_clip(rounded(x, y, bw, bh, bh / 2))
            snap.append_color(rgba(1, 1, 1, 0.85), rect(x, y, bw, bh))
            snap.pop()
            draw_layout(snap, lay, x + (bw - kw) / 2, y + 4, dark)
            draw_text(snap, label, x + bw + 10, y + 2, 19, (1, 1, 1, 0.7))
            x += bw + 10 + lw + 30

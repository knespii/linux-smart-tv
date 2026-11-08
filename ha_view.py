"""Home Assistant panel: your rooms as rows of big tiles, made for a gamepad.

Each room (area) of Home Assistant is a row; its lights, switches, covers,
thermostats, media players, locks, scenes and scripts are tiles. A toggles the
selected tile, LB / RB change brightness, cover position, temperature or volume.
Y opens the full Home Assistant web UI in Chrome.

The data comes from ha_client.HAClient; SmartTV (smart_tv.py) feeds the
gamepad in through action().
"""
import math
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, Gsk, Gtk, Pango  # noqa: E402

import ha_client  # noqa: E402
import home_view as hv  # noqa: E402
from home_view import draw_layout, draw_text, radial, rect, rgba, rounded  # noqa: E402

ACCENT = (0.09, 0.74, 0.95)      # Home Assistant blue
COLORS = {
    "light": (1.00, 0.78, 0.40), "switch": ACCENT, "input_boolean": ACCENT, "fan": (0.45, 0.85, 0.95),
    "cover": (0.40, 0.88, 0.72), "heat": (1.00, 0.55, 0.25), "cool": (0.40, 0.70, 1.00),
    "climate": (0.55, 0.85, 0.55), "media_player": (0.68, 0.52, 1.00), "lock": (1.00, 0.42, 0.35),
    "script": (0.68, 0.52, 1.00), "scene": (0.68, 0.52, 1.00),
}
DOMAINS = ["light", "switch", "input_boolean", "fan", "cover", "climate", "media_player", "lock",
           "scene", "script", "button", "input_button"]
ACTIONS = {"scene", "script", "button", "input_button"}   # run once, no on/off state

TILE_W, TILE_H = 300, 176
GAP = 24
HEAD_H = 58                      # room name above its tiles
ROW_H = HEAD_H + TILE_H + 56
BODY_TOP = 196
OPEN_S, CLOSE_S, ROW_STAGGER_S = 0.45, 0.18, 0.06
SMOOTH = 14.0
OVERRIDE_S = 6.0                 # how long a sent change is shown before HA confirms it
ADJUST_MS = 250                  # LB / RB changes are sent at most this often
ADJUST_REPEAT_S = 0.14           # held LB / RB: one step this often
TOAST_S = 3.5

HVAC = {"off": "Vypnuto", "heat": "Topí", "cool": "Chladí", "auto": "Automaticky", "heat_cool": "Automaticky",
        "dry": "Vysoušení", "fan_only": "Ventilátor"}
COVER = {"open": "Otevřeno", "closed": "Zavřeno", "opening": "Otevírá se", "closing": "Zavírá se"}
LOCK = {"locked": "Zamčeno", "unlocked": "Odemčeno", "locking": "Zamyká se", "unlocking": "Odemyká se",
        "jammed": "Zaseknutý", "open": "Otevřeno"}
MEDIA = {"playing": "Přehrává", "paused": "Pozastaveno", "idle": "Nečinný", "off": "Vypnuto",
         "on": "Zapnuto", "standby": "Pohotovost", "buffering": "Načítá"}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, digits=1):
    """22.50 -> '22,5', 21.0 -> '21' (Czech decimal comma)."""
    s = f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:.0f}"
    return s.replace(".", ",")


def short_name(name, area):
    """'Obývák Strop' in the Obývák row is just 'Strop'."""
    if area and name.lower().startswith(area.lower() + " ") and len(name) > len(area) + 1:
        rest = name[len(area) + 1:]
        return rest[:1].upper() + rest[1:]
    return name


def describe(eid, st, area=None):
    """What a tile shows for an entity: name, state text, icon, on/off, color, level."""
    dom = eid.split(".", 1)[0]
    s = st.get("state", "")
    a = st.get("attributes", {}) or {}
    info = {"eid": eid, "dom": dom, "name": short_name(a.get("friendly_name") or eid, area),
            "on": False, "color": COLORS.get(dom, ACCENT), "level": None, "icon": dom,
            "available": s not in ("unavailable", ""), "text": "", "adjust": None}
    if s == "unavailable":
        info["text"] = "Nedostupné"
        return info
    if dom == "light":
        info["on"] = s == "on"
        b = num(a.get("brightness"))
        modes = a.get("supported_color_modes") or []
        if any(m != "onoff" for m in modes):
            info["adjust"] = "jas"
        if info["on"]:
            rgb = a.get("rgb_color")
            if isinstance(rgb, (list, tuple)) and len(rgb) == 3 and max(rgb) > 0:
                # the bulb's own color, a bit lighter so it glows
                m = max(rgb)
                info["color"] = tuple(0.35 + 0.65 * c / m for c in rgb)
            if b is not None:
                info["level"] = b / 255
                info["text"] = f"Zapnuto · {round(b / 2.55)} %"
            else:
                info["text"] = "Zapnuto"
        else:
            info["text"] = "Vypnuto"
    elif dom in ("switch", "input_boolean"):
        info["on"] = s == "on"
        info["text"] = "Zapnuto" if info["on"] else "Vypnuto"
    elif dom == "fan":
        info["on"] = s == "on"
        p = num(a.get("percentage"))
        if a.get("percentage_step") or p is not None:
            info["adjust"] = "rychlost"
        if info["on"] and p is not None:
            info["level"] = p / 100
            info["text"] = f"Zapnuto · {round(p)} %"
        else:
            info["text"] = "Zapnuto" if info["on"] else "Vypnuto"
    elif dom == "cover":
        pos = num(a.get("current_position"))
        info["on"] = s in ("open", "opening")
        info["text"] = COVER.get(s, s)
        if pos is not None:
            info["adjust"] = "poloha"
            info["level"] = pos / 100
            if s == "open" and 0 < pos < 100:
                info["text"] = f"Otevřeno · {round(pos)} %"
    elif dom == "climate":
        cur, target = num(a.get("current_temperature")), num(a.get("temperature"))
        action = a.get("hvac_action") or ""
        info["on"] = s != "off"
        if s in ("heat",) or action == "heating":
            info["color"] = COLORS["heat"]
        elif s in ("cool",) or action == "cooling":
            info["color"] = COLORS["cool"]
        else:
            info["color"] = COLORS["climate"]
        parts = [HVAC.get(s, s)]
        if cur is not None:
            parts.append(f"{fmt(cur)} °C")
        if target is not None and s != "off":
            parts.append(f"cíl {fmt(target)} °C")
            info["adjust"] = "teplota"
        info["text"] = " · ".join(parts)
    elif dom == "media_player":
        info["on"] = s == "playing"
        title = a.get("media_title")
        info["text"] = f"{MEDIA.get(s, s)} · {title}" if title and s in ("playing", "paused") else MEDIA.get(s, s)
        vol = num(a.get("volume_level"))
        if vol is not None and s not in ("off", "standby"):
            info["adjust"] = "hlasitost"
            info["level"] = vol
    elif dom == "lock":
        info["on"] = s in ("unlocked", "open", "jammed")
        info["text"] = LOCK.get(s, s)
        info["icon"] = "lock" if s == "locked" else "unlock"
    elif dom == "scene":
        info["text"] = "Scéna"
    elif dom == "script":
        info["on"] = s == "on"
        info["text"] = "Běží" if info["on"] else "Skript"
    elif dom in ("button", "input_button"):
        info["text"] = "Tlačítko"
        info["icon"] = "button"
    return info


def a_label(info):
    """What A does on a tile, for the hints."""
    dom = info["dom"]
    if dom in ACTIONS:
        return "spustit" if dom in ("scene", "script") else "stisknout"
    if dom == "lock":
        return "zamknout" if info["on"] else "odemknout"
    if dom == "cover":
        st = info["text"]
        return "zastavit" if st in ("Otevírá se", "Zavírá se") else "zavřít" if info["on"] else "otevřít"
    if dom == "media_player":
        return "pauza" if info["on"] else "přehrát"
    return "vypnout" if info["on"] else "zapnout"


# --- icons, drawn with cairo on a 24 x 24 grid ------------------------------------
def draw_glyph(snap, name, x, y, size, color):
    cr = snap.append_cairo(rect(x - 2, y - 2, size + 4, size + 4))
    cr.translate(x, y)
    cr.scale(size / 24, size / 24)
    cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
    cr.set_line_width(2.0)
    cr.set_line_cap(1)    # round
    cr.set_line_join(1)
    if name == "light":
        cr.arc(12, 9.5, 6.5, math.radians(140), math.radians(40))
        cr.line_to(14.6, 16.2)
        cr.line_to(9.4, 16.2)
        cr.close_path()
        cr.stroke()
        for yy, half in ((19, 2.6), (21.6, 1.6)):
            cr.move_to(12 - half, yy)
            cr.line_to(12 + half, yy)
        cr.stroke()
    elif name == "switch":
        rounded_path(cr, 6, 8.5, 12, 8, 2.2)
        cr.stroke()
        for xx in (9.5, 14.5):
            cr.move_to(xx, 3.5)
            cr.line_to(xx, 8.5)
        cr.move_to(12, 16.5)
        cr.line_to(12, 21)
        cr.stroke()
    elif name == "input_boolean":
        rounded_path(cr, 2.5, 7, 19, 10, 5)
        cr.stroke()
        cr.arc(16.5, 12, 2.8, 0, 2 * math.pi)
        cr.fill()
    elif name == "fan":
        cr.translate(12, 12)
        for k in range(3):
            cr.save()
            cr.rotate(k * 2 * math.pi / 3)
            cr.translate(0, -5.4)
            cr.scale(2.7, 5.0)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
        cr.arc(0, 0, 1.9, 0, 2 * math.pi)
        cr.fill()
    elif name == "cover":
        cr.set_line_width(2.4)
        cr.move_to(3.5, 4)
        cr.line_to(20.5, 4)
        cr.stroke()
        cr.set_line_width(1.8)
        for yy in (8, 11.5, 15, 18.5):
            cr.move_to(5.5, yy)
            cr.line_to(18.5, yy)
        cr.stroke()
    elif name == "climate":
        rounded_path(cr, 9.8, 2.8, 4.4, 12.5, 2.2)
        cr.stroke()
        cr.arc(12, 17.6, 3.9, 0, 2 * math.pi)
        cr.stroke()
        cr.arc(12, 17.6, 1.7, 0, 2 * math.pi)
        cr.fill()
        cr.move_to(12, 8)
        cr.line_to(12, 15.5)
        cr.stroke()
    elif name == "media_player":
        for px_, py_ in ((3.5, 9), (7.5, 9), (12.5, 4.8), (12.5, 19.2), (7.5, 15), (3.5, 15)):
            cr.line_to(px_, py_)
        cr.close_path()
        cr.stroke()
        for r in (3.6, 7.0):
            cr.new_sub_path()
            cr.arc(13.5, 12, r, -0.85, 0.85)
        cr.stroke()
    elif name in ("lock", "unlock"):
        rounded_path(cr, 5.5, 11, 13, 10, 2.2)
        cr.stroke()
        if name == "lock":
            cr.move_to(8.5, 11)
            cr.line_to(8.5, 7.8)
            cr.arc(12, 7.8, 3.5, math.pi, 2 * math.pi)
            cr.line_to(15.5, 11)
        else:
            cr.move_to(8.5, 11)
            cr.line_to(8.5, 6.3)
            cr.arc(12, 6.3, 3.5, math.pi, 2 * math.pi)
            cr.line_to(15.5, 7.5)
        cr.stroke()
        cr.arc(12, 16, 1.5, 0, 2 * math.pi)
        cr.fill()
    elif name == "scene":
        star(cr, 10.5, 13, 7.5)
        star(cr, 18.5, 5.5, 3.2)
        cr.fill()
    elif name == "script":
        rounded_path(cr, 4, 4, 16, 16, 4)
        cr.stroke()
        cr.move_to(10, 8.4)
        cr.line_to(15.6, 12)
        cr.line_to(10, 15.6)
        cr.close_path()
        cr.fill()
    elif name == "button":
        cr.arc(12, 12, 8, 0, 2 * math.pi)
        cr.stroke()
        cr.arc(12, 12, 3.6, 0, 2 * math.pi)
        cr.fill()
    else:   # house
        cr.move_to(3.5, 11.5)
        cr.line_to(12, 4)
        cr.line_to(20.5, 11.5)
        cr.move_to(6.5, 9.5)
        cr.line_to(6.5, 20)
        cr.line_to(17.5, 20)
        cr.line_to(17.5, 9.5)
        cr.move_to(10.3, 20)
        cr.line_to(10.3, 14.5)
        cr.line_to(13.7, 14.5)
        cr.line_to(13.7, 20)
        cr.stroke()


def rounded_path(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def star(cr, cx, cy, r):
    """Four-pointed sparkle."""
    k = r * 0.28
    cr.move_to(cx, cy - r)
    cr.line_to(cx + k, cy - k)
    cr.line_to(cx + r, cy)
    cr.line_to(cx + k, cy + k)
    cr.line_to(cx, cy + r)
    cr.line_to(cx - k, cy + k)
    cr.line_to(cx - r, cy)
    cr.line_to(cx - k, cy - k)
    cr.close_path()


# --- the panel -----------------------------------------------------------------------
class HomeAssistantView:
    def __init__(self, tv, client, app):
        self.tv = tv
        self.client = client
        self.app = app or {}
        self.rows = []               # [{"key", "title", "area", "items": [entity ids], "summary"}]
        self.states = {}
        self.row = 0
        self.cols = {}               # row key -> selected column
        self.top = False             # focus on the "Celý Home Assistant" button
        self.top_auto = False        # ...only because there was nothing else yet
        self.scroll = 0.0            # animated vertical scroll (design px)
        self.xoff = {}               # row key -> [shown, target] horizontal scroll
        self.focus_v = {}            # (row key, entity) -> 0..1 focus animation
        self.top_v = 0.0
        self.overrides = {}          # entity -> (state changes, last_updated when sent, time sent)
        self.adjusts = {}            # entity -> (domain, service, data) waiting to be sent
        self.adjust_id = None
        self.last_adjust = 0.0
        self.armed = None            # lock waiting for a second A
        self.arm_t = -10.0
        self.press_t = -10.0
        self.toast = None            # (text, time)
        now = time.monotonic()
        self.open_t = now
        self.close_t = None
        self.splash_t = None
        self.splash_text = ""
        self.hits = []
        self.tick_id = None
        self.last_frame = None
        self.clock_id = None
        self.window = None
        self.canvas = None
        self.icons = {}              # used by HomeView.draw_icon
        self.px_factor = 1.0
        self.data_changed()

    def __bool__(self):
        return True

    # app icons (Home Assistant's logo) work the same as on the home screen
    icon = hv.HomeView.icon
    draw_icon = hv.HomeView.draw_icon

    # --- window ----------------------------------------------------------
    def show(self):
        hv.ensure_css()
        win = Gtk.Window(title="Smart TV Home Assistant")
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
        win.connect("close-request", lambda *_: self.tv.close_ha(home=True) or True)
        self.window = win
        self.clock_id = GLib.timeout_add_seconds(5, self.on_clock)
        win.fullscreen()
        win.present()
        self.kick()

    def close(self):
        if self.close_t is None:
            self.flush_adjust()
            self.close_t = time.monotonic()
            self.kick()

    def destroy(self):
        for attr in ("clock_id", "adjust_id"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, None)
        if self.window:
            self.window.destroy()
            self.window = self.canvas = None
        return False

    def start_splash(self, text):
        self.splash_t = time.monotonic()
        self.splash_text = text
        self.kick()

    def on_clock(self):
        if self.canvas:
            self.canvas.queue_draw()
        return True

    # --- data --------------------------------------------------------------
    def data_changed(self):
        """New data from Home Assistant: rebuild the rows, keep the selection."""
        keep = self.focused_key()
        areas, devices, entities, states = self.client.snapshot()
        cfg = self.client.cfg
        hide = set(cfg.get("hide", []))
        groups = {aid: [] for aid, _ in areas}
        names = dict(areas)
        sensors = {}
        scenes, other = [], []
        for eid, st in states.items():
            dom = eid.split(".", 1)[0]
            reg = entities.get(eid) or {}
            if reg.get("hidden") or reg.get("category") or eid in hide:
                continue
            area = reg.get("area") or devices.get(reg.get("device"))
            if dom == "sensor":
                dc = (st.get("attributes") or {}).get("device_class")
                if area in groups and dc in ("temperature", "humidity") and num(st.get("state")) is not None:
                    sensors.setdefault(area, {}).setdefault(dc, eid)
                continue
            if dom not in DOMAINS:
                continue
            if area in groups:
                groups[area].append(eid)
            elif dom in ACTIONS:
                scenes.append(eid)
            else:
                other.append(eid)

        def order(eid):
            st = states[eid]
            return DOMAINS.index(eid.split(".", 1)[0]), ((st.get("attributes") or {}).get("friendly_name") or eid).lower()

        rows = []
        favorites = [e for e in cfg.get("favorites", []) if e in states]
        if favorites:
            rows.append({"key": "@fav", "title": "Oblíbené", "area": None, "items": favorites, "sensors": {}})
        for aid, name in areas:
            if groups[aid]:
                rows.append({"key": aid, "title": name, "area": name, "items": sorted(groups[aid], key=order),
                             "sensors": sensors.get(aid, {})})
        if scenes:
            rows.append({"key": "@scenes", "title": "Scény a skripty", "area": None,
                         "items": sorted(scenes, key=order), "sensors": {}})
        if other:
            rows.append({"key": "@other", "title": "Ostatní", "area": None, "items": sorted(other, key=order),
                         "sensors": {}})
        self.rows = rows
        self.states = states
        watch = {e for r in rows for e in r["items"]} | {e for r in rows for e in r["sensors"].values()}
        self.client.watch = watch

        # drop changes we sent once Home Assistant reports the new state
        for eid, (_, sent_from, t) in list(self.overrides.items()):
            st = states.get(eid)
            if not st or st.get("last_updated") != sent_from or time.monotonic() - t > OVERRIDE_S:
                del self.overrides[eid]
        for err in self.client.take_failures():
            self.show_toast(f"Nepovedlo se: {err}")
        self.restore_focus(keep)
        if not self.rows:
            self.top = self.top_auto = True
        elif self.top_auto:          # the devices just arrived: select the first one
            self.top = self.top_auto = False
        self.kick()

    def focused_key(self):
        if not self.rows or self.top:
            return None
        r = self.rows[min(self.row, len(self.rows) - 1)]
        c = min(self.cols.get(r["key"], 0), len(r["items"]) - 1)
        return r["key"], r["items"][c]

    def restore_focus(self, keep):
        if not self.rows:
            self.row = 0
            return
        if keep:
            for i, r in enumerate(self.rows):
                if r["key"] == keep[0]:
                    self.row = i
                    if keep[1] in r["items"]:
                        self.cols[r["key"]] = r["items"].index(keep[1])
                    break
        self.row = min(self.row, len(self.rows) - 1)
        for r in self.rows:
            self.cols[r["key"]] = min(self.cols.get(r["key"], 0), len(r["items"]) - 1)

    def state_of(self, eid):
        """The entity's state, with changes we just sent already applied."""
        st = self.states.get(eid) or {}
        ov = self.overrides.get(eid)
        if ov:
            changes = ov[0]
            st = dict(st, attributes=dict(st.get("attributes") or {}, **changes.get("attributes", {})))
            if "state" in changes:
                st["state"] = changes["state"]
        return st

    def info(self, row, eid):
        return describe(eid, self.state_of(eid), row["area"])

    def focused_info(self):
        key = self.focused_key()
        if not key:
            return None
        row = self.rows[self.row]
        return self.info(row, key[1])

    # --- actions -------------------------------------------------------------
    def action(self, name):
        if self.close_t is not None or self.splash_t is not None:
            return
        if name in ("left", "right"):
            self.move(1 if name == "right" else -1)
        elif name in ("up", "down"):
            self.vertical(1 if name == "down" else -1)
        elif name == "activate":
            self.activate()
        elif name in ("less", "more"):
            self.adjust(1 if name == "more" else -1)
        self.kick()

    def move(self, d):
        if self.top or not self.rows:
            return
        r = self.rows[self.row]
        c = self.cols.get(r["key"], 0) + d
        if 0 <= c < len(r["items"]):
            self.cols[r["key"]] = c
            self.armed = None

    def vertical(self, d):
        self.armed = None
        self.top_auto = False
        if not self.rows:
            self.top = True
        elif self.top:
            if d > 0:
                self.top = False
        elif self.row + d < 0:
            self.top = True
        else:
            self.row = min(len(self.rows) - 1, self.row + d)

    def connected(self):
        if self.client.status == "ready":
            return True
        self.show_toast("Home Assistant není připojený")
        return False

    def activate(self):
        if self.top:
            self.tv.ha_web()
            return
        info = self.focused_info()
        if not info or not self.connected():
            return
        if not info["available"]:
            self.show_toast("Zařízení je nedostupné")
            return
        eid, dom, st = info["eid"], info["dom"], self.state_of(info["eid"])
        self.press_t = time.monotonic()
        if dom == "lock":
            if self.armed != eid:   # like "Vypnout PC": the first A only asks
                self.armed = eid
                self.arm_t = time.monotonic()
                return
            self.armed = None
            locked = st.get("state") == "locked"
            self.send(dom, "unlock" if locked else "lock", eid, None,
                      {"state": "unlocking" if locked else "locking"})
        elif dom in ("light", "switch", "input_boolean", "fan"):
            self.send(dom, "toggle", eid, None, {"state": "off" if info["on"] else "on"})
        elif dom == "cover":
            s = st.get("state")
            if s in ("opening", "closing"):
                self.send(dom, "stop_cover", eid)
            elif info["on"]:
                self.send(dom, "close_cover", eid, None, {"state": "closing"})
            else:
                self.send(dom, "open_cover", eid, None, {"state": "opening"})
        elif dom == "climate":
            if st.get("state") == "off":
                self.send(dom, "turn_on", eid)
            else:
                self.send(dom, "turn_off", eid, None, {"state": "off"})
        elif dom == "media_player":
            self.send(dom, "media_play_pause", eid, None,
                      {"state": "paused" if st.get("state") == "playing" else "playing"})
        elif dom in ("scene", "script"):
            self.send(dom, "turn_on", eid)
            self.show_toast(f"Spuštěno: {info['name']}")
        elif dom in ("button", "input_button"):
            self.send(dom, "press", eid)
            self.show_toast(f"Stisknuto: {info['name']}")

    def send(self, domain, service, eid, data=None, override=None):
        self.client.call(domain, service, eid, data)
        if override:
            st = self.states.get(eid) or {}
            prev = self.overrides.get(eid, ({}, None, 0))[0]
            merged = dict(prev, **{k: v for k, v in override.items() if k != "attributes"})
            merged["attributes"] = dict(prev.get("attributes", {}), **override.get("attributes", {}))
            self.overrides[eid] = (merged, st.get("last_updated"), time.monotonic())

    def adjust(self, d):
        """LB / RB: brightness, position, temperature, volume or fan speed one step."""
        info = None if self.top else self.focused_info()
        if not info or not info["adjust"] or not self.connected() or not info["available"]:
            return
        now = time.monotonic()
        if now - self.last_adjust < ADJUST_REPEAT_S:
            return
        self.last_adjust = now
        eid, dom = info["eid"], info["dom"]
        a = self.state_of(eid).get("attributes") or {}
        if dom == "light":
            b = num(a.get("brightness")) if info["on"] else 0
            pct = max(1, min(100, round((b or 0) / 2.55 / 10) * 10 + 10 * d))
            if not info["on"] and d < 0:
                return
            self.queue_adjust(eid, ("light", "turn_on", {"brightness_pct": pct}),
                              {"state": "on", "attributes": {"brightness": round(pct * 2.55)}})
        elif dom == "fan":
            step = num(a.get("percentage_step")) or 10
            p = num(a.get("percentage")) if info["on"] else 0
            p = max(0, min(100, (p or 0) + step * d))
            self.queue_adjust(eid, ("fan", "set_percentage", {"percentage": round(p)}),
                              {"state": "on" if p else "off", "attributes": {"percentage": p}})
        elif dom == "cover":
            pos = max(0, min(100, round((num(a.get("current_position")) or 0) / 10) * 10 + 10 * d))
            self.queue_adjust(eid, ("cover", "set_cover_position", {"position": pos}),
                              {"attributes": {"current_position": pos}})
        elif dom == "climate":
            step = num(a.get("target_temp_step")) or 0.5
            t = num(a.get("temperature"))
            if t is None:
                return
            t = round((t + step * d) / step) * step
            lo, hi = num(a.get("min_temp")), num(a.get("max_temp"))
            t = max(lo if lo is not None else t, min(hi if hi is not None else t, t))
            self.queue_adjust(eid, ("climate", "set_temperature", {"temperature": t}),
                              {"attributes": {"temperature": t}})
        elif dom == "media_player":
            v = max(0.0, min(1.0, round(((num(a.get("volume_level")) or 0) + 0.05 * d) * 20) / 20))
            self.queue_adjust(eid, ("media_player", "volume_set", {"volume_level": v}),
                              {"attributes": {"volume_level": v}})

    def queue_adjust(self, eid, call, override):
        """Show the change at once, send only the last value every ADJUST_MS."""
        self.adjusts[eid] = call
        st = self.states.get(eid) or {}
        prev = self.overrides.get(eid, ({}, None, 0))[0]
        merged = dict(prev, **{k: v for k, v in override.items() if k != "attributes"})
        merged["attributes"] = dict(prev.get("attributes", {}), **override.get("attributes", {}))
        self.overrides[eid] = (merged, st.get("last_updated"), time.monotonic())
        if self.adjust_id is None:
            self.adjust_id = GLib.timeout_add(ADJUST_MS, self.flush_adjust)

    def flush_adjust(self):
        self.adjust_id = None
        for eid, (domain, service, data) in self.adjusts.items():
            self.client.call(domain, service, eid, data)
        self.adjusts = {}
        return False

    def show_toast(self, text):
        self.toast = (text, time.monotonic())
        self.kick()

    # --- input -----------------------------------------------------------
    def on_key(self, _ctrl, keyval, _keycode, _state):
        name = Gdk.keyval_name(keyval)
        mapping = {"Left": "left", "Right": "right", "Up": "up", "Down": "down", "Return": "activate",
                   "KP_Enter": "activate", "space": "activate", "plus": "more", "KP_Add": "more",
                   "minus": "less", "KP_Subtract": "less", "Page_Up": "more", "Page_Down": "less"}
        if name in ("Escape", "BackSpace"):
            self.tv.close_ha(home=True)
        elif name in ("y", "w"):
            self.tv.ha_web()
        elif name in mapping:
            self.action(mapping[name])
        return True

    def on_click(self, _gesture, _n, x, y):
        if self.close_t is not None:
            return
        for hx, hy, hw, hh, target in self.hits:
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                if target == "web":
                    self.top = True
                else:
                    self.top = False
                    self.row = target[0]
                    self.cols[self.rows[target[0]]["key"]] = target[1]
                self.action("activate")
                break

    # --- animation -------------------------------------------------------------
    def kick(self):
        if self.canvas and self.tick_id is None:
            self.last_frame = None
            self.tick_id = self.canvas.add_tick_callback(self.on_tick)

    def spinning(self):
        c = self.client
        return self.splash_t is not None or (not c.loaded and c.status in ("connecting", "offline", "idle"))

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
        busy = (moving or self.spinning() or self.close_t is not None
                or now - self.open_t < OPEN_S + ROW_STAGGER_S * 4 + 0.1
                or now - self.press_t < 0.3 or now - self.arm_t < 0.5
                or (self.toast and now - self.toast[1] < TOAST_S + 0.4))
        if not busy:
            self.tick_id = None
        return bool(busy)

    def step(self, dt):
        f = 1 - math.exp(-dt * SMOOTH)
        moving = False

        def follow(v, target):
            nonlocal moving
            if abs(target - v) < 0.002 * max(1.0, abs(target)) or abs(target - v) < 0.01:
                return target
            moving = True
            return v + (target - v) * f

        target = self.scroll_target()
        self.scroll = follow(self.scroll, target)
        for i, r in enumerate(self.rows):
            cur = self.xoff.setdefault(r["key"], [0.0, 0.0])
            cur[1] = self.x_target(r, cur[1])
            cur[0] = follow(cur[0], cur[1])
        focused = self.focused_key()
        for key in set(self.focus_v) | ({focused} if focused else set()):
            v = follow(self.focus_v.get(key, 0.0), 1.0 if key == focused else 0.0)
            if v == 0.0:
                self.focus_v.pop(key, None)
            else:
                self.focus_v[key] = v
        self.top_v = follow(self.top_v, 1.0 if self.top else 0.0)
        return moving

    def screen_height(self):
        """Screen height in design px."""
        if not self.canvas or not self.canvas.get_width():
            return 1080
        return self.canvas.get_height() * hv.W / self.canvas.get_width()

    def scroll_target(self):
        """The selected row goes to the top; the last row stays fully above the bottom fade."""
        if not self.rows:
            return 0.0
        last_bottom = BODY_TOP + (len(self.rows) - 1) * ROW_H + HEAD_H + TILE_H + 24
        most = last_bottom - (self.screen_height() - 108 - 120)
        return max(0.0, min(self.row * ROW_H, most))

    def x_target(self, row, cur):
        """Keep the selected tile of a row on screen."""
        c = self.cols.get(row["key"], 0)
        left = c * (TILE_W + GAP)
        visible = hv.W - 2 * hv.SIDE
        if left - cur > visible - TILE_W - 40:
            cur = left - (visible - TILE_W - 40)
        if left - cur < 0:
            cur = left
        return max(0.0, cur)

    # --- drawing ---------------------------------------------------------------
    def render(self, snap, aw, ah, factor=1):
        if aw <= 0 or ah <= 0:
            return
        now = time.monotonic()
        s = aw / hv.W
        hh = ah / s
        self.px_factor = s * factor
        self.hits = []

        fade = hv.ease_out((now - self.open_t) / (OPEN_S * 0.6))
        if self.close_t is not None:
            fade *= 1 - hv.clamp01((now - self.close_t) / CLOSE_S)
        if fade < 0.999:
            snap.push_opacity(fade)
        hv.HomeView.draw_background(self, snap, aw, ah)
        snap.save()
        snap.scale(s, s)
        radial(snap, hv.W * 0.86, -40, 900, ACCENT, 0.26)
        radial(snap, hv.SIDE + 300, hh * 0.78, 760, ACCENT, 0.10)
        self.draw_top(snap, s)
        c = self.client
        if self.rows and c.status != "auth":
            self.draw_rows(snap, now, hh, s)
            if c.status in ("offline", "connecting") and c.loaded:
                self.draw_banner(snap, "Spojení s Home Assistantem se přerušilo, zkouším to znovu…")
        else:
            self.draw_message(snap, now, hh)
        self.draw_hints(snap, hh)
        self.draw_toast(snap, now, hh)
        self.draw_splash(snap, now, hh)
        snap.restore()
        if fade < 0.999:
            snap.pop()

    def draw_top(self, snap, s):
        y = 50
        x = hv.SIDE
        if not self.draw_icon(snap, {"id": "@ha", "icon_file": ha_client.ICON}, x, y - 2, 58):
            draw_glyph(snap, "home", x + 4, y + 2, 50, rgba(*ACCENT))
        draw_text(snap, "Domácnost", x + 80, y - 6, 40, weight=Pango.Weight.SEMIBOLD)
        status = self.client.status
        if status == "ready":
            dot, text = (0.49, 1.00, 0.63), "Připojeno"
        elif status in ("connecting", "idle"):
            dot, text = (1.00, 0.78, 0.35), "Připojuji…"
        elif status == "no_token":
            dot, text = (1.00, 0.78, 0.35), "Nenastaveno"
        elif status == "auth":
            dot, text = (1.00, 0.40, 0.40), "Neplatný token"
        else:
            dot, text = (1.00, 0.40, 0.40), "Bez připojení"
        self.dot(snap, x + 88, y + 62, 6, rgba(*dot))
        w, _ = draw_text(snap, text, x + 102, y + 48, 20, (1, 1, 1, 0.75))
        host = self.client.cfg["url"].split("://", 1)[-1]
        draw_text(snap, "· " + host, x + 112 + w, y + 48, 20, (1, 1, 1, 0.42))

        # clock, and the button for the full web UI next to it
        cw, _ = draw_text(snap, time.strftime("%H:%M"), hv.W - hv.SIDE, y - 4, 48, (1, 1, 1, 0.95),
                          Pango.Weight.LIGHT, align=1.0)
        lay = hv.text_layout("Celý Home Assistant", 22, Pango.Weight.MEDIUM)
        lw, lh = lay.get_pixel_size()
        bw, bh = lw + 92, 62
        bx, by = hv.W - hv.SIDE - cw - 44 - bw, y - 2
        v = self.top_v
        k = 1 + 0.06 * v
        cx, cy = bx + bw / 2, by + bh / 2
        w2, h2 = bw * k, bh * k
        radial(snap, cx, cy, w2 * 0.7, (1, 1, 1), 0.28 * v)
        snap.push_rounded_clip(rounded(cx - w2 / 2, cy - h2 / 2, w2, h2, h2 / 2))
        snap.append_color(rgba(1, 1, 1, hv.mix(0.12, 0.96, v)), rect(cx - w2 / 2, cy - h2 / 2, w2, h2))
        snap.pop()
        col = rgba(*[hv.mix(1.0, c, v) for c in hv.TINT])
        draw_glyph(snap, "home", cx - w2 / 2 + 24, cy - 14, 28, col)
        draw_layout(snap, lay, cx - w2 / 2 + 66, cy - lh / 2, col)
        self.hits.append(((cx - w2 / 2) * s, (cy - h2 / 2) * s, w2 * s, h2 * s, "web"))

    def draw_rows(self, snap, now, hh, s):
        top = BODY_TOP - 40
        bottom = hh - 108
        # soft edges instead of a hard cut: fade out at the bottom, and at the top once scrolled
        span = bottom - top
        fade_top = min(70.0, self.scroll) / span
        fade_bottom = 120 / span
        snap.push_mask(Gsk.MaskMode.ALPHA)
        snap.append_linear_gradient(rect(0, top, hv.W, span), hv.point(0, top), hv.point(0, bottom),
                                    hv.stops((0, rgba(0, 0, 0, 0 if fade_top else 1)), (fade_top, rgba(0, 0, 0, 1)),
                                             (1 - fade_bottom, rgba(0, 0, 0, 1)), (1, rgba(0, 0, 0, 0))))
        snap.pop()
        snap.push_clip(rect(0, top, hv.W, span))
        for i, r in enumerate(self.rows):
            y = BODY_TOP + i * ROW_H - self.scroll
            if y > bottom or y + ROW_H < top:
                continue
            t = (now - self.open_t - ROW_STAGGER_S * min(i, 4)) / OPEN_S
            appear = hv.ease_out(t)
            alpha = hv.clamp01(t / 0.6)
            if alpha <= 0.001:
                continue
            y += (1 - appear) * 70
            if alpha < 0.999:
                snap.push_opacity(alpha)
            focused_row = i == self.row and not self.top
            # rows scrolled up past the top fade out instead of being cut
            head_a = hv.clamp01((y - (BODY_TOP - 60)) / 50)
            if head_a > 0.01:
                snap.push_opacity(head_a)
                self.draw_row_head(snap, r, hv.SIDE, y, focused_row)
                snap.pop()
            off = self.xoff.get(r["key"], [0.0, 0.0])[0]
            ty = y + HEAD_H
            c_sel = self.cols.get(r["key"], 0)
            for c, eid in enumerate(r["items"]):
                x = hv.SIDE + c * (TILE_W + GAP) - off
                if x > hv.W or x + TILE_W < 0:
                    continue
                v = self.focus_v.get((r["key"], eid), 0.0)
                info = self.info(r, eid)
                armed = self.armed == eid and focused_row and c == c_sel
                tile_a = hv.clamp01((ty - (BODY_TOP - 150)) / 130)
                if tile_a <= 0.01:
                    continue
                if tile_a < 0.999:
                    snap.push_opacity(tile_a)
                self.draw_tile(snap, info, x, ty, v, armed, eid in self.overrides, now)
                if tile_a < 0.999:
                    snap.pop()
                if top <= ty <= bottom:
                    self.hits.append((x * s, ty * s, TILE_W * s, TILE_H * s, (i, c)))
            if alpha < 0.999:
                snap.pop()
        snap.pop()
        snap.pop()   # mask

    def draw_row_head(self, snap, r, x, y, focused):
        w, _ = draw_text(snap, r["title"], x, y, 30, (1, 1, 1, 1.0 if focused else 0.72), Pango.Weight.SEMIBOLD)
        parts = []
        for dc, unit in (("temperature", None), ("humidity", "%")):
            eid = r["sensors"].get(dc)
            st = self.states.get(eid) if eid else None
            v = num(st.get("state")) if st else None
            if v is not None:
                u = unit or (st.get("attributes") or {}).get("unit_of_measurement", "°C")
                parts.append(f"{fmt(v)} {u}")
        if parts:
            draw_text(snap, "  ·  ".join(parts), x + w + 22, y + 8, 22, (1, 1, 1, 0.55))

    def draw_tile(self, snap, info, x, y, v, armed, pending, now):
        k = 1 + 0.07 * v
        w, h = TILE_W * k, TILE_H * k
        x -= (w - TILE_W) / 2
        y -= (h - TILE_H) / 2
        on = info["on"]
        col = info["color"]
        dim = 1.0 if info["available"] else 0.45
        if dim < 1:
            snap.push_opacity(dim)
        card = rounded(x, y, w, h, 18)
        if v > 0.01:
            snap.append_outset_shadow(card, rgba(0, 0, 0, 0.45 * v), 0, 18, 0, 40)
        snap.push_rounded_clip(card)
        snap.append_linear_gradient(rect(x, y, w, h), hv.point(x, y), hv.point(x, y + h),
                                    hv.stops((0, rgba(1, 1, 1, 0.11 + 0.10 * v)), (1, rgba(1, 1, 1, 0.05 + 0.07 * v))))
        if on:
            snap.append_linear_gradient(rect(x, y, w, h), hv.point(x, y), hv.point(x + w, y + h),
                                        hv.stops((0, rgba(*col, 0.50)), (1, rgba(*col, 0.16))))
            radial(snap, x + 60 * k, y + 56 * k, 170 * k, col, 0.45)
        if armed:
            snap.append_color(rgba(1, 0.2, 0.25, 0.40), rect(x, y, w, h))
        if info["level"] is not None and (on or info["dom"] == "cover"):
            snap.append_color(rgba(1, 1, 1, 0.14), rect(x, y + h - 6 * k, w, 6 * k))
            snap.append_color(rgba(*col, 0.95) if on else rgba(1, 1, 1, 0.6),
                              rect(x, y + h - 6 * k, w * hv.clamp01(info["level"]), 6 * k))
        snap.pop()
        border = rgba(*col, 0.55 + 0.4 * v) if on else rgba(1, 1, 1, 0.10 + 0.32 * v)
        snap.append_border(card, [1.5 + v] * 4, [border] * 4)

        # icon in a circle; filled with the color while on, pulsing while a change is on its way
        shake = 0.0
        if armed:
            t = now - self.arm_t
            shake = math.sin(t * 45) * 8 * (1 - hv.clamp01(t / 0.45))
        r = 31 * k
        icx, icy = x + 28 * k + r + shake, y + 24 * k + r
        pulse = 1.0
        if pending:
            pulse = 1 + 0.06 * math.sin(now * 9)
            self.kick()
        rr = r * pulse
        snap.push_rounded_clip(rounded(icx - rr, icy - rr, 2 * rr, 2 * rr, rr))
        snap.append_color(rgba(*col, 0.96) if on else rgba(1, 1, 1, 0.13), rect(icx - rr, icy - rr, 2 * rr, 2 * rr))
        snap.pop()
        size = 34 * k
        draw_glyph(snap, info["icon"], icx - size / 2, icy - size / 2, size,
                   rgba(*hv.TINT, 0.95) if on else rgba(1, 1, 1, 0.92))

        text = "Stiskni A znovu" if armed else info["text"]
        draw_text(snap, info["name"], x + 28 * k, y + h - 84 * k, 25 * k, weight=Pango.Weight.MEDIUM,
                  max_w=w - 50 * k)
        draw_text(snap, text, x + 28 * k, y + h - 46 * k, 19 * k,
                  (1, 0.7, 0.7, 1) if armed else (1, 1, 1, 0.70), max_w=w - 50 * k)
        if dim < 1:
            snap.pop()

        if v > 0.01:   # glowing bar under the selected tile, like on the home screen
            bx, by = x + w / 2, y + h
            radial(snap, bx, by + 18, w * 0.33, (1, 1, 1), 0.14 * v)
            bw = w * 0.40 * v
            snap.push_rounded_clip(rounded(bx - bw / 2, by + 14, bw, 5, 2.5))
            snap.append_color(rgba(1, 1, 1, 0.95 * v), rect(bx - bw / 2, by + 14, bw, 5))
            snap.pop()

    def draw_message(self, snap, now, hh):
        c = self.client
        cx = hv.W / 2
        y = hh * 0.30
        url = c.cfg["url"]
        if c.status == "no_token":
            title = "Připoj Home Assistant"
            lines = ["1.  V Home Assistantu otevři svůj profil (vlevo dole) a záložku Zabezpečení.",
                     "2.  Dole u „Dlouhodobé přístupové tokeny“ vytvoř nový token.",
                     f"3.  Vlož ho do souboru {ha_client.CONFIG.replace(os.path.expanduser('~'), '~')}",
                     "     na řádek  token = …  a zkontroluj adresu  url = …",
                     "4.  Otevři tenhle panel znovu."]
            sub = f"Adresa: {url}"
        elif c.status == "auth":
            title = "Home Assistant odmítl token"
            lines = ["Token v souboru " + ha_client.CONFIG.replace(os.path.expanduser("~"), "~")
                     + " nefunguje.", "Vytvoř nový v profilu Home Assistantu (Zabezpečení) a vlož ho tam."]
            sub = c.error
        elif c.status == "offline":
            title = "Home Assistant je nedostupný"
            lines = [f"Nedaří se spojit s {url}", "Zkouším to znovu…"]
            sub = c.error
        elif c.status == "ready":
            title = "Žádná zařízení k ovládání"
            lines = ["Home Assistant nemá světla, zásuvky, žaluzie, termostaty ani scény,",
                     "které by šly zobrazit."]
            sub = ""
        else:
            title = "Připojuji se k Home Assistantu…"
            lines = []
            sub = url
        a = hv.ease_out((now - self.open_t) / OPEN_S)
        y += (1 - a) * 40
        draw_text(snap, title, cx, y, 48, (1, 1, 1, a), Pango.Weight.SEMIBOLD, align=0.5)
        y += 90
        if lines:
            widths = [hv.text_layout(t, 25).get_pixel_size()[0] for t in lines]
            lx = cx - max(widths) / 2
            for t in lines:
                draw_text(snap, t, lx, y, 25, (1, 1, 1, 0.82 * a))
                y += 44
        if sub:
            draw_text(snap, sub, cx, y + 20, 21, (1, 1, 1, 0.5 * a), align=0.5, max_w=hv.W - 2 * hv.SIDE)
        if self.spinning() and self.splash_t is None:
            self.spinner(snap, cx, y + 110, now, a)

    def draw_banner(self, snap, text):
        lay = hv.text_layout(text, 20, Pango.Weight.MEDIUM)
        tw, th = lay.get_pixel_size()
        x, y = (hv.W - tw) / 2 - 24, BODY_TOP - 64
        snap.push_rounded_clip(rounded(x, y, tw + 48, th + 18, (th + 18) / 2))
        snap.append_color(rgba(0.75, 0.22, 0.22, 0.88), rect(x, y, tw + 48, th + 18))
        snap.pop()
        draw_layout(snap, lay, x + 24, y + 9, rgba(1, 1, 1))

    def draw_toast(self, snap, now, hh):
        if not self.toast:
            return
        text, t0 = self.toast
        t = now - t0
        if t > TOAST_S + 0.3:
            self.toast = None
            return
        a = hv.ease_out(t / 0.25) * (1 - hv.clamp01((t - TOAST_S) / 0.3))
        lay = hv.text_layout(text, 22, Pango.Weight.MEDIUM)
        tw, th = lay.get_pixel_size()
        bw, bh = tw + 56, th + 24
        x, y = (hv.W - bw) / 2, hh - 190 + (1 - a) * 20
        snap.push_opacity(a)
        snap.append_outset_shadow(rounded(x, y, bw, bh, bh / 2), rgba(0, 0, 0, 0.4), 0, 10, 0, 30)
        snap.push_rounded_clip(rounded(x, y, bw, bh, bh / 2))
        snap.append_color(rgba(1, 1, 1, 0.94), rect(x, y, bw, bh))
        snap.pop()
        draw_layout(snap, lay, x + 28, y + 12, rgba(*hv.TINT))
        snap.pop()

    def hints(self):
        if self.splash_t is not None:
            return []
        if self.top or not self.rows:
            out = [("A", "otevřít celý Home Assistant")]
            if self.rows:
                out.insert(0, ("▼", "zpět na zařízení"))
        else:
            info = self.focused_info()
            out = [("◀ ▶ ▲ ▼", "vybrat")]
            if info:
                out.append(("A", "stiskni znovu" if self.armed == info["eid"] else a_label(info)))
                if info["adjust"]:
                    out.append(("LB RB", info["adjust"]))
            out.append(("Y", "celý Home Assistant"))
        out.append(("B", "zpět"))
        return out

    def draw_hints(self, snap, hh):
        x, y = hv.SIDE, hh - 84
        dark = rgba(*hv.TINT)
        for key, label in self.hints():
            lay = hv.text_layout(key, 18, Pango.Weight.BOLD)
            kw, kh = lay.get_pixel_size()
            bw, bh = max(kw + 22, kh + 10), kh + 10
            snap.push_rounded_clip(rounded(x, y, bw, bh, bh / 2))
            snap.append_color(rgba(1, 1, 1, 0.88), rect(x, y, bw, bh))
            snap.pop()
            draw_layout(snap, lay, x + (bw - kw) / 2, y + 5, dark)
            lw, _ = draw_text(snap, label, x + bw + 12, y + 3, 21, (1, 1, 1, 0.68))
            x += bw + 12 + lw + 34

    def draw_splash(self, snap, now, hh):
        if self.splash_t is None:
            return
        t = now - self.splash_t
        a = hv.ease_out(t / 0.35)
        snap.append_color(rgba(*hv.TINT, 0.88 * a), rect(0, 0, hv.W, hh))
        cx, cy = hv.W / 2, hh / 2 - 60
        radial(snap, cx, cy, 420, ACCENT, 0.35 * a)
        size = 150 * hv.ease_out_back(t / 0.5)
        if size > 1:
            if not self.draw_icon(snap, {"id": "@ha", "icon_file": ha_client.ICON},
                                         cx - size / 2, cy - size / 2, size, alpha=a):
                draw_glyph(snap, "home", cx - size / 2, cy - size / 2, size, rgba(*ACCENT, a))
        draw_text(snap, self.splash_text, cx, cy + 110, 40, (1, 1, 1, a), Pango.Weight.MEDIUM, align=0.5)
        self.spinner(snap, cx, cy + 200, now, a)

    @staticmethod
    def spinner(snap, cx, cy, now, a):
        cr = snap.append_cairo(rect(cx - 30, cy - 30, 60, 60))
        ang = now * 5.5
        cr.set_line_width(4)
        cr.set_line_cap(1)
        cr.arc(cx, cy, 22, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.15 * a)
        cr.stroke()
        cr.arc(cx, cy, 22, ang, ang + 1.6 + 0.6 * math.sin(now * 3))
        cr.set_source_rgba(1, 1, 1, 0.9 * a)
        cr.stroke()

    @staticmethod
    def dot(snap, cx, cy, r, color):
        snap.push_rounded_clip(rounded(cx - r, cy - r, 2 * r, 2 * r, r))
        snap.append_color(color, rect(cx - r, cy - r, 2 * r, 2 * r))
        snap.pop()

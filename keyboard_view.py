"""On-screen keyboard for the gamepad, TV style.

Slides up over the app in front. You type into its own text field; "Hotovo"
or "Odeslat" puts the text into the app (SmartTV pastes it with Ctrl+V, and
"Odeslat" presses Enter too). Drawn with the same GPU helpers as the home screen.

The view knows nothing about evdev: SmartTV turns gamepad input into
action() calls ("left", "press", "backspace", ...).
"""
import math
import time

import home_view as hv
from home_view import Gdk, GLib, Gtk, Pango, rgba, rect, rounded, radial, draw_text, draw_layout

KEY, GAP, ROW_H = 100, 12, 84     # key unit size (design px, 1920 wide screen)
COLS = 11                        # every row is 11 units wide
PAD = 28                         # panel padding
FIELD_H = 96
SMOOTH = 22.0                    # how fast the highlight glides between keys
OPEN_S, CLOSE_S = 0.35, 0.18
LAYERS = ["abc", "cz", "sym"]
LAYER_NAMES = {"abc": "abc", "cz": "čšř", "sym": "?123"}


def chars(text):
    return [(c, ("char", c), 1) for c in text.split()]


def bottom(left, middle):
    return [(LAYER_NAMES[left], ("layer", left), 1.5), (LAYER_NAMES[middle], ("layer", middle), 1.5),
            ("mezera", "space", 4), ("Hotovo", "done", 2), ("Odeslat", "send", 2)]


SHIFT_ROW = [("⇧", "shift", 1.5)] + chars("y x c v b n m ?") + [("⌫", "backspace", 1.5)]
KEYMAPS = {
    "abc": [chars("1 2 3 4 5 6 7 8 9 0 -"),
            chars("q w e r t z u i o p @"),
            chars("a s d f g h j k l , ."),
            SHIFT_ROW,
            bottom("sym", "cz")],
    "cz": [chars("ě š č ř ž ý á í é ú ů"),
           chars("ď ť ň ó ö ü ä ß « » …"),
           chars("a s d f g h j k l , ."),
           SHIFT_ROW,
           bottom("sym", "abc")],
    "sym": [chars("1 2 3 4 5 6 7 8 9 0 %"),
            chars("! ? @ # $ & * ( ) - _"),
            chars("+ = / \\ : ; ' \" , . €"),
            chars("[ ] { } < > ~ ^ | `") + [("⌫", "backspace", 1)],
            bottom("abc", "cz")],
}


def key_width(units):
    return units * KEY + (units - 1) * GAP


def row_geometry(row):
    """[(x, width)] of the keys in a row, x from the left edge of the key area."""
    out, x = [], 0.0
    for _label, _action, units in row:
        w = key_width(units)
        out.append((x, w))
        x += w + GAP
    return out


class KeyboardView:
    def __init__(self, tv, target_name=""):
        self.tv = tv
        self.target_name = target_name   # app the text goes to, shown above the field
        self.text = ""
        self.layer = "abc"
        self.shift = 0                   # 0 off, 1 next letter, 2 caps lock
        self.row, self.col = 2, 4        # start on "g"
        now = time.monotonic()
        self.open_t = now
        self.close_t = None
        self.press_t = -10.0
        self.press_pos = None
        self.caret_t = now
        self.hl = None                   # animated highlight rect [x, y, w, h]
        self.hits = []
        self.tick_id = None
        self.last_frame = None
        self.blink_id = None
        self.window = None
        self.canvas = None
        self.px_factor = 1.0

    # --- window ----------------------------------------------------------
    def show(self):
        hv.ensure_css()
        win = Gtk.Window(title="Smart TV klávesnice")
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
        win.connect("close-request", lambda *_: self.tv.keyboard_cancel() or True)
        self.window = win
        self.blink_id = GLib.timeout_add(530, self.on_blink)
        hv.cover_screen(win)   # not fullscreen, so the app stays visible behind
        win.present()
        self.kick()

    def copy(self, text):
        """Put text on the clipboard (needs our window, it has the keyboard focus)."""
        if self.window:
            self.window.get_clipboard().set(text)

    def close(self):
        if self.close_t is None:
            self.close_t = time.monotonic()
            self.kick()

    def destroy(self):
        if self.blink_id:
            GLib.source_remove(self.blink_id)
            self.blink_id = None
        if self.window:
            self.window.destroy()
            self.window = self.canvas = None
        return False

    # --- animation -------------------------------------------------------
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
        busy = (moving or now - self.open_t < OPEN_S + 0.1 or now - self.press_t < 0.2
                or self.close_t is not None)
        if not busy:
            self.tick_id = None
        return busy

    def step(self, dt):
        target = self.key_rect(self.row, self.col)
        if self.hl is None:
            self.hl = list(target)
            return False
        f = 1 - math.exp(-dt * SMOOTH)
        moving = False
        for i in range(4):
            if abs(target[i] - self.hl[i]) > 0.3:
                self.hl[i] += (target[i] - self.hl[i]) * f
                moving = True
            else:
                self.hl[i] = target[i]
        return moving

    def on_blink(self):
        if self.canvas:
            self.canvas.queue_draw()
        return True

    # --- layout ----------------------------------------------------------
    @property
    def rows(self):
        return KEYMAPS[self.layer]

    def panel_size(self):
        w = key_width(COLS) + 2 * PAD
        h = 5 * ROW_H + 4 * GAP + 2 * PAD + 56   # + layer tabs
        return w, h

    def key_rect(self, r, c):
        """Key rectangle relative to the key area's top-left corner."""
        x, w = row_geometry(self.rows[r])[c]
        return x, r * (ROW_H + GAP), w, ROW_H

    # --- actions ---------------------------------------------------------
    def action(self, name):
        if self.close_t is not None:
            return
        rows = self.rows
        if name in ("left", "right"):
            n = len(rows[self.row])
            self.col = (self.col + (1 if name == "right" else -1)) % n
        elif name in ("up", "down"):
            x, _y, w, _h = self.key_rect(self.row, self.col)
            center = x + w / 2
            self.row = (self.row + (1 if name == "down" else -1)) % len(rows)
            geo = row_geometry(rows[self.row])
            self.col = min(range(len(geo)), key=lambda i: abs(geo[i][0] + geo[i][1] / 2 - center))
        elif name == "press":
            self.press_t, self.press_pos = time.monotonic(), (self.row, self.col)
            self.do(rows[self.row][self.col][1])
        elif name in ("backspace", "space", "shift", "done", "send"):
            self.do(name)
        elif name in ("layer+", "layer-"):
            i = LAYERS.index(self.layer) + (1 if name == "layer+" else -1)
            self.set_layer(LAYERS[i % len(LAYERS)])
        self.caret_t = time.monotonic()
        self.kick()

    def do(self, act):
        if isinstance(act, tuple) and act[0] == "char":
            self.type(act[1])
        elif isinstance(act, tuple) and act[0] == "layer":
            self.set_layer(act[1])
        elif act == "space":
            self.type(" ")
        elif act == "backspace":
            self.text = self.text[:-1]
        elif act == "shift":
            self.shift = (self.shift + 1) % 3
        elif act == "done":
            self.tv.keyboard_done(self.text, enter=False)
        elif act == "send":
            self.tv.keyboard_done(self.text, enter=True)

    def type(self, ch):
        if self.shift:
            ch = ch.upper() if len(ch.upper()) == 1 else ch   # "ß" would become "SS"
            if self.shift == 1:
                self.shift = 0
        self.text += ch

    def set_layer(self, layer):
        self.layer = layer
        self.row = min(self.row, len(self.rows) - 1)
        self.col = min(self.col, len(self.rows[self.row]) - 1)

    # --- input from the real keyboard / mouse ------------------------------
    def on_key(self, _ctrl, keyval, _keycode, _state):
        if self.close_t is not None:
            return True
        name = Gdk.keyval_name(keyval)
        if name in ("Return", "KP_Enter"):
            self.tv.keyboard_done(self.text, enter=True)
        elif name == "Escape":
            self.tv.keyboard_cancel()
        elif name == "BackSpace":
            self.text = self.text[:-1]
        elif name in ("Left", "Right", "Up", "Down"):
            self.action(name.lower())
            return True
        else:
            ch = chr(Gdk.keyval_to_unicode(keyval) or 0)
            if ch.isprintable() and ch:
                self.text += ch
        self.caret_t = time.monotonic()
        self.kick()
        return True

    def on_click(self, _gesture, _n, x, y):
        for hx, hy, hw, hh, pos in self.hits:
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                self.row, self.col = pos
                self.action("press")
                break

    # --- drawing ---------------------------------------------------------
    def render(self, snap, aw, ah, factor=1):
        if aw <= 0 or ah <= 0:
            return
        now = time.monotonic()
        s = aw / hv.W
        hh = ah / s
        self.hits = []
        if self.hl is None:
            self.step(0)

        t_open = (now - self.open_t) / OPEN_S
        fade = hv.ease_out(t_open)
        slide = 1 - hv.ease_out_back(t_open)
        if self.close_t is not None:
            c = hv.clamp01((now - self.close_t) / CLOSE_S)
            fade *= 1 - c
            slide = max(slide, hv.ease_out(c) * 0.6)
        if fade < 0.999:
            snap.push_opacity(fade)

        # darken the app behind, mostly at the bottom
        snap.append_color(rgba(0, 0, 0, 0.25), rect(0, 0, aw, ah))
        snap.append_linear_gradient(rect(0, ah * 0.25, aw, ah * 0.75), hv.point(0, ah * 0.25), hv.point(0, ah),
                                    hv.stops((0, rgba(*hv.TINT, 0)), (1, rgba(*hv.TINT, 0.95))))

        snap.save()
        snap.scale(s, s)
        pw, ph = self.panel_size()
        px = (hv.W - pw) / 2
        py = hh - 70 - ph + slide * (ph + 260)
        self.draw_field(snap, px, py - FIELD_H - 26, pw, now)
        self.draw_panel(snap, px, py, pw, ph, now, s)
        self.draw_hints(snap, hh)
        snap.restore()
        if fade < 0.999:
            snap.pop()

    def draw_field(self, snap, x, y, w, now):
        if self.target_name:
            hv.draw_text(snap, f"Psaní do: {self.target_name}", x + 6, y - 40, 22, (1, 1, 1, 0.65))
        field = rounded(x, y, w, FIELD_H, 18)
        snap.append_outset_shadow(field, rgba(0, 0, 0, 0.45), 0, 14, 0, 36)
        snap.push_rounded_clip(field)
        snap.append_color(rgba(*hv.TINT, 0.92), rect(x, y, w, FIELD_H))
        snap.append_color(rgba(1, 1, 1, 0.08), rect(x, y, w, FIELD_H))
        snap.pop()
        snap.append_border(field, [2] * 4, [rgba(1, 1, 1, 0.30)] * 4)

        pad = 32
        max_w = w - 2 * pad - 10
        if self.text:
            hv.text_layout(" ", 1)  # makes sure the shared Pango context exists
            lay = Pango.Layout.new(hv._pango)  # own layout: it gets ellipsized
            fd = Pango.FontDescription.from_string(hv.FONT)
            fd.set_absolute_size(42 * Pango.SCALE)
            lay.set_font_description(fd)
            lay.set_text(self.text, -1)
            tw, th = lay.get_pixel_size()
            if tw > max_w:   # long text: show its end
                lay.set_width(int(max_w * Pango.SCALE))
                lay.set_ellipsize(Pango.EllipsizeMode.START)
                tw, th = lay.get_pixel_size()
            ty = y + (FIELD_H - th) / 2
            draw_layout(snap, lay, x + pad, ty, rgba(1, 1, 1, 1))
        else:
            tw = 0
            hv.draw_text(snap, "Napiš text…", x + pad, y + 24, 42, (1, 1, 1, 0.35))
        if int((now - self.caret_t) / 0.53) % 2 == 0:
            snap.append_color(rgba(1, 1, 1, 0.9), rect(x + pad + tw + 4, y + 20, 3, FIELD_H - 40))

    def draw_panel(self, snap, x, y, w, h, now, s):
        panel = rounded(x, y, w, h, 26)
        snap.append_outset_shadow(panel, rgba(0, 0, 0, 0.55), 0, 24, 0, 60)
        snap.push_rounded_clip(panel)
        snap.append_color(rgba(*hv.TINT, 0.94), rect(x, y, w, h))
        snap.append_linear_gradient(rect(x, y, w, h), hv.point(x, y), hv.point(x, y + h),
                                    hv.stops((0, rgba(1, 1, 1, 0.10)), (1, rgba(1, 1, 1, 0.03))))
        snap.pop()
        snap.append_border(panel, [1.5] * 4, [rgba(1, 1, 1, 0.16)] * 4)

        # layer tabs
        tx = x + PAD
        ty = y + PAD - 4
        for layer in LAYERS:
            lay = hv.text_layout(LAYER_NAMES[layer], 22, Pango.Weight.SEMIBOLD)
            lw, lh = lay.get_pixel_size()
            bw, bh = lw + 36, lh + 12
            active = layer == self.layer
            if active:
                snap.push_rounded_clip(rounded(tx, ty, bw, bh, bh / 2))
                snap.append_color(rgba(1, 1, 1, 0.92), rect(tx, ty, bw, bh))
                snap.pop()
            draw_layout(snap, lay, tx + 18, ty + 6, rgba(*hv.TINT) if active else rgba(1, 1, 1, 0.6))
            tx += bw + 10
        hv.draw_text(snap, "LB / RB přepíná znaky", x + w - PAD, ty + 8, 19, (1, 1, 1, 0.45), align=1.0)
        if self.shift:
            hv.draw_text(snap, "VELKÁ PÍSMENA" if self.shift == 2 else "Velké písmeno", tx + 14, ty + 8, 19,
                         (1, 0.85, 0.5, 0.9), Pango.Weight.SEMIBOLD)

        kx, ky = x + PAD, y + PAD + 56
        accent = self.accent()
        # gliding highlight behind the selected key
        hx, hy, hw, hh = self.hl
        pulse = 1.0
        if self.press_pos == (self.row, self.col):
            t = (now - self.press_t) / 0.18
            if t < 1:
                pulse = 0.90 + 0.10 * hv.ease_out(t)
        cx, cy = kx + hx + hw / 2, ky + hy + hh / 2
        hw2, hh2 = hw * pulse * 1.06, hh * pulse * 1.06
        radial(snap, cx, cy + 8, hw2 * 0.9, (1, 1, 1), 0.18)
        box = rounded(cx - hw2 / 2, cy - hh2 / 2, hw2, hh2, 14)
        snap.append_outset_shadow(box, rgba(0, 0, 0, 0.5), 0, 10, 0, 24)
        snap.push_rounded_clip(box)
        snap.append_color(rgba(1, 1, 1, 0.97), rect(cx - hw2 / 2, cy - hh2 / 2, hw2, hh2))
        snap.pop()

        for r, row in enumerate(self.rows):
            geo = row_geometry(row)
            for c, (label, act, _units) in enumerate(row):
                gx, gw = geo[c]
                bx, by = kx + gx, ky + r * (ROW_H + GAP)
                selected = (r, c) == (self.row, self.col)
                special = not (isinstance(act, tuple) and act[0] == "char")
                if not selected:
                    fill = rgba(*accent, 0.85) if act == "send" else rgba(1, 1, 1, 0.07 if special else 0.12)
                    if act == "shift" and self.shift:
                        fill = rgba(1, 0.85, 0.5, 0.35)
                    snap.push_rounded_clip(rounded(bx, by, gw, ROW_H, 14))
                    snap.append_color(fill, rect(bx, by, gw, ROW_H))
                    snap.pop()
                if isinstance(act, tuple) and act[0] == "char" and self.shift:
                    label = label.upper()
                size = 36 if not special else (30 if len(label) <= 2 else 24)
                color = rgba(*hv.TINT) if selected else rgba(1, 1, 1, 0.95)
                lay = hv.text_layout(label, size, Pango.Weight.MEDIUM)
                lw, lh = lay.get_pixel_size()
                draw_layout(snap, lay, bx + (gw - lw) / 2, by + (ROW_H - lh) / 2, color)
                self.hits.append((bx * s, by * s, gw * s, ROW_H * s, (r, c)))

    def accent(self):
        return hv.ACCENTS.get(self.tv.current_theme(), hv.ACCENTS["discord"])

    def draw_hints(self, snap, hh):
        hints = [("A", "napsat"), ("B", "smazat"), ("X", "mezera"), ("Y", "velká"),
                 ("Start", "odeslat"), ("Select", "zavřít")]
        widths = []
        for key, label in hints:
            kw, kh = hv.text_layout(key, 18, Pango.Weight.BOLD).get_pixel_size()
            lw, _ = hv.text_layout(label, 21).get_pixel_size()
            widths.append((max(kw + 22, kh + 10), lw))
        total = sum(bw + 12 + lw for bw, lw in widths) + 34 * (len(hints) - 1)
        x, y = (hv.W - total) / 2, hh - 50
        dark = rgba(*hv.TINT)
        for (key, label), (bw, lw) in zip(hints, widths):
            lay = hv.text_layout(key, 18, Pango.Weight.BOLD)
            kw, kh = lay.get_pixel_size()
            bh = kh + 10
            snap.push_rounded_clip(rounded(x, y, bw, bh, bh / 2))
            snap.append_color(rgba(1, 1, 1, 0.88), rect(x, y, bw, bh))
            snap.pop()
            draw_layout(snap, lay, x + (bw - kw) / 2, y + 5, dark)
            draw_text(snap, label, x + bw + 12, y + 3, 21, (1, 1, 1, 0.75))
            x += bw + 12 + lw + 34

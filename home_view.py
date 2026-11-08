"""Smart TV home screen, drawn with GTK 4 render nodes (on the GPU), so the
animations stay smooth even on a 4K screen.

Look: dark slate glass over a blurred copy of the desktop wallpaper, tall
cards, a big thin clock. All sizes are for a 1920 px wide screen and get
scaled to the real one.

The view only draws; SmartTV (smart_tv.py) owns the state (menu, selection,
running apps...) and calls kick() / menu_changed() / start_splash() / close().
"""
import math
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402

FONT = "Inter Display,Inter,Cantarell,Sans"
W = 1920                        # design width

TINT = (0.10, 0.10, 0.16)       # slate glass over the wallpaper
ACCENTS = {
    "youtube": (1.00, 0.15, 0.25), "netflix": (0.90, 0.10, 0.12),
    "steam": (0.40, 0.75, 0.96), "heroic": (0.10, 0.80, 0.80),
    "discord": (0.42, 0.47, 1.00), "power": (1.00, 0.68, 0.30),
    "danger": (1.00, 0.30, 0.30),
}
RUNNING = (0.49, 1.00, 0.63)

CARD_W, CARD_H = 250, 380       # card size
SEL_W, SEL_H = 292, 452         # selected card size
GAP = 26
SIDE = 96                       # left/right margin

DAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
MONTHS = ["ledna", "února", "března", "dubna", "května", "června", "července",
          "srpna", "září", "října", "listopadu", "prosince"]

OPEN_S, CLOSE_S, SPLASH_S = 0.45, 0.18, 0.35   # animation lengths
STAGGER_S = 0.05                                # delay between cards appearing
SMOOTH = 14.0                                   # how fast selection follows (1/s)

CSS = "window.smart-tv { background: none; }"   # see-through, so it can fade in over the desktop


# --- small helpers -------------------------------------------------------
def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out(x):
    return 1 - (1 - clamp01(x)) ** 3


def ease_out_back(x):
    x = clamp01(x) - 1
    return 1 + 2.70158 * x ** 3 + 1.70158 * x ** 2


def mix(a, b, t):
    return a + (b - a) * t


def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, clamp01(a)
    return c


# Keep the struct object itself: init() returns a pointer into it, and if the
# original is garbage collected that pointer dangles.
def rect(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, max(0.0, w), max(0.0, h))
    return r


def point(x, y):
    p = Graphene.Point()
    p.init(x, y)
    return p


def rounded(x, y, w, h, r):
    rr = Gsk.RoundedRect()
    rr.init_from_rect(rect(x, y, w, h), max(0.0, min(r, w / 2, h / 2)))
    return rr


def stops(*pairs):
    out = []
    for offset, color in pairs:
        s = Gsk.ColorStop()
        s.offset, s.color = offset, color
        out.append(s)
    return out


def radial(snap, cx, cy, radius, color, alpha):
    """Soft round light fading to nothing at `radius`."""
    if alpha <= 0.002:
        return
    r, g, b = color
    snap.append_radial_gradient(rect(cx - radius, cy - radius, radius * 2, radius * 2),
                                point(cx, cy), radius, radius, 0, 1,
                                stops((0, rgba(r, g, b, alpha)), (1, rgba(r, g, b, 0))))


_pango = None
_layouts = {}   # text layouts are reused between frames


def text_layout(text, size, weight=Pango.Weight.NORMAL, spacing=0.0, max_w=None):
    global _pango
    size = round(size, 1)
    max_w = round(max_w) if max_w else None
    key = (text, size, weight, spacing, max_w)
    lay = _layouts.get(key)
    if lay is not None:
        return lay
    if len(_layouts) > 400:
        _layouts.clear()
    if _pango is None:
        _pango = PangoCairo.FontMap.get_default().create_context()
    lay = Pango.Layout.new(_pango)
    _layouts[key] = lay
    fd = Pango.FontDescription.from_string(FONT)
    fd.set_absolute_size(size * Pango.SCALE)
    fd.set_weight(weight)
    lay.set_font_description(fd)
    if spacing:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
        lay.set_attributes(attrs)
    if max_w:
        lay.set_width(int(max_w * Pango.SCALE))
        lay.set_ellipsize(Pango.EllipsizeMode.END)
    lay.set_text(text, -1)
    return lay


def draw_layout(snap, lay, x, y, color):
    snap.save()
    snap.translate(point(x, y))
    snap.append_layout(lay, color)
    snap.restore()


def draw_text(snap, text, x, y, size, color=(1, 1, 1, 1), weight=Pango.Weight.NORMAL,
              align=0.0, spacing=0.0, max_w=None):
    """Draw text with its top-left corner at (x, y); align 0.5 centers, 1 right-aligns."""
    lay = text_layout(text, size, weight, spacing, max_w)
    w, h = lay.get_pixel_size()
    draw_layout(snap, lay, x - w * align, y, rgba(*color))
    return w, h


_wallpaper = {}


def wallpaper_texture():
    """Heavily blurred copy of the desktop wallpaper as a texture (cached), or None."""
    try:
        bg = Gio.Settings.new("org.gnome.desktop.background")
        dark = Gio.Settings.new("org.gnome.desktop.interface").get_string("color-scheme") == "prefer-dark"
        uri = (bg.get_string("picture-uri-dark") if dark else "") or bg.get_string("picture-uri")
    except Exception:
        return None
    if uri not in _wallpaper:
        tex = None
        try:
            path = Gio.File.new_for_uri(uri).get_path()
            if path and os.path.isfile(path) and not path.endswith(".xml"):
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 960, 960, True)
                # Shrink a lot and grow back twice = cheap, smooth blur.
                for div in (28, 10):
                    w, h = pb.get_width(), pb.get_height()
                    small = pb.scale_simple(max(2, w // div), max(2, h // div), GdkPixbuf.InterpType.HYPER)
                    pb = small.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
                tex = Gdk.Texture.new_for_pixbuf(pb)
        except GLib.Error:
            tex = None
        _wallpaper.clear()
        _wallpaper[uri] = tex
    return _wallpaper[uri]


def avatar_path():
    path = f"/var/lib/AccountsService/icons/{os.environ.get('USER') or os.getlogin()}"
    return path if os.path.isfile(path) else None


_css_done = False


def ensure_css():
    global _css_done
    if not _css_done:
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_done = True


def cover_screen(win):
    """Cover the screen with an overlay the app behind can show through.
    Not fullscreen: GNOME draws fullscreen windows as opaque (black behind them).
    A monitor-sized window gets maximized instead, which keeps it see-through."""
    monitors = Gdk.Display.get_default().get_monitors()
    if monitors.get_n_items():
        geo = monitors.get_item(0).get_geometry()
        win.set_default_size(geo.width, geo.height)
    win.maximize()


class Canvas(Gtk.Widget):
    """Fullscreen widget the view draws into."""

    def __init__(self, view):
        super().__init__()
        self.view = view
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snap):
        self.view.render(snap, self.get_width(), self.get_height(), self.get_scale_factor())


# --- the view ------------------------------------------------------------
class HomeView:
    def __init__(self, tv):
        self.tv = tv
        now = time.monotonic()
        self.open_t = now          # whole screen fades in
        self.menu_t = now          # cards fly in (on open and on menu change)
        self.menu_dir = 0
        self.close_t = None
        self.splash_t = None
        self.splash_text = ""
        self.splash_entry = None
        self.arm_t = -10.0
        self.last_armed = False
        self.reset_selection()
        self.accent = list(self.target_accent())
        self.power_focus = 1.0 if tv.focus == "power" else 0.0
        self.icons = {}
        self.hits = []             # (x, y, w, h, action) for mouse clicks, in widget pixels
        self.tick_id = None
        self.last_frame = None
        self.clock_id = None
        self.window = None
        self.canvas = None
        self.px_factor = 1.0

    # --- window ----------------------------------------------------------
    def show(self):
        ensure_css()
        win = Gtk.Window(title="Smart TV")
        win.set_decorated(False)
        win.add_css_class("smart-tv")
        self.canvas = Canvas(self)
        win.set_child(self.canvas)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        win.add_controller(keys)
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_click)
        self.canvas.add_controller(click)
        win.connect("close-request", self.on_close_request)
        self.window = win
        self.clock_id = GLib.timeout_add_seconds(5, self.on_clock)
        win.fullscreen()
        win.present()
        self.kick()

    def close(self):
        if self.close_t is None:
            self.close_t = time.monotonic()
            self.kick()

    def destroy(self):
        if self.clock_id:
            GLib.source_remove(self.clock_id)
            self.clock_id = None
        if self.window:
            self.window.destroy()
            self.window = self.canvas = None
        return False

    def on_close_request(self, _win):
        if self.close_t is None:
            self.tv.close()
        return True

    def __bool__(self):
        return True

    # --- called by SmartTV -----------------------------------------------
    def kick(self):
        """Something changed: animate until everything settles."""
        if self.canvas and self.tick_id is None:
            self.last_frame = None
            self.tick_id = self.canvas.add_tick_callback(self.on_tick)

    def menu_changed(self, direction=0):
        self.menu_t = time.monotonic()
        self.menu_dir = direction
        self.reset_selection()
        self.kick()

    def start_splash(self, text, entry):
        self.splash_t = time.monotonic()
        self.splash_text = text
        self.splash_entry = entry
        self.kick()

    # --- animation -------------------------------------------------------
    def reset_selection(self):
        self.sel = [1.0 if self.selected_card(i) else 0.0 for i in range(len(self.tv.entries))]

    def selected_card(self, i):
        return i == self.tv.selected and self.tv.focus == "cards"

    def target_accent(self):
        if self.tv.focus == "power" and self.tv.menu == "main":
            return ACCENTS["power"]
        entries = self.tv.entries
        e = entries[min(self.tv.selected, len(entries) - 1)]
        return ACCENTS.get(e.get("theme"), ACCENTS["power"])

    def step(self, dt, now):
        """Move animated values toward their targets. Returns True while anything moves."""
        f = 1 - math.exp(-dt * SMOOTH)
        moving = False

        def follow(v, target, k=f):
            nonlocal moving
            if abs(target - v) < 0.002:
                return target
            moving = True
            return v + (target - v) * k

        n = len(self.tv.entries)
        if len(self.sel) != n:
            self.reset_selection()
        self.sel = [follow(v, 1.0 if self.selected_card(i) else 0.0) for i, v in enumerate(self.sel)]
        self.accent = [follow(a, b, 1 - math.exp(-dt * 10)) for a, b in zip(self.accent, self.target_accent())]
        self.power_focus = follow(self.power_focus, 1.0 if self.tv.focus == "power" else 0.0)
        if self.tv.armed and not self.last_armed:
            self.arm_t = now
        self.last_armed = self.tv.armed

        tweens = (now - self.open_t < OPEN_S + 0.1
                  or now - self.menu_t < OPEN_S + STAGGER_S * n + 0.1
                  or now - self.arm_t < 0.5
                  or self.splash_t is not None
                  or self.close_t is not None)
        return moving or tweens

    def on_tick(self, widget, clock):
        now = time.monotonic()
        dt = 1 / 60 if self.last_frame is None else min(0.05, now - self.last_frame)
        self.last_frame = now
        busy = self.step(dt, now)
        widget.queue_draw()
        if self.close_t is not None and now - self.close_t >= CLOSE_S:
            self.tick_id = None
            GLib.idle_add(self.destroy)
            return False
        if not busy:
            self.tick_id = None
            return False
        return True

    def on_clock(self):
        if self.canvas:
            self.canvas.queue_draw()
        return True

    # --- input -----------------------------------------------------------
    def on_key(self, _ctrl, keyval, _keycode, _state):
        if self.close_t is None:
            self.tv.on_key(keyval)
        return True

    def on_click(self, _gesture, _n, x, y):
        if self.close_t is not None:
            return
        for hx, hy, hw, hh, action in self.hits:
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                if action == "power":
                    self.tv.open_power()
                else:
                    self.tv.select_and_activate(action)
                break

    # --- resources -------------------------------------------------------
    def icon(self, entry, size):
        """Icon for an entry at `size` design px: a Gdk.Texture, an IconPaintable, or None."""
        px = max(8, int(round(size * self.px_factor)))
        key = (entry["id"], px)
        if key in self.icons:
            return self.icons[key]
        found = None
        path = os.path.expanduser(entry.get("icon_file", ""))
        if path and os.path.isfile(path):
            try:
                found = Gdk.Texture.new_for_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(path, px, px, True))
            except GLib.Error:
                found = None
        if found is None:
            theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            for name in entry.get("icon", []):
                if theme.has_icon(name):
                    found = theme.lookup_icon(name, None, px, 1, Gtk.TextDirection.NONE, 0)
                    break
        self.icons[key] = found
        return found

    def draw_icon(self, snap, entry, x, y, size, color=(1, 1, 1), alpha=1.0):
        """Returns False when there's no icon (caller draws a glyph instead)."""
        ic = self.icon(entry, size)
        if ic is None:
            return False
        if alpha < 0.999:
            snap.push_opacity(alpha)
        if isinstance(ic, Gdk.Texture):
            snap.append_scaled_texture(ic, Gsk.ScalingFilter.TRILINEAR, rect(x, y, size, size))
        else:
            snap.save()
            snap.translate(point(x, y))
            if entry.get("symbolic"):
                ic.snapshot_symbolic(snap, size, size, [rgba(*color)])
            else:
                ic.snapshot(snap, size, size)
            snap.restore()
        if alpha < 0.999:
            snap.pop()
        return True

    def avatar(self, size):
        px = max(8, int(round(size * self.px_factor)))
        key = ("@avatar", px)
        if key not in self.icons:
            tex = None
            path = avatar_path()
            if path:
                try:
                    tex = Gdk.Texture.new_for_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(path, px, px, False))
                except GLib.Error:
                    tex = None
            self.icons[key] = tex
        return self.icons[key]

    # --- drawing ---------------------------------------------------------
    def render(self, snap, aw, ah, factor=1):
        if aw <= 0 or ah <= 0:
            return
        now = time.monotonic()
        s = aw / W
        hh = ah / s                # screen height in design units
        self.px_factor = s * factor
        self.hits = []

        fade = ease_out((now - self.open_t) / (OPEN_S * 0.6))
        if self.close_t is not None:
            fade *= 1 - clamp01((now - self.close_t) / CLOSE_S)
        if fade < 0.999:
            snap.push_opacity(fade)

        self.draw_background(snap, aw, ah)
        snap.save()
        snap.scale(s, s)
        self.draw_glow(snap, hh)
        self.draw_top(snap, s)
        self.draw_cards(snap, now, hh, s)
        self.draw_clock(snap, hh)
        self.draw_hints(snap, hh)
        self.draw_splash(snap, now, hh)
        snap.restore()

        if fade < 0.999:
            snap.pop()

    def draw_background(self, snap, aw, ah):
        tex = wallpaper_texture()
        if tex:
            k = max(aw / tex.get_width(), ah / tex.get_height())
            tw, th = tex.get_width() * k, tex.get_height() * k
            snap.append_scaled_texture(tex, Gsk.ScalingFilter.LINEAR, rect((aw - tw) / 2, (ah - th) / 2, tw, th))
            snap.append_color(rgba(*TINT, 0.72), rect(0, 0, aw, ah))
        else:
            snap.append_linear_gradient(rect(0, 0, aw, ah), point(0, 0), point(aw, ah),
                                        stops((0, rgba(0.17, 0.17, 0.26)), (1, rgba(0.06, 0.06, 0.10))))
        # darker on the left where the cards are, a bit darker at the bottom
        snap.append_linear_gradient(rect(0, 0, aw, ah), point(0, 0), point(aw, 0),
                                    stops((0, rgba(*TINT, 0.55)), (0.6, rgba(*TINT, 0.20)), (1, rgba(*TINT, 0))))
        snap.append_linear_gradient(rect(0, ah * 0.55, aw, ah * 0.45), point(0, ah * 0.55), point(0, ah),
                                    stops((0, rgba(0, 0, 0, 0)), (1, rgba(0, 0, 0, 0.35))))

    def draw_glow(self, snap, hh):
        # soft light in the selected app's color, top right and behind the cards
        radial(snap, W * 0.86, -40, 900, self.accent, 0.30)
        radial(snap, SIDE + 300, hh * 0.72, 760, self.accent, 0.16)

    def draw_top(self, snap, s):
        y = 52
        w, _ = draw_text(snap, "SMART", SIDE, y, 40, weight=Pango.Weight.HEAVY, spacing=1)
        draw_text(snap, "TV", SIDE + w + 10, y + 15, 22, (1, 1, 1, 0.8), Pango.Weight.LIGHT, spacing=2)
        x = SIDE + w + 74
        snap.append_color(rgba(1, 1, 1, 0.22), rect(x, y + 2, 1.5, 48))

        # gamepad status, where the design has its search box
        cx, cy, r = x + 36 + 34, y + 26, 32
        has_pad = bool(self.tv.pads)
        self.circle(snap, cx, cy, r, rgba(1, 1, 1, 0.10))
        self.draw_icon(snap, {"id": "@pad", "icon": ["input-gaming-symbolic"], "symbolic": True},
                       cx - 15, cy - 15, 30, alpha=1.0 if has_pad else 0.4)
        draw_text(snap, "Ovladač připojen" if has_pad else "Připoj ovladač", cx + r + 18, cy - 14, 22,
                  (1, 1, 1, 0.70) if has_pad else (1, 0.55, 0.55, 0.9))

        # avatar with a red ring
        ar = 40
        ax, ay = W - SIDE - ar, y + 26
        tex = self.avatar(ar * 2)
        snap.push_rounded_clip(rounded(ax - ar, ay - ar, ar * 2, ar * 2, ar))
        if tex:
            snap.append_scaled_texture(tex, Gsk.ScalingFilter.TRILINEAR, rect(ax - ar, ay - ar, ar * 2, ar * 2))
        else:
            snap.append_color(rgba(0.85, 0.18, 0.22), rect(ax - ar, ay - ar, ar * 2, ar * 2))
            draw_text(snap, os.environ.get("USER", "?")[:1].upper(), ax, ay - 22, 36,
                      weight=Pango.Weight.BOLD, align=0.5)
        snap.pop()
        red = rgba(0.90, 0.20, 0.25)
        snap.append_border(rounded(ax - ar, ay - ar, ar * 2, ar * 2, ar), [3] * 4, [red] * 4)

        # power button (D-pad up)
        pf = self.power_focus
        pr = 36 * (1 + 0.14 * pf)
        px, py = ax - ar - 34 - 36, ay
        radial(snap, px, py, pr + 30, (1, 1, 1), 0.35 * pf)
        self.circle(snap, px, py, pr, rgba(1, 1, 1, mix(0.10, 0.95, pf)))
        size = 30 * (1 + 0.14 * pf)
        color = tuple(mix(1.0, c, pf) for c in TINT)
        self.draw_icon(snap, {"id": "@power", "icon": ["system-shutdown-symbolic"], "symbolic": True},
                       px - size / 2, py - size / 2, size, color)
        self.hits.append(((px - pr) * s, (py - pr) * s, pr * 2 * s, pr * 2 * s, "power"))

    @staticmethod
    def circle(snap, cx, cy, r, color):
        snap.push_rounded_clip(rounded(cx - r, cy - r, 2 * r, 2 * r, r))
        snap.append_color(color, rect(cx - r, cy - r, 2 * r, 2 * r))
        snap.pop()

    def draw_cards(self, snap, now, hh, s):
        tv = self.tv
        entries = tv.entries
        cy = hh * 0.53
        if tv.menu != "main":
            a = ease_out((now - self.menu_t) / OPEN_S)
            top = cy - SEL_H / 2 - 120
            draw_text(snap, "Vypnout", SIDE + 40 * (1 - a), top, 52, (1, 1, 1, a), Pango.Weight.SEMIBOLD)
            draw_text(snap, entries[tv.selected].get("desc", ""), SIDE + 40 * (1 - a), top + 68, 24,
                      (1, 1, 1, 0.65 * a))

        x = SIDE
        for i, e in enumerate(entries):
            v = self.sel[i] if i < len(self.sel) else 0.0
            w = mix(CARD_W, SEL_W, v)
            h = mix(CARD_H, SEL_H, v)
            appear = ease_out_back((now - self.menu_t - STAGGER_S * i) / OPEN_S)
            alpha = clamp01((now - self.menu_t - STAGGER_S * i) / (OPEN_S * 0.6))
            dx = (1 - appear) * 80 * (self.menu_dir or 0)
            dy = (1 - appear) * (0 if self.menu_dir else 90)
            shake = 0.0
            if tv.armed and i == tv.selected:
                t = now - self.arm_t
                shake = math.sin(t * 45) * 10 * (1 - clamp01(t / 0.45))
            cx = x + dx + shake
            cy0 = cy - h / 2 + dy
            if alpha > 0.001:
                self.draw_card(snap, e, cx, cy0, w, h, v, alpha, i == tv.selected and tv.armed)
                self.hits.append((cx * s, cy0 * s, w * s, h * s, i))
            x += w + GAP

    def draw_card(self, snap, e, x, y, w, h, v, alpha, armed):
        if alpha < 0.999:
            snap.push_opacity(alpha)
        k = w / CARD_W
        acc = ACCENTS.get(e.get("theme"), ACCENTS["power"])
        card = rounded(x, y, w, h, 16)

        if v > 0.01:
            snap.append_outset_shadow(card, rgba(0, 0, 0, 0.50 * v), 0, 22, 0, 46)

        snap.push_rounded_clip(card)
        snap.append_linear_gradient(rect(x, y, w, h), point(x, y), point(x, y + h),
                                    stops((0, rgba(1, 1, 1, 0.11 + 0.10 * v)), (1, rgba(1, 1, 1, 0.04 + 0.07 * v))))
        if v > 0.01:
            # app color rising from the bottom, and light pooling at the bottom edge
            snap.append_linear_gradient(rect(x, y, w, h), point(x, y + h), point(x, y + h * 0.35),
                                        stops((0, rgba(*acc, 0.32 * v)), (1, rgba(*acc, 0))))
            radial(snap, x + w / 2, y + h, w * 0.55, (1, 1, 1), 0.30 * v)
        if armed:
            snap.append_color(rgba(1, 0.2, 0.25, 0.45), rect(x, y, w, h))
        snap.pop()
        border = rgba(1, 1, 1, 0.10 + 0.28 * v)
        snap.append_border(card, [1.5] * 4, [border] * 4)

        if v > 0.01:  # glowing bar under the card
            bx, by = x + w / 2, y + h
            radial(snap, bx, by + 20, w * 0.35, (1, 1, 1), 0.16 * v)
            bw = w * 0.42 * v
            snap.push_rounded_clip(rounded(bx - bw / 2, by + 17, bw, 6, 3))
            snap.append_color(rgba(1, 1, 1, 0.95 * v), rect(bx - bw / 2, by + 17, bw, 6))
            snap.pop()

        pad = 30 * k
        size = 88 * k
        iy = y + h * 0.40 - size / 2
        if not self.draw_icon(snap, e, x + pad, iy, size):
            draw_text(snap, e.get("glyph", e["name"][:1]), x + pad, iy - 10 * k, 96 * k,
                      (*acc, 1), Pango.Weight.HEAVY)

        sub = "Stiskni A znovu" if armed else e.get("sub", "")
        draw_text(snap, e["name"], x + pad, y + h - 118 * k, 34 * k, weight=Pango.Weight.MEDIUM,
                  max_w=w - pad * 1.5)
        draw_text(snap, sub, x + pad, y + h - 70 * k, 19 * k,
                  (1, 0.7, 0.7, 1) if armed else (1, 1, 1, 0.62), max_w=w - pad * 1.5)

        if e["id"] in self.tv.running:
            self.draw_badge(snap, x + w - 18 * k, y + 18 * k, k)

        if alpha < 0.999:
            snap.pop()

    def draw_badge(self, snap, right, top, k):
        lay = text_layout("Běží", 16 * k, Pango.Weight.SEMIBOLD)
        tw, th = lay.get_pixel_size()
        bw, bh = tw + 40 * k, th + 12 * k
        x = right - bw
        snap.push_rounded_clip(rounded(x, top, bw, bh, bh / 2))
        snap.append_color(rgba(0, 0, 0, 0.40), rect(x, top, bw, bh))
        snap.pop()
        self.circle(snap, x + 15 * k, top + bh / 2, 5 * k, rgba(*RUNNING))
        draw_layout(snap, lay, x + 27 * k, top + 6 * k, rgba(1, 1, 1, 0.95))

    def draw_clock(self, snap, hh):
        now = time.localtime()
        date = f"{DAYS[now.tm_wday]}, {now.tm_mday}. {MONTHS[now.tm_mon - 1]}"
        draw_text(snap, date, W - SIDE, hh - 132, 34, (1, 1, 1, 0.80), Pango.Weight.LIGHT, align=1.0)
        draw_text(snap, time.strftime("%H:%M", now), W - SIDE + 6, hh - 300, 150,
                  (1, 1, 1, 0.95), Pango.Weight.THIN, align=1.0)

    def draw_hints(self, snap, hh):
        x, y = SIDE, hh - 92
        dark = rgba(*TINT)
        for key, label in self.tv.hints():
            lay = text_layout(key, 18, Pango.Weight.BOLD)
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
        a = ease_out(t / SPLASH_S)
        snap.append_color(rgba(*TINT, 0.88 * a), rect(0, 0, W, hh))
        cx, cy = W / 2, hh / 2 - 60
        e = self.splash_entry or {}
        acc = ACCENTS.get(e.get("theme"), ACCENTS["power"])
        radial(snap, cx, cy, 420, acc, 0.35 * a)
        size = 150 * ease_out_back(t / (SPLASH_S * 1.4))
        if e and size > 1:
            if not self.draw_icon(snap, e, cx - size / 2, cy - size / 2, size, alpha=a):
                draw_text(snap, e.get("glyph", "?"), cx, cy - size * 0.7, size * 1.1, (*acc, a),
                          Pango.Weight.HEAVY, align=0.5)
        draw_text(snap, self.splash_text, cx, cy + 110, 40, (1, 1, 1, a), Pango.Weight.MEDIUM, align=0.5)
        # spinner
        sy = cy + 200
        cr = snap.append_cairo(rect(cx - 30, sy - 30, 60, 60))
        ang = t * 5.5
        cr.set_line_width(4)
        cr.set_line_cap(1)  # round
        cr.arc(cx, sy, 22, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.15 * a)
        cr.stroke()
        cr.arc(cx, sy, 22, ang, ang + 1.6 + 0.6 * math.sin(t * 3))
        cr.set_source_rgba(1, 1, 1, 0.9 * a)
        cr.stroke()

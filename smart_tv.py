#!/usr/bin/env python3
"""Smart TV: a Big Picture style home screen for Linux, driven by a gamepad.

Reads gamepads straight from /dev/input (user must be in the 'input' group).
The Home button opens a fullscreen home screen with YouTube, Netflix, Steam,
Heroic and Discord. YouTube and Netflix run as Google Chrome kiosk windows.
While a web app or Discord is in front, the gamepad is turned into arrow keys
and a mouse through a virtual /dev/uinput device (see vpad.py).

  smart_tv.py            background mode (systemd user service in normal GNOME)
  smart_tv.py --session  Smart TV login session (gnome-kiosk): the home screen
                         comes back whenever the app in front quits
  smart_tv.py --open     background mode, but show the home screen right away
"""
import fcntl
import glob
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import home_view  # noqa: E402
import control_center  # noqa: E402
import keyboard_view  # noqa: E402
import vpad  # noqa: E402
import x11win  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.expanduser("~/.local/share/smart-tv/profiles")
CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable") or "google-chrome"

# YouTube shows its TV interface (made for arrow keys) only to TVs.
TV_USER_AGENT = ("Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36")

# --- Apps ----------------------------------------------------------------
# kind "web": Google Chrome kiosk window with its own profile, so logins stay.
#             Only one web app runs at a time, like on a TV.
# pad "keys": gamepad becomes keyboard + mouse while the app is in front.
#     "native": the app reads the gamepad itself (Steam, Heroic).
# flatpak / proc: how to tell the app is running (flatpak id / process name).
# close: command that quits the app nicely (otherwise flatpak kill / stop its scope).
# icon: theme icon names; icon_file: image file; glyph: text drawn when no icon is found.
APPS = [
    {
        "id": "youtube", "name": "YouTube", "theme": "youtube",
        "sub": "Televizní rozhraní",
        "kind": "web", "url": "https://www.youtube.com/tv", "user_agent": TV_USER_AGENT,
        "icon_file": "~/.local/share/icons/hicolor/256x256/apps/"
                     "chrome-agimnkijcaahngcdmfeangaknmldooml-Default.png",
        "glyph": "▶", "pad": "keys",
    },
    {
        "id": "netflix", "name": "Netflix", "theme": "netflix",
        "sub": "Filmy a seriály",
        "kind": "web", "url": "https://www.netflix.com/browse",
        "glyph": "N", "pad": "keys",
    },
    {
        "id": "steam", "name": "Steam", "theme": "steam",
        "sub": "Big Picture",
        "cmd": ["steam", "-bigpicture"], "proc": "steam", "close": ["steam", "-shutdown"],
        "icon": ["steam"], "glyph": "S", "pad": "native",
    },
    {
        "id": "heroic", "name": "Heroic", "theme": "heroic",
        "sub": "Epic · GOG · Amazon",
        "cmd": ["flatpak", "run", "com.heroicgameslauncher.hgl", "--fullscreen", "--console"],
        "flatpak": "com.heroicgameslauncher.hgl",
        "icon": ["com.heroicgameslauncher.hgl"], "glyph": "H", "pad": "native",
    },
    {
        "id": "discord", "name": "Discord", "theme": "discord",
        "sub": "Chat a hlas",
        # On XWayland, so we can focus its window and make it fullscreen (see x11win.py).
        "cmd": ["flatpak", "run", "com.discordapp.Discord", "--ozone-platform=x11"],
        "flatpak": "com.discordapp.Discord",
        "icon": ["com.discordapp.Discord"], "glyph": "D", "pad": "keys",
        "x11_class": "discord",
        # its main window is up once it logs this
        "ready_log": ("~/.var/app/com.discordapp.Discord/config/discord/logs/renderer_js.log",
                      b"[GatewaySocket] [READY]"),
        "buttons": {"Y": "toggle_fullscreen"},   # instead of typing "f" into the chat
    },
]

# Menu behind the power button (top right, D-pad up).
POWER = [
    {
        "id": "suspend", "name": "Uspat", "theme": "power",
        "sub": "Úsporný režim", "desc": "Uspí počítač",
        "cmd": ["systemctl", "suspend"],
        "icon": ["weather-clear-night-symbolic", "system-suspend-symbolic"], "symbolic": True,
        "glyph": "☾",
    },
    {
        "id": "poweroff", "name": "Vypnout PC", "theme": "danger",
        "sub": "Vypne počítač", "desc": "Vypne počítač (potvrdíš druhým stiskem A)",
        "cmd": ["systemctl", "poweroff"], "confirm": True,
        "icon": ["system-shutdown-symbolic"], "symbolic": True, "glyph": "⏻",
    },
    {
        "id": "close_all", "name": "Zavřít vše", "theme": "power",
        "sub": "Ukončí aplikace", "desc": "Ukončí Steam, Heroic, Discord, YouTube i Netflix",
        "action": "close_all",
        "icon": ["window-close-symbolic", "application-exit-symbolic"], "symbolic": True, "glyph": "✕",
    },
    {
        "id": "logout", "name": "Do GNOME", "theme": "power",
        "sub": "Odhlásit Smart TV", "desc": "Odhlásí Smart TV, na přihlašovací obrazovce pak vybereš GNOME",
        "action": "logout", "session_only": True,
        "icon": ["system-log-out-symbolic", "application-exit-symbolic"], "symbolic": True, "glyph": "⇄",
    },
]

APP_BY_ID = {a["id"]: a for a in APPS}

# --- gamepad (evdev) -----------------------------------------------------
EV_KEY, EV_ABS = 0x01, 0x03
BTN_SOUTH, BTN_EAST, BTN_X, BTN_Y = 304, 305, 307, 308  # A, B, X, Y (Xbox layout)
BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE = 310, 311, 314, 315, 316
BTN_THUMBL, BTN_THUMBR = 317, 318
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
EVENT = struct.Struct("llHHi")  # timeval, type, code, value
EVIOCGRAB = (1 << 30) | (4 << 16) | (ord("E") << 8) | 0x90  # _IOW('E', 0x90, int)

# Gamepad -> keyboard/mouse while a "keys" app is in front.
BUTTON_KEYS = {
    BTN_SOUTH: [vpad.KEY_ENTER],
    BTN_EAST: [vpad.KEY_ESC],
    BTN_X: [vpad.KEY_SPACE],
    BTN_Y: [vpad.KEY_F],
    BTN_TL: [vpad.KEY_LEFTSHIFT, vpad.KEY_TAB],
    BTN_TR: [vpad.KEY_TAB],
    BTN_THUMBR: [vpad.BTN_LEFT],
    BTN_SELECT: "open_keyboard",   # on-screen keyboard (keyboard_view.py)
    BTN_START: "open_control_center",   # volume & playback (control_center.py)
}
PAD_BUTTONS = {"A": BTN_SOUTH, "B": BTN_EAST, "X": BTN_X, "Y": BTN_Y, "LB": BTN_TL, "RB": BTN_TR}
STICK_ON, STICK_OFF = 0.55, 0.35      # left stick -> arrow keys (with hysteresis)
TRIGGER_ON, TRIGGER_OFF = 0.5, 0.3    # triggers -> mouse clicks
MOUSE_DEAD = 0.15                     # right stick dead zone
MOUSE_SPEED = 22                      # pixels per tick at full tilt
MOUSE_TICK_MS = 16

# While Steam runs, a short Home press belongs to Steam; hold it this long to open our menu.
HOLD_MS = 1000
LAUNCH_SPLASH_MS = 1200   # "Spouštím…" stays this long before the home screen hides
LAUNCH_GRACE_S = 15       # a just launched app counts as running while it starts up


def eviocgabs(axis):
    # _IOR('E', 0x40 + axis, struct input_absinfo) ; absinfo = 6 x int32
    return (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + axis)


def has_key(event_name, code):
    try:
        words = open(f"/sys/class/input/{event_name}/device/capabilities/key").read().split()
    except OSError:
        return False
    bits = int("".join(w.rjust(16, "0") for w in words), 16)
    return bool(bits >> code & 1)


def run_quiet(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def steam_running():
    r = run_quiet(["pgrep", "-x", "steam"])
    return bool(r and r.returncode == 0)


def scope_name(app):
    return f"smart-tv-{app['id']}.scope"


def browser_pids(app):
    """Main Chrome process(es) of a web app: its profile, and not a helper (--type=...)."""
    flag = f"--user-data-dir={os.path.join(PROFILES, app['id'])}".encode()
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                # Chrome rewrites its command line into one space-separated string
                args = f.read().replace(b"\0", b" ").split()
        except OSError:
            continue
        if flag in args and not any(a.startswith(b"--type=") for a in args):
            pids.append(int(d))
    return pids


def quit_browser(app, timeout=10):
    """Close a web app's Chrome like closing its window, so it saves the login
    (cookies, local storage). Killing all its processes at once loses it."""
    for pid in browser_pids(app):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.monotonic() + timeout
    while browser_pids(app) and time.monotonic() < end:
        time.sleep(0.2)
    run_quiet(["systemctl", "--user", "stop", scope_name(app)], timeout=20)  # leftovers / if it hung


def chrome_cmd(app):
    cmd = [
        CHROME,
        f"--user-data-dir={os.path.join(PROFILES, app['id'])}",
        "--kiosk", f"--app={app['url']}",
        "--no-first-run", "--no-default-browser-check",
        "--password-store=basic",
        "--autoplay-policy=no-user-gesture-required",
        "--hide-crash-restore-bubble", "--noerrdialogs",
        "--disable-features=Translate,MediaRouter",
    ]
    if app.get("user_agent"):
        cmd.append(f"--user-agent={app['user_agent']}")
    return cmd


class Gamepad:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.ranges = {}
        for axis in (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ):
            try:
                buf = fcntl.ioctl(self.fd, eviocgabs(axis), bytes(24))
                _, lo, hi, *_ = struct.unpack("6i", buf)
                if hi > lo:
                    self.ranges[axis] = (lo, hi)
            except OSError:
                pass
        self.menu_dir = {}   # left stick direction in the menu, per axis
        self.held = {}       # source -> key/button the keyboard mode holds for it
        self.stick = {}      # right stick, -1..1
        self.grabbed = False

    def stick_value(self, axis, value):
        lo, hi = self.ranges.get(axis, (-32768, 32767))
        return max(-1.0, min(1.0, (2 * value - lo - hi) / (hi - lo)))

    def trigger_value(self, axis, value):
        lo, hi = self.ranges.get(axis, (0, 255))
        return max(0.0, min(1.0, (value - lo) / (hi - lo)))

    def grab(self, on):
        # Exclusive access, so apps behind us don't also react to the buttons.
        if on == self.grabbed:
            return
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1 if on else 0)
            self.grabbed = on
        except OSError:
            pass


class SmartTV:
    def __init__(self, session=False):
        self.session = session
        self.pads = {}            # path -> (Gamepad, watch id)
        self.window = None        # HomeView while the home screen is shown
        self.focus = "cards"      # "cards" or "power" (the power button, top right)
        self.menu = "main"
        self.selected = 0
        self.main_selected = 0
        self.armed = False        # first A press on a "confirm" entry only asks
        self.busy = False         # "Spouštím…" splash is showing
        self.hold_timer = None
        self.mouse_timer = None
        self.mouse_rest = [0.0, 0.0]
        self.mapping = False      # gamepad -> keyboard/mouse is on
        self.current = None       # id of the app we last brought to front
        self.running = set()
        self.launched = {}        # app id -> launch time
        self.procs = []           # started processes, to reap them
        self.osk = None           # KeyboardView while the on-screen keyboard is open
        self.cc = None            # ControlCenter while it is open
        self.osk_held = {}        # source -> direction held on the keyboard (for repeat)
        self.osk_rep_id = None
        self.osk_rep_dir = None
        try:
            self.vpad = vpad.VirtualPad()
        except OSError as e:
            self.vpad = None
            print(f"no /dev/uinput ({e}), keyboard mode disabled", flush=True)
        self.scan()
        GLib.timeout_add_seconds(2, self.tick)

    # --- device handling -------------------------------------------------
    def scan(self):
        for path in glob.glob("/dev/input/event*"):
            name = os.path.basename(path)
            if path in self.pads or not (has_key(name, BTN_MODE) and has_key(name, BTN_SOUTH)):
                continue
            try:
                pad = Gamepad(path)
            except OSError:
                continue
            wid = GLib.io_add_watch(pad.fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_input, pad)
            self.pads[path] = (pad, wid)
            pad.grab(self.want_grab())
            print(f"gamepad connected: {path}", flush=True)
            self.update_pad_status()
        return True

    def drop(self, pad):
        self.pads.pop(pad.path, None)
        self.release_pad(pad)
        try:
            os.close(pad.fd)
        except OSError:
            pass
        print(f"gamepad disconnected: {pad.path}", flush=True)
        self.update_pad_status()

    def on_input(self, fd, cond, pad):
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            self.drop(pad)
            return False
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return True
        except OSError:
            self.drop(pad)
            return False
        for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, etype, code, value = EVENT.unpack_from(data, i)
            self.handle(pad, etype, code, value)
        return True

    def want_grab(self):
        return bool(self.window) or bool(self.osk) or bool(self.cc) or self.mapping

    def apply_grab(self):
        on = self.want_grab()
        for pad, _ in self.pads.values():
            pad.grab(on)

    def handle(self, pad, etype, code, value):
        if etype == EV_KEY and code == BTN_MODE:
            if value == 1:
                if self.osk:
                    self.keyboard_cancel()
                if self.cc:
                    self.close_control_center()
                self.home_pressed()
            elif value == 0:
                self.cancel_hold()
        elif self.osk:
            self.handle_osk(pad, etype, code, value)
        elif self.cc:
            self.handle_cc(pad, etype, code, value)
        elif self.window:
            self.handle_menu(pad, etype, code, value)
        elif self.mapping:
            self.handle_keys(pad, etype, code, value)

    # --- app state -------------------------------------------------------
    def running_ids(self):
        ids = set()
        r = run_quiet(["systemctl", "--user", "list-units", "--type=scope", "--state=active",
                       "--plain", "--no-legend", "smart-tv-*"])
        if r:
            for line in r.stdout.splitlines():
                unit = line.split()[0] if line.split() else ""
                if unit.startswith("smart-tv-") and unit.endswith(".scope"):
                    ids.add(unit[len("smart-tv-"):-len(".scope")])
        flatpaks = None
        for app in APPS:
            if app.get("flatpak"):
                if flatpaks is None:
                    r = run_quiet(["flatpak", "ps", "--columns=application"])
                    flatpaks = set(r.stdout.split()) if r else set()
                if app["flatpak"] in flatpaks:
                    ids.add(app["id"])
            if app.get("proc"):
                r = run_quiet(["pgrep", "-x", app["proc"]])
                if r and r.returncode == 0:
                    ids.add(app["id"])
        now = time.monotonic()
        ids |= {i for i, t in self.launched.items() if now - t < LAUNCH_GRACE_S}
        return ids

    def can_leave(self):
        # In the Smart TV session there is no desktop behind us: only leave
        # the home screen when there's an app to go back to.
        return not self.session or self.current in self.running

    def tick(self):
        self.scan()
        self.procs = [p for p in self.procs if p.poll() is None]
        cur = APP_BY_ID.get(self.current)
        if self.window or self.session or (cur and cur.get("pad") == "keys"):
            self.running = self.running_ids()
        if self.current and self.current not in self.running:
            print(f"{self.current} quit", flush=True)
            self.current = None
        if self.window and not self.busy:
            self.refresh()
        self.update_mapping()
        if self.session and not self.window and not self.osk and not self.cc and not self.can_leave():
            self.open()
        return True

    def update_mapping(self):
        app = APP_BY_ID.get(self.current)
        want = bool(self.vpad and not self.window and not self.osk and not self.cc and app and app.get("pad") == "keys"
                    and app["id"] in self.running)
        if want != self.mapping:
            self.mapping = want
            if not want:
                for pad, _ in self.pads.values():
                    self.release_pad(pad)
            print(f"keyboard mode {'on' if want else 'off'}", flush=True)
        self.apply_grab()

    # --- keyboard mode ---------------------------------------------------
    def hold(self, pad, source, code):
        """Make `source` (a button, stick direction...) hold `code`, or nothing if None."""
        old = pad.held.get(source)
        if old == code:
            return
        if old is not None:
            self.vpad.release(old)
            del pad.held[source]
        if code is not None:
            self.vpad.press(code)
            pad.held[source] = code

    def release_pad(self, pad):
        if self.vpad:
            for code in pad.held.values():
                self.vpad.release(code)
        pad.held.clear()
        pad.stick.clear()

    def button_keys(self):
        keymap = dict(BUTTON_KEYS)
        app = APP_BY_ID.get(self.current) or {}
        for name, keys in app.get("buttons", {}).items():
            keymap[PAD_BUTTONS[name]] = keys
        return keymap

    def handle_keys(self, pad, etype, code, value):
        keymap = self.button_keys()
        if etype == EV_KEY and isinstance(keymap.get(code), str):
            if value == 1:
                getattr(self, keymap[code])()   # e.g. toggle_fullscreen
        elif etype == EV_KEY and code in keymap:
            keys = keymap[code]  # e.g. LB = Shift+Tab: press in order, release reversed
            if value:
                for i, key in enumerate(keys):
                    self.hold(pad, (code, i), key)
            else:
                for i in reversed(range(len(keys))):
                    self.hold(pad, (code, i), None)
        elif etype == EV_ABS:
            if code == ABS_HAT0X:
                self.hold(pad, "hatx", vpad.KEY_LEFT if value < 0 else vpad.KEY_RIGHT if value > 0 else None)
            elif code == ABS_HAT0Y:
                self.hold(pad, "haty", vpad.KEY_UP if value < 0 else vpad.KEY_DOWN if value > 0 else None)
            elif code in (ABS_X, ABS_Y):
                v = pad.stick_value(code, value)
                neg, pos = (vpad.KEY_LEFT, vpad.KEY_RIGHT) if code == ABS_X else (vpad.KEY_UP, vpad.KEY_DOWN)
                old = pad.held.get(("ls", code))
                if v <= -STICK_ON or (old == neg and v < -STICK_OFF):
                    self.hold(pad, ("ls", code), neg)
                elif v >= STICK_ON or (old == pos and v > STICK_OFF):
                    self.hold(pad, ("ls", code), pos)
                else:
                    self.hold(pad, ("ls", code), None)
            elif code in (ABS_RX, ABS_RY):
                pad.stick[code] = pad.stick_value(code, value)
                if self.mouse_timer is None and math.hypot(*self.stick_of(pad)) > MOUSE_DEAD:
                    self.mouse_timer = GLib.timeout_add(MOUSE_TICK_MS, self.mouse_tick)
            elif code in (ABS_Z, ABS_RZ):
                t = pad.trigger_value(code, value)
                button = vpad.BTN_RIGHT if code == ABS_Z else vpad.BTN_LEFT
                down = pad.held.get(("trig", code)) is not None
                self.hold(pad, ("trig", code), button if t >= TRIGGER_ON or (down and t > TRIGGER_OFF) else None)

    @staticmethod
    def stick_of(pad):
        return pad.stick.get(ABS_RX, 0.0), pad.stick.get(ABS_RY, 0.0)

    def mouse_tick(self):
        dx = dy = 0.0
        moving = False
        if self.mapping:
            for pad, _ in self.pads.values():
                x, y = self.stick_of(pad)
                m = math.hypot(x, y)
                if m <= MOUSE_DEAD:
                    continue
                moving = True
                k = ((min(m, 1.0) - MOUSE_DEAD) / (1 - MOUSE_DEAD)) ** 2 * MOUSE_SPEED / m
                dx += x * k
                dy += y * k
        if not moving:
            self.mouse_timer = None
            self.mouse_rest = [0.0, 0.0]
            return False
        self.mouse_rest[0] += dx
        self.mouse_rest[1] += dy
        ix, iy = int(self.mouse_rest[0]), int(self.mouse_rest[1])
        self.mouse_rest[0] -= ix
        self.mouse_rest[1] -= iy
        self.vpad.move(ix, iy)
        return True

    # --- launching -------------------------------------------------------
    def spawn(self, cmd, unit=None, stop=(), before=None, quit_web=()):
        def work():
            for app in quit_web:   # web apps to close nicely first
                quit_browser(app)
            if before:
                run_quiet(before, timeout=20)
                time.sleep(1.5)  # let it exit before starting again
            if stop:
                run_quiet(["systemctl", "--user", "stop", *stop], timeout=20)
            full = cmd
            # Own systemd scope: restarting this service doesn't kill the app,
            # and we can tell whether it still runs and stop it.
            if shutil.which("systemd-run"):
                full = ["systemd-run", "--user", "--scope", "--collect", "--quiet"]
                full += [f"--unit={unit}"] if unit else []
                full += ["--"] + cmd
            try:
                proc = subprocess.Popen(full, start_new_session=True, cwd=os.path.expanduser("~"),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError as e:
                print(f"cannot start {cmd[0]}: {e}", flush=True)
                return
            self.procs.append(proc)  # reaped in tick(); Chrome stays our child its whole life
        threading.Thread(target=work, daemon=True).start()

    def launch_app(self, app):
        aid = app["id"]
        if aid in self.running and self.current == aid:
            print(f"back to {aid}", flush=True)
            self.close()            # close() also brings X11 apps back to front
            return
        if app.get("kind") == "web":
            # One web app at a time. A running one is restarted, because we
            # can't bring its window to front from outside on Wayland.
            self.spawn(chrome_cmd(app), unit=scope_name(app), quit_web=self.running_web_apps())
            for a in APPS:
                if a.get("kind") == "web":
                    self.running.discard(a["id"])
                    self.launched.pop(a["id"], None)
        elif aid in self.running and app.get("x11_class"):
            if x11win.main_window(app["x11_class"]):
                self.front_later(app)    # we can raise it ourselves
            else:
                # Running natively on Wayland (started outside Smart TV): we can't
                # focus or resize it, so restart it on XWayland.
                print(f"{aid}: restarting on XWayland", flush=True)
                self.spawn(app["cmd"], unit=scope_name(app), stop=[scope_name(app)],
                           before=["flatpak", "kill", app["flatpak"]])
                self.watch_ready(app)
        elif aid in self.running:
            # Steam and Heroic bring their window to front when started again.
            self.spawn(app["cmd"])
        else:
            self.spawn(app["cmd"], unit=scope_name(app))
            if app.get("x11_class"):
                self.watch_ready(app)
        print(f"launching {aid}", flush=True)
        self.current = aid
        self.launched[aid] = time.monotonic()
        self.running.add(aid)
        self.splash(f"Spouštím {app['name']}", app)

    # --- X11 apps we focus and make fullscreen ourselves (Discord) ----------
    def watch_ready(self, app):
        """After a fresh start, wait until the app logs that its main window is up."""
        if self.session:
            return  # gnome-kiosk makes windows fullscreen itself
        path = os.path.expanduser(app["ready_log"][0])
        try:
            pos = os.path.getsize(path)
        except OSError:
            pos = 0
        state = {"pos": pos, "tail": b"", "until": time.monotonic() + 60}
        GLib.timeout_add(500, self.wait_ready, app, path, state)

    def wait_ready(self, app, path, state):
        if self.current != app["id"] or time.monotonic() > state["until"]:
            return False
        marker = app["ready_log"][1]
        try:
            if os.path.getsize(path) < state["pos"]:
                state["pos"] = 0  # log was rotated
            with open(path, "rb") as f:
                f.seek(state["pos"])
                chunk = f.read()
        except OSError:
            return True
        state["pos"] += len(chunk)
        if marker in state["tail"] + chunk:
            self.front_later(app, 600)
            return False
        state["tail"] = chunk[-len(marker):]
        return True

    def front_later(self, app, delay=LAUNCH_SPLASH_MS + 300):
        if self.session:
            return
        state = {"until": time.monotonic() + delay / 1000 + 15}
        GLib.timeout_add(delay, self.bring_to_front, app, state)

    def bring_to_front(self, app, state):
        """Focus the app's X11 window and make it fullscreen. Retries while it isn't there yet."""
        if self.current != app["id"] or time.monotonic() > state["until"]:
            return False
        if self.window:
            return True  # home screen is open, try again once it's gone
        win = x11win.main_window(app["x11_class"])
        if not win:
            return True
        x11win.bring_to_front(win, fullscreen=True)
        print(f"{app['id']}: in front, fullscreen", flush=True)
        return False

    # --- on-screen keyboard ------------------------------------------------
    def current_theme(self):
        app = APP_BY_ID.get(self.current)
        return app.get("theme", "") if app else ""

    def open_keyboard(self):
        if self.osk or self.window or not self.vpad:
            return
        for pad, _ in self.pads.values():
            self.release_pad(pad)
        app = APP_BY_ID.get(self.current)
        self.osk = keyboard_view.KeyboardView(self, app["name"] if app else "")
        self.osk_held = {}
        self.osk.show()
        self.update_mapping()
        print("keyboard open", flush=True)

    def close_keyboard(self):
        osk, self.osk = self.osk, None
        self.osk_held = {}
        if self.osk_rep_id:
            GLib.source_remove(self.osk_rep_id)
            self.osk_rep_id = None
        osk.close()
        self.update_mapping()

    def keyboard_done(self, text, enter):
        """Put the typed text into the app in front: clipboard + Ctrl+V (+ Enter)."""
        if not self.osk:
            return
        use_wl_copy = bool(text) and shutil.which("wl-copy") and shutil.which("wl-paste")
        if text and not use_wl_copy:
            self.osk.copy(text)   # fallback; needs our window, so before it closes
        self.close_keyboard()
        print(f"keyboard: {len(text)} chars{' + Enter' if enter else ''}", flush=True)
        if not (text or enter):
            return

        def work():
            old = None
            if use_wl_copy:
                # remember the user's clipboard, to put it back after pasting
                r = run_quiet(["wl-paste", "-n", "-t", "text"], timeout=3)
                old = r.stdout if r and r.returncode == 0 else None
                try:
                    subprocess.run(["wl-copy"], input=text.encode(), timeout=3,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"wl-copy failed: {e}", flush=True)
            GLib.idle_add(lambda: GLib.timeout_add(300, self.paste_into_app, bool(text), enter, old) and False)
        threading.Thread(target=work, daemon=True).start()

    def keyboard_cancel(self):
        if not self.osk:
            return
        self.close_keyboard()
        app = APP_BY_ID.get(self.current)
        if app and app.get("x11_class"):
            self.front_later(app, 250)

    def paste_into_app(self, paste, enter, old_clipboard=None):
        app = APP_BY_ID.get(self.current)
        if app and app.get("x11_class") and not self.session:
            win = x11win.main_window(app["x11_class"])
            if win:
                x11win.bring_to_front(win)
        GLib.timeout_add(150, self.paste_keys, paste, enter, old_clipboard)
        return False

    def paste_keys(self, paste, enter, old_clipboard=None):
        if self.window or self.osk or not self.vpad:
            return False
        if paste:
            self.tap([vpad.KEY_LEFTCTRL, vpad.KEY_V])
        if enter:
            GLib.timeout_add(150 if paste else 1, lambda: self.tap([vpad.KEY_ENTER]) or False)
        if old_clipboard is not None:
            def restore():
                threading.Thread(target=lambda: subprocess.run(
                    ["wl-copy"], input=old_clipboard.encode(), timeout=3,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), daemon=True).start()
                return False
            GLib.timeout_add(1500, restore)
        return False

    def tap(self, keys):
        for k in keys:
            self.vpad.press(k)
        for k in reversed(keys):
            self.vpad.release(k)

    def handle_osk(self, pad, etype, code, value):
        if etype == EV_KEY:
            if code == BTN_EAST:   # B deletes, and keeps deleting while held
                self.osk_hold("B", "backspace" if value else None)
            elif value == 1 and code == BTN_SELECT:
                self.keyboard_cancel()
            elif value == 1:
                act = {BTN_SOUTH: "press", BTN_X: "space", BTN_Y: "shift", BTN_TL: "layer-",
                       BTN_TR: "layer+", BTN_START: "send"}.get(code)
                if act:
                    self.osk.action(act)
        elif etype == EV_ABS:
            if code == ABS_HAT0X:
                self.osk_hold("hatx", "left" if value < 0 else "right" if value > 0 else None)
            elif code == ABS_HAT0Y:
                self.osk_hold("haty", "up" if value < 0 else "down" if value > 0 else None)
            elif code in (ABS_X, ABS_Y):
                v = pad.stick_value(code, value)
                neg, pos = ("left", "right") if code == ABS_X else ("up", "down")
                old = self.osk_held.get(("ls", code))
                if v <= -STICK_ON or (old == neg and v < -STICK_OFF):
                    self.osk_hold(("ls", code), neg)
                elif v >= STICK_ON or (old == pos and v > STICK_OFF):
                    self.osk_hold(("ls", code), pos)
                else:
                    self.osk_hold(("ls", code), None)

    def overlay(self):
        """The open overlay (keyboard or control center) that gets repeated actions."""
        return self.osk or self.cc

    # --- control center -----------------------------------------------------
    def player_pids(self):
        app = APP_BY_ID.get(self.current)
        return set(browser_pids(app)) if app and app.get("kind") == "web" else set()

    def open_control_center(self):
        if self.cc or self.osk:
            return
        if self.window:   # from the home screen: hide it first, then show over the app
            self.close()
            GLib.timeout_add(220, lambda: self.open_control_center() or False)
            return
        for pad, _ in self.pads.values():
            self.release_pad(pad)
        self.cc = control_center.ControlCenter(self, APP_BY_ID.get(self.current))
        self.osk_held = {}
        self.cc.show()
        self.update_mapping()
        print("control center open", flush=True)

    def close_control_center(self, then_key=None):
        if not self.cc:
            return
        cc, self.cc = self.cc, None
        self.osk_held = {}
        if self.osk_rep_id:
            GLib.source_remove(self.osk_rep_id)
            self.osk_rep_id = None
        cc.close()
        self.update_mapping()
        app = APP_BY_ID.get(self.current)
        if app and app.get("x11_class"):
            self.front_later(app, 250)
        if then_key == "space" and self.vpad and app and app.get("pad") == "keys":
            GLib.timeout_add(400, lambda: self.tap([vpad.KEY_SPACE]) or False)

    def handle_cc(self, pad, etype, code, value):
        if etype == EV_KEY:
            if code == BTN_SOUTH:   # A held on a volume button repeats
                focus = self.cc.focus
                if focus in ("vol-", "vol+"):
                    self.osk_hold("A", focus if value else None)
                elif value == 1:
                    self.cc.action("activate")
            elif value == 1 and code in (BTN_EAST, BTN_START, BTN_SELECT):
                self.close_control_center()
            elif value == 1:
                act = {BTN_X: "play", BTN_Y: "mute", BTN_TL: "prev", BTN_TR: "next"}.get(code)
                if act:
                    self.cc.action(act)
        elif etype == EV_ABS:
            if code == ABS_HAT0X:
                self.osk_hold("hatx", "left" if value < 0 else "right" if value > 0 else None)
            elif code == ABS_HAT0Y:
                self.osk_hold("haty", "up" if value < 0 else "down" if value > 0 else None)
            elif code in (ABS_X, ABS_Y):
                v = pad.stick_value(code, value)
                neg, pos = ("left", "right") if code == ABS_X else ("up", "down")
                old = self.osk_held.get(("ls", code))
                if v <= -STICK_ON or (old == neg and v < -STICK_OFF):
                    self.osk_hold(("ls", code), neg)
                elif v >= STICK_ON or (old == pos and v > STICK_OFF):
                    self.osk_hold(("ls", code), pos)
                else:
                    self.osk_hold(("ls", code), None)

    def osk_hold(self, source, act):
        """A direction (or B) pressed / released; held ones repeat like on a TV."""
        if self.osk_held.get(source) == act:
            return
        if act is None:
            self.osk_held.pop(source, None)
            if not self.osk_held and self.osk_rep_id:
                GLib.source_remove(self.osk_rep_id)
                self.osk_rep_id = None
            return
        self.osk_held[source] = act
        self.overlay().action(act)
        self.osk_rep_dir = act
        if self.osk_rep_id:
            GLib.source_remove(self.osk_rep_id)
        self.osk_rep_id = GLib.timeout_add(380, self.osk_repeat_start)

    def osk_repeat_start(self):
        self.osk_rep_id = GLib.timeout_add(75, self.osk_repeat)
        return False

    def osk_repeat(self):
        if not self.overlay() or self.osk_rep_dir not in self.osk_held.values():
            self.osk_rep_id = None
            return False
        self.overlay().action(self.osk_rep_dir)
        return True

    def toggle_fullscreen(self):
        app = APP_BY_ID.get(self.current) or {}
        win = x11win.main_window(app["x11_class"]) if app.get("x11_class") else None
        if win:
            x11win.set_fullscreen(win, x11win.NET_WM_STATE_TOGGLE)

    def close_app(self, app):
        aid = app["id"]
        print(f"closing {aid}", flush=True)
        if app.get("kind") == "web":
            threading.Thread(target=quit_browser, args=(app,), daemon=True).start()
        else:
            cmd = app["close"] if app.get("close") else ["flatpak", "kill", app["flatpak"]]
            threading.Thread(target=run_quiet, args=(cmd, 30), daemon=True).start()
        self.running.discard(aid)
        self.launched.pop(aid, None)
        if self.current == aid:
            self.current = None

    def close_all(self):
        for app in APPS:
            if app["id"] in self.running:
                self.close_app(app)
        self.set_menu("main", self.main_selected, -1)

    def running_web_apps(self):
        return [a for a in APPS if a.get("kind") == "web" and a["id"] in self.running]

    def logout(self):
        self.spawn(["sh", "-c", 'gnome-session-quit --logout --no-prompt || '
                                'loginctl terminate-session "$XDG_SESSION_ID"'],
                   quit_web=self.running_web_apps())

    def splash(self, text, app):
        self.busy = True
        self.armed = False
        if self.window:
            self.window.start_splash(text, app)
        GLib.timeout_add(LAUNCH_SPLASH_MS, self.end_splash)

    def end_splash(self):
        self.busy = False
        self.close()
        return False

    # --- home screen -----------------------------------------------------
    def home_pressed(self):
        if self.window:
            if self.can_leave() and not self.busy:
                self.close()
        elif self.mapping or not steam_running():
            self.open()
        else:
            # Short press is Steam's; open our menu only if Home is held.
            self.cancel_hold()
            self.hold_timer = GLib.timeout_add(HOLD_MS, self.on_hold)

    def on_hold(self):
        self.hold_timer = None
        if not self.window:
            self.open()
        return False

    def cancel_hold(self):
        if self.hold_timer:
            GLib.source_remove(self.hold_timer)
            self.hold_timer = None

    @property
    def entries(self):
        if self.menu == "main":
            return APPS
        return [e for e in POWER if self.session or not e.get("session_only")]

    def open(self):
        if self.window:
            return
        self.cancel_hold()
        self.running = self.running_ids()
        self.menu, self.focus, self.armed, self.busy = "main", "cards", False, False
        self.selected = min(self.main_selected, len(APPS) - 1)
        if self.current in APP_BY_ID:
            self.selected = APPS.index(APP_BY_ID[self.current])
        self.window = home_view.HomeView(self)
        self.window.show()
        self.update_mapping()   # takes the gamepad for the menu

    def close(self):
        if not self.window:
            return
        if self.menu == "main":
            self.main_selected = self.selected
        view, self.window = self.window, None
        self.busy = False
        view.close()            # fades out, then destroys itself
        self.update_mapping()
        app = APP_BY_ID.get(self.current)
        if app and app.get("x11_class") and not self.busy:
            self.front_later(app, 250)

    def refresh(self):
        if self.window:
            self.window.kick()

    def update_pad_status(self):
        self.refresh()

    def set_menu(self, name, selected=0, direction=0):
        self.menu = name
        self.selected = max(0, min(selected, len(self.entries) - 1))
        self.armed = False
        if self.window:
            self.window.menu_changed(direction)

    def hints(self):
        if self.busy:
            return []
        if self.menu != "main":
            return [("◀ ▶", "vybrat"), ("A", "potvrdit"), ("B", "zpět")]
        if self.focus == "power":
            return [("A", "vypnutí a spánek"), ("▼", "zpět na aplikace")]
        running = self.entries[self.selected]["id"] in self.running
        hints = [("◀ ▶", "vybrat"), ("A", "přepnout" if running else "spustit")]
        if running:
            hints.append(("Y", "zavřít"))
        hints.append(("X", "zvuk"))
        hints.append(("▲", "vypnutí"))
        if self.can_leave():
            hints.append(("B", "skrýt"))
        return hints

    def back(self):
        if self.focus == "power":
            self.focus = "cards"
            self.refresh()
        elif self.menu != "main":
            self.set_menu("main", self.main_selected, -1)
        elif self.can_leave():
            self.close()

    def move(self, d):
        if self.focus != "cards":
            return
        self.selected = (self.selected + d) % len(self.entries)
        self.armed = False
        self.refresh()

    def vertical(self, d):
        if self.menu != "main":
            return
        focus = "power" if d < 0 else "cards"
        if focus != self.focus:
            self.focus = focus
            self.refresh()

    def open_power(self):
        if self.busy or self.menu != "main":
            return
        self.main_selected = self.selected
        self.focus = "cards"
        self.set_menu("power", 0, 1)

    def select_and_activate(self, i):
        self.focus = "cards"
        self.selected = i
        self.activate()

    def activate(self):
        if self.busy:
            return
        if self.menu == "main" and self.focus == "power":
            self.open_power()
            return
        entry = self.entries[self.selected]
        if entry.get("confirm") and not self.armed:
            self.armed = True
            self.refresh()
        elif entry.get("action"):
            getattr(self, entry["action"])()
        elif self.menu == "main":
            self.launch_app(entry)
        else:
            print(f"running {entry['id']}", flush=True)
            # save YouTube/Netflix logins before the PC goes off
            quit_web = self.running_web_apps() if entry["id"] == "poweroff" else ()
            self.close()
            self.spawn(entry["cmd"], quit_web=quit_web)

    def close_selected(self):
        entry = self.entries[self.selected]
        if self.menu == "main" and self.focus == "cards" and entry["id"] in self.running:
            self.close_app(entry)
            self.refresh()

    def handle_menu(self, pad, etype, code, value):
        if self.busy:
            return
        if etype == EV_KEY and value == 1:
            if code in (BTN_SOUTH, BTN_START):
                self.activate()
            elif code == BTN_EAST:
                self.back()
            elif code == BTN_Y:
                self.close_selected()
            elif code == BTN_X:
                self.open_control_center()
            elif code == BTN_SELECT:
                self.open_power()
            elif code == BTN_TL:
                self.move(-1)
            elif code == BTN_TR:
                self.move(1)
        elif etype == EV_ABS:
            if code == ABS_HAT0X and value:
                self.move(1 if value > 0 else -1)
            elif code == ABS_HAT0Y and value:
                self.vertical(1 if value > 0 else -1)
            elif code in (ABS_X, ABS_Y):
                v = pad.stick_value(code, value)
                old = pad.menu_dir.get(code, 0)
                d = -1 if v <= -STICK_ON else 1 if v >= STICK_ON else old if abs(v) > STICK_OFF else 0
                if d and d != old:
                    (self.move if code == ABS_X else self.vertical)(d)
                pad.menu_dir[code] = d

    def on_key(self, keyval):
        if self.busy:
            return True
        key = Gdk.keyval_name(keyval)
        if key in ("Left", "a"):
            self.move(-1)
        elif key in ("Right", "d"):
            self.move(1)
        elif key in ("Up", "w"):
            self.vertical(-1)
        elif key in ("Down", "s"):
            self.vertical(1)
        elif key in ("Return", "KP_Enter", "space"):
            self.activate()
        elif key in ("Escape", "BackSpace"):
            self.back()
        elif key in ("y", "Delete"):
            self.close_selected()
        return True

    def shutdown(self):
        for pad, _ in self.pads.values():
            self.release_pad(pad)
            pad.grab(False)
        if self.vpad:
            self.vpad.close()


def main():
    Gtk.init()
    session = "--session" in sys.argv
    tv = SmartTV(session=session)
    if session or "--open" in sys.argv:
        GLib.idle_add(lambda: tv.open() and False)
    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: loop.quit() or False)
    loop.run()
    tv.shutdown()


if __name__ == "__main__":
    main()

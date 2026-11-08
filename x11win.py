"""Bring an X11 (XWayland) window to front and make it fullscreen, with plain ctypes.

On Wayland, GNOME won't let one program focus or resize another program's
window. For X11 windows it still honors the standard requests a taskbar would
send (_NET_ACTIVE_WINDOW, _NET_WM_STATE), so apps we need to control (Discord)
are started on XWayland and handled here.
"""
import ctypes
import ctypes.util

c_ulong, c_long, c_int, c_uint = ctypes.c_ulong, ctypes.c_long, ctypes.c_int, ctypes.c_uint
c_void_p, c_char_p = ctypes.c_void_p, ctypes.c_char_p

ClientMessage = 33
SubstructureRedirectMask, SubstructureNotifyMask = 1 << 20, 1 << 19
NET_WM_STATE_REMOVE, NET_WM_STATE_ADD, NET_WM_STATE_TOGGLE = 0, 1, 2
SOURCE_PAGER = 2  # "request from a taskbar": GNOME always honors it


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("window", c_ulong), ("message_type", c_ulong), ("format", c_int), ("data", c_long * 5)]


class XEvent(ctypes.Union):
    _fields_ = [("xclient", XClientMessageEvent), ("pad", c_long * 24)]


class XClassHint(ctypes.Structure):
    _fields_ = [("res_name", c_void_p), ("res_class", c_void_p)]  # raw pointers, freed with XFree


ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
_x = None
_ignore_errors = ERROR_HANDLER(lambda _d, _e: 0)  # a vanished window must not kill us


def _lib():
    global _x
    if _x is None:
        x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
        x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = c_void_p, [c_char_p]
        x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = c_ulong, [c_void_p]
        x.XInternAtom.restype, x.XInternAtom.argtypes = c_ulong, [c_void_p, c_char_p, c_int]
        x.XGetWindowProperty.argtypes = [c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
                                         ctypes.POINTER(c_ulong), ctypes.POINTER(c_int),
                                         ctypes.POINTER(c_ulong), ctypes.POINTER(c_ulong),
                                         ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        x.XGetClassHint.argtypes = [c_void_p, c_ulong, ctypes.POINTER(XClassHint)]
        x.XGetGeometry.argtypes = [c_void_p, c_ulong, ctypes.POINTER(c_ulong), ctypes.POINTER(c_int),
                                   ctypes.POINTER(c_int), ctypes.POINTER(c_uint), ctypes.POINTER(c_uint),
                                   ctypes.POINTER(c_uint), ctypes.POINTER(c_uint)]
        x.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, ctypes.POINTER(XEvent)]
        x.XFree.argtypes = [c_void_p]
        x.XFlush.argtypes = [c_void_p]
        x.XSync.argtypes = [c_void_p, c_int]
        x.XCloseDisplay.argtypes = [c_void_p]
        x.XSetErrorHandler.argtypes = [ERROR_HANDLER]
        x.XSetErrorHandler(_ignore_errors)
        _x = x
    return _x


class _Display:
    def __enter__(self):
        self.x = _lib()
        self.d = self.x.XOpenDisplay(None)
        if not self.d:
            raise OSError("no X display")
        self.root = self.x.XDefaultRootWindow(self.d)
        return self

    def __exit__(self, *_):
        self.x.XCloseDisplay(self.d)

    def atom(self, name):
        return self.x.XInternAtom(self.d, name.encode(), 0)

    def client_list(self):
        atype, fmt, n, after = c_ulong(), c_int(), c_ulong(), c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        ok = self.x.XGetWindowProperty(self.d, self.root, self.atom("_NET_CLIENT_LIST"), 0, 4096, 0, 33,
                                       ctypes.byref(atype), ctypes.byref(fmt), ctypes.byref(n),
                                       ctypes.byref(after), ctypes.byref(data))
        if ok != 0 or not data:
            return []
        wins = list(ctypes.cast(data, ctypes.POINTER(c_ulong))[:n.value])
        self.x.XFree(data)
        return wins

    def wm_class(self, win):
        hint = XClassHint()
        if not self.x.XGetClassHint(self.d, win, ctypes.byref(hint)):
            return ""
        cls = ctypes.string_at(hint.res_class).decode(errors="replace") if hint.res_class else ""
        for p in (hint.res_name, hint.res_class):
            if p:
                self.x.XFree(p)
        return cls

    def area(self, win):
        root, x, y, w, h, b, depth = c_ulong(), c_int(), c_int(), c_uint(), c_uint(), c_uint(), c_uint()
        if not self.x.XGetGeometry(self.d, win, ctypes.byref(root), ctypes.byref(x), ctypes.byref(y),
                                   ctypes.byref(w), ctypes.byref(h), ctypes.byref(b), ctypes.byref(depth)):
            return 0
        return w.value * h.value

    def send(self, win, message, data):
        ev = XEvent()
        ev.xclient.type = ClientMessage
        ev.xclient.send_event = 1
        ev.xclient.window = win
        ev.xclient.message_type = self.atom(message)
        ev.xclient.format = 32
        for i, v in enumerate(data):
            ev.xclient.data[i] = v
        self.x.XSendEvent(self.d, self.root, 0, SubstructureRedirectMask | SubstructureNotifyMask,
                          ctypes.byref(ev))


def main_window(wm_class):
    """The biggest window with this WM_CLASS (case-insensitive), or None."""
    try:
        with _Display() as dpy:
            wins = [(dpy.area(w), w) for w in dpy.client_list() if dpy.wm_class(w).lower() == wm_class.lower()]
    except OSError:
        return None
    return max(wins)[1] if wins else None


def bring_to_front(win, fullscreen=True):
    """Focus the window and (optionally) make it fullscreen. Safe to repeat."""
    try:
        with _Display() as dpy:
            if fullscreen:
                dpy.send(win, "_NET_WM_STATE", [NET_WM_STATE_ADD, dpy.atom("_NET_WM_STATE_FULLSCREEN"), 0,
                                                SOURCE_PAGER, 0])
            dpy.send(win, "_NET_ACTIVE_WINDOW", [SOURCE_PAGER, 0, 0, 0, 0])
            dpy.x.XSync(dpy.d, 0)
        return True
    except OSError:
        return False


def set_fullscreen(win, mode=NET_WM_STATE_ADD):
    try:
        with _Display() as dpy:
            dpy.send(win, "_NET_WM_STATE", [mode, dpy.atom("_NET_WM_STATE_FULLSCREEN"), 0, SOURCE_PAGER, 0])
            dpy.x.XSync(dpy.d, 0)
        return True
    except OSError:
        return False


def is_fullscreen(win):
    try:
        with _Display() as dpy:
            atype, fmt, n, after = c_ulong(), c_int(), c_ulong(), c_ulong()
            data = ctypes.POINTER(ctypes.c_ubyte)()
            ok = dpy.x.XGetWindowProperty(dpy.d, win, dpy.atom("_NET_WM_STATE"), 0, 64, 0, 4,
                                          ctypes.byref(atype), ctypes.byref(fmt), ctypes.byref(n),
                                          ctypes.byref(after), ctypes.byref(data))
            if ok != 0 or not data:
                return False
            states = list(ctypes.cast(data, ctypes.POINTER(c_ulong))[:n.value])
            dpy.x.XFree(data)
            return dpy.atom("_NET_WM_STATE_FULLSCREEN") in states
    except OSError:
        return False

"""Virtual keyboard + mouse through /dev/uinput, in plain Python (no python-evdev).

Smart TV uses it to turn gamepad input into arrow keys, Enter, Esc and mouse
movement for apps that don't understand a gamepad (YouTube, Netflix, Discord).
"""
import fcntl
import os
import struct
import time

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT = 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08

KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_F, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V, KEY_SPACE = 33, 29, 42, 47, 57
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112

KEYS = [KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_F, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V, KEY_SPACE,
        KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN, BTN_LEFT, BTN_RIGHT, BTN_MIDDLE]

BUS_VIRTUAL = 0x06
EVENT = struct.Struct("llHHi")  # timeval, type, code, value


def _iow(nr, size):
    return (1 << 30) | (size << 16) | (ord("U") << 8) | nr


def _io(nr):
    return (ord("U") << 8) | nr


UI_SET_EVBIT = _iow(100, 4)
UI_SET_KEYBIT = _iow(101, 4)
UI_SET_RELBIT = _iow(102, 4)
UI_DEV_SETUP = _iow(3, 92)  # struct uinput_setup: input_id (4 x u16), name[80], u32
UI_DEV_CREATE = _io(1)
UI_DEV_DESTROY = _io(2)


class VirtualPad:
    def __init__(self, name="Smart TV virtual keyboard and mouse"):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            for ev in (EV_KEY, EV_REL):
                fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
            for key in KEYS:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
            for rel in (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL):
                fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
            setup = struct.pack("4H80sI", BUS_VIRTUAL, 0x1209, 0x5754, 1, name.encode()[:79], 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        # Several gamepad inputs can hold the same key (D-pad and stick both
        # pressing Left), so count holders and release only when the last one lets go.
        self.held = {}

    def _emit(self, events):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        data = b"".join(EVENT.pack(sec, usec, t, c, v) for t, c, v in events)
        data += EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0)
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def press(self, code):
        n = self.held.get(code, 0)
        self.held[code] = n + 1
        if n == 0:
            self._emit([(EV_KEY, code, 1)])

    def release(self, code):
        n = self.held.get(code, 0)
        if n <= 0:
            return
        if n == 1:
            del self.held[code]
            self._emit([(EV_KEY, code, 0)])
        else:
            self.held[code] = n - 1

    def move(self, dx, dy):
        events = []
        if dx:
            events.append((EV_REL, REL_X, dx))
        if dy:
            events.append((EV_REL, REL_Y, dy))
        if events:
            self._emit(events)

    def scroll(self, vertical, horizontal=0):
        events = []
        if vertical:
            events.append((EV_REL, REL_WHEEL, vertical))
        if horizontal:
            events.append((EV_REL, REL_HWHEEL, horizontal))
        if events:
            self._emit(events)

    def release_all(self):
        for code in list(self.held):
            self.held[code] = 1
            self.release(code)

    def close(self):
        self.release_all()
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)

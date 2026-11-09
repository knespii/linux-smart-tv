#!/usr/bin/env python3
"""Adds (or removes) Smart TV's keyboard shortcuts in GNOME Settings > Keyboard > Custom Shortcuts.

  shortcuts.py install     add them, keeping your own shortcuts and any key you already use
  shortcuts.py uninstall   remove them

They send the running smart-tv service a signal (see smart_tv.py). You can change the
keys later in GNOME Settings, they show up there like any custom shortcut.
"""
import ast
import subprocess
import sys

LIST_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM_SCHEMA = LIST_SCHEMA + ".custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
KILL = "systemctl --user kill --kill-whom=main --signal={} smart-tv.service"
SHORTCUTS = [
    ("smart-tv-home", "Smart TV: hlavní obrazovka", "<Super><Alt>t", KILL.format("SIGUSR1")),
    ("smart-tv-ha", "Smart TV: Domácnost (Home Assistant)", "<Super><Alt>h", KILL.format("SIGUSR2")),
]
# schemas whose keys could clash with ours
KEY_SCHEMAS = ["org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings",
               "org.gnome.mutter.wayland.keybindings", LIST_SCHEMA]


def gs(*args):
    r = subprocess.run(["gsettings", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def parse(value):
    if not value:
        return []
    value = value.removeprefix("@as ")
    try:
        out = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return []
    return list(out) if isinstance(out, (list, tuple)) else [out]


def norm(key):
    key = key.lower().replace("<primary>", "<control>").replace("<ctrl>", "<control>")
    mods = sorted(part + ">" for part in key.split(">")[:-1])
    return "".join(mods) + key.split(">")[-1]


def used_keys(skip_paths):
    used = set()
    for schema in KEY_SCHEMAS:
        out = gs("list-recursively", schema) or ""
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[2].startswith(("[", "@as")):
                used |= {norm(k) for k in parse(parts[2]) if isinstance(k, str) and k}
    for path in parse(gs("get", LIST_SCHEMA, "custom-keybindings")):
        if path not in skip_paths:
            b = gs("get", f"{ITEM_SCHEMA}:{path}", "binding")
            if b:
                used.add(norm(parse(b)[0] if b.startswith("[") else b.strip("'")))
    return used


def install():
    paths = parse(gs("get", LIST_SCHEMA, "custom-keybindings"))
    ours = [BASE + sid + "/" for sid, *_ in SHORTCUTS]
    used = used_keys(ours)
    for (sid, name, binding, command), path in zip(SHORTCUTS, ours):
        item = f"{ITEM_SCHEMA}:{path}"
        if path in paths:   # already there: keep the key the user may have changed, update the command
            gs("set", item, "command", command)
            continue
        if norm(binding) in used:
            print(f"Zkratka {binding} je už obsazená, „{name}“ si nastav v Nastavení → Klávesnice.")
            binding = ""
        gs("set", item, "name", name)
        gs("set", item, "command", command)
        gs("set", item, "binding", binding)
        paths.append(path)
    gs("set", LIST_SCHEMA, "custom-keybindings", str(paths))


def uninstall():
    ours = [BASE + sid + "/" for sid, *_ in SHORTCUTS]
    paths = [p for p in parse(gs("get", LIST_SCHEMA, "custom-keybindings")) if p not in ours]
    gs("set", LIST_SCHEMA, "custom-keybindings", str(paths) if paths else "@as []")
    for path in ours:
        gs("reset-recursively", f"{ITEM_SCHEMA}:{path}")


if __name__ == "__main__":
    {"install": install, "uninstall": uninstall}[sys.argv[1] if len(sys.argv) > 1 else "install"]()

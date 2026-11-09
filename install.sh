#!/usr/bin/env bash
# Installs Smart TV for the current user (no root needed).
# The "Smart TV" login session is installed separately by install-session.sh (needs sudo).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/smart-tv"
UNIT_DIR="$HOME/.config/systemd/user"
KIOSK="$HOME/.local/bin/gnome-kiosk-script"

if ! id -nG | tr ' ' '\n' | grep -qx input; then
    echo "Uživatel není ve skupině 'input', Smart TV by neviděl ovladač."
    echo "Spusť:  sudo usermod -aG input \$USER   a pak se odhlas a znovu přihlas."
    exit 1
fi

if ! python3 -c "import gi; gi.require_version('Gtk', '4.0')" 2>/dev/null; then
    echo "Chybí Python GTK 4. Na Debianu/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-4.0"
    exit 1
fi

if ! command -v google-chrome >/dev/null; then
    echo "Upozornění: není nainstalovaný Google Chrome (deb balíček), YouTube a Netflix nepůjdou spustit."
fi

if ! command -v wl-copy >/dev/null; then
    echo "Upozornění: chybí wl-clipboard, klávesnice na obrazovce nebude umět vložit text. Doinstaluj:  sudo apt install wl-clipboard"
fi

if [ ! -w /dev/uinput ]; then
    echo "Upozornění: nejde zapisovat do /dev/uinput, ovladač v YouTube/Netflixu/Discordu nebude fungovat."
fi

mkdir -p "$DEST/profiles" "$UNIT_DIR" "$(dirname "$KIOSK")"
install -m 755 "$SRC/smart_tv.py" "$DEST/smart_tv.py"
install -m 644 "$SRC/vpad.py" "$SRC/home_view.py" "$SRC/keyboard_view.py" "$SRC/control_center.py" \
    "$SRC/x11win.py" "$SRC/ha_client.py" "$SRC/ha_view.py" "$DEST/"
rm -f "$DEST/style.css"
install -m 644 "$SRC/smart-tv.service" "$UNIT_DIR/smart-tv.service"

# Home Assistant settings (address + token); never overwritten, only readable by you.
HA_CONF="$HOME/.config/smart-tv/homeassistant.conf"
if [ ! -e "$HA_CONF" ]; then
    mkdir -p "$(dirname "$HA_CONF")"
    install -m 600 "$SRC/config/homeassistant.conf" "$HA_CONF"
fi

# Script for the "Smart TV" login session. Keep a backup of someone else's script.
if [ -e "$KIOSK" ] && ! grep -q "smart-tv" "$KIOSK"; then
    mv "$KIOSK" "$KIOSK.bak"
    echo "Původní $KIOSK je zálohovaný jako $KIOSK.bak"
fi
install -m 755 "$SRC/session/gnome-kiosk-script" "$KIOSK"

# Smart TV replaces Gamepad Launcher (both would react to the Home button).
if systemctl --user is-enabled --quiet gamepad-launcher.service 2>/dev/null \
   || systemctl --user is-active --quiet gamepad-launcher.service 2>/dev/null; then
    systemctl --user disable --now gamepad-launcher.service
    echo "Gamepad Launcher vypnutý (soubory zůstaly, uninstall.sh ho zase zapne)."
fi

# Keyboard shortcuts: Super+Alt+T home screen, Super+Alt+H Home Assistant (GNOME custom shortcuts).
if command -v gsettings >/dev/null; then
    python3 "$SRC/shortcuts.py" install || echo "Upozornění: klávesové zkratky se nepodařilo nastavit."
fi

systemctl --user daemon-reload
systemctl --user enable smart-tv.service
systemctl --user restart smart-tv.service

echo "Hotovo. Zmáčkni Home na ovladači, nebo Super+Alt+T na klávesnici (Super+Alt+H = Domácnost)."
if ! grep -qE '^\s*token\s*=\s*\S' "$HA_CONF"; then
    echo "Home Assistant: doplň adresu a token do $HA_CONF (návod je přímo v souboru)."
fi
if [ ! -e /usr/share/wayland-sessions/smart-tv.desktop ]; then
    echo "Samostatnou relaci \"Smart TV\" na přihlašovací obrazovce přidáš:  ./install-session.sh"
fi

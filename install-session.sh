#!/usr/bin/env bash
# Adds the "Smart TV" session to the login screen (needs sudo).
# It runs gnome-kiosk with the Smart TV home screen instead of the GNOME desktop.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"

if [ ! -e "$HOME/.local/bin/gnome-kiosk-script" ]; then
    echo "Nejdřív spusť ./install.sh"
    exit 1
fi

sudo apt install -y gnome-kiosk gnome-kiosk-script-session
sudo install -m 644 "$SRC/session/smart-tv.desktop" /usr/share/wayland-sessions/smart-tv.desktop

echo "Hotovo. Odhlas se, na přihlašovací obrazovce klikni na své jméno,"
echo "vpravo dole na ozubené kolečko vyber \"Smart TV\" a přihlas se."

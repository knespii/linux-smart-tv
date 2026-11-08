#!/usr/bin/env bash
# Removes Smart TV for the current user and turns Gamepad Launcher back on.
# Chrome profiles (YouTube/Netflix logins) stay in ~/.local/share/smart-tv/profiles.
set -euo pipefail

DEST="$HOME/.local/share/smart-tv"
KIOSK="$HOME/.local/bin/gnome-kiosk-script"

systemctl --user disable --now smart-tv.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/smart-tv.service"
rm -f "$DEST/smart_tv.py" "$DEST/vpad.py" "$DEST/home_view.py" "$DEST/x11win.py" "$DEST/keyboard_view.py" "$DEST/control_center.py" "$DEST/style.css" \
      "$DEST/ha_client.py" "$DEST/ha_view.py" "$DEST/homeassistant.png"
rm -rf "$DEST/__pycache__"

if [ -e "$KIOSK" ] && grep -q "smart-tv" "$KIOSK"; then
    rm -f "$KIOSK"
    [ -e "$KIOSK.bak" ] && mv "$KIOSK.bak" "$KIOSK"
fi

systemctl --user daemon-reload
if [ -e "$HOME/.config/systemd/user/gamepad-launcher.service" ]; then
    systemctl --user enable --now gamepad-launcher.service
    echo "Gamepad Launcher je zase zapnutý."
fi

echo "Smart TV odinstalován."
echo "Přihlášení do YouTube/Netflixu zůstalo v $DEST/profiles (smažeš:  rm -rf $DEST)."
echo "Nastavení Home Assistantu (token) zůstalo v ~/.config/smart-tv (smažeš:  rm -rf ~/.config/smart-tv)."
if [ -e /usr/share/wayland-sessions/smart-tv.desktop ]; then
    echo "Relaci z přihlašovací obrazovky odebereš:  sudo rm /usr/share/wayland-sessions/smart-tv.desktop"
fi

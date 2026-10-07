#!/bin/bash
# MixPre Remote installer for a Pi that's already running Raspberry Pi OS Lite.
# (Easier option: flash the ready-made image instead - see README.)
# Run ONCE while the Pi has internet:   sudo ./install.sh
# Optional hotspot password:            sudo MIXPRE_WIFI_PASSWORD='yourpass' ./install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Please run with sudo: sudo ./install.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

bash "$SRC/image/setup-rootfs.sh"
systemctl daemon-reload || true
command -v raspi-config >/dev/null && [ -z "$(raspi-config nonint get_wifi_country 2>/dev/null || true)" ] \
  && raspi-config nonint do_wifi_country "${MIXPRE_COUNTRY:-US}" || true

SUFFIX=$(tr -d '\0' < /sys/firmware/devicetree/base/serial-number | tail -c 4 | tr 'a-f' 'A-F')
cat <<EOF

============================================================
 MixPre Remote installed.

 Bluetooth / Wi-Fi name : MixPre-Remote-$SUFFIX
 Wi-Fi password         : ${MIXPRE_WIFI_PASSWORD:-mixpreremote}
 Hotspot page           : http://mixpre.local  (backup: http://192.168.4.1)
 Remote code / Wi-Fi    : open the page > Settings

 After reboot the Pi rejoins your home Wi-Fi if it's in range,
 otherwise it hosts its own hotspot. SSH later (same network or hotspot):
     ssh $(logname 2>/dev/null || echo pi)@mixpre.local

 Now run:  sudo reboot
============================================================
EOF

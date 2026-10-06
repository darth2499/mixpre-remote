#!/bin/bash
# Turns a Raspberry Pi OS system into a MixPre Remote.
# Used by install.sh (on a running Pi) and build-image.sh (inside the image, IN_CHROOT=1).
# Optional env: MIXPRE_WIFI_PASSWORD, MIXPRE_COUNTRY (default US)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/mixpre-remote
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
COUNTRY="${MIXPRE_COUNTRY:-US}"
IN_CHROOT="${IN_CHROOT:-0}"
export DEBIAN_FRONTEND=noninteractive LC_ALL=C

echo "==> Installing packages"
apt-get update
apt-get install -y --no-install-recommends \
  python3-aiohttp python3-venv python3-pip bluez avahi-daemon network-manager rfkill iw dnsmasq-base
# WebRTC for direct connections (relay-only still works without it)
apt-get install -y --no-install-recommends python3-aiortc || echo "  (python3-aiortc not in apt - will try pip)"

echo "==> Installing MixPre Remote to $DEST"
mkdir -p "$DEST/web"
install -m 755 "$SRC/server/mixpre_remote.py" "$SRC/server/mixpre_net.py" "$SRC/server/mixpre_cloud.py" "$SRC/server/gadget.sh" "$DEST/"
install -m 644 "$SRC/web/index.html" "$DEST/web/"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv --system-site-packages "$DEST/venv"
"$DEST/venv/bin/pip" install --no-cache-dir --upgrade "bless>=0.2.6"
"$DEST/venv/bin/python" -c "import aiortc" 2>/dev/null || "$DEST/venv/bin/pip" install --no-cache-dir aiortc || echo "  (aiortc unavailable: remote access will use the relay only)"

[ -f "$BOOT/mixpre-remote.json" ] || install -m 644 "$SRC/mixpre-remote.json" "$BOOT/mixpre-remote.json"
if [ -n "${MIXPRE_WIFI_PASSWORD:-}" ]; then
  [ ${#MIXPRE_WIFI_PASSWORD} -ge 8 ] || { echo "Wi-Fi password must be 8+ characters"; exit 1; }
  python3 - "$BOOT/mixpre-remote.json" "$MIXPRE_WIFI_PASSWORD" <<'PY'
import json, sys
p, pw = sys.argv[1], sys.argv[2]
c = json.load(open(p)); c["wifi_password"] = pw
open(p, "w").write(json.dumps(c, indent=2))
PY
fi

echo "==> USB device (gadget) mode"
grep -q '^dtoverlay=dwc2' "$BOOT/config.txt" || printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$BOOT/config.txt"
printf 'dwc2\nlibcomposite\n' > /etc/modules-load.d/mixpre-remote.conf

echo "==> Hostname: mixpre  (http://mixpre.local)"
echo mixpre > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tmixpre/' /etc/hosts
else
  printf '127.0.1.1\tmixpre\n' >> /etc/hosts
fi
[ "$IN_CHROOT" = 1 ] || hostname mixpre || true

echo "==> Wi-Fi country ($COUNTRY), internet check"
if [ -f "$BOOT/cmdline.txt" ] && ! grep -q 'ieee80211_regdom' "$BOOT/cmdline.txt"; then
  sed -i "1 s/\$/ cfg80211.ieee80211_regdom=$COUNTRY/" "$BOOT/cmdline.txt"
fi
rm -f /etc/NetworkManager/dnsmasq-shared.d/mixpre-remote.conf   # (older versions)
# lets the Pi tell "internet ok" from "this network needs a sign-in page"
mkdir -p /etc/NetworkManager/conf.d
printf '[connectivity]\nuri=http://nmcheck.gnome.org/check_network_status.txt\ninterval=120\n' \
  > /etc/NetworkManager/conf.d/mixpre-connectivity.conf

echo "==> Services"
install -m 644 "$SRC"/systemd/*.service /etc/systemd/system/
[ "$IN_CHROOT" = 1 ] || systemctl daemon-reload
systemctl disable mixpre-wifi.service 2>/dev/null || true; rm -f /etc/systemd/system/mixpre-wifi.service
for s in mixpre-gadget mixpre-remote mixpre-net bluetooth avahi-daemon NetworkManager; do
  systemctl enable "$s.service" || echo "  (could not enable $s)"
done

apt-get clean
echo "==> MixPre Remote setup complete"

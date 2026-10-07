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
# The app lives in releases/<build>/ with "current" pointing at it, so it can update itself.
mkdir -p "$DEST/releases"
BTMP="$(mktemp -d)"
bash "$SRC/image/make-bundle.sh" "$BTMP"
BUILD_ID=$(python3 -c "import json,sys;m=json.load(open(sys.argv[1]));print(f\"{m['ct']}-{m['commit']}\")" "$BTMP/manifest.json")
REL="$DEST/releases/$BUILD_ID"
rm -rf "$REL"; mkdir -p "$REL"
tar -xzf "$BTMP/mixpre-app.tar.gz" -C "$REL"
rm -rf "$BTMP"
# older installs kept the app files directly in $DEST
rm -f "$DEST"/mixpre_remote.py "$DEST"/mixpre_net.py "$DEST"/mixpre_cloud.py "$DEST"/gadget.sh; rm -rf "$DEST/web"
ln -sfn "$REL" "$DEST/current"
install -m 755 "$SRC/server/mixpre_guard.py" "$DEST/guard.py"
install -m 755 "$SRC/image/files/mixpre-status.sh" "$DEST/status.sh"
install -m 644 "$SRC/image/SYSTEM_LEVEL" "$DEST/SYSTEM_LEVEL"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv --system-site-packages "$DEST/venv"
DPKG_ARCH="$(dpkg --print-architecture)"
if [ "$DPKG_ARCH" = armhf ]; then
  # original Pi Zero W (ARMv6): use pure-Python dbus-fast instead of compiling it
  SKIP_CYTHON=1 "$DEST/venv/bin/pip" install --no-cache-dir --upgrade "bless>=0.2.6"
else
  "$DEST/venv/bin/pip" install --no-cache-dir --upgrade "bless>=0.2.6"
fi
if ! "$DEST/venv/bin/python" -c "import aiortc" 2>/dev/null; then
  if [ "$DPKG_ARCH" = armhf ]; then
    echo "  (aiortc not in apt for ARMv6 - remote access will use the cloud relay, no direct links)"
  else
    "$DEST/venv/bin/pip" install --no-cache-dir aiortc || echo "  (aiortc unavailable: remote access will use the relay only)"
  fi
fi

[ -f "$BOOT/mixpre-remote.json" ] || install -m 644 "$SRC/mixpre-remote.json" "$BOOT/mixpre-remote.json"
if [ -n "${MIXPRE_WIFI_PASSWORD:-}" ]; then
  [ ${#MIXPRE_WIFI_PASSWORD} -ge 8 ] || { echo "Wi-Fi password must be 8+ characters"; exit 1; }
fi
python3 - "$BOOT/mixpre-remote.json" "${MIXPRE_WIFI_PASSWORD:-}" "${MIXPRE_UPDATE_REPO:-}" <<'PY'
import json, sys
p, pw, repo = sys.argv[1], sys.argv[2], sys.argv[3]
c = json.load(open(p))
if pw: c["wifi_password"] = pw
if repo and not c.get("update_repo"): c["update_repo"] = repo   # where updates come from
open(p, "w").write(json.dumps(c, indent=2))
PY

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
install -m 644 "$SRC"/systemd/*.service "$SRC"/systemd/*.timer /etc/systemd/system/
[ "$IN_CHROOT" = 1 ] || systemctl daemon-reload
systemctl disable mixpre-wifi.service 2>/dev/null || true; rm -f /etc/systemd/system/mixpre-wifi.service
for s in mixpre-gadget mixpre-remote mixpre-net bluetooth avahi-daemon NetworkManager; do
  systemctl enable "$s.service" || echo "  (could not enable $s)"
done
systemctl enable mixpre-status.timer || echo "  (could not enable status report)"

apt-get clean
echo "==> MixPre Remote setup complete"

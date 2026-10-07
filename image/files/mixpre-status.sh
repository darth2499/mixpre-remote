#!/bin/bash
# MixPre Remote - writes a plain-text health report to the SD card's boot drive
# (mixpre-status.txt) so you can read it on a Mac/PC when the Pi isn't reachable.
# Runs ~45 s after boot and then every minute (mixpre-status.timer).
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
OUT="$BOOT/mixpre-status.txt"
TMP="$BOOT/.mixpre-status.tmp"
sec() { printf '\n===== %s =====\n' "$1"; }
run() { timeout 10 "$@" 2>&1 | head -n 60; }
{
  echo "MixPre Remote status - $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Uptime: $(uptime -p 2>/dev/null)   Model: $(tr -d '\0' 2>/dev/null < /proc/device-tree/model)"
  echo "App: $(cat /opt/mixpre-remote/current/BUILD.json 2>/dev/null || echo 'not found')"
  echo "OS: $(. /etc/os-release; echo "$PRETTY_NAME") $(dpkg --print-architecture)"

  sec "SUMMARY"
  for s in mixpre-gadget mixpre-net mixpre-remote mixpre-dhcp NetworkManager bluetooth avahi-daemon; do
    printf '%-16s %s\n' "$s" "$(systemctl is-active "$s.service" 2>/dev/null)"
  done
  echo "USB gadget mode: $(cat /run/mixpre-gadget-mode 2>/dev/null || echo none)   USB to host: $(cat /sys/class/udc/*/state 2>/dev/null || echo no UDC)"
  echo "IP addresses: $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $2" "$4}' | tr '\n' ' ')"
  echo "Failed services: $(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"

  sec "WI-FI"
  run rfkill list
  run nmcli -t dev status
  run nmcli -t -f NAME,TYPE,DEVICE,ACTIVE con show
  run iw dev
  echo "Regulatory: $(iw reg get 2>/dev/null | grep -m1 country)"

  sec "LOG: mixpre-net (Wi-Fi / hotspot)"
  run journalctl -b -u mixpre-net -u mixpre-dhcp --no-pager -n 40 -o short-monotonic
  sec "LOG: mixpre-remote (app)"
  run journalctl -b -u mixpre-remote --no-pager -n 50 -o short-monotonic
  sec "LOG: NetworkManager"
  run journalctl -b -u NetworkManager --no-pager -n 30 -o short-monotonic
  sec "KERNEL: Wi-Fi / USB messages"
  dmesg 2>/dev/null | grep -iE 'brcm|wlan|dwc2|gadget|usb|under-voltage' | tail -n 30
} > "$TMP" 2>&1
mv -f "$TMP" "$OUT"
sync

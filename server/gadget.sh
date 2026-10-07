#!/bin/bash
# MixPre Remote - USB gadget setup for Raspberry Pi Zero 2 W
# Makes the Pi look like a USB keyboard and/or a Korg nanoKONTROL2 to the MixPre.
#
# Usage: gadget.sh keyboard|midi|both|auto|off
#   auto = read "mode" from mixpre-remote.json on the boot partition

set -u
G=/sys/kernel/config/usb_gadget/mixpre
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
MODE="${1:-auto}"

# Early sign of life on the SD card (mixpre-status.sh replaces it ~45 s later)
if [ -d "$BOOT" ] && [ -w "$BOOT" ]; then
  echo "MixPre Remote: booting since $(date '+%H:%M:%S'). If this text never changes, start-up stalled after the USB step." \
    > "$BOOT/mixpre-status.txt" 2>/dev/null; sync
fi
DEBUG_CONSOLE=$(python3 -c "import json;print(1 if json.load(open('$BOOT/mixpre-remote.json')).get('debug_usb_console') else 0)" 2>/dev/null || echo 0)

if [ "$MODE" = "auto" ]; then
  MODE=$(python3 -c "import json;print(json.load(open('$BOOT/mixpre-remote.json')).get('mode','both'))" 2>/dev/null || echo both)
fi

modprobe libcomposite 2>/dev/null
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

teardown() {
  [ -d "$G" ] || return 0
  echo "" > "$G/UDC" 2>/dev/null
  for l in "$G"/configs/c.1/*.usb0; do [ -L "$l" ] && rm -f "$l"; done
  rmdir "$G"/configs/c.1/strings/0x409 2>/dev/null
  rmdir "$G"/configs/c.1 2>/dev/null
  for f in "$G"/functions/*; do [ -d "$f" ] && rmdir "$f" 2>/dev/null; done
  rmdir "$G"/strings/0x409 2>/dev/null
  rmdir "$G" 2>/dev/null
}

teardown
if [ "$MODE" = "off" ]; then echo "off" > /run/mixpre-gadget-mode; exit 0; fi

SERIAL=$(tr -d '\0' < /sys/firmware/devicetree/base/serial-number 2>/dev/null | tail -c 8)
[ -n "$SERIAL" ] || SERIAL="00000001"

mkdir -p "$G" && cd "$G" || exit 1
echo 0x0200 > bcdUSB
echo 0x00   > bDeviceClass
mkdir -p strings/0x409
echo "$SERIAL" > strings/0x409/serialnumber

if [ "$MODE" = "keyboard" ]; then
  echo 0x1d6b > idVendor        # Linux Foundation
  echo 0x0104 > idProduct       # Multifunction Composite Gadget
  echo 0x0100 > bcdDevice
  echo "Raspberry Pi"          > strings/0x409/manufacturer
  echo "MixPre Remote Keyboard" > strings/0x409/product
else
  echo 0x0944 > idVendor        # KORG INC.
  echo 0x0117 > idProduct       # nanoKONTROL2
  echo 0x0100 > bcdDevice
  echo "KORG INC."    > strings/0x409/manufacturer
  echo "nanoKONTROL2" > strings/0x409/product
fi

mkdir -p configs/c.1/strings/0x409
echo "MixPre Remote" > configs/c.1/strings/0x409/configuration
echo 0x80 > configs/c.1/bmAttributes
echo 100  > configs/c.1/MaxPower

if [ "$MODE" = "midi" ] || [ "$MODE" = "both" ]; then
  mkdir -p functions/midi.usb0
  echo "nanoKONTROL2" > functions/midi.usb0/id
  echo 1   > functions/midi.usb0/in_ports
  echo 1   > functions/midi.usb0/out_ports
  echo 512 > functions/midi.usb0/buflen
  echo 32  > functions/midi.usb0/qlen
  ln -s functions/midi.usb0 configs/c.1/
fi

if [ "$MODE" = "keyboard" ] || [ "$MODE" = "both" ]; then
  mkdir -p functions/hid.usb0
  echo 1 > functions/hid.usb0/protocol      # keyboard
  echo 1 > functions/hid.usb0/subclass      # boot interface
  echo 8 > functions/hid.usb0/report_length
  # Standard boot keyboard report descriptor
  printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > functions/hid.usb0/report_desc
  ln -s functions/hid.usb0 configs/c.1/
fi

if [ "$DEBUG_CONSOLE" = 1 ]; then
  # Debug only: also appear as a serial terminal (log in from a Mac with: screen /dev/tty.usbmodem* 115200)
  mkdir -p functions/acm.usb0
  ln -s functions/acm.usb0 configs/c.1/
fi

UDC=$(ls /sys/class/udc 2>/dev/null | head -n1)
if [ -z "$UDC" ]; then
  echo "No USB device controller found. Is dtoverlay=dwc2 in config.txt and are you on the Pi's USB (data) port?" >&2
  exit 1
fi
echo "$UDC" > UDC
echo "$MODE" > /run/mixpre-gadget-mode
[ "$DEBUG_CONSOLE" = 1 ] && systemctl --no-block start serial-getty@ttyGS0.service 2>/dev/null
echo "USB gadget ready: $MODE on $UDC"

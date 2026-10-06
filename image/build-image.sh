#!/bin/bash
# Builds a ready-to-flash MixPre Remote SD card image from Raspberry Pi OS Lite (64-bit).
#
#   sudo ./image/build-image.sh
#
# Runs on Ubuntu/Debian (x86_64 with qemu-user-static, or arm64). GitHub Actions runs it for you,
# see .github/workflows/build-image.yml.
#
# Env options:
#   BASE_IMG          use a local .img or .img.xz instead of downloading
#   BASE_URL          Raspberry Pi OS Lite arm64 download (default: latest)
#   MIXPRE_USER       login user (default: mixpre)
#   MIXPRE_PASSWORD   login password (default: mixpreremote)
#   MIXPRE_WIFI_PASSWORD, MIXPRE_COUNTRY   passed to setup-rootfs.sh
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$REPO/build}"
OUT="${OUT:-$REPO/out}"
BASE_URL="${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
USERNAME="${MIXPRE_USER:-mixpre}"
PASSWORD="${MIXPRE_PASSWORD:-mixpreremote}"
EXTRA_MB="${EXTRA_MB:-1024}"
ROOT="$WORK/root"
mkdir -p "$WORK" "$OUT" "$ROOT"
cd "$WORK"

echo "==> Getting Raspberry Pi OS Lite"
if [ -n "${BASE_IMG:-}" ]; then
  case "$BASE_IMG" in
    *.xz) xz -dc -T0 "$BASE_IMG" > mixpre.img ;;
    *)    cp "$BASE_IMG" mixpre.img ;;
  esac
else
  [ -f base.img.xz ] || curl -fL --retry 3 -o base.img.xz "$BASE_URL"
  xz -dc -T0 base.img.xz > mixpre.img
fi

echo "==> Growing image by ${EXTRA_MB} MB"
truncate -s "+${EXTRA_MB}M" mixpre.img
parted -s mixpre.img resizepart 2 100%
# Separate loop devices per partition (works without udev, e.g. in containers)
part_loop() {  # $1 = partition number
  local start sectors
  read -r start sectors < <(partx -g -o START,SECTORS -n "$1" mixpre.img)
  losetup -f --show -o $((start * 512)) --sizelimit $((sectors * 512)) mixpre.img
}
BOOTLOOP=$(part_loop 1)
ROOTLOOP=$(part_loop 2)
cleanup() {
  set +e
  for m in dev/pts dev proc sys; do umount "$ROOT/$m" 2>/dev/null; done
  umount "$ROOT/boot/firmware" 2>/dev/null; umount "$ROOT/boot" 2>/dev/null
  umount "$ROOT" 2>/dev/null
  losetup -d "$BOOTLOOP" "$ROOTLOOP" 2>/dev/null
}
trap cleanup EXIT
e2fsck -fy "$ROOTLOOP" || [ $? -le 1 ]
resize2fs "$ROOTLOOP"

echo "==> Mounting"
mount "$ROOTLOOP" "$ROOT"
BOOTDIR="$ROOT/boot/firmware"; [ -d "$BOOTDIR" ] || BOOTDIR="$ROOT/boot"
mount "$BOOTLOOP" "$BOOTDIR"
mount --bind /dev "$ROOT/dev"; mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"; mount -t sysfs sys "$ROOT/sys"

# chroot prep: network, no service starts, no preload, qemu if needed
[ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ] && mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.mixpre-bak"
cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
[ -f "$ROOT/etc/ld.so.preload" ] && mv "$ROOT/etc/ld.so.preload" "$ROOT/etc/ld.so.preload.mixpre-bak"
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"; chmod +x "$ROOT/usr/sbin/policy-rc.d"
if [ "$(uname -m)" != aarch64 ] && command -v qemu-aarch64-static >/dev/null; then
  cp "$(command -v qemu-aarch64-static)" "$ROOT/usr/bin/"
fi
rm -rf "$ROOT/tmp/mixpre-src"; mkdir -p "$ROOT/tmp/mixpre-src"
cp -r "$REPO/server" "$REPO/web" "$REPO/systemd" "$REPO/image" "$REPO/mixpre-remote.json" "$ROOT/tmp/mixpre-src/"

echo "==> Installing MixPre Remote inside the image"
chroot "$ROOT" /usr/bin/env IN_CHROOT=1 \
  MIXPRE_WIFI_PASSWORD="${MIXPRE_WIFI_PASSWORD:-}" MIXPRE_COUNTRY="${MIXPRE_COUNTRY:-US}" \
  bash /tmp/mixpre-src/image/setup-rootfs.sh

echo "==> Login user '$USERNAME', SSH on, skip first-boot wizard"
HASH=$(openssl passwd -6 "$PASSWORD")
chroot "$ROOT" /usr/bin/env USERNAME="$USERNAME" HASH="$HASH" bash -e <<'CH'
old=$(getent passwd 1000 | cut -d: -f1 || true)
if [ -n "$old" ] && [ "$old" != "$USERNAME" ]; then
  usermod -l "$USERNAME" -d "/home/$USERNAME" -m "$old"
  groupmod -n "$USERNAME" "$old" 2>/dev/null || true
elif [ -z "$old" ]; then
  useradd -m -u 1000 -s /bin/bash "$USERNAME"
fi
for g in sudo adm dialout audio video plugdev netdev bluetooth gpio; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$USERNAME"
done
echo "$USERNAME:$HASH" | chpasswd -e
systemctl disable userconfig.service 2>/dev/null || true
systemctl mask userconfig.service 2>/dev/null || true
[ -d /etc/cloud ] && touch /etc/cloud/cloud-init.disabled
systemctl enable ssh.service 2>/dev/null || systemctl enable sshd.service 2>/dev/null || true
CH
rm -f "$BOOTDIR/userconf.txt"   # user already created; don't let first boot rename it

echo "==> Cleaning up"
rm -rf "$ROOT/tmp/mixpre-src" "$ROOT/usr/sbin/policy-rc.d" "$ROOT/usr/bin/qemu-aarch64-static"
rm -f "$ROOT/etc/resolv.conf"
[ -e "$ROOT/etc/resolv.conf.mixpre-bak" ] || [ -L "$ROOT/etc/resolv.conf.mixpre-bak" ] && mv "$ROOT/etc/resolv.conf.mixpre-bak" "$ROOT/etc/resolv.conf"
[ -f "$ROOT/etc/ld.so.preload.mixpre-bak" ] && mv "$ROOT/etc/ld.so.preload.mixpre-bak" "$ROOT/etc/ld.so.preload"
rm -rf "$ROOT/var/lib/apt/lists/"* "$ROOT/root/.cache"
sync
cleanup
trap - EXIT

echo "==> Compressing"
NAME="mixpre-remote-$(date +%Y%m%d).img"
xz -T0 -6 -c mixpre.img > "$OUT/$NAME.xz"
( cd "$OUT" && sha256sum "$NAME.xz" > "$NAME.xz.sha256" )
rm -f mixpre.img
echo "==> Done: $OUT/$NAME.xz"

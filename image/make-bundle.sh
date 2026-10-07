#!/bin/bash
# Packs the app (not the OS) into an update bundle the Pi can install by itself.
#   image/make-bundle.sh <out-dir>   ->  <out-dir>/mixpre-app.tar.gz + manifest.json
# Commit info comes from git, or MIXPRE_COMMIT / MIXPRE_COMMIT_CT if set.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$SRC/out}"
mkdir -p "$OUT"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT

COMMIT="${MIXPRE_COMMIT:-$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo local)}"
CT="${MIXPRE_COMMIT_CT:-$(git -C "$SRC" log -1 --format=%ct 2>/dev/null || date +%s)}"
VERSION="$(tr -d ' \n' < "$SRC/VERSION")"
SYSTEM="$(tr -d ' \n' < "$SRC/image/SYSTEM_LEVEL")"

mkdir -p "$STAGE/web"
cp "$SRC"/server/mixpre_remote.py "$SRC"/server/mixpre_net.py "$SRC"/server/mixpre_cloud.py \
   "$SRC"/server/mixpre_update.py "$SRC"/server/gadget.sh "$STAGE/"
cp "$SRC/web/index.html" "$STAGE/web/"
printf '{"version": "%s", "commit": "%s", "ct": %s, "system": %s}\n' "$VERSION" "$COMMIT" "$CT" "$SYSTEM" > "$STAGE/BUILD.json"

tar -czf "$OUT/mixpre-app.tar.gz" --owner=0 --group=0 -C "$STAGE" .
SHA=$(sha256sum "$OUT/mixpre-app.tar.gz" | cut -d' ' -f1)
SIZE=$(stat -c %s "$OUT/mixpre-app.tar.gz")
printf '{"version": "%s", "commit": "%s", "ct": %s, "system": %s, "sha256": "%s", "size": %s}\n' \
  "$VERSION" "$COMMIT" "$CT" "$SYSTEM" "$SHA" "$SIZE" > "$OUT/manifest.json"
echo "Bundle $VERSION ($COMMIT): $OUT/mixpre-app.tar.gz"

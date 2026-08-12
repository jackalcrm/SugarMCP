#!/usr/bin/env bash
# Build the module-loadable package zip for install via Admin -> Module Loader.
#
#   ./tools/build.sh
#
# Produces dist/sugarmcp-<version>.zip with manifest.php at the archive root -- which is what
# <basepath> resolves to on install. Needs only `zip`: no Sugar instance, nothing local to the
# CRM. Module Loader runs its own ModuleScanner (denylist) and Rector (PHP-compat) passes when
# you upload the zip, so validation happens on the instance, wherever it lives.
set -euo pipefail

PKG="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$PKG/src"
DIST="$PKG/dist"

# Keep the zip name in step with the manifest, so a version bump is one edit rather than two.
version="$(grep -oE "'version'[[:space:]]*=>[[:space:]]*'[^']+'" "$SRC/manifest.php" \
  | grep -oE "[0-9][^']*")"
if [ -z "${version:-}" ]; then
  echo "Could not read 'version' from $SRC/manifest.php" >&2
  exit 1
fi

ZIP="$DIST/sugarmcp-$version.zip"
mkdir -p "$DIST"
rm -f "$ZIP"

# Zip from inside src/ so manifest.php lands at the archive root, not under a src/ prefix.
# Exclude dotfiles at the root and in any subdirectory (.DS_Store, editor cruft).
( cd "$SRC" && zip -rX "$ZIP" . -x '.*' -x '*/.*' ) >/dev/null

echo "Built $ZIP"
unzip -l "$ZIP"

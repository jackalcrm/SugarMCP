#!/usr/bin/env bash
# Build the module-loadable package zip for install via Admin -> Module Loader.
#
#   ./tools/build.sh
#
# Produces dist/sugarmcp-<version>.zip with manifest.php at the archive root -- which is what
# <basepath> resolves to on install, and what Module Loader's ZipArchive::extractTo(dir,
# 'manifest.php') requires. Needs only `zip`: no Sugar instance, nothing local to the CRM.
# Module Loader runs its own ModuleScanner (denylist) and Rector (PHP-compat) passes on upload.
#
# IMPORTANT: install THIS file as produced. Do NOT re-compress it in Finder ("Compress") or
# with `ditto` -- those nest everything under a folder and add __MACOSX/._* dotfiles, so
# manifest.php is no longer at the root and Module Loader reports "does not contain a manifest".
# The verification step below fails the build if any of that junk is present.
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
#   -X            strip macOS extended attributes (prevents AppleDouble resource forks)
#   -x ...        drop dotfiles at root and in subdirs, plus any __MACOSX wrapper
( cd "$SRC" && zip -rX "$ZIP" . \
    -x '.*' -x '*/.*' -x '__MACOSX' -x '__MACOSX/*' ) >/dev/null

# --- Verify the archive is Module-Loader-shaped before handing it over. -----------------
names="$(zipinfo -1 "$ZIP")"
fail() { echo "BUILD FAILED: $1" >&2; rm -f "$ZIP"; exit 1; }

# manifest.php must exist as an exact root entry (no directory prefix, no ./).
grep -qx 'manifest.php' <<<"$names" \
  || fail "manifest.php is not at the archive root -- Module Loader will reject it."

# Nothing macOS/Finder may have smuggled in, and no ./-prefixed entries.
if grep -qE '(^__MACOSX/|(^|/)\._|(^|/)\.DS_Store$|^\./)' <<<"$names"; then
  fail "archive contains macOS junk (__MACOSX / ._* / .DS_Store / ./ prefix)."
fi

echo "Built $ZIP"
echo "sha256: $(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
echo "root manifest: OK   macOS junk: none"
unzip -l "$ZIP"

#!/usr/bin/env bash
# Verify the built package against Module Loader policy before uploading it.
#
#   ./tools/scan.sh [path/to/package.zip]
#
# Module Loader runs TWO independent scans and a package can pass one and fail the other:
#
#   1. ModuleScanner  — the function/class denylist (callbacks, filesystem, exec, network).
#   2. Rector         — PHP-compatibility rules, reported as "PHP Compatibility Issues".
#
# Both run here. They need Sugar's bootstrap, so they run on the instance host with php and
# the Sugar web root available — nothing is assumed to be local.
#
# Configure the target instance:
#   SUGAR_ROOT  the Sugar web root (required).
#   SUGAR_RUN   command prefix that runs a shell snippet on the instance host. Defaults to
#               running on this machine. Override for a non-local instance, e.g.
#                 SUGAR_RUN='orb -m <vm> bash -lc'        # OrbStack VM
#                 SUGAR_RUN='ssh user@host bash -lc'      # remote over SSH
#               When SUGAR_RUN targets another host, the zip, the scanner scripts under
#               tools/, and SUGAR_ROOT must all be reachable from that host.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ZIP="${1:-$(cd "$HERE/.." && pwd)/dist/sugarmcp-0.1.0.zip}"
SUGAR_ROOT="${SUGAR_ROOT:?set SUGAR_ROOT to the Sugar web root}"
# Default runner executes on this machine; unquoted expansion splits a multi-word prefix.
SUGAR_RUN="${SUGAR_RUN:-bash -lc}"

# ModuleScanner rejects any package path outside the Sugar base dir ("file outside basedir"),
# so stage the unzip inside the instance's cache directory rather than a system temp dir.
STAGE="$SUGAR_ROOT/cache/mcp-scan"

status=0

echo "== 1/2  ModuleScanner (denylist) =="
$SUGAR_RUN "
  rm -rf '$STAGE' && mkdir -p '$STAGE' &&
  unzip -q '$ZIP' -d '$STAGE' &&
  SUGAR_ROOT='$SUGAR_ROOT' php '$HERE/scan_package.php' '$STAGE'
" || status=1

echo
echo "== 2/2  Rector (PHP compatibility) =="
$SUGAR_RUN "
  SUGAR_ROOT='$SUGAR_ROOT' php '$HERE/scan_rector.php' '$STAGE'
" || status=1

$SUGAR_RUN "rm -rf '$STAGE'" || true

echo
if [ "$status" -eq 0 ]; then
  echo "PACKAGE OK — both scans clean"
else
  echo "PACKAGE REJECTED — fix the issues above before uploading"
fi
exit "$status"

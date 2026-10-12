#!/bin/sh
# Shellie installer entry point. Keep this file small: it is the target of the
# one-line install, so its URL must not change.
#
#   curl -fsSL https://raw.githubusercontent.com/nicechester/shellie/main/launchd/install.sh | sh
#   sh launchd/install.sh [--uninstall]        from a checkout
#
# Outside a checkout, this fetches setup.sh from the latest release (or from
# the SHELLIE_REF release), so each install is counted on that asset. If no
# release asset exists, it fetches setup.sh from the requested ref instead.
set -eu

REPO="nicechester/shellie"
SELF="$0"

# From a checkout: run the checkout's installer directly.
if [ -f "$SELF" ]; then
    HERE=$(cd "$(dirname "$SELF")" && pwd)
    if [ -f "$HERE/setup.sh" ] && [ -f "$HERE/../src/agent/__main__.py" ]; then
        exec sh "$HERE/setup.sh" "$@"
    fi
fi

if [ -n "${SHELLIE_REF:-}" ]; then
    ASSET_URL="https://github.com/$REPO/releases/download/$SHELLIE_REF/setup.sh"
    RAW_URL="https://raw.githubusercontent.com/$REPO/$SHELLIE_REF/launchd/setup.sh"
else
    ASSET_URL="https://github.com/$REPO/releases/latest/download/setup.sh"
    RAW_URL="https://raw.githubusercontent.com/$REPO/main/launchd/setup.sh"
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
if ! curl -fsSL "$ASSET_URL" -o "$TMP" 2>/dev/null; then
    curl -fsSL "$RAW_URL" -o "$TMP"
fi
sh "$TMP" "$@"

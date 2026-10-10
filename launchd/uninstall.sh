#!/bin/sh
# Shellie uninstaller.
#
# Stops and removes the background service (launchd on macOS, systemd user
# unit on Linux). If this copy was installed by the one-line installer
# (under ~/.local/share/shellie), its source tree is removed as well.
#
# Left untouched on purpose: your data in ~/.shellie (memory, settings,
# schedules, tasks) and ~/workspace (files the agent produced), plus any
# git checkout you installed from.
#
#   sh launchd/uninstall.sh
#   (or: sh launchd/install.sh --uninstall)
set -eu

INSTALL_ROOT="${SHELLIE_INSTALL_DIR:-$HOME/.local/share/shellie}"
PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
OS=$(uname -s)

if [ "$OS" = "Darwin" ]; then
    PLIST_DST="$HOME/Library/LaunchAgents/com.user.shellie.plist"
    # Ignore errors: the agent may never have been loaded.
    launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "Removed launchd agent com.user.shellie."

elif [ "$OS" = "Linux" ]; then
    SERVICE_DST="$HOME/.config/systemd/user/shellie.service"
    systemctl --user disable --now shellie.service 2>/dev/null || true
    rm -f "$SERVICE_DST"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Removed systemd user service shellie.service."

else
    echo "Unsupported platform: $OS. No service to remove." >&2
    exit 1
fi

# Remove the source tree only when it is the one-line installer's copy.
case "$PROJECT_DIR" in
    "$INSTALL_ROOT"/shellie)
        rm -rf "$PROJECT_DIR"
        echo "Removed installed source, including its .env and settings.json: $PROJECT_DIR"
        ;;
esac

echo ""
echo "Shellie is uninstalled. Kept your data:"
echo "  config and memory: ${SHELLIE_HOME:-$HOME/.shellie}"
echo "  workspace:         ${SHELLIE_WORKSPACE:-$HOME/workspace}"
echo "Delete those directories yourself if you no longer need them."

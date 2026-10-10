#!/bin/sh
# Shellie installer / updater.
#
#   One-line install (no checkout needed):
#     curl -fsSL https://raw.githubusercontent.com/nicechester/shellie/main/launchd/install.sh | sh
#
#   From a checkout:
#     sh launchd/install.sh
#
#   Environment:
#     SHELLIE_REF          release tag or branch to install (default: latest
#                          GitHub release, falling back to main)
#     SHELLIE_INSTALL_DIR  where the bootstrap copy lives
#                          (default: ~/.local/share/shellie)
#
#   Uninstall:
#     sh launchd/install.sh --uninstall
set -eu

REPO="nicechester/shellie"
INSTALL_ROOT="${SHELLIE_INSTALL_DIR:-$HOME/.local/share/shellie}"
SELF="$0"

# Resolve the repo root when this script runs from a checkout. When piped
# through `curl | sh`, $0 is not a file, so PROJECT_DIR stays empty.
PROJECT_DIR=""
if [ -f "$SELF" ]; then
    CANDIDATE=$(cd "$(dirname "$SELF")/.." && pwd)
    if [ -f "$CANDIDATE/src/agent/__main__.py" ]; then
        PROJECT_DIR=$CANDIDATE
    fi
fi

die() {
    echo "error: $*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' is required but was not found."
}

check_python() {
    PY=$(command -v python3 || true)
    [ -n "$PY" ] || die "python3 was not found. Install Python 3.8 or newer and retry."
    if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
        die "Python 3.8 or newer is required (found $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))."
    fi
}

# Print the latest published release tag, or nothing if there is none.
latest_release_tag() {
    curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1
}

# Download and unpack the source for REF into the staging directory $1.
# Sets SRC_DIR to the unpacked source directory. Not run in a subshell, so
# die() really aborts the installer.
fetch_source() {
    STAGE=$1
    REF=$2
    for KIND in tags heads; do
        URL="https://github.com/$REPO/archive/refs/$KIND/$REF.tar.gz"
        if curl -fsSL "$URL" -o "$STAGE/src.tar.gz" 2>/dev/null; then
            tar -xzf "$STAGE/src.tar.gz" -C "$STAGE"
            rm -f "$STAGE/src.tar.gz"
            # GitHub names the top directory after the repo and ref
            # (shellie-main/, shellie-0.1.0/), so pick the one directory.
            SRC_DIR=$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -n 1)
            return 0
        fi
    done
    die "could not download '$REF' from github.com/$REPO (no such tag or branch?)."
}

# Download the requested source into INSTALL_ROOT/shellie, keeping the
# user's .env and settings.json, then hand off to the installed copy.
bootstrap() {
    need_cmd curl
    need_cmd tar
    check_python

    REF=${SHELLIE_REF:-}
    if [ -z "$REF" ]; then
        REF=$(latest_release_tag)
        REF=${REF:-main}
    fi
    echo "Installing shellie ($REF) into $INSTALL_ROOT/shellie ..."

    mkdir -p "$INSTALL_ROOT"
    STAGE=$(mktemp -d "$INSTALL_ROOT/.stage.XXXXXX")
    fetch_source "$STAGE" "$REF"
    TREE="$INSTALL_ROOT/shellie"

    # Carry over local configuration: .env and settings.json live in the
    # project root and must survive an upgrade.
    for KEEP in .env settings.json; do
        if [ -f "$TREE/$KEEP" ]; then
            cp -p "$TREE/$KEEP" "$SRC_DIR/$KEEP"
        fi
    done

    rm -rf "$TREE"
    mv "$SRC_DIR" "$TREE"
    rm -rf "$STAGE"

    # Run the installed copy's installer, which sees a checkout.
    exec sh "$TREE/launchd/install.sh" "$@"
}

# ----------------------------------------------------------------------------
# --uninstall
# ----------------------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
    if [ -n "$PROJECT_DIR" ]; then
        exec sh "$PROJECT_DIR/launchd/uninstall.sh"
    elif [ -f "$INSTALL_ROOT/shellie/launchd/uninstall.sh" ]; then
        exec sh "$INSTALL_ROOT/shellie/launchd/uninstall.sh"
    else
        die "no installed copy found at $INSTALL_ROOT/shellie."
    fi
fi

# Not running from a checkout: fetch the source and re-run from there.
if [ -z "$PROJECT_DIR" ]; then
    bootstrap "$@"
fi

# ----------------------------------------------------------------------------
# Install from the checkout at PROJECT_DIR
# ----------------------------------------------------------------------------
check_python

# Common hardening: restrict file permissions
if [ -f "$PROJECT_DIR/.env" ]; then
    chmod 600 "$PROJECT_DIR/.env"
fi
if [ -f "$PROJECT_DIR/settings.json" ]; then
    chmod 600 "$PROJECT_DIR/settings.json"
fi

# Branch on OS
OS=$(uname -s)

if [ "$OS" = "Darwin" ]; then
    # macOS: use launchd
    PLIST_SRC="$PROJECT_DIR/launchd/com.user.shellie.plist"
    PLIST_DST="$HOME/Library/LaunchAgents/com.user.shellie.plist"

    # Create destination directory if needed
    mkdir -p "$(dirname "$PLIST_DST")"

    # sed-replace /Users/USERNAME/shellie with PROJECT_DIR in the plist
    sed -e "s|/Users/USERNAME/shellie|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

    # Clean reinstall: bootout (ignore errors) then bootstrap, which starts the agent
    launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

    echo "Shellie is installed as a macOS launchd agent and is running."
    echo "Logs: $PROJECT_DIR/agent.log"

elif [ "$OS" = "Linux" ]; then
    # Linux: use systemd user unit
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"

    SERVICE_SRC="$PROJECT_DIR/systemd/shellie.service"
    SERVICE_DST="$SYSTEMD_DIR/shellie.service"

    # sed-replace %h/shellie with PROJECT_DIR (absolute path)
    sed -e "s|%h/shellie|$PROJECT_DIR|g" "$SERVICE_SRC" > "$SERVICE_DST"

    # Reload, enable for future logins, and start. restart (not just --now)
    # so that an already-running service picks up the upgraded code.
    systemctl --user daemon-reload
    systemctl --user enable shellie.service
    systemctl --user restart shellie.service

    echo "Shellie is installed as a Linux systemd user service and is running."
    echo "Logs: journalctl --user -u shellie -f"
    echo "To start at boot without a login session, run once: loginctl enable-linger $USER"

else
    die "unsupported platform: $OS"
fi

# ----------------------------------------------------------------------------
# Next steps
# ----------------------------------------------------------------------------
echo ""
echo "Next steps:"
echo "  Shellie starts in setup-required mode until TELEGRAM_BOT_TOKEN,"
echo "  GEMINI_API_KEY and ALLOWED_USER_ID are set. Either:"
echo "    - open http://127.0.0.1:8321/ in a browser and fill them in, or"
echo "    - create $PROJECT_DIR/.env with those keys (then restart the service)."
echo "  Uninstall: sh $PROJECT_DIR/launchd/install.sh --uninstall"

#!/bin/sh
set -eu

# Resolve PROJECT_DIR as the script's parent's parent (repo root)
PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)

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

    # Clean reinstall: bootout (ignore errors) then bootstrap
    launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

    # Korean success message
    echo "Shellie가 macOS 백그라운드 에이전트로 설치되었습니다."
    echo "로그는 다음 위치에서 확인할 수 있습니다: $PROJECT_DIR/agent.log"

elif [ "$OS" = "Linux" ]; then
    # Linux: use systemd user unit
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"

    SERVICE_SRC="$PROJECT_DIR/systemd/shellie.service"
    SERVICE_DST="$SYSTEMD_DIR/shellie.service"

    # sed-replace %h/shellie with PROJECT_DIR (absolute path)
    sed -e "s|%h/shellie|$PROJECT_DIR|g" "$SERVICE_SRC" > "$SERVICE_DST"

    # Reload and enable
    systemctl --user daemon-reload
    systemctl --user enable --now shellie.service

    # Korean messages
    echo "Shellie가 Linux 백그라운드 에이전트로 설치되었습니다."
    echo "로그 확인: journalctl --user -u shellie -f"
    echo "부팅 시 자동 시작하려면 한 번 실행하세요: loginctl enable-linger $USER"

else
    # Unsupported platform
    echo "지원하지 않는 플랫폼입니다"
    exit 1
fi

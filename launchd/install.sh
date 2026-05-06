#!/usr/bin/env bash
# Install the launchd agent for the OpenCode Telegram bridge.
# Usage: bash launchd/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="ai.opencode.tg-bridge"
PLIST_SRC="$SCRIPT_DIR/${LABEL}.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PROJECT_DIR/config.yaml" ]]; then
    echo "missing $PROJECT_DIR/config.yaml -- copy config.example.yaml and edit first" >&2
    exit 1
fi
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "missing $PROJECT_DIR/.venv -- create venv and install deps first" >&2
    exit 1
fi
if [[ ! -f "$PROJECT_DIR/opencode_tg_bridge.py" ]]; then
    echo "missing $PROJECT_DIR/opencode_tg_bridge.py" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# Reload
launchctl unload -w "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"

echo "installed: $PLIST_DST"
echo "status:"
launchctl list | grep "$LABEL" || echo "  (not listed yet, check /tmp/opencode-tg-bridge.err)"
echo
echo "tail logs with:"
echo "  tail -f /tmp/opencode-tg-bridge.out /tmp/opencode-tg-bridge.err"
echo "stop / uninstall:"
echo "  launchctl unload -w $PLIST_DST && rm $PLIST_DST"

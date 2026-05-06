#!/usr/bin/env bash
# One-shot diagnostic snapshot for a wedged opencode bridge.
#
# Usage:  bash diagnose.sh            (writes snapshot to ./diagnostics/<ts>/)
#
# Captures:
#   - process state (ps, lsof, open files, fds)
#   - SIGUSR1 stack dump (sent to bridge; appears in its log)
#   - py-spy native dump (requires sudo — will prompt)
#   - recent log tails (bridge, launchd out/err)
#   - opencode serve processes spawned by bridge
#
# NOTE: the bridge keeps running after this script; nothing is killed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$SCRIPT_DIR/diagnostics/$TS"
mkdir -p "$OUT"

echo "=== bridge diagnostic snapshot → $OUT ==="

PID=$(pgrep -f opencode_tg_bridge.py | head -1 || true)
if [[ -z "$PID" ]]; then
    echo "bridge is NOT running" | tee "$OUT/00-not-running.txt"
    exit 1
fi
echo "bridge pid: $PID" | tee "$OUT/00-pid.txt"

# 1. Trigger in-process stack dump (goes to bridge's own log)
echo
echo "--- sending SIGUSR1 (bridge will log python stacks to its stderr) ---"
kill -USR1 "$PID"
# give it a moment to flush
sleep 1

# 2. Process/system state
echo "--- process state ---"
{
    ps -o pid,ppid,state,etime,%cpu,%mem,wchan,command -p "$PID" 2>&1 || true
    echo
    echo "# lsof -p $PID (first 50):"
    lsof -p "$PID" 2>&1 | head -50 || true
    echo
    echo "# opencode serve children (ppid=$PID):"
    pgrep -P "$PID" -a || echo "(none)"
} > "$OUT/01-ps-lsof.txt"
echo "wrote $OUT/01-ps-lsof.txt"

# 3. Dump the bridge's own log tail (includes the SIGUSR1 dump we just triggered)
echo "--- log tails ---"
cp /tmp/opencode-tg-bridge.err "$OUT/02-bridge-err.log" 2>/dev/null || true
cp /tmp/opencode-tg-bridge.out "$OUT/02-bridge-out.log" 2>/dev/null || true
tail -200 /tmp/opencode-tg-bridge.err > "$OUT/02-bridge-err-tail.log" 2>/dev/null || true

# 4. py-spy (needs sudo on macOS)
echo "--- py-spy dump (needs sudo) ---"
if command -v "$SCRIPT_DIR/.venv/bin/py-spy" >/dev/null; then
    PYSPY="$SCRIPT_DIR/.venv/bin/py-spy"
    if sudo -n true 2>/dev/null; then
        sudo "$PYSPY" dump --pid "$PID" > "$OUT/03-pyspy.txt" 2>&1 \
            && echo "wrote $OUT/03-pyspy.txt" \
            || echo "py-spy dump failed; see $OUT/03-pyspy.txt"
    else
        echo "py-spy requires sudo on macOS. Run this once and re-run script:" \
             "sudo -v"
        echo "Skipping py-spy section." > "$OUT/03-pyspy-skipped.txt"
    fi
else
    echo "py-spy not installed in .venv; skipping" > "$OUT/03-pyspy-missing.txt"
fi

# 5. A compact summary
{
    echo "Snapshot taken at: $TS"
    echo "Bridge PID: $PID"
    echo "Files captured:"
    ls -1 "$OUT"
} > "$OUT/SUMMARY.txt"

echo
echo "=== done. Snapshot dir: $OUT ==="
echo "Tail the bridge log to see the SIGUSR1 python stack dump:"
echo "  tail -200 /tmp/opencode-tg-bridge.err | less"

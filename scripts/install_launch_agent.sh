#!/usr/bin/env bash
# Install Content Filtering Agent as a per-user Launch Agent (starts at login, restarts on crash).
# Does not require Cursor. Stop any manual `nohup` copy first: kill "$(cat agent.pid)" 2>/dev/null || true

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LABEL="com.user.contentagent"
SRC="$ROOT/launchd/com.user.contentagent.plist.example"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$SRC" ]]; then
  echo "Missing $SRC"
  exit 1
fi
if [[ ! -x "$PY" ]]; then
  echo "No venv Python at $PY — run: cd \"$ROOT\" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

sed -e "s|__PROJECT_ROOT__|$ROOT|g" -e "s|__PYTHON__|$PY|g" "$SRC" > "$DST"
echo "Wrote $DST"

# Unload old job if present (ignore errors)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

launchctl bootstrap "gui/$(id -u)" "$DST"
echo "Loaded. The agent will start at login and respawn if it exits."
echo "Logs: $ROOT/content_agent.launchd.{out,err}.log"
echo "Unload later: launchctl bootout gui/$(id -u)/$LABEL"

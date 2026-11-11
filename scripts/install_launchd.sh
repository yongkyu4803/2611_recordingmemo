#!/bin/bash
# launchd 사용자 에이전트로 등록해 기본 3분마다 `drive2notion poll`을 실행한다.
#   등록:  scripts/install_launchd.sh
#   해제:  scripts/install_launchd.sh uninstall
set -euo pipefail

LABEL="com.user.drive2notion"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$ROOT/.venv/bin/python"
INTERVAL="${DRIVE2NOTION_INTERVAL:-180}"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "해제했습니다: $LABEL"
  exit 0
fi

[[ -x "$PY" ]] || { echo "가상환경이 없습니다. 먼저 'uv sync'를 실행하세요."; exit 1; }
[[ -f "$ROOT/.env" ]] || { echo ".env가 없습니다. .env.example을 복사해 채우세요."; exit 1; }
command -v claude >/dev/null || { echo "claude CLI를 찾을 수 없습니다."; exit 1; }

CLAUDE_DIR="$(dirname "$(command -v claude)")"
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string>
    <string>drive2notion</string>
    <string>poll</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$CLAUDE_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>DRIVE2NOTION_ROOT</key><string>$ROOT</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/data/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "등록했습니다: $PLIST (${INTERVAL}초마다 실행)"
echo "로그: $ROOT/data/logs/app.log"

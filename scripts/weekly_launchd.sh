#!/bin/zsh
# launchd가 매주 화요일 07:00에 실행하는 진입점.
# Claude Code 헤드리스로 주간 런북(.claude/commands/weekly-run.md)을 수행한다.
# 로그: ~/Library/Logs/ai-newsletter-weekly/<날짜>.log

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$HOME/workspace/ai-newsletter-automation" || exit 1

LOG_DIR="$HOME/Library/Logs/ai-newsletter-weekly"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

{
  echo "===== 주간 자동 실행 시작: $(date) ====="
  claude -p "/weekly-run" --permission-mode acceptEdits
  status=$?
  echo "===== 종료 (exit $status): $(date) ====="
  if [[ $status -ne 0 ]]; then
    osascript -e 'display notification "주간 실행 실패 — 로그를 확인하세요" with title "AI 뉴스레터 자동화"' || true
  fi
} >> "$LOG" 2>&1

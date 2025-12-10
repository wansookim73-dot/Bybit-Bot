#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[CLEANUP] Working dir: $ROOT_DIR"

# ----------------------------------------------------------
# 1) mexc_bot_backup_*.tgz 중에서 가장 최신 1개만 남기고 나머지 삭제
# ----------------------------------------------------------
echo "[CLEANUP] Keeping newest mexc_bot_backup_*.tgz, deleting older ones..."

BACKUPS=(mexc_bot_backup_*.tgz)
if ls mexc_bot_backup_*.tgz >/dev/null 2>&1; then
  NEWEST="$(ls -t mexc_bot_backup_*.tgz | head -n 1)"
  echo "[CLEANUP] Newest backup: $NEWEST"
  # 나머지 오래된 백업들
  OLD_BACKUPS="$(ls -t mexc_bot_backup_*.tgz | tail -n +2 || true)"
  if [ -n "$OLD_BACKUPS" ]; then
    echo "$OLD_BACKUPS" | while read -r f; do
      [ -n "$f" ] || continue
      echo "[CLEANUP] Removing old backup: $f"
      rm -f -- "$f"
    done
  else
    echo "[CLEANUP] No old backups to remove."
  fi
else
  echo "[CLEANUP] No mexc_bot_backup_*.tgz files found."
fi

# ----------------------------------------------------------
# 2) bot_snapshot_*.txt 스냅샷 삭제
# ----------------------------------------------------------
echo "[CLEANUP] Removing bot_snapshot_*.txt files (if any)..."
find "$ROOT_DIR" -maxdepth 1 -type f -name "bot_snapshot_*.txt" -print -exec rm -f {} \;

# ----------------------------------------------------------
# 3) .bak 파일들 정리 (코드 백업본)
#    주의: core/strategy/utils/wave_bot.py 등 본체는 건드리지 않음
# ----------------------------------------------------------
echo "[CLEANUP] Removing *.bak backup files under project (core/strategy/utils/wave_bot/main_v10 제외)..."

# top-level *.bak
find "$ROOT_DIR" -maxdepth 1 -type f -name "*.bak" -print -exec rm -f {} \;

# core/strategy/utils 내 *.bak
find "$ROOT_DIR/core" "$ROOT_DIR/strategy" "$ROOT_DIR/utils" \
  -type f -name "*.bak" -print -exec rm -f {} \; 2>/dev/null || true

echo "[CLEANUP] Done."

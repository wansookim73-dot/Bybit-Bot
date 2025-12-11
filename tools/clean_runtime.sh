#!/bin/bash
set -e

echo "--------------------------------"
echo " WaveBot Runtime Cleanup Script "
echo "--------------------------------"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_ROOT"

echo "[1/6] 삭제: 프로젝트 루트의 오래된 tgz 스냅샷..."
rm -f *.tgz 2>/dev/null || true

echo "[2/6] 삭제: ~/mexc_bot_snapshots/ 아래 스냅샷..."
rm -rf ~/mexc_bot_snapshots/* 2>/dev/null || true

echo "[3/6] 삭제: data/ 내 런타임 로그 및 백업..."
rm -f data/bot.log* 2>/dev/null || true
rm -f data/bot_state_waveid_backup.json 2>/dev/null || true

echo "[INFO] data/bot_state.json 은 보존합니다."

echo "[4/6] 삭제: Python 캐시(__pycache__)..."
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "[5/6] 삭제: Python .pyc 파일..."
find . -name "*.pyc" -delete 2>/dev/null || true

echo "[6/6] 삭제: pytest 캐시..."
rm -rf .pytest_cache 2>/dev/null || true

echo "--------------------------------"
echo " Cleanup 완료!"
echo " 프로젝트는 안전하게 유지되었으며,"
echo " 다시 실행 시 필요한 파일은 자동 생성됩니다."
echo "--------------------------------"

#!/bin/bash
set -e

# ----------------------------------------
# WaveBot Snapshot Maker (v10.1+)
# ----------------------------------------
# 기능:
#   1) Git working tree 깨끗한지 검사
#   2) Git tag 생성 + push + push --tags
#   3) .tgz 스냅샷 생성 (코드/설정 전체)
#   4) BACKUP_S3_BUCKET 이 설정돼 있으면 S3로 자동 업로드
#
# 사용 예:
#   ./tools/make_snapshot.sh v10.1.2
#
# 환경 변수:
#   BACKUP_S3_BUCKET  : (필수) 업로드할 S3 버킷 이름 (예: my-backup-bucket)
#   BACKUP_S3_PREFIX  : (선택) 버킷 내 경로 prefix (기본값: mexc_bot)
#                        예: BACKUP_S3_PREFIX="bybit/wavebot"
# ----------------------------------------

# 1) 입력 확인
if [ -z "$1" ]; then
    echo "Usage: $0 <tag_name>"
    echo "예:  ./tools/make_snapshot.sh v10.1.2"
    exit 1
fi

TAG_NAME="$1"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SNAP_DIR="$HOME/mexc_bot_snapshots"

# 2) Git clean check
cd "$PROJECT_ROOT"
if ! git diff --quiet; then
    echo "[ERROR] Git working tree가 깨끗하지 않습니다."
    echo "commit 또는 stash 후 다시 실행하세요."
    exit 1
fi

if ! git diff --cached --quiet; then
    echo "[ERROR] 커밋 안 된 staged 변경이 있습니다."
    exit 1
fi

echo "[OK] Git working tree clean."

# 3) Tag 생성 + Push
echo "[INFO] Creating git tag: $TAG_NAME"
# 이미 동일 태그가 있으면 에러 대신 스킵하도록 방어
if git rev-parse "$TAG_NAME" >/dev/null 2>&1; then
    echo "[WARN] tag $TAG_NAME already exists locally. 재사용합니다."
else
    git tag "$TAG_NAME"
fi

git push
git push --tags
echo "[OK] Git tag pushed: $TAG_NAME"

# 4) 백업 폴더 생성
mkdir -p "$SNAP_DIR"

# 5) timestamp
TS=$(date +%Y%m%d_%H%M%S)
OUTFILE="$SNAP_DIR/mexc_bot_${TAG_NAME}_snapshot_${TS}.tgz"

# 6) Tar 생성 (필요 없는 것 제외)
echo "[INFO] Creating snapshot: $OUTFILE"

tar czf "$OUTFILE" \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='data/bot.log*' \
    --exclude='data/bot_state.json' \
    --exclude='mexc_bot_snapshots' \
    -C "$PROJECT_ROOT" .

echo "[OK] Snapshot created: $OUTFILE"

# 7) S3 업로드 (옵션)
if [ -n "$BACKUP_S3_BUCKET" ]; then
    PREFIX="${BACKUP_S3_PREFIX:-mexc_bot}"
    BASENAME="$(basename "$OUTFILE")"
    S3_KEY="${PREFIX}/${TAG_NAME}/${BASENAME}"
    S3_URI="s3://${BACKUP_S3_BUCKET}/${S3_KEY}"

    echo "[INFO] S3 업로드 시작: $S3_URI"

    # aws CLI 필수
    if ! command -v aws >/dev/null 2>&1; then
        echo "[ERROR] aws CLI 를 찾을 수 없습니다. (aws 명령어 없음)"
        echo "       S3 업로드 없이 로컬 스냅샷만 생성된 상태입니다."
        exit 1
    fi

    aws s3 cp "$OUTFILE" "$S3_URI"

    echo "[OK] S3 업로드 완료: $S3_URI"
else
    echo "[INFO] BACKUP_S3_BUCKET 이 설정되지 않았습니다."
    echo "[INFO] S3 업로드는 건너뛰고 로컬 스냅샷만 생성했습니다."
fi

# 8) 결과 안내
echo "---------------------------------------"
echo "Snapshot 완료!"
echo "GitHub 태그 : $TAG_NAME"
echo "로컬 백업   : $OUTFILE"

if [ -n "$BACKUP_S3_BUCKET" ]; then
    echo "S3 백업     : s3://${BACKUP_S3_BUCKET}/${S3_KEY}"
else
    echo "S3 백업     : (생성 안 함: BACKUP_S3_BUCKET 미설정)"
fi
echo "---------------------------------------"

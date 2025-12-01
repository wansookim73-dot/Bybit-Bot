import os
from dotenv import load_dotenv

load_dotenv()

# === 1. 계정 및 API 설정 ===
# [보안] API Key는 환경변수 또는 아래에 직접 입력 (문자열)
BYBIT_API_KEY = "DEi5nK4G4zjj43B9pA"
BYBIT_SECRET_KEY = "xDI1GoZz3KfhG3m5SggS4CnDE6BMYpngFOPY"
DRY_RUN = False  # False: 실전 매매, True: 로그만 출력

# === 2. 거래 기본 설정 ===
EXCHANGE_ID = "bybit"
SYMBOL = "BTC/USDT"
LEVERAGE = 7
OPEN_TYPE = "cross"  # CCXT Bybit default

# === 3. 자금 관리 & 전략 파라미터 ===
INITIAL_SEED_USDT = 600.0
GRID_SPLIT_COUNT = 20
ATR_MULTIPLIER = 0.3
MIN_GRID_GAP = 150.0  # 최소 그리드 간격 (USDT)

# [안전장치 v9.1]
MAX_ALLOCATION_RATE = 0.25   # 방향별 최대 할당 비율 (25%)
HEDGE_MAX_FACTOR = 2.0       # 헷징 최대 배수 (물린 물량의 2배)
ESCAPE_CUSHION_PCT = 0.02    # 탈출 목표가 2% 쿠션

# === 4. 타임아웃 (Dual Mode) ===
GRID_TIMEOUT_SEC = 60.0      # Mode A: Maker Only
ESCAPE_TIMEOUT_SEC = 1.0     # Mode B: Taker Fallback

# === 5. 시스템 설정 ===
API_TIMEOUT = 30
MAX_RETRIES = 5
LOG_FILE_PATH = "data/bot.log"
STATE_FILE_PATH = "data/bot_state.json"

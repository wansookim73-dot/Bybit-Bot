import os
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# 1) Live/Dry 안전 게이트 (실수 방지용 2중 잠금)
# =========================================================
# - REQUEST_LIVE=1  AND  LIVE_GATE=YES  => DRY_RUN=False (REAL)
# - 그 외는 항상 DRY_RUN=True
_request_live = (os.getenv("REQUEST_LIVE", "0").strip() == "1")
_live_gate    = (os.getenv("LIVE_GATE", "").strip().upper() == "YES")

DRY_RUN = not (_request_live and _live_gate)

# =========================================================
# 2) 계정 및 API 설정
# =========================================================
# 보안상: 가능하면 환경변수(.env 또는 systemd Environment)로만 주입 권장
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET_KEY = os.getenv("BYBIT_SECRET_KEY", "")

# =========================================================
# 3) 거래 기본 설정 (v10.1에서 ExchangeAPI가 BTCUSDT/7x 강제)
# =========================================================
EXCHANGE_ID = "bybit"
SYMBOL = "BTC/USDT:USDT"
LEVERAGE = 7
OPEN_TYPE = "cross"

# =========================================================
# 4) 전략 파라미터
# =========================================================
INITIAL_SEED_USDT = float(os.getenv("INITIAL_SEED_USDT", "1200.0"))
GRID_SPLIT_COUNT = 13
ATR_MULTIPLIER = 0.15
MIN_GRID_GAP = 100.0

MAX_ALLOCATION_RATE = 0.25
HEDGE_MAX_FACTOR = 2.0
ESCAPE_CUSHION_PCT = 0.02

GRID_TIMEOUT_SEC = 60.0
ESCAPE_TIMEOUT_SEC = 1.0

# =========================================================
# 5) 시스템 설정
# =========================================================
API_TIMEOUT = 30
MAX_RETRIES = 5
LOG_FILE_PATH = "data/bot.log"
STATE_FILE_PATH = "data/bot_state.json"

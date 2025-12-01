import ccxt
import os
from dotenv import load_dotenv

# 1. 설정 로드
load_dotenv()
api_key = os.getenv("BYBIT_API_KEY", "")
secret = os.getenv("BYBIT_SECRET_KEY", "")

print(f"🔑 API Key 확인: {api_key[:5]}***")

# 2. CCXT 연결 (봇과 동일한 설정)
bybit = ccxt.bybit({
    'apiKey': api_key,
    'secret': secret,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'linear',
        'adjustForTimeDifference': True
    }
})

print("📡 Bybit Ticker 연결 시도 중...")

try:
    # 3. 가격 조회 시도 (category='linear' 필수)
    ticker = bybit.fetch_ticker('BTC/USDT', params={'category': 'linear'})
    print(f"✅ 성공! 현재가: {ticker['last']}")
except Exception as e:
    print(f"❌ 실패! 진짜 에러 메시지:\n{e}")

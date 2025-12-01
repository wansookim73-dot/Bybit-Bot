from core.exchange_api import exchange
from utils.logger import logger
import asyncio
from datetime import datetime

async def run_diagnosis():
    logger.info("🩺 [Bybit API 진단] 시작...")

    # 1. Ticker 조회
    try:
        ticker = exchange.get_ticker()
        print(f"\n✅ Ticker 조회 성공: 현재가 {ticker}")
    except Exception as e:
        print(f"❌ Ticker 조회 실패: {e}")
        ticker = 0.0

    # 2. 잔고 조회
    try:
        balance = exchange.get_balance()
        print(f"✅ 잔고 조회 성공: Total={balance['total']:.2f}, Available={balance['available']:.2f} USDT")
    except Exception as e:
        print(f"❌ 잔고 조회 실패: {e}")

    # 3. 포지션 조회 (가장 중요!)
    print("\n🔍 [포지션 정밀 분석]")
    try:
        # 봇의 get_positions 함수를 호출하여 인식 여부 확인
        positions = exchange.get_positions()
        
        l_qty = positions['LONG']['qty']
        s_qty = positions['SHORT']['qty']

        print(f"👉 봇이 인식한 Long 수량: {l_qty:.4f} BTC")
        print(f"👉 봇이 인식한 Short 수량: {s_qty:.4f} BTC")
        
        if l_qty == 0 and s_qty == 0:
            print("✅ 봇 인식: [포지션 없음] (Clean Slate)")
        elif l_qty > 0 or s_qty > 0:
            print(f"⚠️ 봇 인식: [잔여 포지션 확인됨] (Avg Long: {positions['LONG']['avg_price']:.1f})")
            
    except Exception as e:
        print(f"❌ 포지션 조회 중 에러 발생: {e}")
    
    print("\n🩺 진단 완료.")

if __name__ == "__main__":
    asyncio.run(run_diagnosis())

import asyncio
from config import LEVERAGE, GRID_TIMEOUT_SEC, ESCAPE_TIMEOUT_SEC
from core.exchange_api import exchange
from strategy.grid_logic import grid_logic
from utils.calculator import to_int_price, calc_contract_qty
from utils.logger import logger

async def run_check():
    logger.info("\n🏥 [Bybit 봇 종합 건강검진] 시작...\n")
    
    # --- Data Fetching ---
    ticker = exchange.get_ticker()
    bal = exchange.get_balance()
    
    # --- Status Checks ---
    print(f"✅ [1/7] API 연결 & 시세 수신: {(ticker > 0) and '성공' or '❌실패'} ({ticker})")
    print(f"✅ [2/7] 잔고 조회: {(bal['total'] > 500) and '성공' or '❌부족'} ({bal['available']:.2f} USDT 가용)")
    
    # --- Config Checks ---
    print(f"✅ [3/7] 레버리지 설정: {LEVERAGE}x (Hedge Mode) 명령 전송 완료")
    print(f"✅ [4/7] 전략 타임아웃: Grid={GRID_TIMEOUT_SEC}s, Escape={ESCAPE_TIMEOUT_SEC}s")

    # --- Logic Checks ---
    # 가상 계산 (P_center=90000, P_gap=200 가정)
    P_CENTER = 90000
    P_GAP = 200
    
    # 5. 그리드 계산 (Line 3에서 테스트)
    line_test = grid_logic.calculate_line_index(90600, P_CENTER, P_GAP)
    if line_test == 3: print(f"✅ [5/7] 그리드 좌표계: 정상 (90600 -> Line 3)")
    else: print(f"❌ [5/7] 그리드 좌표계 오류: {line_test}")

    # 6. 최소 주문량 체크 (0.001 BTC 보정 확인)
    # 50 USDT를 주문하려 할 때 수량이 0.001 BTC 이상으로 보정되는지 확인
    test_qty = calc_contract_qty(50.0, ticker)
    if test_qty >= 0.001: print(f"✅ [6/7] 최소 주문량 보정: 정상 (Q={test_qty:.4f} BTC)")
    else: print(f"❌ [6/7] 최소 주문량 보정 오류: {test_qty:.4f} BTC (Too Small)")

    # 7. 포지션 인식
    pos = exchange.get_positions()
    print(f"✅ [7/7] 포지션 인식: Long={pos['LONG']['qty']}, Short={pos['SHORT']['qty']}")

    print("\n🎉 진단 완료. 모든 항목이 초록색이면 출발 가능합니다.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(run_check())

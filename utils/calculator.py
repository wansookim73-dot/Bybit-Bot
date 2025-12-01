import math
from decimal import Decimal, ROUND_FLOOR

MIN_QTY_BTC = Decimal("0.001")

def to_int_price(price: float) -> float:
    """
    가격 정밀도 처리
    - Bybit BTCUSDT 호가 단위(0.1 or 0.5)를 고려하되,
    - 명세서에 따라 소수점 첫째 자리까지 버림(Floor) 처리하여 안전성 확보
    """
    if price is None or price <= 0:
        return 0.0
    # 0.1 단위로 내림 (예: 90123.49 -> 90123.4)
    return float(math.floor(Decimal(str(price)) * 10) / 10.0)

def to_int_usdt(usdt_amount: float) -> float:
    """
    금액 정밀도 처리
    - USDT 금액은 정수로 내림
    """
    if usdt_amount is None: return 0.0
    return float(math.floor(usdt_amount))

def calc_contract_qty(usdt_notional: float, price: float) -> float:
    """
    주문 수량 계산
    - USDT 금액을 현재가로 나누어 BTC 수량 계산
    - 최소 주문 수량(0.001) 미만일 경우 강제 보정 (Up-sizing)
    """
    if price <= 0: return 0.0
    
    # Decimal 연산으로 정밀도 보장
    notional = Decimal(str(usdt_notional))
    px = Decimal(str(price))
    
    btc_qty = notional / px
    
    # 0.001 단위로 내림
    qty_floor = btc_qty.quantize(MIN_QTY_BTC, rounding=ROUND_FLOOR)
    
    # 최소 수량 보정
    return float(max(qty_floor, MIN_QTY_BTC))

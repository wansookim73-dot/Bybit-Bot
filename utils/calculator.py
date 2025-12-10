from __future__ import annotations

import math
from typing import Dict


# =======================================================
# 심볼별 정밀도 스펙 (예시 값)
# 실제 운영 시 Bybit BTCUSDT Perp 스펙에 맞춰 조정해야 함.
# =======================================================

SYMBOL_INFO: Dict[str, Dict[str, float]] = {
    "BTCUSDT": {
        # 수량 관련
        "min_qty": 0.001,    # 최소 수량
        "qty_step": 0.0001,  # stepSize
        # 가격 관련
        "tick_size": 0.5,    # tickSize (예시) - 실 운영 시 실제 스펙으로 교체
    },
}


# =======================================================
# 가격 / 금액 보조 함수 (레거시 호환)
# =======================================================

def to_int_price(price: float) -> int:
    """
    (레거시) Bybit BTCUSDT용: 가격을 달러 정수로 맞춘다.
    (예: 92894.7 -> 92894)

    v10.1 정밀도 규칙에서는 tickSize 단위로 floor하는
    price_floor_to_tick() 사용을 우선 권장한다.
    """
    if price is None:
        return 0
    return int(price)


def to_int_usdt(amount: float) -> int:
    """
    USDT 금액을 정수 달러로 맞춘다.
    (예: 1499.83 -> 1499)
    """
    if amount is None:
        return 0
    return int(amount)


def calc_dca_price(p_center: float, p_gap: float, line: int) -> int:
    """
    그리드 기준선(p_center), 간격(p_gap), 라인 인덱스(line)로
    실제 주문 가격을 계산해서 정수 달러로 변환.
    예: line = -3이면 p_center + gap * (-3)

    v10.1에서는 필요 시 price_floor_to_tick()을 통해
    tickSize 정밀도까지 맞춘 뒤 int(...)를 취할 수 있다.
    """
    raw_price = p_center + p_gap * line
    return to_int_price(raw_price)


# =======================================================
# v10.1 정밀도 규칙: price / qty floor
# =======================================================

def price_floor_to_tick(
    price: float,
    tick_size: float | None = None,
    *,
    symbol: str = "BTCUSDT",
) -> float:
    """
    거래소 tickSize 에 맞춰 항상 아래로 내림(floor) 처리한 주문 가격을 리턴.

    - price     : 원시 가격
    - tick_size : 명시되면 이 값을 사용, None이면 SYMBOL_INFO[symbol]["tick_size"] 사용
    - symbol    : 심볼 (기본 "BTCUSDT")

    반환값:
    - tickSize 정렬된 가격 (float)
    """
    if price is None:
        return 0.0

    try:
        price = float(price)
    except (TypeError, ValueError):
        return 0.0

    if tick_size is None:
        info = SYMBOL_INFO.get(symbol, {})
        tick_size = float(info.get("tick_size", 0.0))

    try:
        tick_size = float(tick_size)
    except (TypeError, ValueError):
        tick_size = 0.0

    if price <= 0.0 or tick_size <= 0.0:
        return 0.0

    steps = math.floor(price / tick_size)
    return steps * tick_size


def qty_floor_to_step(
    qty: float,
    step: float,
    min_qty: float,
) -> float:
    """
    qty 를 stepSize 에 맞춰 항상 아래로 내림(floor) 처리한 뒤,
    min_qty 미만이면 0.0 을 리턴.

    - qty     : 원하는 수량 (BTC)
    - step    : stepSize (예: 0.0001)
    - min_qty : minQty  (예: 0.001; BTCUSDT 기준)

    규칙:
      1) stepSize 에 맞춰 아래로 floor
      2) 최종 수량 < min_qty 이면 0.0 반환
    """
    try:
        qty = float(qty)
        step = float(step)
        min_qty = float(min_qty)
    except (TypeError, ValueError):
        return 0.0

    if qty <= 0.0 or step <= 0.0 or min_qty <= 0.0:
        return 0.0

    steps = math.floor(qty / step)
    floored_qty = steps * step

    # float 오차 방지용 작은 epsilon 적용
    if floored_qty + 1e-12 < min_qty:
        return 0.0

    return floored_qty


# =======================================================
# v10.1 기준 calc_contract_qty(usdt_amount → qty)
# =======================================================

def calc_contract_qty(
    usdt_amount: float,
    price: float,
    min_qty: float | None = None,
    qty_step: float | None = None,
    *,
    symbol: str = "BTCUSDT",
    dry_run: bool = False,
) -> float:
    """
    v10.1 기준 수량 계산 함수.

    - usdt_amount: USDT 기준 주문 금액 (예: 100 USDT)
    - price      : 주문 가격
    - min_qty    : 최소 수량 (BTC), None 이면 SYMBOL_INFO[symbol]["min_qty"]
    - qty_step   : 수량 스텝 (BTC), None 이면 SYMBOL_INFO[symbol]["qty_step"]
    - symbol     : 심볼 (기본 "BTCUSDT")
    - dry_run    : DRY_RUN 여부 (동일 로직, 의미상 플래그용)

    정밀도/회계 규칙 (v10.1 명세 1:1):

      1) usdt_amount <= 0 또는 price <= 0 이면 0.0 반환 (주문 안 냄)
      2) raw_qty   = usdt_amount / price
      3) floored   = qty_floor_to_step(raw_qty, qty_step, min_qty)
      4) floored 를 최종 qty 로 사용
         - floored < min_qty 이면 qty_floor_to_step 에서 0.0 반환

    core/exchange_api.py 및 core/order_manager.py 에서
    이 함수 하나만 공통으로 사용하도록 설계되어 있어야 한다.
    """
    try:
        usdt_amount = float(usdt_amount)
        price = float(price)
    except (TypeError, ValueError):
        return 0.0

    if usdt_amount <= 0.0 or price <= 0.0:
        return 0.0

    # 심볼 기준 기본 min_qty / qty_step 채우기
    info = SYMBOL_INFO.get(symbol, {})
    if min_qty is None:
        min_qty = float(info.get("min_qty", 0.0))
    if qty_step is None:
        qty_step = float(info.get("qty_step", 0.0))

    try:
        min_qty = float(min_qty)
        qty_step = float(qty_step)
    except (TypeError, ValueError):
        return 0.0

    if min_qty <= 0.0 or qty_step <= 0.0:
        # 스펙 상 여기까지 오면 안 됨. 안전하게 0.0 반환.
        return 0.0

    # 1) USDT → BTC (raw)
    raw_qty = usdt_amount / price

    # 2) stepSize + minQty 규칙으로 floor
    return qty_floor_to_step(raw_qty, qty_step, min_qty)

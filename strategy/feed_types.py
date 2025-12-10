from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

from .state_model import BotState


# ------------------------------
# OrderInfo
# ------------------------------

@dataclass
class OrderInfo:
    """
    현재 활성 주문 스냅샷 (Bybit → 내부 표준 형태).
    """
    order_id: str
    side: str              # "BUY" or "SELL"
    price: float
    qty: float
    filled_qty: float
    reduce_only: bool
    order_type: str        # "Limit", "Market", ...
    time_in_force: str     # "PostOnly", "GTC", ...
    tag: Optional[str]     # 예: "W4_GRID_A_-3_BUY", "MB_SLICE_ESCAPE", ...
    created_ts: float      # epoch seconds


# ------------------------------
# StrategyFeed
# ------------------------------

@dataclass
class StrategyFeed:
    """
    WaveBot v10.1 공통 입력 Feed.

    - GridLogic
    - EscapeLogic
    - OrderManager
    - RiskManager
    - main_v10

    위 모든 컴포넌트는 전역 mutable 상태에 의존하지 않고,
    이 StrategyFeed 인스턴스를 통해 필요한 정보를 주입받아야 한다.
    """
    # 현재 BTCUSDT 기준 가격
    price: float

    # 4H ATR(42) 값
    # - Grid p_gap 산출
    # - ESCAPE Vol-Spike 조건 등에 사용
    atr_4h_42: float

    # state_model.py 에 정의된 BotState (단일 진실 원천)
    state: BotState

    # 현재 활성화된 Bybit 주문 리스트 (내부 표준 OrderInfo 형태)
    open_orders: List[OrderInfo]

    # 계정 전체 PnL (USDT 기준, long/short/hedge 포함)
    pnl_total: float

    # pnl_total / Total_Balance_snap
    # 일반적으로 state.total_balance(스냅샷) 를 분모로 사용
    pnl_total_pct: float

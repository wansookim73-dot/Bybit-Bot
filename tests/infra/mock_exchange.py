from dataclasses import dataclass
from typing import Dict, List

from .mock_clock import MockClock


@dataclass
class Order:
    """아주 단순한 주문 객체."""
    id: int
    symbol: str
    side: str          # "buy" / "sell"
    price: float
    qty: float
    mode: str          # "A" (Mode A) 또는 "B" (Mode B)
    tag: str
    created_ts: float  # 주문이 만들어진 시각 (초 단위)


@dataclass
class Trade:
    """아주 단순한 체결 객체."""
    order_id: int
    symbol: str
    side: str
    price: float
    qty: float
    kind: str          # "limit" / "market"
    ts: float          # 체결 시각 (초 단위)


class MockExchange:
    """
    테스트용 모의 거래소.

    - 주문/체결만 아주 단순하게 저장한다.
    - price/qty 체결 로직은 넣지 않고,
      지금은 Mode A / Mode B 시간 기반 동작만 흉내낸다.
    """

    def __init__(self, clock: MockClock):
        self._clock = clock
        self._mark_price: float = 0.0
        self._orders: Dict[int, Order] = {}
        self._trades: List[Trade] = []
        self._next_order_id: int = 1

    # ------------------------------------------------------------------
    # 기초 유틸
    # ------------------------------------------------------------------
    def reset_to_flat(self) -> None:
        """모든 주문/체결 기록을 초기화한다."""
        self._orders.clear()
        self._trades.clear()
        self._mark_price = 0.0

    def set_mark_price(self, price: float) -> None:
        """현재 마크 가격을 설정한다."""
        self._mark_price = float(price)

    def get_mark_price(self) -> float:
        return self._mark_price

    def get_all_orders(self) -> List[Order]:
        """현재 살아있는 모든 주문 리스트."""
        return list(self._orders.values())

    def get_all_trades(self) -> List[Trade]:
        """지금까지 발생한 모든 체결 리스트."""
        return list(self._trades)

    def _next_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    # ------------------------------------------------------------------
    # 주문 발행 / 취소
    # ------------------------------------------------------------------
    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        mode: str,
        tag: str = "",
    ) -> Order:
        """
        단순 LIMIT 주문을 한 개 넣는다.
        mode: "A" 또는 "B"
        """
        oid = self._next_id()
        now = self._clock.now()

        order = Order(
            id=oid,
            symbol=symbol,
            side=side.lower(),
            price=float(price),
            qty=float(qty),
            mode=mode.upper(),
            tag=tag,
            created_ts=now,
        )
        self._orders[oid] = order
        return order

    def cancel_order(self, order_id: int) -> None:
        """주문 한 개를 취소한다 (없으면 무시)."""
        self._orders.pop(int(order_id), None)

    # ------------------------------------------------------------------
    # Mode B 전용 helper
    # ------------------------------------------------------------------
    def submit_limit_order_mode_b(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        tag: str = "",
    ) -> int:
        """
        Mode B(Taker Fallback) 전용 편의 함수.

        - 일단 LIMIT 주문으로 넣고
        - 이후 tick()에서 1초가 지나면 시장가로 강제 체결 처리.
        """
        order = self.place_limit_order(
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            mode="B",
            tag=tag,
        )
        return order.id

    # ------------------------------------------------------------------
    # 시간 진행(tick) 처리
    # ------------------------------------------------------------------
    def tick(self) -> None:
        """
        현재 시각(self._clock.now())를 기준으로
        Mode A / Mode B 주문의 시간 조건을 처리한다.

        - Mode B: 1초 이상 경과하면 남은 잔량 전부 market 체결
        - Mode A: 60초 이상 경과하면 같은 price/qty로 새 limit 주문 재발행
        """
        now = self._clock.now()

        # 순회 중에 딕셔너리를 수정해야 하므로 values()를 리스트로 복사
        for order in list(self._orders.values()):
            age = now - order.created_ts

            # -----------------------
            # Mode B: 1초 후 Market
            # -----------------------
            if order.mode == "B":
                if age >= 1.0:
                    # 주문을 삭제하고, 남은 qty 전부를 market 체결로 기록
                    self._orders.pop(order.id, None)

                    trade = Trade(
                        order_id=order.id,
                        symbol=order.symbol,
                        side=order.side,
                        price=self._mark_price,
                        qty=order.qty,
                        kind="market",
                        ts=now,
                    )
                    self._trades.append(trade)

            # -----------------------
            # Mode A: 60초 후 재발주
            # -----------------------
            elif order.mode == "A":
                if age >= 60.0:
                    # 기존 주문 취소
                    self._orders.pop(order.id, None)

                    # 같은 price/qty/tag로 새 주문을 발행 (id와 created_ts만 새로)
                    new_id = self._next_id()
                    new_order = Order(
                        id=new_id,
                        symbol=order.symbol,
                        side=order.side,
                        price=order.price,
                        qty=order.qty,
                        mode=order.mode,
                        tag=order.tag,
                        created_ts=now,
                    )
                    self._orders[new_id] = new_order

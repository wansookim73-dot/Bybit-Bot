# tests/verify/mock_exchange.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .execution_journal import ExecutionJournal
from .fill_engine import FillEngineFullFill


def _pos_template() -> Dict[str, Dict[str, float]]:
    return {
        "LONG": {"qty": 0.0, "avg_price": 0.0},
        "SHORT": {"qty": 0.0, "avg_price": 0.0},
    }


class MockExchange:
    """
    L0 Mock Exchange:
    - 주문 접수/취소
    - 전량 체결
    - 헤지 포지션(LONG/SHORT) 갱신
    """

    def __init__(
        self,
        *,
        journal: ExecutionJournal,
        fill_engine: Optional[FillEngineFullFill] = None,
        init_positions: Optional[Dict[str, Dict[str, float]]] = None,
        init_balance_total: float = 10_000.0,
    ) -> None:
        self.journal = journal
        self.fill_engine = fill_engine or FillEngineFullFill()

        self._positions = _pos_template()
        if init_positions:
            for k in ("LONG", "SHORT"):
                self._positions[k]["qty"] = float(init_positions.get(k, {}).get("qty", 0.0))
                self._positions[k]["avg_price"] = float(init_positions.get(k, {}).get("avg_price", 0.0))

        self._open_orders: List[Dict[str, Any]] = []
        self._id_counter = 0

        self._bal_total = float(init_balance_total)
        self._bal_free = float(init_balance_total)

    def get_positions(self) -> Dict[str, Dict[str, float]]:
        return {
            "LONG": {"qty": float(self._positions["LONG"]["qty"]), "avg_price": float(self._positions["LONG"]["avg_price"])},
            "SHORT": {"qty": float(self._positions["SHORT"]["qty"]), "avg_price": float(self._positions["SHORT"]["avg_price"])},
        }

    def get_open_orders(self) -> List[Dict[str, Any]]:
        # NEW만 반환
        return [dict(o) for o in self._open_orders if o.get("status") == "NEW"]

    def get_balance(self) -> Dict[str, float]:
        return {"total": float(self._bal_total), "free": float(self._bal_free)}

    def create_order(self, order: Dict[str, Any], ts: int) -> str:
        self._validate_order(order)

        o = dict(order)
        if not o.get("order_id") or o.get("order_id") == "AUTO":
            self._id_counter += 1
            o["order_id"] = f"AUTO_{self._id_counter:06d}"

        o["status"] = "NEW"
        o["created_ts"] = int(ts)

        self._open_orders.append(o)
        self.journal.add(int(ts), "ORDER", {"order": dict(o)})
        return str(o["order_id"])

    def cancel_order(self, order_id: str, ts: int) -> bool:
        ok = False
        for o in self._open_orders:
            if o.get("order_id") == order_id and o.get("status") == "NEW":
                o["status"] = "CANCELED"
                ok = True
                self.journal.add(int(ts), "CANCEL", {"order_id": order_id})
                break
        return ok

    def tick(self, market_price: float, ts: int) -> List[Dict[str, Any]]:
        fills: List[Dict[str, Any]] = []
        for o in list(self._open_orders):
            if o.get("status") != "NEW":
                continue

            fill = self.fill_engine.try_fill(o, float(market_price), int(ts))
            if not fill:
                continue

            o["status"] = "FILLED"
            fills.append(fill)
            self.journal.add(int(ts), "FILL", {"fill": dict(fill)})

            self._apply_fill_to_positions(fill)

        return fills

    @staticmethod
    def _validate_order(order: Dict[str, Any]) -> None:
        required = ("symbol", "side", "order_type", "qty")
        for k in required:
            if k not in order:
                raise ValueError(f"MockExchange order missing required key: {k}")

        if str(order.get("side")) not in ("Buy", "Sell"):
            raise ValueError(f"MockExchange invalid side: {order.get('side')}")

        if str(order.get("order_type")) not in ("Limit", "Market"):
            raise ValueError(f"MockExchange invalid order_type: {order.get('order_type')}")

        qty = float(order.get("qty") or 0.0)
        if qty <= 0.0:
            raise ValueError(f"MockExchange invalid qty: {qty}")

        # hedge 모드 전제: positionIdx는 1/2 이어야 한다 (TP/ENTRY 모두)
        pos_idx = int(order.get("positionIdx") or 0)
        if pos_idx not in (1, 2):
            raise ValueError(f"MockExchange invalid positionIdx: {pos_idx}")

    def _apply_fill_to_positions(self, fill: Dict[str, Any]) -> None:
        qty = float(fill.get("filled_qty") or 0.0)
        price = float(fill.get("fill_price") or 0.0)
        reduce_only = bool(fill.get("reduce_only"))
        pos_idx = int(fill.get("positionIdx") or 0)
        side = str(fill.get("side") or "")

        if qty <= 0.0:
            return

        # reduceOnly/positionIdx/side 정합성 체크 (L0에서 버그 조기 탐지)
        if pos_idx == 1:
            if reduce_only and side != "Sell":
                raise AssertionError("FILL_SEMANTICS_INVALID: LONG reduceOnly must be Sell")
            if (not reduce_only) and side != "Buy":
                raise AssertionError("FILL_SEMANTICS_INVALID: LONG entry must be Buy")
        elif pos_idx == 2:
            if reduce_only and side != "Buy":
                raise AssertionError("FILL_SEMANTICS_INVALID: SHORT reduceOnly must be Buy")
            if (not reduce_only) and side != "Sell":
                raise AssertionError("FILL_SEMANTICS_INVALID: SHORT entry must be Sell")

        if pos_idx == 1:
            if reduce_only:
                self._reduce_position("LONG", qty)
            else:
                self._increase_position("LONG", qty, price)
        elif pos_idx == 2:
            if reduce_only:
                self._reduce_position("SHORT", qty)
            else:
                self._increase_position("SHORT", qty, price)

    def _increase_position(self, key: str, add_qty: float, add_price: float) -> None:
        cur_qty = float(self._positions[key]["qty"])
        cur_avg = float(self._positions[key]["avg_price"])
        new_qty = cur_qty + float(add_qty)

        if new_qty <= 0.0:
            self._positions[key]["qty"] = 0.0
            self._positions[key]["avg_price"] = 0.0
            return

        if cur_qty <= 0.0:
            new_avg = float(add_price)
        else:
            new_avg = (cur_avg * cur_qty + float(add_price) * float(add_qty)) / new_qty

        self._positions[key]["qty"] = float(new_qty)
        self._positions[key]["avg_price"] = float(new_avg)

    def _reduce_position(self, key: str, red_qty: float) -> None:
        cur_qty = float(self._positions[key]["qty"])
        new_qty = cur_qty - float(red_qty)
        if new_qty <= 0.0:
            self._positions[key]["qty"] = 0.0
            self._positions[key]["avg_price"] = 0.0
        else:
            self._positions[key]["qty"] = float(new_qty)

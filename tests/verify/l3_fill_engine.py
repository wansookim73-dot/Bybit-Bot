from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class FillEvent:
    """
    강제 체결 이벤트 (order_id 기준).
    filled_qty만큼 해당 오더를 체결 처리한다.
    """
    order_id: str
    filled_qty: float


class L3FillEngine:
    """
    단순 Fill 엔진:
    - tick.fills에 명시된 order_id만 체결 처리
    - limit/market 구분 없이 '주문 qty 감소 + 포지션 qty 반영'만 수행
    """

    def apply_fills(
        self,
        *,
        open_orders: List[Dict[str, Any]],
        positions: Dict[str, Dict[str, float]],
        fills: List[FillEvent],
    ) -> None:
        by_id = {str(o.get("order_id", "")): o for o in (open_orders or [])}

        for f in fills:
            oid = str(f.order_id or "")
            if not oid:
                continue
            o = by_id.get(oid)
            if not o:
                continue

            qty_left = float(o.get("qty", 0.0) or 0.0)
            fill_qty = float(f.filled_qty or 0.0)
            if qty_left <= 0.0 or fill_qty <= 0.0:
                continue

            filled = min(qty_left, fill_qty)
            side = str(o.get("side", "")).upper()
            reduce_only = bool(o.get("reduce_only", False))

            # 포지션 반영 (단순 qty 기반)
            if side == "BUY":
                if reduce_only:
                    positions["SHORT"]["qty"] = max(0.0, float(positions["SHORT"]["qty"]) - filled)
                else:
                    positions["LONG"]["qty"] = float(positions["LONG"]["qty"]) + filled
            elif side == "SELL":
                if reduce_only:
                    positions["LONG"]["qty"] = max(0.0, float(positions["LONG"]["qty"]) - filled)
                else:
                    positions["SHORT"]["qty"] = float(positions["SHORT"]["qty"]) + filled

            # 오더 잔량 감소
            o["qty"] = qty_left - filled

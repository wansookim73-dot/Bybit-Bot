# tests/verify/fill_engine.py
from __future__ import annotations

from typing import Any, Dict, Optional


class FillEngineFullFill:
    """
    L0: 전량 체결 only
    - Market: 즉시 체결 (runner에서 정책상 maker_only면 금지)
    - Limit:
        Buy  : market_price <= limit_price  -> fill at limit
        Sell : market_price >= limit_price  -> fill at limit
    """

    def try_fill(self, order: Dict[str, Any], market_price: float, ts: int) -> Optional[Dict[str, Any]]:
        if order.get("status") != "NEW":
            return None

        order_type = str(order.get("order_type") or "")
        side = str(order.get("side") or "")
        qty = float(order.get("qty") or 0.0)
        if qty <= 0.0:
            return None

        if order_type == "Market":
            return self._mk_fill(order, qty, float(market_price), ts)

        if order_type == "Limit":
            limit_price = order.get("price")
            if limit_price is None:
                return None
            lp = float(limit_price)

            if side == "Buy" and float(market_price) <= lp:
                return self._mk_fill(order, qty, lp, ts)
            if side == "Sell" and float(market_price) >= lp:
                return self._mk_fill(order, qty, lp, ts)

        return None

    @staticmethod
    def _mk_fill(order: Dict[str, Any], filled_qty: float, fill_price: float, ts: int) -> Dict[str, Any]:
        meta = order.get("meta") or {}
        return {
            "order_id": order.get("order_id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "order_type": order.get("order_type"),
            "filled_qty": float(filled_qty),
            "fill_price": float(fill_price),
            "ts": int(ts),
            "reduce_only": bool(order.get("reduce_only")),
            "positionIdx": int(order.get("positionIdx") or 0),
            "meta": {
                "reason": meta.get("reason"),
                "scenario": meta.get("scenario"),
                "tag": meta.get("tag"),
            },
        }

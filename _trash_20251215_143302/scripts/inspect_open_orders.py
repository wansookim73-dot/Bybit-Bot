# scripts/inspect_open_orders.py
import os, sys
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.exchange_api import exchange

def pick_reduce_only(order: dict):
    info = order.get("info") or {}
    # 1) ccxt top-level
    if "reduceOnly" in order:
        return order.get("reduceOnly")
    # 2) bybit raw fields
    if "reduceOnly" in info:
        return info.get("reduceOnly")
    if "isReduceOnly" in info:
        return info.get("isReduceOnly")
    return None

def pick_position_idx(order: dict):
    info = order.get("info") or {}
    if "positionIdx" in info:
        return info.get("positionIdx")
    # sometimes exchanges return other keys
    if "position_idx" in info:
        return info.get("position_idx")
    if "positionIdx" in order:
        return order.get("positionIdx")
    return None

def pick_link_id(order: dict):
    info = order.get("info") or {}
    # bybit: orderLinkId
    if "orderLinkId" in info:
        return info.get("orderLinkId")
    # ccxt unified naming on some exchanges
    if "clientOrderId" in info:
        return info.get("clientOrderId")
    if "clientOrderId" in order:
        return order.get("clientOrderId")
    return None

orders = exchange.get_open_orders()
print("open_orders =", len(orders))

for o in orders:
    info = o.get("info") or {}
    print(
        "id=%s side=%s price=%s amount=%s filled=%s posIdx=%s reduceOnly=%s linkId=%s rawKeys=%s"
        % (
            o.get("id"),
            o.get("side"),
            o.get("price"),
            o.get("amount"),
            o.get("filled"),
            pick_position_idx(o),
            pick_reduce_only(o),
            pick_link_id(o),
            ",".join(sorted([k for k in info.keys() if k in ("reduceOnly", "isReduceOnly", "positionIdx", "orderLinkId", "clientOrderId")])),
        )
    )

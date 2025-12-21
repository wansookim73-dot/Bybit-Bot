# tests/verify/scenarios_l0_spec.py
from __future__ import annotations

from typing import Any, Dict, List

# L0 고정 스펙
EPS_QTY = 1e-9
EPS_PRICE = 1e-6
SYMBOL = "BTCUSDT"


def mk_limit_order(
    *,
    order_id: str,
    symbol: str,
    side: str,          # "Buy" | "Sell"
    qty: float,
    price: float,
    reduce_only: bool,
    positionIdx: int,   # 1=LONG, 2=SHORT (hedge)
    post_only: bool,
    tif: str,
    reason: str,        # "TP" | "ENTRY"
    scenario: str,
    tag: str = "VERIFY",
) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "order_type": "Limit",
        "qty": float(qty),
        "price": float(price),
        "reduce_only": bool(reduce_only),
        "positionIdx": int(positionIdx),
        "post_only": bool(post_only),
        "tif": str(tif),
        "meta": {"reason": reason, "scenario": scenario, "tag": tag},
    }


def mk_fill(
    *,
    order_id: str,
    symbol: str,
    side: str,
    order_type: str,
    filled_qty: float,
    fill_price: float,
    ts: int,
    reduce_only: bool,
    positionIdx: int,
    reason: str,
    scenario: str,
    tag: str = "VERIFY",
) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "filled_qty": float(filled_qty),
        "fill_price": float(fill_price),
        "ts": int(ts),
        "reduce_only": bool(reduce_only),
        "positionIdx": int(positionIdx),
        "meta": {"reason": reason, "scenario": scenario, "tag": tag},
    }


# -------------------------
# S1: TP ReduceOnly — LONG 전량 청산
# -------------------------
S1_ORDERS: List[Dict[str, Any]] = [
    mk_limit_order(
        order_id="S1_TP_LONG_001",
        symbol=SYMBOL,
        side="Sell",
        qty=0.010,
        price=101.0,
        reduce_only=True,
        positionIdx=1,
        post_only=False,
        tif="GTC",
        reason="TP",
        scenario="S1",
    )
]

S1_FILLS: List[Dict[str, Any]] = [
    mk_fill(
        order_id="S1_TP_LONG_001",
        symbol=SYMBOL,
        side="Sell",
        order_type="Limit",
        filled_qty=0.010,
        fill_price=101.0,
        ts=2,
        reduce_only=True,
        positionIdx=1,
        reason="TP",
        scenario="S1",
    )
]

S1_FINAL = {
    "positions": {
        "LONG": {"qty": 0.000, "avg_price": 0.0},
        "SHORT": {"qty": 0.000, "avg_price": 0.0},
    },
    "open_orders": [],
    "escape_active": False,
}

S1 = {
    "id": "S1",
    "name": "TP ReduceOnly — LONG 전량 청산",
    "init_state": {
        "mode": "NORMAL",
        "maker_only": False,
        "escape_active": False,
        "positions": {
            "LONG": {"qty": 0.010, "avg_price": 100.0},
            "SHORT": {"qty": 0.000, "avg_price": 0.0},
        },
        "open_orders": [],
    },
    "price_seq": [
        {"ts": 0, "price": 100.0},
        {"ts": 1, "price": 100.6},
        {"ts": 2, "price": 101.2},
    ],
    "plan": {"create_orders_at_ts": {2: S1_ORDERS}},
    "expect_orders": S1_ORDERS,
    "expect_fills": S1_FILLS,
    "expect_final": S1_FINAL,
}


# -------------------------
# S2: TP ReduceOnly — SHORT 전량 청산
# -------------------------
S2_ORDERS: List[Dict[str, Any]] = [
    mk_limit_order(
        order_id="S2_TP_SHORT_001",
        symbol=SYMBOL,
        side="Buy",
        qty=0.020,
        price=198.5,
        reduce_only=True,
        positionIdx=2,
        post_only=False,
        tif="GTC",
        reason="TP",
        scenario="S2",
    )
]

S2_FILLS: List[Dict[str, Any]] = [
    mk_fill(
        order_id="S2_TP_SHORT_001",
        symbol=SYMBOL,
        side="Buy",
        order_type="Limit",
        filled_qty=0.020,
        fill_price=198.5,
        ts=2,
        reduce_only=True,
        positionIdx=2,
        reason="TP",
        scenario="S2",
    )
]

S2_FINAL = {
    "positions": {
        "LONG": {"qty": 0.000, "avg_price": 0.0},
        "SHORT": {"qty": 0.000, "avg_price": 0.0},
    },
    "open_orders": [],
    "escape_active": False,
}

S2 = {
    "id": "S2",
    "name": "TP ReduceOnly — SHORT 전량 청산",
    "init_state": {
        "mode": "NORMAL",
        "maker_only": False,
        "escape_active": False,
        "positions": {
            "LONG": {"qty": 0.000, "avg_price": 0.0},
            "SHORT": {"qty": 0.020, "avg_price": 200.0},
        },
        "open_orders": [],
    },
    "price_seq": [
        {"ts": 0, "price": 200.0},
        {"ts": 1, "price": 199.0},
        {"ts": 2, "price": 198.0},
    ],
    "plan": {"create_orders_at_ts": {2: S2_ORDERS}},
    "expect_orders": S2_ORDERS,
    "expect_fills": S2_FILLS,
    "expect_final": S2_FINAL,
}


# -------------------------
# S3: Escape 활성 시 ENTRY/TP 차단 (주문 0)
# -------------------------
S3_FINAL = {
    "positions": {
        "LONG": {"qty": 0.015, "avg_price": 150.0},
        "SHORT": {"qty": 0.000, "avg_price": 0.0},
    },
    "open_orders": [],
    "escape_active": True,
}

S3 = {
    "id": "S3",
    "name": "Escape 활성 시 ENTRY/TP 차단 (주문 0)",
    "init_state": {
        "mode": "NORMAL",
        "maker_only": False,
        "escape_active": True,
        "escape_reason": "VERIFY_FORCE_ESCAPE",
        "positions": {
            "LONG": {"qty": 0.015, "avg_price": 150.0},
            "SHORT": {"qty": 0.000, "avg_price": 0.0},
        },
        "open_orders": [],
    },
    "price_seq": [
        {"ts": 0, "price": 150.0},
        {"ts": 1, "price": 149.0},
        {"ts": 2, "price": 151.5},
    ],
    "plan": {"create_orders_at_ts": {}},
    "expect_orders": [],
    "expect_fills": [],
    "expect_final": S3_FINAL,
}


# -------------------------
# S4: Maker Only — Market 금지 + post_only 강제
# -------------------------
S4_ORDERS: List[Dict[str, Any]] = [
    mk_limit_order(
        order_id="S4_ENTRY_SHORT_001",
        symbol=SYMBOL,
        side="Sell",
        qty=0.010,
        price=101.5,
        reduce_only=False,
        positionIdx=2,
        post_only=True,
        tif="GTC",
        reason="ENTRY",
        scenario="S4",
    )
]

S4_FINAL = {
    "positions": {
        "LONG": {"qty": 0.000, "avg_price": 0.0},
        "SHORT": {"qty": 0.000, "avg_price": 0.0},
    },
    "open_orders": S4_ORDERS,
    "escape_active": False,
}

S4 = {
    "id": "S4",
    "name": "Maker Only에서 Market 금지 + post_only 강제",
    "init_state": {
        "mode": "A",
        "maker_only": True,
        "escape_active": False,
        "positions": {
            "LONG": {"qty": 0.000, "avg_price": 0.0},
            "SHORT": {"qty": 0.000, "avg_price": 0.0},
        },
        "open_orders": [],
    },
    "price_seq": [
        {"ts": 0, "price": 100.0},
        {"ts": 1, "price": 100.9},
        {"ts": 2, "price": 101.1},
        {"ts": 3, "price": 100.2},
    ],
    "plan": {"create_orders_at_ts": {0: S4_ORDERS}},
    "expect_orders": S4_ORDERS,
    "expect_fills": [],
    "expect_final": S4_FINAL,
}


SCENARIOS = {"S1": S1, "S2": S2, "S3": S3, "S4": S4}

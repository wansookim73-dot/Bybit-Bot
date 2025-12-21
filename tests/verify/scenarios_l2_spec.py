from __future__ import annotations

# L2: OrderManager ↔ Exchange 호출 경계 검증
# - "필수 호출이 실제로 존재하는지" 중심(내부 유지보수 주문이 추가되어도 덜 깨지게)
# - 각 시나리오는 고유한 price를 사용해 대상 주문을 식별한다.

SCENARIOS = {
    "OM1_LONG_ENTRY_LIMIT": {
        "desc": "LONG entry limit emits non-reduceOnly and position_idx=1 (no MARKET).",
        "price": 50000.0,
        "decision": {
            "mode": "NORMAL",
            "entries": [
                {
                    "side": "BUY",
                    "price": 50111.0,   # unique
                    "qty": 0.001,
                    "grid_index": -1,
                    "wave_id": 7,
                    "reduce_only": False,
                    "position_idx": 1,
                    "step_cost": 2,
                }
            ],
            "replaces": [],
            "cancels": [],
            "state_updates": {},
        },
        "feed": {
            "open_orders": [],
        },
        "expect": {
            "must_have_order_at_price": 50111.0,
            "must_have_reduce_only": False,
            "must_have_position_idx": 1,
            "forbid_market": True,
        },
    },

    "OM2_SHORT_ENTRY_LIMIT": {
        "desc": "SHORT entry limit emits non-reduceOnly and position_idx=2 (no MARKET).",
        "price": 50000.0,
        "decision": {
            "mode": "NORMAL",
            "entries": [
                {
                    "side": "SELL",
                    "price": 49877.0,   # unique
                    "qty": 0.001,
                    "grid_index": +1,
                    "wave_id": 7,
                    "reduce_only": False,
                    "position_idx": 2,
                    "step_cost": 2,
                }
            ],
            "replaces": [],
            "cancels": [],
            "state_updates": {},
        },
        "feed": {
            "open_orders": [],
        },
        "expect": {
            "must_have_order_at_price": 49877.0,
            "must_have_reduce_only": False,
            "must_have_position_idx": 2,
            "forbid_market": True,
        },
    },

    "OM3_TP_LONG_REDUCEONLY": {
        "desc": "TP for LONG must be reduceOnly=True and position_idx=1 (no MARKET).",
        "price": 50000.0,
        "decision": {
            "mode": "NORMAL",
            "entries": [
                {
                    "side": "SELL",
                    "price": 50999.0,   # unique
                    "qty": 0.001,
                    "grid_index": +2,
                    "wave_id": 7,
                    "reduce_only": True,
                    "position_idx": 1,
                    "step_cost": 1,
                }
            ],
            "replaces": [],
            "cancels": [],
            "state_updates": {},
        },
        "feed": {
            "open_orders": [],
        },
        "expect": {
            "must_have_order_at_price": 50999.0,
            "must_have_reduce_only": True,
            "must_have_position_idx": 1,
            "forbid_market": True,
        },
    },

    "OM4_CANCEL_ONE": {
        "desc": "Cancel must call cancel_order(order_id) exactly for the target.",
        "price": 50000.0,
        "decision": {
            "mode": "NORMAL",
            "entries": [],
            "replaces": [],
            "cancels": [
                {
                    "order_id": "OID_CANCEL_1",
                    "side": "BUY",
                    "price": 49000.0,
                    "qty": 0.001,
                    "filled_qty": 0.0,
                    "reduce_only": False,
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "tag": "VERIFY_CANCEL_TAG",
                    "created_ts": 1700000000.0,
                }
            ],
            "state_updates": {},
        },
        "feed": {
            "open_orders": [
                {
                    "order_id": "OID_CANCEL_1",
                    "side": "BUY",
                    "price": 49000.0,
                    "qty": 0.001,
                    "filled_qty": 0.0,
                    "reduce_only": False,
                    "order_type": "Limit",
                    "time_in_force": "PostOnly",
                    "tag": "VERIFY_CANCEL_TAG",
                    "created_ts": 1700000000.0,
                }
            ],
        },
        "expect": {
            "must_cancel_order_id": "OID_CANCEL_1",
            "forbid_market": True,
        },
    },
}

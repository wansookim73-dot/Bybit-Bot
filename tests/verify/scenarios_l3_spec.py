from __future__ import annotations

from typing import Any, Dict, List
from strategy.state_model import BotState


def base_state(wave_id: int = 1) -> BotState:
    st = BotState(
        mode="NORMAL",
        wave_id=wave_id,
        p_center=50000.0,
        p_gap=300.0,
        atr_value=300.0,
        long_seed_total_effective=120.0,
        short_seed_total_effective=120.0,
        unit_seed_long=10.0,
        unit_seed_short=10.0,
        k_long=0,
        k_short=0,
        total_balance_snap=1200.0,
        total_balance=1200.0,
        free_balance=1200.0,
    )
    st.line_memory_long = {}
    st.line_memory_short = {}
    st.dca_used_indices = []
    st.dca_last_idx = 10**9
    st.dca_last_ts = 0.0
    st.dca_last_price = 0.0
    return st


def _ovr_entry(
    *,
    wave_id: int,
    grid_index: int,
    side: str,
    price: float,
    qty: float,
    position_idx: int,
    reduce_only: bool,
    step_cost: int,
) -> Dict[str, Any]:
    return dict(
        wave_id=wave_id,
        grid_index=grid_index,
        side=side,
        price=price,
        qty=qty,
        mode="A",
        position_idx=position_idx,
        reduce_only=reduce_only,
        step_cost=step_cost,
    )


SCENARIOS: Dict[str, Dict[str, Any]] = {
    # A) Start-up (기존)
    "L3_A1_STARTUP_ENTRY": {
        "initial_state": base_state(1),
        "initial_positions": {"LONG": 0.0, "SHORT": 0.0},
        "initial_open_orders": [],
        "atr_4h_42": 300.0,
        "ticks": [
            {"t": 0, "price": 50000.0, "trend_strength": "RANGE"},
        ],
        "expect": {
            "min_entries_total": 1,
            "forbid_market": True,
        },
    },

    # B) DCA (발동 자체는 override로 강제, 실행층/상태 반영 검증)
    "L3_B1_DCA_ONCE_ENTRY": {
        "initial_state": base_state(2),
        "initial_positions": {"LONG": 0.001, "SHORT": 0.0},
        "initial_open_orders": [],
        "atr_4h_42": 300.0,
        "ticks": [
            {
                "t": 0,
                "price": 49800.0,
                "trend_strength": "RANGE",
                "decision_override": {
                    "mode": "NORMAL",
                    "entries": [
                        _ovr_entry(
                            wave_id=2, grid_index=-2, side="BUY",
                            price=49800.0, qty=0.001,
                            position_idx=1, reduce_only=False, step_cost=2
                        )
                    ],
                    "cancels": [],
                    "state_updates": {"dca_used_indices": [-2]},
                },
            },
        ],
        "expect": {
            "must_have_limit": True,
            "must_set_position_idx_for_entry": True,
        },
    },

    # C) TP LONG / SHORT (reduceOnly + positionIdx)
    "L3_C1_TP_LONG_REDUCEONLY": {
        "initial_state": base_state(3),
        "initial_positions": {"LONG": 0.002, "SHORT": 0.0},
        "initial_open_orders": [],
        "atr_4h_42": 300.0,
        "ticks": [
            {
                "t": 0,
                "price": 51000.0,
                "trend_strength": "RANGE",
                "decision_override": {
                    "mode": "NORMAL",
                    "entries": [
                        _ovr_entry(
                            wave_id=3, grid_index=2, side="SELL",
                            price=51000.0, qty=0.001,
                            position_idx=1, reduce_only=True, step_cost=1
                        )
                    ],
                    "cancels": [],
                    "state_updates": {"long_tp_active": True, "long_tp_max_index": 2},
                },
            },
        ],
        "expect": {
            "must_have_tp_limit": True,
            "tp_position_idx": 1,
            "tp_reduce_only": True,
        },
    },

    "L3_C2_TP_SHORT_REDUCEONLY": {
        "initial_state": base_state(4),
        "initial_positions": {"LONG": 0.0, "SHORT": 0.002},
        "initial_open_orders": [],
        "atr_4h_42": 300.0,
        "ticks": [
            {
                "t": 0,
                "price": 49000.0,
                "trend_strength": "RANGE",
                "decision_override": {
                    "mode": "NORMAL",
                    "entries": [
                        _ovr_entry(
                            wave_id=4, grid_index=-2, side="BUY",
                            price=49000.0, qty=0.001,
                            position_idx=2, reduce_only=True, step_cost=1
                        )
                    ],
                    "cancels": [],
                    "state_updates": {"short_tp_active": True, "short_tp_max_index": 2},
                },
            },
        ],
        "expect": {
            "must_have_tp_limit": True,
            "tp_position_idx": 2,
            "tp_reduce_only": True,
        },
    },

    # D) TP 부분체결 → reentry reset(정책상 dca_used_indices 리셋이 반영되는지)
    "L3_D1_TP_PARTIAL_FILL_REENTRY_RESET": {
        "initial_state": base_state(5),
        "initial_positions": {"LONG": 0.002, "SHORT": 0.0},
        "initial_open_orders": [],
        "atr_4h_42": 300.0,
        "ticks": [
            # tick0: TP 생성
            {
                "t": 0,
                "price": 51000.0,
                "trend_strength": "RANGE",
                "decision_override": {
                    "mode": "NORMAL",
                    "entries": [
                        _ovr_entry(
                            wave_id=5, grid_index=2, side="SELL",
                            price=51000.0, qty=0.001,
                            position_idx=1, reduce_only=True, step_cost=1
                        )
                    ],
                    "cancels": [],
                    "state_updates": {"dca_used_indices": [-3, -2], "long_tp_active": True},
                },
            },
            # tick1: 부분체결(방향: LONG 감소) + state 업데이트로 리셋 반영(검증은 state 반영 확인)
            {
                "t": 1,
                "price": 51010.0,
                "trend_strength": "RANGE",
                "fills": [{"order_id": "l3_oid_1", "filled_qty": 0.001}],
                "decision_override": {
                    "mode": "NORMAL",
                    "entries": [],
                    "cancels": [],
                    "state_updates": {"dca_used_indices": []},
                },
            },
        ],
        "expect": {
            "final_long_qty": 0.001,   # 0.002 - 0.001
            "dca_used_indices_empty": True,
        },
    },

    # E) ESCAPE: 신규 진입 차단 + (E2) 취소는 수행
    "L3_E1_ESCAPE_BLOCKS_ENTRY": {
        "initial_state": base_state(6),
        "initial_positions": {"LONG": 0.0, "SHORT": 0.0},
        "initial_open_orders": [],
        "atr_4h_42": 300.0,
        "ticks": [
            {
                "t": 0,
                "price": 50000.0,
                "trend_strength": "RANGE",
                "decision_override": {
                    "mode": "ESCAPE",
                    "entries": [
                        _ovr_entry(
                            wave_id=6, grid_index=-1, side="BUY",
                            price=50000.0, qty=0.001,
                            position_idx=1, reduce_only=False, step_cost=2
                        )
                    ],
                    "cancels": [],
                    "state_updates": {"mode": "ESCAPE"},
                },
            },
        ],
        "expect": {
            "no_new_orders": True,
        },
    },

    "L3_E2_ESCAPE_CANCEL_ONLY": {
        "initial_state": base_state(7),
        "initial_positions": {"LONG": 0.0, "SHORT": 0.0},
        "initial_open_orders": [
            {"order_id": "OID_CANCEL_X", "side": "BUY", "price": 49900.0, "qty": 0.001, "reduce_only": False, "position_idx": 1, "tag": "PRE_EXIST"},
        ],
        "atr_4h_42": 300.0,
        "ticks": [
            {
                "t": 0,
                "price": 50000.0,
                "trend_strength": "RANGE",
                "decision_override": {
                    "mode": "ESCAPE",
                    "entries": [
                        _ovr_entry(
                            wave_id=7, grid_index=-1, side="BUY",
                            price=50000.0, qty=0.001,
                            position_idx=1, reduce_only=False, step_cost=2
                        )
                    ],
                    "cancels": ["OID_CANCEL_X"],
                    "state_updates": {"mode": "ESCAPE"},
                },
            },
        ],
        "expect": {
            "must_cancel": "OID_CANCEL_X",
            "no_new_orders": True,
        },
    },
}

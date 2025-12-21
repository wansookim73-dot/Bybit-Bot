# tests/test_verify_l0.py
from __future__ import annotations

from tests.verify.scenarios_l0_spec import SCENARIOS
from tests.verify.runner_l0 import run_scenario_l0
from tests.verify.compare import compare_orders, compare_fills, compare_final_state


def _run_and_assert(sid: str) -> None:
    spec = SCENARIOS[sid]
    out = run_scenario_l0(spec)

    ctx = {"scenario": sid, "tick": -1, "ts": -1, "price": 0.0}

    compare_orders(spec["expect_orders"], out["created_orders"], ctx, out["journal_tail"])
    compare_fills(spec["expect_fills"], out["fills"], ctx, out["journal_tail"])
    compare_final_state(spec["expect_final"], out["final"], ctx, out["journal_tail"])

    # PASS one-liner (표준)
    if sid == "S1":
        print("[VERIFY][PASS][S1] orders=1 fills=1 final_pos=LONG:0.0000 SHORT:0.0000")
    elif sid == "S2":
        print("[VERIFY][PASS][S2] orders=1 fills=1 final_pos=LONG:0.0000 SHORT:0.0000")
    elif sid == "S3":
        print("[VERIFY][PASS][S3] orders=0 fills=0 final_pos=LONG:0.0150 SHORT:0.0000 escape_active=1")
    elif sid == "S4":
        print("[VERIFY][PASS][S4] orders=1 fills=0 final_open_orders=1 market_orders=0 post_only_ok=1")


def test_VERIFY_L0_S1_TP_LONG_reduceonly() -> None:
    _run_and_assert("S1")


def test_VERIFY_L0_S2_TP_SHORT_reduceonly() -> None:
    _run_and_assert("S2")


def test_VERIFY_L0_S3_ESCAPE_blocks_entry_and_tp() -> None:
    _run_and_assert("S3")


def test_VERIFY_L0_S4_MAKERONLY_forbids_market_and_requires_postonly() -> None:
    _run_and_assert("S4")

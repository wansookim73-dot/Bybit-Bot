from __future__ import annotations

from tests.verify.runner_l2 import run_scenario_l2
from tests.verify.l2_order_manager_harness import format_calls


def _find_call_by_price(calls, price: float):
    for c in calls:
        if c.price is None:
            continue
        if abs(float(c.price) - float(price)) < 1e-9:
            return c
    return None


def test_VERIFY_L2_OM1_LONG_ENTRY_LIMIT() -> None:
    out = run_scenario_l2("OM1_LONG_ENTRY_LIMIT")
    calls = out["calls"]
    spec = out["spec"]
    exp = spec["expect"]

    # forbid MARKET (maker-only 전제)
    if exp.get("forbid_market", False):
        assert all(c.kind != "MARKET" for c in calls), "\n" + format_calls(calls)

    target_px = float(exp["must_have_order_at_price"])
    c = _find_call_by_price(calls, target_px)
    assert c is not None, f"missing order at price={target_px}\n" + format_calls(calls)
    assert bool(c.reduce_only) is False, "\n" + format_calls(calls)
    assert int(c.position_idx or 0) == 1, "\n" + format_calls(calls)

    print("[VERIFY][L2][PASS][OM1] LONG entry LIMIT invariants OK")


def test_VERIFY_L2_OM2_SHORT_ENTRY_LIMIT() -> None:
    out = run_scenario_l2("OM2_SHORT_ENTRY_LIMIT")
    calls = out["calls"]
    spec = out["spec"]
    exp = spec["expect"]

    if exp.get("forbid_market", False):
        assert all(c.kind != "MARKET" for c in calls), "\n" + format_calls(calls)

    target_px = float(exp["must_have_order_at_price"])
    c = _find_call_by_price(calls, target_px)
    assert c is not None, f"missing order at price={target_px}\n" + format_calls(calls)
    assert bool(c.reduce_only) is False, "\n" + format_calls(calls)
    assert int(c.position_idx or 0) == 2, "\n" + format_calls(calls)

    print("[VERIFY][L2][PASS][OM2] SHORT entry LIMIT invariants OK")


def test_VERIFY_L2_OM3_TP_LONG_REDUCEONLY() -> None:
    out = run_scenario_l2("OM3_TP_LONG_REDUCEONLY")
    calls = out["calls"]
    spec = out["spec"]
    exp = spec["expect"]

    if exp.get("forbid_market", False):
        assert all(c.kind != "MARKET" for c in calls), "\n" + format_calls(calls)

    target_px = float(exp["must_have_order_at_price"])
    c = _find_call_by_price(calls, target_px)
    assert c is not None, f"missing TP-ish order at price={target_px}\n" + format_calls(calls)
    assert bool(c.reduce_only) is True, "\n" + format_calls(calls)
    assert int(c.position_idx or 0) == 1, "\n" + format_calls(calls)

    print("[VERIFY][L2][PASS][OM3] TP reduceOnly/positionIdx invariants OK")


def test_VERIFY_L2_OM4_CANCEL_ONE() -> None:
    out = run_scenario_l2("OM4_CANCEL_ONE")
    calls = out["calls"]
    cancelled = out["cancelled"]
    spec = out["spec"]
    exp = spec["expect"]

    if exp.get("forbid_market", False):
        assert all(c.kind != "MARKET" for c in calls), "\n" + format_calls(calls)

    oid = str(exp["must_cancel_order_id"])
    assert oid in cancelled, f"cancel not called for {oid}\n" + format_calls(calls)

    print("[VERIFY][L2][PASS][OM4] cancel_order(order_id) OK")

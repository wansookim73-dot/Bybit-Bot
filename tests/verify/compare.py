# tests/verify/compare.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .scenarios_l0_spec import EPS_PRICE, EPS_QTY


def _eq_float(a: float, b: float, eps: float) -> bool:
    return abs(float(a) - float(b)) <= float(eps)


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _fp_order(o: Dict[str, Any]) -> Tuple[Any, ...]:
    meta = o.get("meta") or {}
    return (
        o.get("symbol"),
        o.get("side"),
        o.get("order_type"),
        round(float(o.get("qty") or 0.0), 9),
        round(float(o.get("price") or 0.0), 6),
        bool(o.get("reduce_only")),
        int(o.get("positionIdx") or 0),
        bool(o.get("post_only")),
        str(o.get("tif") or ""),
        meta.get("reason"),
        meta.get("scenario"),
        meta.get("tag"),
    )


def _fp_fill(f: Dict[str, Any]) -> Tuple[Any, ...]:
    meta = f.get("meta") or {}
    return (
        f.get("symbol"),
        f.get("side"),
        f.get("order_type"),
        round(float(f.get("filled_qty") or 0.0), 9),
        round(float(f.get("fill_price") or 0.0), 6),
        int(f.get("ts") or 0),
        bool(f.get("reduce_only")),
        int(f.get("positionIdx") or 0),
        meta.get("reason"),
        meta.get("scenario"),
        meta.get("tag"),
    )


def _fail(rule_id: str, msg: str, ctx: Dict[str, Any], expect: Any, actual: Any, hint: str, journal_tail: List[Dict[str, Any]]) -> None:
    s = ctx.get("scenario", "?")
    tick = ctx.get("tick", -1)
    ts = ctx.get("ts", -1)
    price = ctx.get("price", 0.0)

    print(f"[VERIFY][FAIL][{s}] {rule_id} {msg}")
    print(f"[VERIFY][FAIL_META] scenario={s} tick={tick} ts={ts} price={price}")
    print(f"[VERIFY][EXPECT] {_j(expect)}")
    print(f"[VERIFY][ACTUAL] {_j(actual)}")
    print(f"[VERIFY][HINT] {hint}")
    print(f"[VERIFY][JOURNAL_TAIL] {_j(journal_tail)}")
    print(f"[VERIFY][RERUN] PYTHONHASHSEED=0 python3 -m pytest -q -s -x tests/test_verify_l0.py -k \"{s}\"")
    raise AssertionError(f"{rule_id}: {msg}")


def compare_orders(expect_orders: List[Dict[str, Any]], actual_orders: List[Dict[str, Any]], ctx: Dict[str, Any], journal_tail: List[Dict[str, Any]]) -> None:
    exp = sorted([_fp_order(o) for o in expect_orders])
    act = sorted([_fp_order(o) for o in actual_orders])

    if exp != act:
        hint = "Expected/Actual order fingerprint mismatch. Check policy guard + create timing + order schema."
        _fail(
            "RULE_ORDERS_MISMATCH",
            "orders multiset mismatch",
            ctx,
            {"expect_orders": expect_orders},
            {"actual_orders": actual_orders},
            hint,
            journal_tail,
        )


def compare_fills(expect_fills: List[Dict[str, Any]], actual_fills: List[Dict[str, Any]], ctx: Dict[str, Any], journal_tail: List[Dict[str, Any]]) -> None:
    exp = sorted([_fp_fill(f) for f in expect_fills])
    act = sorted([_fp_fill(f) for f in actual_fills])

    if exp != act:
        hint = "Expected/Actual fill fingerprint mismatch. Check fill condition (limit vs market) and reduceOnly semantics."
        _fail(
            "RULE_FILLS_MISMATCH",
            "fills multiset mismatch",
            ctx,
            {"expect_fills": expect_fills},
            {"actual_fills": actual_fills},
            hint,
            journal_tail,
        )


def compare_final_state(expect_final: Dict[str, Any], actual_final: Dict[str, Any], ctx: Dict[str, Any], journal_tail: List[Dict[str, Any]]) -> None:
    exp_pos = expect_final.get("positions") or {}
    act_pos = actual_final.get("positions") or {}

    for side in ("LONG", "SHORT"):
        e = exp_pos.get(side) or {}
        a = act_pos.get(side) or {}

        if not _eq_float(float(e.get("qty") or 0.0), float(a.get("qty") or 0.0), EPS_QTY):
            _fail(
                "RULE_FINAL_STATE_MISMATCH",
                f"{side}.qty mismatch",
                ctx,
                {"expect_final": expect_final},
                {"actual_final": actual_final},
                "Final position qty mismatch. Check fill->position application and reduceOnly/positionIdx wiring.",
                journal_tail,
            )

        if not _eq_float(float(e.get("avg_price") or 0.0), float(a.get("avg_price") or 0.0), EPS_PRICE):
            _fail(
                "RULE_FINAL_STATE_MISMATCH",
                f"{side}.avg_price mismatch",
                ctx,
                {"expect_final": expect_final},
                {"actual_final": actual_final},
                "Final avg_price mismatch. Check avg calculation rules for add fills.",
                journal_tail,
            )

    if bool(expect_final.get("escape_active")) != bool(actual_final.get("escape_active")):
        _fail(
            "RULE_FINAL_STATE_MISMATCH",
            "escape_active mismatch",
            ctx,
            {"expect_final": expect_final},
            {"actual_final": actual_final},
            "escape_active mismatch; check runner propagation.",
            journal_tail,
        )

    exp_orders = expect_final.get("open_orders") or []
    act_orders = actual_final.get("open_orders") or []
    exp_fp = sorted([_fp_order(o) for o in exp_orders])
    act_fp = sorted([_fp_order(o) for o in act_orders])

    if exp_fp != act_fp:
        _fail(
            "RULE_FINAL_STATE_MISMATCH",
            "open_orders mismatch",
            ctx,
            {"expect_final": expect_final},
            {"actual_final": actual_final},
            "open_orders mismatch; check create/cancel/fill transitions.",
            journal_tail,
        )

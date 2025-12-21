# tests/verify/runner_l0.py
from __future__ import annotations

import copy
from typing import Any, Dict, List

from .execution_journal import ExecutionJournal
from .mock_exchange import MockExchange


def _policy_fail(rule_id: str, msg: str, ctx: Dict[str, Any], order: Dict[str, Any], journal_tail: List[Dict[str, Any]], hint: str) -> None:
    s = ctx.get("scenario", "?")
    tick = ctx.get("tick", -1)
    ts = ctx.get("ts", -1)
    price = ctx.get("price", 0.0)

    print(f"[VERIFY][FAIL][{s}] {rule_id} {msg}")
    print(f"[VERIFY][FAIL_META] scenario={s} tick={tick} ts={ts} price={price}")
    print(f"[VERIFY][ACTUAL_ORDER] {order}")
    print(f"[VERIFY][HINT] {hint}")
    print(f"[VERIFY][JOURNAL_TAIL] {journal_tail}")
    print(f"[VERIFY][RERUN] PYTHONHASHSEED=0 python3 -m pytest -q -s -x tests/test_verify_l0.py -k \"{s}\"")
    raise AssertionError(f"{rule_id}: {msg}")


def _policy_guard(order: Dict[str, Any], *, maker_only: bool, escape_active: bool, ctx: Dict[str, Any], journal_tail: List[Dict[str, Any]]) -> None:
    order_type = str(order.get("order_type") or "")
    post_only = bool(order.get("post_only"))
    reduce_only = bool(order.get("reduce_only"))
    pos_idx = int(order.get("positionIdx") or 0)
    side = str(order.get("side") or "")

    meta = order.get("meta") or {}
    reason = meta.get("reason")

    # 1) MakerOnly: Market 금지 + post_only 강제 + Limit 권장
    if maker_only:
        if order_type == "Market":
            _policy_fail(
                "RULE_MAKERONLY_MARKET_FORBIDDEN",
                "Market order created under maker_only",
                ctx, order, journal_tail,
                "MakerOnly 모드에서는 Market 주문이 금지됩니다.",
            )
        if not post_only:
            _policy_fail(
                "RULE_MAKERONLY_POSTONLY_REQUIRED",
                "post_only is required under maker_only",
                ctx, order, journal_tail,
                "MakerOnly 모드에서는 post_only=True가 강제입니다.",
            )

    # 2) Escape: ENTRY/TP 모두 차단 (L0 고정 규칙)
    if escape_active:
        _policy_fail(
            "RULE_ESCAPE_ORDERS_FORBIDDEN",
            "escape_active=True but an order was created",
            ctx, order, journal_tail,
            "Escape 활성 상태에서는 L0에서 ENTRY/TP를 모두 생성하면 안 됩니다.",
        )

    # 3) TP: reduceOnly + positionIdx + 방향 정합성
    if reason == "TP":
        if not reduce_only:
            _policy_fail(
                "RULE_TP_REDUCEONLY_MISSING",
                "TP order must be reduce_only=True",
                ctx, order, journal_tail,
                "TP는 reduce_only=True 이어야 합니다.",
            )
        if pos_idx not in (1, 2):
            _policy_fail(
                "RULE_TP_POSITIONIDX_MISSING",
                "TP order must specify positionIdx 1 or 2",
                ctx, order, journal_tail,
                "TP는 hedge 모드에서 positionIdx(1=LONG,2=SHORT)가 필수입니다.",
            )
        if pos_idx == 1 and side != "Sell":
            _policy_fail(
                "RULE_TP_DIRECTION_INVALID",
                "LONG TP must be Sell",
                ctx, order, journal_tail,
                "LONG TP는 Sell + reduce_only=True 조합이어야 합니다.",
            )
        if pos_idx == 2 and side != "Buy":
            _policy_fail(
                "RULE_TP_DIRECTION_INVALID",
                "SHORT TP must be Buy",
                ctx, order, journal_tail,
                "SHORT TP는 Buy + reduce_only=True 조합이어야 합니다.",
            )

    # 4) ENTRY: reduceOnly=False + 방향 정합성(hedge)
    if reason == "ENTRY":
        if reduce_only:
            _policy_fail(
                "RULE_ENTRY_REDUCEONLY_INVALID",
                "ENTRY order must be reduce_only=False",
                ctx, order, journal_tail,
                "ENTRY는 reduce_only=False 이어야 합니다.",
            )
        if pos_idx == 1 and side != "Buy":
            _policy_fail(
                "RULE_ENTRY_DIRECTION_INVALID",
                "LONG ENTRY must be Buy",
                ctx, order, journal_tail,
                "LONG ENTRY는 Buy + reduce_only=False 조합이어야 합니다.",
            )
        if pos_idx == 2 and side != "Sell":
            _policy_fail(
                "RULE_ENTRY_DIRECTION_INVALID",
                "SHORT ENTRY must be Sell",
                ctx, order, journal_tail,
                "SHORT ENTRY는 Sell + reduce_only=False 조합이어야 합니다.",
            )


def run_scenario_l0(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    L0 runner:
    - 운영 코드 호출 없이, 시나리오 plan을 실행해 주문/체결/포지션 변화를 검증한다.
    - 목적: reduceOnly/positionIdx, maker_only, escape 차단 같은 '안전 규칙'을 오프라인에서 고정한다.
    """
    sid = spec["id"]
    init = spec["init_state"]
    plan = spec.get("plan") or {}
    create_map: Dict[int, List[Dict[str, Any]]] = (plan.get("create_orders_at_ts") or {})

    price_seq = spec["price_seq"]

    journal = ExecutionJournal()
    ex = MockExchange(journal=journal, init_positions=init["positions"])

    maker_only = bool(init.get("maker_only"))
    escape_active = bool(init.get("escape_active"))

    created_orders: List[Dict[str, Any]] = []
    fills_all: List[Dict[str, Any]] = []

    for i, p in enumerate(price_seq):
        ts = int(p["ts"])
        price = float(p["price"])
        ctx = {"scenario": sid, "tick": i, "ts": ts, "price": price}

        # 계획된 주문 생성
        if ts in create_map:
            for od in create_map[ts]:
                order = copy.deepcopy(od)  # 기대값/플랜 원본 오염 방지
                _policy_guard(order, maker_only=maker_only, escape_active=escape_active, ctx=ctx, journal_tail=journal.tail(10))
                ex.create_order(order, ts=ts)
                created_orders.append(copy.deepcopy(order))

        # 시세 tick -> 체결 시도
        fills = ex.tick(price, ts=ts)
        fills_all.extend(fills)

    actual_final = {
        "positions": ex.get_positions(),
        "open_orders": ex.get_open_orders(),
        "escape_active": escape_active,
    }

    return {
        "created_orders": created_orders,
        "fills": fills_all,
        "final": actual_final,
        "journal_tail": journal.tail(10),
    }

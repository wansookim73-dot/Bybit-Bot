#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategy.escape_logic import EscapeLogic  # noqa: E402


class DummyCapital:
    pass


def mk_state(**kw):
    base = dict(
        mode="NORMAL",
        p_center=10000.0,
        p_gap=200.0,
        atr_value=100.0,
        long_seed_total_effective=10000.0,
        short_seed_total_effective=10000.0,
        unit_seed_long=100.0,
        unit_seed_short=100.0,
        k_long=10,
        k_short=10,
        news_block=False,
        cb_block=False,
        hedge_side=None,
        hedge_size=0.0,
        escape_active=False,
        escape_long_active=False,
        escape_short_active=False,
        escape_enter_ts=0.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def mk_feed(
    state,
    *,
    price,
    price_prev=None,
    pnl_total_pct=0.0,
    atr_4h_42=100.0,
    long_size=0.0,
    long_pnl=0.0,
    short_size=0.0,
    short_pnl=0.0,
    hedge_size_pos=0.0,
    news_signal_on=False,
    news_signal_off=False,
):
    if price_prev is None:
        price_prev = price

    positions = {
        "long_size": float(long_size),
        "long_pnl": float(long_pnl),
        "short_size": float(short_size),
        "short_pnl": float(short_pnl),
        "hedge_size": float(hedge_size_pos),
    }

    return SimpleNamespace(
        state=state,
        price=float(price),
        price_prev=float(price_prev),
        atr_4h_42=float(atr_4h_42),
        pnl_total_pct=float(pnl_total_pct),
        positions=positions,
        news_signal_on=bool(news_signal_on),
        news_signal_off=bool(news_signal_off),
    )


def dump(tag, decision, state):
    print("\n" + "=" * 90)
    print(f"[{tag}]")
    print(f"- decision.mode_override = {decision.mode_override!r}")
    print(f"- decision.full_exit     = {decision.full_exit!r}")
    print(f"- orders                = {len(decision.orders)}")
    for i, o in enumerate(decision.orders):
        try:
            d = vars(o)
        except TypeError:
            d = {"repr": repr(o)}
        print(f"  [{i}] {d}")

    keys = [
        "mode",
        "escape_active",
        "escape_long_active",
        "escape_short_active",
        "escape_long_pending",
        "escape_short_pending",
        "escape_trigger_line_long",
        "escape_trigger_line_short",
        "hedge_side",
        "hedge_size",
        "escape_enter_ts",
        "escape_reason",
        "news_block",
        "cb_block",
    ]
    print("- state snapshot:")
    for k in keys:
        if hasattr(state, k):
            print(f"  {k:24s} = {getattr(state, k)!r}")


def main():
    logic = EscapeLogic(DummyCapital())

    # CASE 1) FULL_EXIT 강제 재현
    st1 = mk_state()
    feed1 = mk_feed(
        st1,
        price=10000,
        pnl_total_pct=0.05,
        long_size=0.01,
        long_pnl=10.0,
        short_size=0.0,
        short_pnl=0.0,
    )
    dec1 = logic.evaluate(feed1)
    dump("CASE1_FULL_EXIT", dec1, st1)

    # CASE 2) Hedge Entry 경로 강제 재현 (LONG 메인 → SHORT 헤지)
    st2 = mk_state(
        escape_active=True,
        escape_long_active=True,
        escape_enter_ts=time.time(),
        hedge_side=None,
        hedge_size=0.0,
    )
    feed2 = mk_feed(
        st2,
        price=10000,
        pnl_total_pct=-0.001,
        long_size=0.02,
        long_pnl=-20.0,
        short_size=0.0,
        short_pnl=0.0,
        hedge_size_pos=0.0,
    )
    dec2 = logic.evaluate(feed2)
    dump("CASE2_HEDGE_ENTRY_FORCED_LONG", dec2, st2)

    # CASE 3) Hedge Exit 경로 강제 재현 (ESCAPE OFF인데 hedge 남음)
    st3 = mk_state(
        escape_active=False,
        escape_long_active=False,
        escape_short_active=False,
        hedge_side="SHORT",
        hedge_size=0.005,
    )
    feed3 = mk_feed(
        st3,
        price=10000,
        pnl_total_pct=0.0,
        long_size=0.02,
        long_pnl=-5.0,
        short_size=0.0,
        short_pnl=0.0,
        hedge_size_pos=0.005,
    )
    dec3 = logic.evaluate(feed3)
    dump("CASE3_HEDGE_EXIT_ESCAPE_OFF", dec3, st3)

    # CASE 4) 양방향 포지션 + 방향 active 없음(main_side=None 가능성)
    st4 = mk_state(
        escape_active=True,
        escape_long_active=False,
        escape_short_active=False,
        escape_enter_ts=time.time(),
        hedge_side=None,
        hedge_size=0.0,
    )
    feed4 = mk_feed(
        st4,
        price=10000,
        pnl_total_pct=-0.01,
        long_size=0.02,
        long_pnl=-20.0,
        short_size=0.01,
        short_pnl=5.0,
        hedge_size_pos=0.0,
    )
    dec4 = logic.evaluate(feed4)
    dump("CASE4_BOTH_SIDES_NO_DIRECTION_ACTIVE", dec4, st4)


if __name__ == "__main__":
    main()


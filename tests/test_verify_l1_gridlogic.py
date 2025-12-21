from __future__ import annotations

from tests.verify.l1_gridlogic_harness import (
    freeze_gridlogic_time,
    make_feed,
    make_orderinfo,
    make_state,
    run_gridlogic_once,
    assert_griddecision_invariants,
    summarize,
)


def test_verify_l1_gridlogic_smoke_flat() -> None:
    """
    가장 기본: 포지션 0 / 오픈오더 0 / TrendGate=RANGE
    - 목표: GridLogic.process가 예외 없이 의사결정 구조를 반환
    - 그리고 L1 불변조건을 만족
    """
    with freeze_gridlogic_time(1700000000.0):
        feed = make_feed(
            price=50000.0,
            atr_4h_42=300.0,
            state=make_state({"wave_id": 1}),
            open_orders=[],
            trend_strength="RANGE",
            trend_valid=True,
            trend_fresh=True,
        )
        decision = run_gridlogic_once(feed)
        assert_griddecision_invariants(decision)
        print(f"[VERIFY][L1][PASS][FLAT] {summarize(decision)}")


def test_verify_l1_gridlogic_smoke_with_stale_open_order() -> None:
    """
    오래된 오픈오더가 있는 상태(교체/취소 로직 방아쇠 가능)
    - 목표: cancel/replaces가 나오든 안 나오든, 구조/무결성은 유지
    """
    with freeze_gridlogic_time(1700000000.0):
        old = make_orderinfo(
            order_id="OLD1",
            side="BUY",
            price=49000.0,
            qty=0.001,
            reduce_only=False,
            created_ts=1700000000.0 - 3600.0,
            tag="W1_GRID_A_-1_BUY",
        )
        feed = make_feed(
            price=50000.0,
            atr_4h_42=300.0,
            state=make_state({"wave_id": 1}),
            open_orders=[old],
            trend_strength="RANGE",
            trend_valid=True,
            trend_fresh=True,
        )
        decision = run_gridlogic_once(feed)
        assert_griddecision_invariants(decision)
        print(f"[VERIFY][L1][PASS][STALE_OO] {summarize(decision)}")


def test_verify_l1_gridlogic_smoke_existing_long_position() -> None:
    """
    롱 포지션이 있다고 가정한 상태.
    - 목표: TP/reduce_only 주문이 나오면 position_idx(1)가 반드시 세팅되는지 잡아냄.
    - (TP가 안 나와도 테스트는 통과: 핵심은 '깨지지 않고, 나오면 불변조건을 만족')
    """
    with freeze_gridlogic_time(1700000000.0):
        st = make_state({
            "wave_id": 2,
            "pos_long_qty": 0.01,
            "pos_long_avg": 50000.0,
        })
        feed = make_feed(
            price=50500.0,
            atr_4h_42=300.0,
            state=st,
            open_orders=[],
            trend_strength="RANGE",
            trend_valid=True,
            trend_fresh=True,
        )
        decision = run_gridlogic_once(feed)
        assert_griddecision_invariants(decision)
        print(f"[VERIFY][L1][PASS][LONG_POS] {summarize(decision)}")

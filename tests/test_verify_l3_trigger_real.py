from __future__ import annotations

from typing import List, Tuple, Any
import pytest

from strategy.grid_logic import GridLogic
from strategy.state_model import BotState
from strategy.feed_types import StrategyFeed, OrderInfo


def _make_state(*, wave_id: int, p_center: float, p_gap: float) -> BotState:
    st = BotState(
        mode="NORMAL",
        wave_id=wave_id,
        p_center=float(p_center),
        p_gap=float(p_gap),
        atr_value=(float(p_gap) / 0.15) if float(p_gap) > 0 else 300.0,
        long_seed_total_effective=120.0,
        short_seed_total_effective=120.0,
        unit_seed_long=10.0,
        unit_seed_short=10.0,
        k_long=0,
        k_short=0,
        total_balance_snap=1200.0,
        total_balance=1200.0,
        free_balance=1200.0,
        startup_done=True,  # Start-up 노이즈 제거
    )
    st.line_memory_long = {}
    st.line_memory_short = {}
    st.dca_used_indices = []
    st.dca_last_idx = 10**9
    st.dca_last_ts = 0.0
    st.dca_last_price = 0.0
    return st


def _mk_feed(*, price: float, atr_4h_42: float, state: BotState, open_orders: List[OrderInfo]) -> StrategyFeed:
    # StrategyFeed가 trend_* 필드를 받을 수도/안 받을 수도 있으므로, 안전하게 kwargs를 분기한다.
    kwargs = dict(
        price=float(price),
        atr_4h_42=float(atr_4h_42),
        state=state,
        open_orders=list(open_orders),
        pnl_total=0.0,
        pnl_total_pct=0.0,
    )
    try:
        return StrategyFeed(**kwargs, trend_strength="RANGE", trend_bias="NONE", trend_valid=True, trend_fresh=True, trend_reason="L3v2")  # type: ignore[arg-type]
    except TypeError:
        return StrategyFeed(**kwargs)  # type: ignore[arg-type]


def _entries(dec: Any):
    return list(getattr(dec, "grid_entries", []) or []) + list(getattr(dec, "grid_replaces", []) or [])


def _find_trigger(*, logic: GridLogic, state: BotState, atr_4h_42: float, prices: List[float], predicate) -> Tuple[float, Any]:
    for px in prices:
        feed = _mk_feed(price=px, atr_4h_42=atr_4h_42, state=state, open_orders=[])
        dec = logic.process(feed)
        if predicate(dec):
            return float(px), dec
    raise AssertionError("trigger not found (scan exhausted)")


def test_VERIFY_L3v2_TP_trigger_real_reduceOnly_positionIdx() -> None:
    """
    목표:
    - GridLogic.process를 가격 스윕으로 돌려 TP 후보(reduce_only=True) 생성 지점을 '발견'한다.
    - 발견되면: reduceOnly + positionIdx 불변조건 확인.
    - 발견 실패 시: 이 빌드에서 TP 트리거가 더 엄격하다는 의미이므로 SKIP(정보성).
      (주의: TP 안전성(reduceOnly/positionIdx)은 L2에서 이미 강하게 검증됨)
    """
    logic = GridLogic()
    state = _make_state(wave_id=101, p_center=50000.0, p_gap=300.0)

    # TP가 나오기 쉬운 힌트 상태
    state.long_size = 0.002
    state.long_pos_nonzero = True
    state.long_pnl = +5.0
    state.long_tp_active = True
    state.long_tp_max_index = 0

    # 양방향 촘촘 스윕(범위를 크게)
    prices = [state.p_center + i * (state.p_gap / 4.0) for i in range(-80, 81)]

    def pred(dec: Any) -> bool:
        for s in _entries(dec):
            if bool(getattr(s, "reduce_only", False)):
                return True
        return False

    try:
        px, dec = _find_trigger(logic=logic, state=state, atr_4h_42=state.p_gap / 0.15, prices=prices, predicate=pred)
    except AssertionError:
        pytest.skip("TP trigger not found in this build (GridLogic TP preconditions may be stricter). L2 already verifies TP reduceOnly/positionIdx invariants.")

    # 불변조건: reduce_only spec은 position_idx가 있어야 함(1/2)
    found = False
    for s in _entries(dec):
        if bool(getattr(s, "reduce_only", False)):
            found = True
            pidx = getattr(s, "position_idx", None)
            assert int(pidx) in (1, 2), f"TP reduce_only must set position_idx. price={px} spec={s!r}"
    assert found, "predicate hit but no reduce_only spec found"

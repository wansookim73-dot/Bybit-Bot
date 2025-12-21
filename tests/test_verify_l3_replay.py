from __future__ import annotations

from tests.verify.l3_replay_harness import L3ReplayHarness
from tests.verify.scenarios_l3_spec import SCENARIOS


def _kinds(stub_calls):
    return [c.get("kind") for c in (stub_calls or [])]


def test_VERIFY_L3_A1_STARTUP_ENTRY() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_A1_STARTUP_ENTRY"])
    calls = out.calls
    stub_calls = out.stub_calls

    total_entries = sum(int(c.get("entries_cnt", 0)) for c in calls)
    assert total_entries >= SCENARIOS["L3_A1_STARTUP_ENTRY"]["expect"]["min_entries_total"], "\n" + h.format_tail(calls)

    if SCENARIOS["L3_A1_STARTUP_ENTRY"]["expect"].get("forbid_market", False):
        assert "MARKET" not in _kinds(stub_calls), str(stub_calls[-20:])


def test_VERIFY_L3_B1_DCA_ONCE_ENTRY() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_B1_DCA_ONCE_ENTRY"])
    kinds = _kinds(out.stub_calls)
    assert "LIMIT" in kinds, str(out.stub_calls)

    # entry는 reduceOnly False이고, positionIdx가 들어가야 한다(우리 override 기준)
    limit = [c for c in out.stub_calls if c.get("kind") == "LIMIT"]
    assert len(limit) >= 1, str(out.stub_calls)
    kw = (limit[0].get("kwargs") or {})
    pos_idx = kw.get("position_idx", kw.get("positionIdx"))
    ro = kw.get("reduce_only", kw.get("reduceOnly", False))

    # kwargs에 position_idx가 없을 수 있으므로(정상 케이스),
    # 최종적으로 Stub가 저장한 open_orders에 position_idx가 들어갔는지 확인한다.
    if pos_idx is None:
        assert len(out.final_open_orders) >= 1, f"no open_orders recorded. calls={out.stub_calls}"
        pos_idx = out.final_open_orders[0].get("position_idx")

    assert int(pos_idx) in (1, 2), f"position_idx missing: kw={kw} open_orders={out.final_open_orders}"
    assert bool(ro) is False, f"reduce_only should be False for entry: {kw}"


def test_VERIFY_L3_C1_TP_LONG_REDUCEONLY() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_C1_TP_LONG_REDUCEONLY"])
    tp = [c for c in out.stub_calls if c.get("kind") == "TP_LIMIT"]
    assert len(tp) == 1, str(out.stub_calls)
    assert bool(tp[0].get("reduce_only")) is True
    assert int(tp[0].get("position_idx")) == 1


def test_VERIFY_L3_C2_TP_SHORT_REDUCEONLY() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_C2_TP_SHORT_REDUCEONLY"])
    tp = [c for c in out.stub_calls if c.get("kind") == "TP_LIMIT"]
    assert len(tp) == 1, str(out.stub_calls)
    assert bool(tp[0].get("reduce_only")) is True
    assert int(tp[0].get("position_idx")) == 2


def test_VERIFY_L3_D1_TP_PARTIAL_FILL_REENTRY_RESET() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_D1_TP_PARTIAL_FILL_REENTRY_RESET"])

    # partial fill로 LONG qty가 감소해야 한다
    assert abs(out.final_positions["LONG"]["qty"] - 0.001) < 1e-12, out.final_positions

    # dca_used_indices가 비워졌는지(state_updates 반영 확인)
    assert (out.final_state.dca_used_indices or []) == [], out.final_state.dca_used_indices


def test_VERIFY_L3_E1_ESCAPE_BLOCKS_ENTRY() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_E1_ESCAPE_BLOCKS_ENTRY"])

    # ESCAPE 모드에서 apply_decision은 entries/replaces를 블록해야 한다 (OrderManager escape-lockdown)
    kinds = _kinds(out.stub_calls)
    assert "LIMIT" not in kinds and "TP_LIMIT" not in kinds and "MARKET" not in kinds, str(out.stub_calls)


def test_VERIFY_L3_E2_ESCAPE_CANCEL_ONLY() -> None:
    h = L3ReplayHarness()
    out = h.run(SCENARIOS["L3_E2_ESCAPE_CANCEL_ONLY"])

    # cancel은 수행
    cancels = [c for c in out.stub_calls if c.get("kind") == "CANCEL"]
    assert any(c.get("order_id") == "OID_CANCEL_X" for c in cancels), str(out.stub_calls)

    # 신규 주문은 없어야 함
    kinds = _kinds(out.stub_calls)
    assert "LIMIT" not in kinds and "TP_LIMIT" not in kinds and "MARKET" not in kinds, str(out.stub_calls)

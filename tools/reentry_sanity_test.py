from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import List

from strategy.feed_types import StrategyFeed, OrderInfo
from strategy.grid_logic import GridLogic
from core.state_manager import StateManager


def _mk_empty_orders() -> List[OrderInfo]:
    return []


def _print_decision(decision, title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)

    # 1) 신규 주문 후보
    ge = getattr(decision, "grid_entries", [])
    print(f"[grid_entries] count={len(ge)}")
    for i, e in enumerate(ge[:20], 1):
        # GridOrderSpec는 dataclass가 아닐 수도 있으니 안전하게 getattr로 출력
        print(
            f"  {i:02d}) side={getattr(e,'side',None)} "
            f"idx={getattr(e,'grid_index',None)} "
            f"price={getattr(e,'price',None)} "
            f"qty={getattr(e,'qty',None)} "
            f"reduce_only={getattr(e,'reduce_only',None)} "
            f"position_idx={getattr(e,'position_idx',None)} "
            f"mode={getattr(e,'mode',None)} "
            f"wave_id={getattr(e,'wave_id',None)}"
        )

    # 2) 캔슬
    cancels = getattr(decision, "grid_cancels", [])
    print(f"[grid_cancels] count={len(cancels)} (show up to 10)")
    for oid in cancels[:10]:
        print("  -", oid)

    # 3) state_updates
    su = getattr(decision, "state_updates", {}) or {}
    keys = sorted(list(su.keys()))
    print(f"[state_updates] keys({len(keys)}):", keys)
    for k in keys:
        v = su[k]
        if k in ("dca_used_indices", "line_memory_long", "line_memory_short"):
            print(f"  - {k} =", v)
        elif "tp_" in k or "dca_" in k or "k_" in k:
            print(f"  - {k} =", v)


def main() -> None:
    sm = StateManager()
    # --- load BotState (compat) ---
    if hasattr(sm, "load_state"):
        state = sm.load_state()
    elif hasattr(sm, "load"):
        state = sm.load()
    elif hasattr(sm, "_load_state"):
        state = sm._load_state()
    else:
        raise AttributeError(
            "StateManager has no load method: load_state / load / _load_state"
        )
    if state is None:
        raise SystemExit("[STOP] BotState load failed")

    # GridLogic 인스턴스 (실거래 호출 없음: process()는 decision만 만든다)
    gl = GridLogic()

    # 공통: ATR은 아무 값이나 가능(단 p_gap 계산/로직에 쓰이니 1 이상 권장)
    atr = float(getattr(state, "atr_4h_42", 1000.0) or 1000.0)

    # 공통: pnl_total/pct는 TP/DCA에 직접적 필수는 아니지만 feed 필드라 넣음
    pnl_total = float(getattr(state, "pnl_total", 0.0) or 0.0)
    pnl_total_pct = float(getattr(state, "pnl_total_pct", 0.0) or 0.0)

    # --------------------------------------------------------------------------------
    # [TEST-A] "익절 1분할이라도 발생하면 dca_used_indices(기존 진입 기록) 리셋"을 확인하기 위한 셋업
    #
    # 현실에서는 TP가 체결되면(=reduceOnly TP 주문이 체결되면) state 쪽에서
    # tp_active/tp_max + dca_used_indices 리셋이 일어나야 함.
    #
    # 여기서는 '리셋 트리거'를 인위적으로 만들기 위해:
    #  - dca_used_indices에 -11을 넣어 "이미 쓴 라인" 상태를 만들고
    #  - long_tp_active=True, long_tp_max_index>0 같은 TP 진행 상태를 줌
    #  - 그 다음 "TP가 끝난 상황"을 가정해 tp_active를 False로 바꾼 뒤,
    #    process() 한번 더 돌려서 dca_used_indices가 비워지는지(또는 line_memory가 초기화되는지) 본다.
    # --------------------------------------------------------------------------------

    base = deepcopy(state)

    # 1) "이미 -11 라인을 한 번 진입했다"는 기록을 심는다.
    setattr(base, "dca_used_indices", [-11])
    setattr(base, "dca_last_idx", -11)
    setattr(base, "dca_last_ts", 0.0)
    setattr(base, "dca_last_price", float(getattr(base, "p_center", 90000.0)))

    # 2) 포지션 값을 "그럴듯하게" 넣는다 (GridLogic가 로그/판단에 사용)
    #    (정확한 필드명은 main_v10이 state에 쓰는 이름을 따른다)
    setattr(base, "long_size", float(getattr(base, "long_size", 0.01) or 0.01))
    setattr(base, "short_size", float(getattr(base, "short_size", 0.0) or 0.0))
    setattr(base, "long_pnl", float(getattr(base, "long_pnl", 1.0) or 1.0))   # + 이면 TP 흐름 가능성
    setattr(base, "short_pnl", float(getattr(base, "short_pnl", 0.0) or 0.0))

    # 3) TP 진행중 상태(가정)
    setattr(base, "long_tp_active", True)
    setattr(base, "long_tp_max_index", 3)

    # 4) TP 진행중(process 1회)
    feed1 = StrategyFeed(
        price=float(getattr(base, "last_price", 90500.0) or 90500.0),
        atr_4h_42=atr,
        state=base,
        open_orders=_mk_empty_orders(),
        pnl_total=pnl_total,
        pnl_total_pct=pnl_total_pct,
    )
    dec1 = gl.process(feed1)
    _print_decision(dec1, "[TEST-A1] TP 진행중 상태에서 process() 결과")

    # 5) "TP가 끝나서 tp_active가 False가 됐다"는 상황을 인위로 만든다.
    after_tp = deepcopy(base)
    setattr(after_tp, "long_size", float(getattr(after_tp, "long_size", 0.0) or 0.0) - 0.001)
    setattr(after_tp, "long_tp_active", False)
    setattr(after_tp, "long_tp_max_index", 0)

    feed2 = StrategyFeed(
        price=float(getattr(after_tp, "last_price", 90500.0) or 90500.0),
        atr_4h_42=atr,
        state=after_tp,
        open_orders=_mk_empty_orders(),
        pnl_total=pnl_total,
        pnl_total_pct=pnl_total_pct,
    )
    dec2 = gl.process(feed2)
    _print_decision(dec2, "[TEST-A2] TP 종료(가정) 후 process() 결과 — dca_used_indices 리셋 여부 확인")

    print("\n[CHECK] 아래 중 하나라도 만족하면 '익절 후 진입기록 리셋'이 구현된 것입니다.")
    print("  - state_updates에 dca_used_indices = [] 가 있다")
    print("  - 또는 line_memory_long/short 초기화/리셋이 있다")
    print("  - 또는 tp_active 관련 reset 로그/키가 찍힌다")

    # --------------------------------------------------------------------------------
    # [TEST-B] '손실 전환 후 같은 라인 재진입'은 시장 조건이 필요하므로
    #          여기서는 최소 검증: "이전엔 guard_block 됐던 idx가, 리셋 후엔 block 되지 않는지"
    # --------------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("[TEST-B] 해석 방법")
    print("=" * 80)
    print("만약 운영 로그에서:")
    print("  [DCA-DBG] guard_block: idx=-11 already used")
    print("이런게 보였는데, TP 1분할 이후에는 같은 상황에서 저 guard_block이 사라져야 합니다.")
    print("이 스크립트의 TEST-A2 결과 state_updates에서 'dca_used_indices=[]'가 보이면, 그 조건이 충족됩니다.")


if __name__ == "__main__":
    main()

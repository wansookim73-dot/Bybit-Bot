from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict

from strategy.escape_logic import EscapeLogic
from strategy import escape_config as CFG


class DummyCapital:
    pass


def mk_state(**kw) -> Any:
    base = dict(
        # BotState 기본 필드(최소)
        mode="ESCAPE",
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

        # ESCAPE 런타임 필드(강제)
        escape_active=True,
        escape_long_active=True,
        escape_short_active=False,
        escape_long_pending=False,
        escape_short_pending=False,
        escape_trigger_line_long=None,
        escape_trigger_line_short=None,
        escape_enter_ts=time.time(),
        escape_reason=None,

        # Hedge 런타임 필드(강제)
        hedge_side="SHORT",   # 메인이 LONG이면 hedge는 SHORT가 정상
        hedge_size=0.02,      # hedge BTC qty

        # +2% 동시청산(5.5) 기준 노출 기록 (작게 잡으면 쉽게 트리거됨)
        escape_pair_exposure_long=500.0,   # N_entry (USDT)
        escape_pair_exposure_short=0.0,

        # BE 청산(5.4) 히스토리 플래그
        hedge_pnl_positive_seen_long=False,
        hedge_pnl_positive_seen_short=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def mk_feed(state: Any, price: float, positions: Dict[str, float], pnl_total_pct: float) -> Any:
    # EscapeLogic 내부에서 _extract_position_info(feed) 를 사용하므로 positions dict 제공
    pos = dict(
        long_size=float(positions.get("long_size", 0.0)),
        long_pnl=float(positions.get("long_pnl", 0.0)),
        short_size=float(positions.get("short_size", 0.0)),
        short_pnl=float(positions.get("short_pnl", 0.0)),
        hedge_size=float(positions.get("hedge_size", 0.0)),
    )
    return SimpleNamespace(
        state=state,
        price=float(price),
        price_prev=float(price),
        atr_4h_42=100.0,
        pnl_total_pct=float(pnl_total_pct),
        pnl_total=0.0,
        positions=pos,
        news_signal_on=False,
        news_signal_off=False,
    )


def show_decision(tag: str, dec: Any, state: Any) -> None:
    print("\n" + "=" * 100)
    print(f"[{tag}]")
    print("- full_exit:", bool(getattr(dec, "full_exit", False)))
    print("- mode_override:", getattr(dec, "mode_override", None))
    orders = getattr(dec, "orders", []) or []
    print("- orders:", len(orders))
    for i, o in enumerate(orders):
        try:
            print(f"  [{i}] {vars(o)}")
        except Exception:
            print(f"  [{i}] {o}")

    # 상태 핵심만 출력
    for k in (
        "escape_active",
        "escape_long_active",
        "escape_short_active",
        "escape_reason",
        "escape_enter_ts",
        "hedge_side",
        "hedge_size",
        "hedge_pnl_positive_seen_long",
        "hedge_pnl_positive_seen_short",
        "escape_pair_exposure_long",
        "escape_pair_exposure_short",
        "mode",
    ):
        print(f"  state.{k} = {getattr(state, k, None)!r}")


def main() -> None:
    logic = EscapeLogic(DummyCapital())

    print("[CFG]")
    print("  ESCAPE_ON_PNL_PCT =", getattr(CFG, "ESCAPE_ON_PNL_PCT", None))
    print("  FULL_EXIT_PNL_PCT =", getattr(CFG, "FULL_EXIT_PNL_PCT", None))
    print("  HEDGE_ENTRY_MIN_NOTIONAL_USDT =", getattr(CFG, "HEDGE_ENTRY_MIN_NOTIONAL_USDT", None))
    print("  HEDGE_BE_EPS =", getattr(CFG, "HEDGE_BE_EPS", None))

    price = 10000.0

    # ----------------------------------------------------------------------
    # CASE 1) +2% 동시 청산(5.5) 강제 재현:
    # - N_entry(escape_pair_exposure_long)=500 USDT 로 설정
    # - pnl_total(main+hedge) 을 10~20 USDT 이상으로 만들면 2% 초과가 쉬움
    #
    # hedge_pnl 계산은 short_pnl 비율로 추정될 수 있으므로:
    # - short_size == hedge_size 로 두고
    # - short_pnl 을 크게(+) 주면 hedge_pnl 이 커짐
    # ----------------------------------------------------------------------
    st1 = mk_state(
        escape_active=True,
        escape_long_active=True,
        hedge_side="SHORT",
        hedge_size=0.02,
        escape_pair_exposure_long=500.0,  # 2% = 10 USDT
    )
    feed1 = mk_feed(
        st1,
        price=price,
        positions=dict(
            long_size=0.02,   long_pnl=-5.0,     # 메인 손실
            short_size=0.02,  short_pnl=+20.0,   # hedge 방향 이익 크게
            hedge_size=0.02,
        ),
        pnl_total_pct=-0.02,
    )
    dec1 = logic.evaluate(feed1)
    show_decision("CASE1_PAIR_EXIT_FORCE", dec1, st1)

    # ----------------------------------------------------------------------
    # CASE 2) 헷지 본절(BE) 청산(5.4) 강제 재현 (2-step):
    # Step A: hedge_pnl 이 +가 되는 tick 한번 찍어 positive_seen = True 만들기
    # Step B: hedge_pnl 을 0 근처로 떨어뜨려(BE 근처) 청산 트리거 유도
    #
    # 주의: 실제 BE 조건은 escape_logic 구현에 따라 다르므로,
    # orders에 HEDGE_EXIT/PAIR_EXIT 류가 나오는지 확인한다.
    # ----------------------------------------------------------------------
    st2 = mk_state(
        escape_active=True,
        escape_long_active=True,
        hedge_side="SHORT",
        hedge_size=0.02,
        hedge_pnl_positive_seen_long=False,
        escape_pair_exposure_long=999999.0,  # pair exit이 먼저 안 걸리게 크게 잡음
    )

    # Step A: hedge 이익(+)
    feed2a = mk_feed(
        st2,
        price=price,
        positions=dict(
            long_size=0.02,   long_pnl=-10.0,
            short_size=0.02,  short_pnl=+5.0,    # hedge pnl +
            hedge_size=0.02,
        ),
        pnl_total_pct=-0.03,
    )
    dec2a = logic.evaluate(feed2a)
    show_decision("CASE2A_BE_SEEN_POSITIVE", dec2a, st2)

    # Step B: hedge pnl ≈ 0 (BE 근처)
    feed2b = mk_feed(
        st2,
        price=price,
        positions=dict(
            long_size=0.02,   long_pnl=-10.0,
            short_size=0.02,  short_pnl=0.0,     # hedge pnl -> 0
            hedge_size=0.02,
        ),
        pnl_total_pct=-0.03,
    )
    dec2b = logic.evaluate(feed2b)
    show_decision("CASE2B_BE_EXIT_TRIGGER", dec2b, st2)


if __name__ == "__main__":
    main()

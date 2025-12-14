import time
from types import SimpleNamespace

from strategy.escape_logic import EscapeLogic
from strategy import escape_config as CFG


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
        # ESCAPE 강제 ON + 방향 강제(예: SHORT 물림)
        escape_active=True,
        escape_long_active=False,
        escape_short_active=True,
        escape_enter_ts=time.time(),
        # hedge 트래킹 필드
        hedge_side=None,
        hedge_size=0.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def mk_feed(state, price, long_size, long_pnl, short_size, short_pnl, pnl_total_pct=-0.01):
    pos = dict(
        long_size=float(long_size),
        long_pnl=float(long_pnl),
        short_size=float(short_size),
        short_pnl=float(short_pnl),
        hedge_size=0.0,
    )
    return SimpleNamespace(
        state=state,
        price=float(price),
        price_prev=float(price),
        atr_4h_42=100.0,
        pnl_total_pct=float(pnl_total_pct),
        positions=pos,
        news_signal_on=False,
        news_signal_off=False,
    )


def dump_case(title, logic, feed):
    state = feed.state

    print("\n" + "=" * 90)
    print(f"[{title}]")
    print(f"- CFG.HEDGE_ENTRY_MIN_NOTIONAL_USDT = {getattr(CFG, 'HEDGE_ENTRY_MIN_NOTIONAL_USDT', None)}")

    # 1) 공통 메트릭
    m = logic._compute_common_metrics(feed, state)  # type: ignore[attr-defined]
    print("- metrics(m):")
    for k in (
        "price",
        "pnl_total_pct",
        "long_size",
        "short_size",
        "long_pnl",
        "short_pnl",
        "main_side",
        "main_qty",
        "main_notional",
        "hedge_side",
        "hedge_size",
        "hedge_notional",
    ):
        print(f"  {k:>14} = {m.get(k)}")

    # 2) _plan_hedge_orders 단독 호출 결과
    planned = logic._plan_hedge_orders(state, m)  # type: ignore[attr-defined]
    print(f"- plan_orders (direct) = {len(planned)}")
    for i, o in enumerate(planned):
        print(f"  [{i}] {vars(o)}")

    # 3) evaluate() 최종 결과
    dec = logic.evaluate(feed)
    print(f"- evaluate().orders = {len(dec.orders)}")
    for i, o in enumerate(dec.orders):
        print(f"  [{i}] {vars(o)}")

    # 4) evaluate() 이후 state
    print("- state(after evaluate):")
    for k in (
        "mode",
        "escape_active",
        "escape_long_active",
        "escape_short_active",
        "escape_enter_ts",
        "escape_reason",
        "hedge_side",
        "hedge_size",
        "news_block",
        "cb_block",
    ):
        print(f"  {k:>14} = {getattr(state, k, None)!r}")


def main():
    logic = EscapeLogic(DummyCapital())

    price = 10000.0

    # CASE A: 메인 SHORT만 있음(기존 롱 0) → 헤지 진입이 "나와야" 정상(임계 200 이상 되게)
    st_a = mk_state()
    feed_a = mk_feed(
        st_a,
        price=price,
        long_size=0.0,
        long_pnl=0.0,
        short_size=0.03,      # notional = 300
        short_pnl=-10.0,
        pnl_total_pct=-0.02,
    )
    dump_case("A_MAIN_SHORT_ONLY_EXPECT_HEDGE_ENTRY", logic, feed_a)

    # CASE B: 메인 SHORT + 기존 LONG 존재(0.009)
    st_b = mk_state()
    feed_b = mk_feed(
        st_b,
        price=price,
        long_size=0.009,
        long_pnl=1.0,
        short_size=0.02,      # main notional = 200
        short_pnl=-10.0,
        pnl_total_pct=-0.01,
    )
    dump_case("B_MAIN_SHORT_WITH_EXISTING_LONG_0_009", logic, feed_b)

    # CASE C: 메인 SHORT + 기존 LONG 더 큼(0.015)
    st_c = mk_state()
    feed_c = mk_feed(
        st_c,
        price=price,
        long_size=0.015,
        long_pnl=1.0,
        short_size=0.02,
        short_pnl=-10.0,
        pnl_total_pct=-0.01,
    )
    dump_case("C_MAIN_SHORT_WITH_EXISTING_LONG_0_015", logic, feed_c)

    # CASE D: 메인 SHORT를 크게 만들어서(0.06) 기존 LONG이 있어도 "추가 헤지"가 200USDT를 넘게 만들기
    # - 넷팅 방식이면: (0.06 - 0.009) * 10000 = 510 USDT → HEDGE_ENTRY가 나와야 정상
    # - "반대 포지션 있으면 무조건 금지" 방식이면: 여전히 0개
    st_d = mk_state()
    feed_d = mk_feed(
        st_d,
        price=price,
        long_size=0.009,
        long_pnl=1.0,
        short_size=0.06,     # notional = 600
        short_pnl=-20.0,
        pnl_total_pct=-0.03,
    )
    dump_case("D_MAIN_SHORT_0_06_WITH_EXISTING_LONG_0_009", logic, feed_d)

if __name__ == "__main__":
    main()

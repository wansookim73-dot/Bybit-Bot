import pytest

from tests.infra.mock_clock import MockClock
from tests.infra.mock_exchange import MockExchange


def make_env():
    clock = MockClock(start=0.0)
    ex = MockExchange(clock=clock)
    return clock, ex


def test_mode_b_fallback_after_1s():
    """
    [T-EX-02] Mode B – 1초 후 Market Fallback

    - 주문 후 1초 이내에는 원래 지정가 주문이 그대로 있고
    - 1초가 지나면 남은 잔량을 시장가로 강제 체결한다.
    """
    clock, ex = make_env()

    # 1) 초기 상태 정리
    ex.reset_to_flat()
    ex.set_mark_price(10_000.0)

    # 2) Mode B 지정가 주문을 한 개 넣는다.
    order_id = ex.submit_limit_order_mode_b(
        symbol="BTCUSDT",
        side="buy",
        price=9_900.0,
        qty=0.01,
        tag="MODE_B_TEST",
    )

    # 3) 0.5초 경과: 아직 1초가 안 됐으므로
    #    - 지정가 주문은 그대로
    #    - Market 체결은 없어야 한다.
    clock.sleep(0.5)
    ex.tick()

    orders_mid = ex.get_all_orders()
    trades_mid = ex.get_all_trades()

    assert len(orders_mid) == 1
    assert orders_mid[0].id == order_id
    assert len(trades_mid) == 0

    # 4) 추가로 0.6초 경과 → 총 1.1초
    #    이 시점에 tick()을 돌리면
    #    - 지정가 주문은 사라지고
    #    - 남은 qty 전부를 시장가 체결로 기록해야 한다.
    clock.sleep(0.6)
    ex.tick()

    orders_after = ex.get_all_orders()
    trades_after = ex.get_all_trades()

    # 지정가 주문은 없어야 한다.
    assert len(orders_after) == 0

    # Market 체결이 1건 발생해야 한다.
    assert len(trades_after) == 1
    trade = trades_after[0]

    assert trade.order_id == order_id
    assert trade.kind == "market"
    assert trade.qty == pytest.approx(0.01)
    # 시장가는 현재 mark_price 기준
    assert trade.price == pytest.approx(10_000.0)

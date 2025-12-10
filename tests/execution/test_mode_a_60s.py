import pytest

from tests.infra.mock_clock import MockClock
from tests.infra.mock_exchange import MockExchange


def make_env():
    """테스트용 시계 + 모의 거래소 생성."""
    clock = MockClock(start=0.0)
    ex = MockExchange(clock=clock)
    return clock, ex


def test_mode_a_reposts_after_60s():
    """
    [T-EX-01] Mode A – 60초 후 재발주

    - 60초가 지났는데도 체결이 안 되면
      남은 잔량을 전부 취소하고
      같은 가격/같은 수량으로 다시 지정가 주문을 낸다.
    - 이 과정에서 Market 주문은 절대 발생하지 않는다.
    """
    clock, ex = make_env()

    # 1) 초기 상태: 완전 초기화 + 마크가격 설정
    ex.reset_to_flat()
    ex.set_mark_price(30_000.0)

    # 2) Mode A로 일부러 체결 안 되는 지정가 주문 한 개 발행
    first_order = ex.place_limit_order(
        symbol="BTCUSDT",
        side="buy",
        price=29_000.0,   # 현재가보다 아래 → 바로는 체결 안 되는 가격
        qty=0.01,
        mode="A",
        tag="MODE_A_TEST",
    )

    orig_price = first_order.price
    orig_qty = first_order.qty

    # 3) 아직 60초가 안 지난 상태에서 tick() → 아무 변화 없음
    clock.sleep(30.0)
    ex.tick()

    orders_mid = ex.get_all_orders()
    trades_mid = ex.get_all_trades()

    assert len(orders_mid) == 1
    assert orders_mid[0].id == first_order.id
    assert len(trades_mid) == 0

    # 4) 추가로 40초 더 경과 → 총 70초 경과
    #    이 시점에 tick()을 돌리면
    #    기존 주문은 취소되고, 같은 price/qty로 새 주문이 깔려야 한다.
    clock.sleep(40.0)
    ex.tick()

    orders_after = ex.get_all_orders()
    trades_after = ex.get_all_trades()

    # 여전히 지정가 주문은 1개여야 한다.
    assert len(orders_after) == 1
    new_order = orders_after[0]

    # 가격과 수량은 완전히 동일
    assert new_order.price == pytest.approx(orig_price)
    assert new_order.qty == pytest.approx(orig_qty)

    # 새로 깔린 주문이므로 id는 달라야 한다.
    assert new_order.id != first_order.id

    # Mode A에서는 어떤 Market 주문도 나오면 안 된다.
    assert all(tr.kind != "market" for tr in trades_after)

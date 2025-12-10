import pytest

from tests.infra.mock_clock import MockClock
from tests.infra.mock_exchange import MockExchange


def test_mock_infra_sanity():
    clock = MockClock()
    ex = MockExchange(clock=clock)

    # 1) 처음에는 주문/체결이 하나도 없어야 한다.
    assert ex.get_all_orders() == []
    assert ex.get_all_trades() == []

    # 2) 시계를 10초 앞으로
    clock.sleep(10.0)
    assert clock.now() == 10.0

    # 3) 간단한 LIMIT 주문 한 개 넣어보기
    ex.set_mark_price(30_000.0)
    order = ex.place_limit_order(
        symbol="BTCUSDT",
        side="buy",
        price=29_000.0,
        qty=0.01,
        mode="A",   # Mode A (Maker Only)
        tag="SANITY",
    )

    # 4) 주문이 잘 저장되었는지 확인
    all_orders = ex.get_all_orders()
    assert len(all_orders) == 1
    assert all_orders[0].id == order.id
    assert all_orders[0].price == pytest.approx(29_000.0)

    # 5) 주문을 취소하면 주문이 0개가 되어야 한다.
    ex.cancel_order(order.id)
    assert ex.get_all_orders() == []

    # 6) 아직 어떤 체결도 일어나지 않았다.
    assert ex.get_all_trades() == []

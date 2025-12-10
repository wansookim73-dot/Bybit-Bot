import pytest
from main_v10 import WaveBot


class DummyExchange:
    """
    통합 테스트용 가짜 거래소.

    - LONG = 0.005 BTC
    - SHORT = 0.001 BTC
    - total_balance = 900 USDT
    - free_balance  = 800 USDT
    """

    def __init__(self) -> None:
        self._positions = {
            "LONG": {"qty": 0.005},
            "SHORT": {"qty": 0.001},
        }
        # 여러 키 이름을 한꺼번에 넣어줘서
        # _update_market_state 가 어떤 키를 써도 900 / 800 을 읽도록 만든다.
        self._balance = {
            "total": 900.0,
            "total_balance": 900.0,
            "free": 800.0,
            "free_balance": 800.0,
            "available_balance": 800.0,
        }

    def get_positions(self):
        return self._positions

    def get_balance(self):
        return self._balance


def make_bot_with_fakes(monkeypatch: pytest.MonkeyPatch) -> WaveBot:
    """
    실제 ExchangeAPI 대신 DummyExchange 를 꽂은 WaveBot 생성.

    - LONG = 0.005 BTC
    - SHORT = 0.001 BTC
    - total_balance = 900 USDT
    - free_balance  = 800 USDT
    """
    bot = WaveBot()

    # 진짜 exchange 대신 DummyExchange 사용
    bot.exchange = DummyExchange()

    # StateManager.state 를 dict 로 초기화해서 쓰기 쉽게.
    bot.state_manager.state = {}

    return bot


def test_update_market_state_writes_sizes_and_balance(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    WaveBot._update_market_state 가
    - long_size / short_size
    - total_balance / free_balance

    를 StateManager.state 에 올바른 이름으로 써주는지 검증한다.
    """
    bot = make_bot_with_fakes(monkeypatch)

    # 실제 함수 호출
    bot._update_market_state()

    state = bot.state_manager.state

    # 포지션 사이즈
    assert state["long_size"] == pytest.approx(0.005)
    assert state["short_size"] == pytest.approx(0.001)

    # 잔고
    assert state["total_balance"] == pytest.approx(900.0)
    assert state["free_balance"] == pytest.approx(800.0)

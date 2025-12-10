"""
실제 봇 코드와 v10.1 테스트 세트를 연결하기 위한
가장 첫 번째 브리지 테스트 파일입니다.

현재 이 파일은 "틀"만 잡아둔 상태이고,
실제 봇 코드(main 파일/전략 엔진)를 import할 수 없으면
자동으로 pytest.skip() 되도록 만들어져 있습니다.

나중에 실제 봇 코드 구조를 알게 되면,
아래 load_real_bot_module() 안의 import 부분과
test_wave_start_seed_allocation() 안의 TODO 부분을
봇 코드에 맞게 채워 넣으면 됩니다.
"""

import pytest

from tests.infra.mock_clock import MockClock
from tests.infra.mock_exchange import MockExchange


def load_real_bot_module():
    """
    실제 봇의 메인 모듈을 import하는 자리입니다.

    예시)
        - 평소에 `python main_v10.py`로 실행한다면 → import main_v10
        - `python main.py`라면 → import main

    지금은 모듈 이름을 확정하지 않았기 때문에,
    ImportError가 나면 pytest.skip()으로 이 테스트 전체를 건너뜁니다.
    """

    try:
        # ★★★ 여기서 실제 파일 이름에 맞게 수정하면 됩니다. ★★★
        # 예: main_v10.py 파일이 있다면 → import main_v10
        # 예: main.py 파일이 있다면   → import main
        import main_v10 as bot_module  # 필요 시 'main' 등으로 바꿔서 사용
    except ImportError:
        pytest.skip("실제 봇 모듈(main_v10 등)을 아직 연결하지 않음.")
    return bot_module


def test_real_bot_importable_and_can_create_env():
    """
    이 테스트는 "실제 봇 모듈을 가져와서, 최소한 import는 된다"를 확인하는 자리입니다.

    지금 단계에서는:
        - 모듈 import
        - MockClock / MockExchange 준비

    까지만 하고, 실제 전략 로직은 건드리지 않습니다.
    """

    bot_module = load_real_bot_module()  # 모듈이 없으면 여기서 skip

    # 우리의 테스트 환경에서 쓰는 가짜 시계/거래소
    clock = MockClock()
    ex = MockExchange(clock=clock)

    # 여기서부터는 "실제 봇 코드 구조"에 따라 달라집니다.
    # 예를 들어, 봇 안에 이런 함수가 있을 수도 있습니다:
    #
    #   def create_wave_engine(clock, exchange):
    #       ...
    #
    # 그런 경우에는 아래처럼 쓸 수 있습니다:
    #
    #   engine = bot_module.create_wave_engine(clock=clock, exchange=ex)
    #
    # 다만, 현재는 실제 구조를 모르는 상태이므로
    # 아래 줄은 임시로만 남겨둡니다.

    # TODO: 봇 코드 구조를 알게 되면, 위 설명처럼 실제 엔진 생성 코드를 채워 넣으세요.
    # 일단은 "모듈이 정상 import 되고, Mock 인프라를 만들 수 있다"까지만 확인합니다.

    assert clock.now() == 0.0  # MockClock 기본 상태 확인
    assert ex.get_all_orders() == []  # MockExchange 기본 상태 확인


def test_wave_start_seed_allocation_placeholder():
    """
    [미래용] v10.1 Wave 시작 시 Seed/그리드 초기화가
    실제 봇 코드에서도 제대로 되는지를 검증할 자리입니다.

    지금은 실제 봇 엔진 구조를 모른 상태라,
    이 테스트는 바로 pytest.skip()으로 건너뜁니다.

    나중에:
        - 실제 Wave 엔진 생성 함수
        - Wave start 호출 방식
    을 알게 되면, 이 테스트 안에 실제 호출 로직을 채워 넣으면 됩니다.
    """
    pytest.skip("실제 Wave 엔진 구조 파악 전까지는 placeholder로 유지.")

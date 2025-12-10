import pytest

from tests.infra.mock_clock import MockClock
from tests.infra.mock_exchange import MockExchange


@pytest.fixture
def mock_clock():
    return MockClock()


@pytest.fixture
def mock_exchange(mock_clock):
    return MockExchange(clock=mock_clock)

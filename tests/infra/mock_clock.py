class MockClock:
    """
    아주 단순한 가짜 시계.

    - now(): 현재 "가상 시간"을 float 초 단위로 반환
    - sleep(sec): sec 만큼 시간을 앞으로 보냄
    - set_time(ts): 절대 시간으로 설정
    """

    def __init__(self, start: float = 0.0):
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        """seconds 만큼 시간을 앞으로 진행시킨다."""
        self._now += float(seconds)

    def set_time(self, ts: float) -> None:
        """절대 시간을 ts로 설정한다."""
        self._now = float(ts)

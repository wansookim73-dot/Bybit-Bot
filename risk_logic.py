from dataclasses import dataclass
from typing import List


@dataclass
class Candle:
    """
    1분봉 하나를 표현하는 단순 구조체.
    - high, low 만 있으면 우리가 필요한 변동폭 계산은 충분하다.
    """
    high: float
    low: float


def is_in_news_window(now_ts: float, event_ts: float) -> bool:
    """
    [T-RM-01] 뉴스 필터 – 진입 차단

    명세:
      T_event - 60분 ~ T_event + 30분 동안
      -> Start-up / DCA / Refill 진입 계열은 전부 PAUSE.

    여기서는 "지금 시각(now_ts)이 그 구간 안이냐?"만 판단한다.
    (단위는 초 기준이라고 가정)
    """
    start = event_ts - 60 * 60       # 60분 전
    end = event_ts + 30 * 60         # 30분 후
    return start <= now_ts <= end


def is_circuit_breaker_triggered(vol_1m: float,
                                 volume_1m: float,
                                 volume_ma20: float) -> bool:
    """
    [T-RM-02] 서킷 브레이커 – 진입 차단 + 15분 유지

    발동 조건(둘 중 하나라도 참이면 서킷 발동):

      1) vol_1m ≥ 0.006        (1분 변동폭 ≥ 0.6%)
      2) Volume_1m ≥ 4 * MA20  (1분 거래량이 20분 평균의 4배 이상)

    여기서는 "발동 여부"만 판단한다.
    """
    if vol_1m >= 0.006:
        return True
    if volume_1m >= 4.0 * volume_ma20:
        return True
    return False


def is_resume_ready(candles: List[Candle],
                    volume_1m: float,
                    volume_ma20: float,
                    p_gap: float,
                    p_center: float) -> bool:
    """
    [T-RM-03] 재개 조건 충족 여부

    명세:
      1) 최근 3개의 1분봉에 대해:
           (고가-저가)/((고가+저가)/2) ≤ P_gap / P_center
      2) 동시에:
           Volume_1m < 1.5 * Volume_MA20

    → 이 두 조건이 모두 만족되면 Entry PAUSE 해제 가능.
    """

    # 3개 미만이면 '최근 3개 봉'이라는 조건 자체를 만족 못함 → 재개 불가
    if len(candles) < 3:
        return False

    # 마지막 3개 봉만 사용
    last_three = candles[-3:]

    # 변동폭 기준
    if p_center <= 0:
        # 센터 가격이 0 이하인 것은 말이 안 되는 상황이므로
        # 보수적으로 "재개 불가"로 본다.
        return False

    threshold = p_gap / p_center

    for c in last_three:
        mid = (c.high + c.low) / 2.0
        if mid <= 0:
            # 비정상 값 → 재개 불가
            return False
        rel_range = (c.high - c.low) / mid
        if rel_range > threshold:
            # 한 봉이라도 기준보다 요동이 크면 재개 조건 실패
            return False

    # 거래량 기준
    if volume_1m >= 1.5 * volume_ma20:
        return False

    # 위 두 조건을 모두 통과해야 재개 가능
    return True

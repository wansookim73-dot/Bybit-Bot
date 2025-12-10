from risk_logic import (
    Candle,
    is_in_news_window,
    is_circuit_breaker_triggered,
    is_resume_ready,
)


def test_news_filter_window_basic():
    """
    [T-RM-01] 뉴스 필터 – 진입 차단

    T_event - 60분 ~ T_event + 30분 구간에서만 True 여야 한다.
    """
    event_ts = 10_000.0  # 임의 이벤트 시각 (초 단위)

    # 경계 포함 구간: True
    assert is_in_news_window(event_ts - 60 * 60, event_ts)  # 정확히 -60분
    assert is_in_news_window(event_ts, event_ts)            # 이벤트 시각 자체
    assert is_in_news_window(event_ts + 30 * 60, event_ts)  # 정확히 +30분

    # 구간 밖: False
    assert not is_in_news_window(event_ts - 60 * 60 - 1, event_ts)  # -60분보다 1초 더 이전
    assert not is_in_news_window(event_ts + 30 * 60 + 1, event_ts)  # +30분보다 1초 더 이후


def test_circuit_breaker_triggers_on_vol_or_volume():
    """
    [T-RM-02] 서킷 브레이커 발동 조건

    1) vol_1m ≥ 0.006 이면 무조건 발동
    2) volume_1m ≥ 4 * MA20 이면 무조건 발동
    둘 다 아니면 발동하지 않는다.
    """
    # 케이스 1: 변동성 기준만으로 발동
    assert is_circuit_breaker_triggered(
        vol_1m=0.006,
        volume_1m=1000,
        volume_ma20=1000,
    )

    # 케이스 2: 거래량 기준만으로 발동
    assert is_circuit_breaker_triggered(
        vol_1m=0.001,
        volume_1m=4000,
        volume_ma20=1000,
    )

    # 케이스 3: 어느 조건도 만족하지 않으면 발동 X
    assert not is_circuit_breaker_triggered(
        vol_1m=0.005,
        volume_1m=3999,
        volume_ma20=1000,
    )


def test_resume_ready_true_when_candles_and_volume_ok():
    """
    [T-RM-03] 재개 조건 – 정상 케이스

    최근 3개 봉의 변동폭이 모두 P_gap/P_center 이하이고,
    Volume_1m < 1.5 * MA20 이면 True.
    """
    p_center = 30_000.0
    p_gap = 150.0  # → 임계값 threshold = 150/30000 = 0.005

    # 각 봉의 변동폭 비율이 0.005보다 작도록 설계
    candles = [
        Candle(high=30_050.0, low=30_000.0),  # range=50, mid~30025, 비율~0.0016
        Candle(high=30_080.0, low=30_030.0),  # range=50, mid~30055, 비율~0.0016
        Candle(high=30_100.0, low=30_050.0),  # range=50, mid~30075, 비율~0.0016
    ]

    volume_1m = 1000.0
    volume_ma20 = 1000.0  # → 1.5 * MA20 = 1500

    assert is_resume_ready(
        candles=candles,
        volume_1m=volume_1m,
        volume_ma20=volume_ma20,
        p_gap=p_gap,
        p_center=p_center,
    )


def test_resume_ready_false_when_range_too_large_or_volume_high():
    """
    [T-RM-03] 재개 조건 – 실패 케이스들
    1) 봉 하나라도 변동폭이 기준보다 크면 False
    2) 거래량이 1.5 * MA20 이상이면 False
    """
    p_center = 30_000.0
    p_gap = 150.0  # threshold = 0.005

    # 첫 번째 봉의 변동폭이 너무 큰 케이스
    candles_wide = [
        Candle(high=31_000.0, low=30_000.0),  # range=1000, mid~30500 → 비율~0.0328 (기준 초과)
        Candle(high=30_080.0, low=30_030.0),
        Candle(high=30_100.0, low=30_050.0),
    ]

    assert not is_resume_ready(
        candles=candles_wide,
        volume_1m=1000.0,
        volume_ma20=1000.0,
        p_gap=p_gap,
        p_center=p_center,
    )

    # 봉은 괜찮지만 거래량이 너무 큰 케이스
    candles_ok = [
        Candle(high=30_050.0, low=30_000.0),
        Candle(high=30_080.0, low=30_030.0),
        Candle(high=30_100.0, low=30_050.0),
    ]

    assert not is_resume_ready(
        candles=candles_ok,
        volume_1m=2000.0,    # 1.5 * 1000 = 1500 → 2000은 기준 초과
        volume_ma20=1000.0,
        p_gap=p_gap,
        p_center=p_center,
    )


def test_resume_ready_requires_three_candles():
    """
    [T-RM-03] 재개 조건 – 최소 3개 봉 필요.

    2개만 있으면 최근 3개 봉 조건을 만족할 수 없으므로 False.
    """
    p_center = 30_000.0
    p_gap = 150.0

    candles_two = [
        Candle(high=30_050.0, low=30_000.0),
        Candle(high=30_080.0, low=30_030.0),
    ]

    assert not is_resume_ready(
        candles=candles_two,
        volume_1m=1000.0,
        volume_ma20=1000.0,
        p_gap=p_gap,
        p_center=p_center,
    )

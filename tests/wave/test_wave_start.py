from wave_init import init_wave


def test_wave_start_seed_and_grid_basic():
    """
    [T-WS-01] Flat 상태에서 Wave 시작

    - 25/25/50 seed 배분
    - unit_seed = allocated / 13
    - P_center, P_gap, grid 범위 체크
    - k_long, k_short = 0
    """
    total_balance = 1000.0
    current_price = 30000.0
    atr_4h = 200.0  # 200 * 0.15 = 30 < 100 이므로 P_gap = 100

    state = init_wave(total_balance, current_price, atr_4h)

    seed = state.seed
    grid = state.grid

    # Seed 배분
    assert seed.total_balance_snap == total_balance
    assert seed.allocated_seed_long == total_balance * 0.25
    assert seed.allocated_seed_short == total_balance * 0.25
    assert seed.reserve_seed == total_balance * 0.50

    # Unit seed
    assert seed.unit_seed_long == seed.allocated_seed_long / 13.0
    assert seed.unit_seed_short == seed.allocated_seed_short / 13.0

    # 분할 카운터 초기값
    assert seed.k_long == 0
    assert seed.k_short == 0

    # Grid 기본 설정
    assert grid.p_center == current_price
    assert grid.p_gap == 100.0  # ATR*0.15 보다 100이 크므로 100

    assert grid.long_min_index == -12
    assert grid.long_max_index == 7
    assert grid.short_min_index == -7
    assert grid.short_max_index == 12

    # line_price 헬퍼 간단 체크
    assert grid.line_price(0) == current_price
    assert grid.line_price(-7) == current_price + (-7) * grid.p_gap

    # 상태 플래그
    assert state.status == "INIT"

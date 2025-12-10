from wave_lifecycle import (
    WaveSeeds,
    GridConfig,
    compute_wave_seeds,
    compute_grid_config,
    can_start_new_wave,
    should_end_wave,
)


def test_wave_start_seed_and_grid_basic():
    """
    [T-WS-01] Flat 상태에서 Wave 시작

    - Total_Balance_snap 기준 25/25/50 배분
    - 방향별 unit seed = allocated / 13
    - P_gap = max(ATR_4H * 0.15, 100)
    - P_center는 전달받은 현재가
    - Long/Short 운용 범위 인덱스가 명세와 동일
    """
    total_balance = 10_000.0
    seeds: WaveSeeds = compute_wave_seeds(total_balance)

    # Seed 배분 25/25/50
    assert seeds.allocated_seed_long == total_balance * 0.25
    assert seeds.allocated_seed_short == total_balance * 0.25
    assert seeds.reserve_seed == total_balance * 0.50

    # 13분할 unit seed
    assert abs(seeds.unit_seed_long * 13.0 - seeds.allocated_seed_long) < 1e-6
    assert abs(seeds.unit_seed_short * 13.0 - seeds.allocated_seed_short) < 1e-6

    # Grid 설정
    p_center = 30_000.0
    atr_4h = 1_000.0  # → P_gap = max(150, 100) = 150

    grid: GridConfig = compute_grid_config(p_center=p_center, atr_4h=atr_4h)

    assert grid.p_center == p_center
    assert grid.p_gap == 150.0

    # 운용 범위 인덱스
    assert grid.long_min_line == -12
    assert grid.long_max_line == 7
    assert grid.short_min_line == -7
    assert grid.short_max_line == 12


def test_grid_gap_minimum_100_when_atr_small():
    """
    [T-WS-01 보조] ATR이 작을 때도 P_gap은 최소 100을 유지해야 한다.
    """
    p_center = 30_000.0
    atr_4h = 200.0  # 0.15 * 200 = 30 < 100

    grid = compute_grid_config(p_center=p_center, atr_4h=atr_4h)
    assert grid.p_gap == 100.0


def test_can_start_new_wave_only_when_flat():
    """
    [T-WS-02] Flat이 아닐 때 Wave 시작 시도

    - Long/Short 둘 다 0일 때만 새 Wave 시작 허용
    - 어느 한쪽이라도 포지션이 있으면 False
    """
    # 완전 Flat → True
    assert can_start_new_wave(0.0, 0.0)

    # Long만 열려 있음 → False
    assert not can_start_new_wave(0.01, 0.0)

    # Short만 열려 있음 → False
    assert not can_start_new_wave(0.0, -0.02)

    # 부동소수점 오차 수준의 미세한 qty는 0으로 간주
    assert can_start_new_wave(1e-13, -1e-13)


def test_wave_ends_when_positions_back_to_flat():
    """
    [T-WL-01] Wave 종료 및 New Game

    - Long/Short 포지션 합이 다시 0이 되면 Wave 종료 조건을 만족한 것으로 본다.
    - 종료 조건은 '새 Wave를 시작할 수 있는 상태'와 동일하게 설계.
    """
    # 포지션이 살아 있을 때는 종료 아님
    assert not should_end_wave(0.05, 0.0)
    assert not should_end_wave(0.0, -0.03)

    # 다시 완전 Flat이 되면 종료 조건 충족
    assert should_end_wave(0.0, 0.0)
    # 미세한 오차 수준은 0으로 간주
    assert should_end_wave(1e-13, -1e-13)

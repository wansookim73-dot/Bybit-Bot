from dataclasses import dataclass


@dataclass
class GridConfig:
    """
    그리드 설정:
    - P_center: Wave 시작 시점의 현재가
    - P_gap: max(ATR_4H * 0.15, 100)
    - long, short 각 방향의 운용 인덱스 범위
    """
    p_center: float
    p_gap: float
    long_min_index: int
    long_max_index: int
    short_min_index: int
    short_max_index: int

    def line_price(self, index: int) -> float:
        """그리드 인덱스에서 실제 가격 계산."""
        return self.p_center + index * self.p_gap


@dataclass
class SeedState:
    """
    Seed 상태:
    - Total_Balance_snap
    - Long/Short 25/25/50 배분
    - 방향별 unit_seed = allocated / 13
    - k_long, k_short: 현재 분할 카운터 (초기 0)
    """
    total_balance_snap: float
    allocated_seed_long: float
    allocated_seed_short: float
    reserve_seed: float
    unit_seed_long: float
    unit_seed_short: float
    k_long: int
    k_short: int


@dataclass
class WaveState:
    """
    Wave 전체 상태를 한 번에 들고 있는 구조체.
    - seed: SeedState
    - grid: GridConfig
    - status: "INIT" (Wave 시작 직후 상태)
    """
    seed: SeedState
    grid: GridConfig
    status: str  # "INIT" 등


def init_wave(total_balance_snap: float, current_price: float, atr_4h: float) -> WaveState:
    """
    Wave 시작 시점에 seed / grid / 상태를 초기화한다.
    v10.1 명세의 [2.2], [2.3], [3.1]을 그대로 구현.

    - Seed 25/25/50 배분
    - unit_seed_dir = allocated_seed_dir / 13
    - P_center = 현재가
    - P_gap = max(ATR_4H * 0.15, 100)
    - Long 운용 범위:  -12 ~ +7
    - Short 운용 범위: -7  ~ +12
    """
    # 1) Seed 배분 25/25/50
    allocated_seed_long = total_balance_snap * 0.25
    allocated_seed_short = total_balance_snap * 0.25
    reserve_seed = total_balance_snap * 0.50

    # 2) 방향별 unit_seed = allocated / 13
    unit_seed_long = allocated_seed_long / 13.0
    unit_seed_short = allocated_seed_short / 13.0

    # 3) 그리드 설정
    p_center = float(current_price)
    p_gap = max(atr_4h * 0.15, 100.0)

    grid = GridConfig(
        p_center=p_center,
        p_gap=p_gap,
        long_min_index=-12,
        long_max_index=7,
        short_min_index=-7,
        short_max_index=12,
    )

    seed_state = SeedState(
        total_balance_snap=total_balance_snap,
        allocated_seed_long=allocated_seed_long,
        allocated_seed_short=allocated_seed_short,
        reserve_seed=reserve_seed,
        unit_seed_long=unit_seed_long,
        unit_seed_short=unit_seed_short,
        k_long=0,
        k_short=0,
    )

    return WaveState(
        seed=seed_state,
        grid=grid,
        status="INIT",
    )

from dataclasses import dataclass
from wave_init import WaveState


@dataclass
class StartupDecision:
    """Start-up Entry 여부를 나타내는 단순 구조체."""
    enter_long: bool
    enter_short: bool


# Overlap Zone: Line -7 ~ +7
OVERLAP_MIN_INDEX = -7
OVERLAP_MAX_INDEX = 7


def _remain_seed_long(seed) -> float:
    """Long 방향 remain_seed 계산."""
    return seed.allocated_seed_long - seed.k_long * seed.unit_seed_long


def _remain_seed_short(seed) -> float:
    """Short 방향 remain_seed 계산."""
    return seed.allocated_seed_short - seed.k_short * seed.unit_seed_short


def decide_startup_entry(
    state: WaveState,
    pos_long_qty: float,
    pos_short_qty: float,
    current_line_index: int,
) -> StartupDecision:
    """
    v10.1 명세 3.2.3 (Start-up Entry) + 2.3.5 (마지막 조각 규칙)을 그대로 구현한 순수 로직.

    조건:
    - 현재 라인 인덱스가 Overlap Zone(Line -7 ~ +7) 안이고
    - 해당 방향 포지션 수량이 0이며
    - 해당 방향 remain_seed_dir ≥ unit_seed_dir 이면
      그 방향에 대해 1분할 Start-up Entry 허용.
    """
    seed = state.seed
    grid = state.grid

    remain_long = _remain_seed_long(seed)
    remain_short = _remain_seed_short(seed)

    enter_long = False
    enter_short = False

    # 공통: Overlap Zone 여부
    in_overlap = OVERLAP_MIN_INDEX <= current_line_index <= OVERLAP_MAX_INDEX

    # Long 방향 Start-up 판단
    if pos_long_qty == 0.0 and in_overlap:
        # Long 진입 허용 범위: Line -12 ~ +7
        if grid.long_min_index <= current_line_index <= grid.long_max_index:
            if remain_long >= seed.unit_seed_long:
                enter_long = True

    # Short 방향 Start-up 판단
    if pos_short_qty == 0.0 and in_overlap:
        # Short 진입 허용 범위: Line -7 ~ +12
        if grid.short_min_index <= current_line_index <= grid.short_max_index:
            if remain_short >= seed.unit_seed_short:
                enter_short = True

    return StartupDecision(enter_long=enter_long, enter_short=enter_short)

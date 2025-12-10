from dataclasses import dataclass


@dataclass
class WaveSeeds:
    """
    Wave 시작 시점 Seed 구조.

    - 명세 2.2, 2.3: 25/25/50 배분 + 13분할
    """
    total_balance_snap: float
    allocated_seed_long: float
    allocated_seed_short: float
    reserve_seed: float
    unit_seed_long: float
    unit_seed_short: float


@dataclass
class GridConfig:
    """
    Wave 시작 시점 Grid 구조.

    - 명세 3.1: P_center, P_gap, 운용 범위
    """
    p_center: float
    p_gap: float
    long_min_line: int = -12
    long_max_line: int = 7
    short_min_line: int = -7
    short_max_line: int = 12


_EPS = 1e-12


def _is_effectively_zero(x: float) -> bool:
    """
    부동소수점 오차를 고려한 '사실상 0' 판정.
    """
    return abs(x) <= _EPS


def compute_wave_seeds(total_balance_snap: float) -> WaveSeeds:
    """
    [T-WS-01] Flat 상태에서 Wave 시작 시 Seed 계산.

    명세 2.2 / 2.3:
      - Long  : 25%
      - Short : 25%
      - Reserve: 50%
      - 방향별 Unit Seed = allocated_seed_dir / 13
    """
    allocated_seed_long = total_balance_snap * 0.25
    allocated_seed_short = total_balance_snap * 0.25
    reserve_seed = total_balance_snap * 0.50

    unit_seed_long = allocated_seed_long / 13.0
    unit_seed_short = allocated_seed_short / 13.0

    return WaveSeeds(
        total_balance_snap=total_balance_snap,
        allocated_seed_long=allocated_seed_long,
        allocated_seed_short=allocated_seed_short,
        reserve_seed=reserve_seed,
        unit_seed_long=unit_seed_long,
        unit_seed_short=unit_seed_short,
    )


def compute_grid_config(p_center: float, atr_4h: float) -> GridConfig:
    """
    [T-WS-01] Wave 시작 시 Grid 계산.

    명세 3.1:
      P_gap = max( ATR_4H * 0.15 , 100 )
      P_center = Wave 시작 시점 현재가
      Long 범위:  -12 ~ +7
      Short 범위: -7  ~ +12
    """
    p_gap = max(atr_4h * 0.15, 100.0)

    return GridConfig(
        p_center=p_center,
        p_gap=p_gap,
        long_min_line=-12,
        long_max_line=7,
        short_min_line=-7,
        short_max_line=12,
    )


def can_start_new_wave(long_pos_qty: float, short_pos_qty: float) -> bool:
    """
    [T-WS-02] Wave 시작 허용 조건.

    명세 2.1, 7.1:
      - Wave는 항상 계정이 완전 Flat일 때만 시작.
      - Long/Short 포지션이 모두 0이어야 한다.

    (Escape 헷지는 결국 Long/Short 포지션에 포함된다고 보고,
     여기서는 단순히 두 방향 모두 0인지 여부만 본다.)
    """
    return _is_effectively_zero(long_pos_qty) and _is_effectively_zero(short_pos_qty)


def should_end_wave(long_pos_qty: float, short_pos_qty: float) -> bool:
    """
    [T-WL-01] Wave 종료 기준.

    명세 7.4:
      - Long/Short 양 방향의 포지션이 모두 0이 되었을 때
        → Wave 종료 및 Reset 대상.

    여기서는 '완전 Flat인가?'만 판단하고,
    실제 Reset 동작(Seed 재계산, Grid 재설정)은
    상위 레이어에서 compute_wave_seeds / compute_grid_config를 다시 호출하는 식으로 구현하면 된다.
    """
    # 종료 조건은 "새 Wave를 시작할 수 있는 상태"와 동일하게 본다.
    return can_start_new_wave(long_pos_qty, short_pos_qty)

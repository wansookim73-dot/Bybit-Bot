from dataclasses import dataclass
from typing import Literal, Optional, Set
import math

Direction = Literal["long", "short"]


@dataclass
class TpDecision:
    """
    TP(익절) 한 스텝에 대한 의사결정 결과를 담는 구조체.
    """
    should_tp: bool
    profit_line_index: Optional[int] = None
    new_k: Optional[int] = None
    new_used_seed: Optional[float] = None
    new_remain_seed: Optional[float] = None


def compute_profit_line_index(
    direction: Direction,
    avg_entry: float,
    current_price: float,
    p_gap: float,
) -> int:
    """
    v10.1 명세의 profit_line_index 정의를 그대로 구현.

    Long:
        floor( (현재가 - avg_entry) / P_gap )

    Short:
        floor( (avg_entry - 현재가) / P_gap )
    """
    if p_gap <= 0:
        raise ValueError("p_gap must be positive")

    if direction == "long":
        diff = current_price - avg_entry
    elif direction == "short":
        diff = avg_entry - current_price
    else:
        raise ValueError(f"invalid direction: {direction}")

    # floor와 동일한 동작을 위해 math.floor 사용
    return int(math.floor(diff / p_gap))


def decide_tp_step(
    profit_line_index: int,
    used_tp_lines: Set[int],
    k_dir: int,
    allocated_seed_dir: float,
    unit_seed: float,
) -> TpDecision:
    """
    v10.1 TP 로직의 핵심 부분을 순수 함수로 표현.

    - profit_line_index ≥ 3 일 때만 TP 시작
    - 각 profit_line_index는 Wave+방향 기준 1번만 TP에 사용
    - TP 1회 발생 시:
        k_dir -= 1
        used_seed_dir   = k_dir * unit_seed
        remain_seed_dir = allocated_seed_dir - used_seed_dir

    여기서는 '현재 profit_line_index에 대해 이번 틱에서 TP를 해야 하는지'만 판단한다.
    (한 번에 한 라인씩만 처리하는 최소 버전)
    """
    # 포지션이 없으면 TP 불가
    if k_dir <= 0:
        return TpDecision(should_tp=False)

    # 아직 평단+3 라인에 도달하지 못했으면 TP 불가
    if profit_line_index < 3:
        return TpDecision(should_tp=False)

    # 이미 이 profit_line_index에서 TP를 한 번 수행했다면 재사용 불가
    if profit_line_index in used_tp_lines:
        return TpDecision(should_tp=False)

    # 여기까지 왔으면 이번 틱에서 이 라인에 대해 1스텝 TP 허용
    new_k = k_dir - 1
    new_used_seed = new_k * unit_seed
    new_remain_seed = allocated_seed_dir - new_used_seed

    return TpDecision(
        should_tp=True,
        profit_line_index=profit_line_index,
        new_k=new_k,
        new_used_seed=new_used_seed,
        new_remain_seed=new_remain_seed,
    )

from dataclasses import dataclass


@dataclass
class LineMemoryResetDecision:
    """
    라인 메모리 리셋 여부와,
    '최근 리셋 이후 한 번이라도 PnL이 0 이상이었는지' 플래그를 함께 관리.
    """
    should_reset: bool
    new_had_non_negative: bool


def decide_line_memory_reset(
    pos_size_before: float,
    pos_size_now: float,
    pnl_before: float,
    pnl_now: float,
    had_non_negative_since_last_reset: bool,
) -> LineMemoryResetDecision:
    """
    v10.1 명세의 라인 메모리 리셋 조건을 순수 함수로 표현.

    1) 포지션이 완전히 0이 되는 순간 (전량 익절/청산):
       -> 라인 메모리 리셋

    2) 부분 익절 후 PnL이 0 이상(>= 0) 구간에 한 번이라도 머문 적이 있고,
       이후 다시 PnL이 음수(< 0)로 내려가는 순간:
       -> 라인 메모리 리셋

    리셋이 발생하면 had_non_negative 플래그도 False 로 초기화한다.
    """

    # 절대값 기준으로 포지션 유무 판단 (롱/숏 모두 허용)
    had_pos_before = abs(pos_size_before) > 0.0
    has_pos_now = abs(pos_size_now) > 0.0

    # 1) 포지션 완전 종료 → 무조건 리셋 (수익/손실 여부 관계 없음)
    if had_pos_before and not has_pos_now:
        return LineMemoryResetDecision(
            should_reset=True,
            new_had_non_negative=False,
        )

    # 여기까지 왔으면 아직 포지션이 남아있다.
    new_flag = had_non_negative_since_last_reset

    # 이번 틱 PnL이 0 이상이면 "수익 구간에 한 번이라도 진입" 플래그를 킨다.
    if pnl_now >= 0.0:
        new_flag = True

    # 2) 수익 구간(>=0)을 한 번이라도 지나간 뒤,
    #    다시 손실 구간(<0)으로 재진입하는 '크로스 지점'에서 리셋.
    #    (pnl_before >= 0 이었고, pnl_now < 0 으로 떨어지는 순간)
    if new_flag and (pnl_before >= 0.0) and (pnl_now < 0.0):
        return LineMemoryResetDecision(
            should_reset=True,
            new_had_non_negative=False,
        )

    # 그 외에는 리셋 없음
    return LineMemoryResetDecision(
        should_reset=False,
        new_had_non_negative=new_flag,
    )


@dataclass
class FullCloseSeedResetResult:
    """
    전량 익절/청산 시 seed 회계를 v10.1 명세대로 초기화한 결과.

    - k_dir = 0
    - used_seed_dir = 0
    - remain_seed_dir = allocated_seed_dir
    """
    new_k: int
    new_used_seed: float
    new_remain_seed: float


def reset_seed_after_full_close(
    allocated_seed_dir: float,
    unit_seed: float,  # 명세상 인자를 받지만 계산에는 사용되지 않는다.
) -> FullCloseSeedResetResult:
    """
    v10.1 3.3.5 / 3.3.6 에 따라,
    해당 방향 포지션이 완전히 0이 되는 순간:

      - k_dir = 0
      - used_seed_dir = 0
      - remain_seed_dir = allocated_seed_dir

    로 초기화한다.
    """
    return FullCloseSeedResetResult(
        new_k=0,
        new_used_seed=0.0,
        new_remain_seed=allocated_seed_dir,
    )

from dataclasses import dataclass
from typing import Literal, Optional, Set


Direction = Literal["long", "short"]


@dataclass
class DcaDecision:
    """
    DCA 진입 여부를 나타내는 단순 구조체.

    - should_enter: 이번 틱에서 새 1분할 DCA를 넣을지 여부
    - line_index: DCA를 넣게 된다면, 어떤 라인 인덱스에서 넣는지
    """
    should_enter: bool
    line_index: Optional[int] = None


def decide_dca_entry(
    direction: Direction,
    pnl: float,
    prev_line_index: int,
    current_line_index: int,
    used_dca_lines: Set[int],
    remain_seed: float,
    unit_seed: float,
) -> DcaDecision:
    """
    v10.1 명세의 DCA 핵심 조건을 그대로 구현한 순수 로직.

    조건 요약 (방향별 독립):

    1) 해당 방향 포지션 미실현 PnL < 0 (손실 상태)
    2) 가격이 손실 방향으로 이동하여 '새로운' 라인 L_n을 터치
       - Long: 위 → 아래 (라인 인덱스 감소)
       - Short: 아래 → 위 (라인 인덱스 증가)
    3) 그 라인 L_n은 아직 DCA/Start-up/Refill에 사용되지 않은 라인
       (라인 메모리 집합 used_dca_lines에 포함되지 않을 것)
    4) remain_seed_dir ≥ unit_seed_dir

    여기서는 테스트 단순화를 위해
    '한 틱에 한 라인만 새로 통과한다'는 케이스만 다룬다.
    (멀티 라인 통과는 이후 확장 가능)
    """

    # 1) PnL이 손실 상태가 아니면 DCA 불가
    if pnl >= 0:
        return DcaDecision(should_enter=False, line_index=None)

    # 2) 손실 방향으로 실제로 한 칸 움직였는지 확인
    if direction == "long":
        # Long 손실 방향: 아래쪽으로 내려가는 것 (라인 인덱스 감소)
        if current_line_index >= prev_line_index:
            # 위로 갔거나 그대로면 손실 방향 이동이 아님
            return DcaDecision(should_enter=False, line_index=None)
        target_line = current_line_index
    else:  # direction == "short"
        # Short 손실 방향: 위쪽으로 올라가는 것 (라인 인덱스 증가)
        if current_line_index <= prev_line_index:
            return DcaDecision(should_enter=False, line_index=None)
        target_line = current_line_index

    # 3) 이미 사용한 라인인지(라인 메모리) 체크
    if target_line in used_dca_lines:
        return DcaDecision(should_enter=False, line_index=None)

    # 4) seed 잔량 체크: remain_seed_dir ≥ unit_seed_dir 이어야 함
    if remain_seed < unit_seed:
        return DcaDecision(should_enter=False, line_index=None)

    # 모든 조건을 통과하면 이번 틱에서 해당 라인에 1분할 DCA 허용
    return DcaDecision(should_enter=True, line_index=target_line)

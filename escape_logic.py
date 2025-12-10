from dataclasses import dataclass
from typing import Optional

# v10.1 명세 기준 Escape 상태 문자열
ESCAPE_STATUS_NONE = "NONE"
ESCAPE_STATUS_PENDING = "PENDING"
ESCAPE_STATUS_ACTIVE = "ACTIVE"
ESCAPE_STATUS_DONE = "DONE"

# Escape Hedge 실행 모드는 항상 Mode B
ESCAPE_HEDGE_EXECUTION_MODE = "B"


@dataclass
class EscapeTriggerResult:
    """
    Escape Trigger Line 설정 결과.

    - should_set_trigger: 이번 터치에서 새 Trigger Line 을 기록해야 하는지 여부
    - new_trigger_line_index: 기록 후 유지할 Trigger Line 인덱스 (없으면 None)
    """
    should_set_trigger: bool
    new_trigger_line_index: Optional[int]


def decide_escape_trigger_line(
    *,
    pos_pnl: float,
    remain_seed_dir: float,
    unit_seed: float,
    current_trigger_line_index: Optional[int],
    touched_line_index: int,
    direction: str,
) -> EscapeTriggerResult:
    """
    [v10.1 5.1] Seed 기반 Escape Trigger 규칙을 순수 함수로 표현.

    조건:
      1) 해당 방향 포지션 PnL < 0 (손실 상태)
      2) remain_seed_dir ≤ unit_seed
      3) 아직 Escape_Trigger_Line 이 설정되지 않은 상태

    동작:
      - Long 물림  → Escape_Trigger_Line = L_{k-1}
      - Short 물림 → Escape_Trigger_Line = L_{k+1}

    한 Wave·방향당 Trigger Line 은 한 번만 기록(갱신 없음).
    """
    # 이미 Trigger 가 설정돼 있으면 더 이상 갱신하지 않는다.
    if current_trigger_line_index is not None:
        return EscapeTriggerResult(
            should_set_trigger=False,
            new_trigger_line_index=current_trigger_line_index,
        )

    # 손실이 아니면 Escape Trigger 자체가 없다.
    if pos_pnl >= 0.0:
        return EscapeTriggerResult(
            should_set_trigger=False,
            new_trigger_line_index=current_trigger_line_index,
        )

    # 남은 seed 가 unit_seed 를 초과하면 아직 Escape 단계가 아니다.
    if remain_seed_dir > unit_seed:
        return EscapeTriggerResult(
            should_set_trigger=False,
            new_trigger_line_index=current_trigger_line_index,
        )

    d = direction.lower()
    if d == "long":
        trigger_line = touched_line_index - 1
    elif d == "short":
        trigger_line = touched_line_index + 1
    else:
        raise ValueError("direction must be 'long' or 'short'")

    return EscapeTriggerResult(
        should_set_trigger=True,
        new_trigger_line_index=trigger_line,
    )


def compute_escape_hedge_qty(
    q_main: float,
    q_max: float,
) -> float:
    """
    [v10.1 5.2] Escape Hedge 수량 계산.

      Q_hedge = min( 2 * |Q_main| , Q_max )
    """
    base = abs(q_main) * 2.0
    return min(base, q_max)


def is_wave_action_allowed(escape_status: str) -> bool:
    """
    [v10.1 5.3] ESCAPE_ACTIVE 상태에서 일반 웨이브 동작 금지 규칙.

    - ESCAPE_ACTIVE 인 방향  → Start-up / DCA / Refill / Grid TP 전부 금지
    - 그 외 상태(NONE/PENDING/DONE) → 웨이브 로직 허용
    """
    return escape_status != ESCAPE_STATUS_ACTIVE


@dataclass
class EscapeExitDecision:
    """
    Escape 상태에서 한 틱 기준으로 어떤 액션을 취할지 결정한 결과.

    action:
      - "NONE"               : 아무 것도 하지 않음
      - "HEDGE_BE_CLOSE"     : 헷지 포지션만 본절(시장가 전량 청산)
      - "FULL_CLOSE_PLUS_2"  : 본체 + 헷지를 +2% 수익 기준으로 동시에 청산

    new_hedge_had_positive:
      - '헷지 PnL 이 한 번이라도 > 0 이었는지' 플래그의 갱신 결과
    """
    action: str
    new_hedge_had_positive: bool


def decide_escape_exit_action(
    *,
    pnl_main: float,
    pnl_hedge: float,
    n_exposure: float,
    hedge_had_positive_before: bool,
) -> EscapeExitDecision:
    """
    [v10.1 5.4 / 5.5] Escape 종료 조건(+2% 동시 청산 / 헷지 BE 청산) 우선순위 로직.

    1) +2% 동시 청산 조건 (우선순위 1)
        PnL_total = PnL_main + PnL_hedge
        Threshold = N_exposure * 0.02

        PnL_total ≥ Threshold 이면
          → 본체 + 헷지 포지션을 전부 Market 으로 동시에 청산
          → action = "FULL_CLOSE_PLUS_2"

    2) 헷지 단독 BE 청산 (우선순위 2)
        - 헷지 PnL 이 한 번이라도 양수(>0) 였던 적이 있고
        - 이후 다시 0 으로 돌아온 순간 (단순화: pnl_hedge == 0.0 으로 판단)
          → 헷지 포지션만 Market 으로 전량 청산
          → action = "HEDGE_BE_CLOSE"

    3) 그 외에는 아무 것도 하지 않는다.
    """
    # 헷지가 한 번이라도 수익 구간(>0)에 진입했는지 플래그 갱신
    had_positive = hedge_had_positive_before or (pnl_hedge > 0.0)

    pnl_total = pnl_main + pnl_hedge
    threshold = 0.02 * n_exposure if n_exposure > 0.0 else 0.0

    # 1) +2% 동시 청산이 항상 우선
    if n_exposure > 0.0 and pnl_total >= threshold:
        return EscapeExitDecision(
            action="FULL_CLOSE_PLUS_2",
            new_hedge_had_positive=had_positive,
        )

    # 2) 헷지 단독 BE 청산 (한 번이라도 +였고, 다시 0으로 복귀하는 지점)
    if had_positive and pnl_hedge == 0.0:
        return EscapeExitDecision(
            action="HEDGE_BE_CLOSE",
            new_hedge_had_positive=False,
        )

    # 3) 아무 일도 없음
    return EscapeExitDecision(
        action="NONE",
        new_hedge_had_positive=had_positive,
    )

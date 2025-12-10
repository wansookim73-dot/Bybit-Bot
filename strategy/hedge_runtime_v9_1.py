from __future__ import annotations

from typing import Any

from utils.logger import get_logger
from .escape_logic import evaluate_hedge_v9_1_from_context


class HedgeRuntime:
    """
    v9.1 Hedge Runtime 스켈레톤.

    v10.1 현재 단계에서는:
      - WaveBot.loop_once 에서 hedge_runtime.on_tick(state, decision, ctx)를 호출해도
        실제로 헷지 포지션을 열거나 닫지 않는다.
      - decide_hedge_runtime_v9_1() 를 통해 context 기반 평가를 할 수 있도록
        연결만 제공하고, 기본 동작은 로그 전용이다.
    """

    def __init__(self) -> None:
        self.logger = get_logger("hedge_runtime_v9_1")

    def on_tick(self, state: Any, decision: Any, ctx: Any) -> None:
        """
        WaveBot.loop_once 에서 호출되는 엔트리 포인트.

        현재 C-1차 복원 단계에서는:
          - hedge runtime 이 실제로 포지션을 조작하지 않는다.
          - 필요하다면 나중에 state/decision/ctx 를 바탕으로
            decide_hedge_runtime_v9_1(...) 를 호출해 로그만 남길 수 있다.
        """
        # 스켈레톤: 아무것도 하지 않음 (action=NONE)
        self.logger.debug(
            "HedgeRuntime v9.1 skeleton: no hedge action (state=%s, decision_mode=%s)",
            getattr(state, "mode", None),
            getattr(decision, "mode", None),
        )


def decide_hedge_runtime_v9_1(
    *,
    side_seed: float,
    used_seed: float,
    grid_split_count: int,
    next_loss_line_touched: bool = False,
    pnl_base: float,
    pnl_hedge: float,
    total_notional: float,
    hedge_notional: float,
    target_combined_pct: float = 0.02,
    breakeven_eps_usdt: float = 0.5,
) -> dict:
    """
    v9.1 헷지 의사결정 런타임 어댑터.

    main 루프 / 포지션 매니저 쪽에서는 이 함수만 호출하면 됨.

    매개변수 설명:
      - side_seed: 해당 방향에 배정된 시드 (예: total_balance * 0.25)
      - used_seed: 지금까지 이 방향에 실제로 투입된 금액
      - grid_split_count: GRID_SPLIT_COUNT (예: 13)
      - next_loss_line_touched: "다음 손실 라인" 막 터치했는지 여부 (True/False)
      - pnl_base: 본체 포지션 PnL (USDT)
      - pnl_hedge: 헷지 포지션 PnL (USDT, 없으면 0)
      - total_notional: 본체 + 헷지 전체 명목가 (USDT)
      - hedge_notional: 헷지 명목가 (USDT, 없으면 0)
      - target_combined_pct: 합산 PnL / total_notional 목표 비율 (기본 0.02 = 2%)
      - breakeven_eps_usdt: 헷지 본절 판정 허용 오차 (기본 0.5 USDT)

    반환:
      {
        "action": "NONE" | "OPEN_HEDGE" | "CLOSE_HEDGE" | "CLOSE_BOTH",
        "reason": str,
        "remaining_seed": float,
        "trigger_seed": float,
      }
    """
    context = {
        "side_seed": float(side_seed),
        "used_seed": float(used_seed),
        "grid_split_count": int(grid_split_count),
        "next_loss_line_touched": bool(next_loss_line_touched),
        "pnl_base": float(pnl_base),
        "pnl_hedge": float(pnl_hedge),
        "total_notional": float(total_notional),
        "hedge_notional": float(hedge_notional),
        "target_combined_pct": float(target_combined_pct),
        "breakeven_eps_usdt": float(breakeven_eps_usdt),
    }

    return evaluate_hedge_v9_1_from_context(context)

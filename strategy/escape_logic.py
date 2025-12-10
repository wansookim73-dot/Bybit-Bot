from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import time

from .state_model import (
    MarketState,
    PositionState,
    AccountState,
    WaveState,
    OrderSpec,
)
from .capital import CapitalManager
from .feed_types import StrategyFeed
from . import escape_config as CFG
from utils.logger import logger as root_logger
from utils.escape_events import (
    log_escape_trigger,
    log_escape_clear,
    log_hedge_plan,
    log_hedge_exit_plan,
)
from .grid_logic import (
    detect_touched_lines,
    line_price,
    LONG_LINE_MIN,
    LONG_LINE_MAX,
    SHORT_LINE_MIN,
    SHORT_LINE_MAX,
    _extract_position_info,
)

SAFE_FACTOR = 0.9
SPLIT_COUNT = 13

# 5.4 / 5.5 관련 기본 상수 (escape_config 에 없으면 fallback)
HEDGE_PNL_POS_EPS = getattr(CFG, "HEDGE_PNL_POS_EPS", 1e-6)   # "양수"로 본 최소 PnL
HEDGE_BE_EPS = getattr(CFG, "HEDGE_BE_EPS", 0.5)              # 헷지 BE 청산 epsilon (USDT)
PAIR_EXIT_PNL_PCT = getattr(CFG, "PAIR_EXIT_PNL_PCT", 0.02)   # N_exposure 대비 +2% 규칙


# ============================================================
# Legacy 호환용: EscapeOrders (v9.x API)
# ============================================================

@dataclass
class EscapeOrders:
    orders: List[OrderSpec]
    long_active: bool
    short_active: bool


# ============================================================
# v10.1 설계 기준: EscapeOrderSpec / EscapeDecision
# ============================================================

@dataclass
class EscapeOrderSpec:
    """
    Escape/Hedge 전용 주문 스펙.

    type:
      - "HEDGE_ENTRY" : 헷지 신규 진입/증가
      - "HEDGE_EXIT"  : 지정 방향 포지션 전량/부분 청산
    side:
      - "LONG" / "SHORT" (청산/진입 대상 포지션 방향)
    qty:
      - 수량 (BTC)
    """
    type: str           # "HEDGE_ENTRY" | "HEDGE_EXIT"
    side: str           # "LONG" | "SHORT"
    qty: float          # BTC


@dataclass
class EscapeDecision:
    """
    WaveFSM ↔ EscapeLogic 인터페이스.

    - mode_override : "NORMAL"/"ESCAPE"/"NEWS_BLOCK"/None
    - orders        : EscapeOrderSpec 리스트
    - full_exit     : True 이면 OrderManager 에서 FULL_EXIT 수행
    - state_updates : (현재 설계에서는 최소화, 주로 news_block 정도만 사용)
    """
    mode_override: Optional[str]
    orders: List[EscapeOrderSpec]
    full_exit: bool
    state_updates: Dict[str, Any]


class EscapeLogic:
    """
    v10.1 Escape & Hedge 로직.

    중심 포인트:
    - Seed 기반 Escape Trigger (5.1)
      · remain_seed_dir ≤ unit_seed_dir && 방향 PnL < 0 && 라인 터치 시
      · Escape_Trigger_Line 기록 (한 Wave에서 방향별 1회)
    - ESCAPE_PENDING → ESCAPE_ACTIVE 전환 (5.2)
      · Escape_Trigger_Line 터치 시 ESCAPE_ACTIVE 방향 ON
      · Hedge Entry (2배 cap) 계획 생성 (Mode B에서 사용)
    - ESCAPE_ACTIVE 동안 행동 제한 플래그 제공 (5.3)
      · state.escape_active (글로벌)
      · state.escape_long_active / state.escape_short_active (방향별)
      · grid_logic / order_manager 등에서 Gate 로 사용
    - 헷지 단독 BE 청산 (5.4)
    - N_exposure 기준 +2% 동시 청산 (5.5)
    - 계정 단위 FULL_EXIT (pnl_total_pct ≥ 0.02, 5.5 확장)
    """

    def __init__(self, capital: CapitalManager) -> None:
        self.capital = capital
        self.logger = root_logger

    # --------------------------------------------------------
    # Legacy WaveFSM 호환용: process(...) → EscapeOrders
    # (v9.x 스타일 인터페이스. 현재는 disable 상태로 유지.)
    # --------------------------------------------------------

    def process(
        self,
        market: MarketState,
        positions: PositionState,
        account: AccountState,
        wave_state: WaveState,
        touched_lines: List[int],
    ) -> EscapeOrders:
        """
        v9.x 호환용 더미. v10.1에서는 StrategyFeed 기반 evaluate(feed)를 사용.
        """
        return EscapeOrders(
            orders=[],
            long_active=False,
            short_active=False,
        )

    # --------------------------------------------------------
    # 내부 유틸: BotState 런타임 필드 보장
    # --------------------------------------------------------

    def _ensure_runtime_fields(self, state: Any) -> None:
        """
        BotState(dataclass)에 없는 ESCAPE/NEWS/CB/HE Hedge 관련
        런타임 필드를 동적으로 붙여준다.
        (state_manager 직렬화에는 포함되지 않지만, 프로세스 내에서 공유됨)
        """
        defaults = {
            "escape_active": False,
            "escape_reason": None,
            "escape_enter_ts": 0.0,

            "escape_trigger_line_long": None,
            "escape_trigger_line_short": None,
            "escape_long_pending": False,
            "escape_short_pending": False,
            "escape_long_active": False,
            "escape_short_active": False,

            # 5.5 +2% 규칙용 노출 기록 (방향별)
            "escape_pair_exposure_long": 0.0,
            "escape_pair_exposure_short": 0.0,

            # 5.4 헷지 BE 청산용 PnL 히스토리 플래그
            "hedge_pnl_positive_seen_long": False,
            "hedge_pnl_positive_seen_short": False,

            "news_block": False,
            "cb_block": False,

            "hedge_side": None,   # "LONG"/"SHORT" (헷지 포지션 방향)
            "hedge_size": 0.0,    # BTC
        }
        for k, v in defaults.items():
            if not hasattr(state, k):
                setattr(state, k, v)

    def _compute_seed_usage_dir(
        self,
        effective_total: float,
        unit_seed: float,
        k_dir: int,
    ) -> Dict[str, float]:
        """
        capital.py / grid_logic.py 의 규칙과 맞춰 Seed 사용량 계산.

        - effective_total : long/short_seed_total_effective
        - unit_seed       : unit_seed_long/short
        - k_dir           : k_long / k_short

        정의 (v10.1 기준):
          allocated_seed_dir = effective_total / SAFE_FACTOR   (nominal 25% 분배)
          used_seed_dir      = k_dir * unit_seed
          remain_seed_dir    = allocated_seed_dir - used_seed_dir
        """
        k = max(0, min(int(k_dir), SPLIT_COUNT))

        eff = float(effective_total or 0.0)
        u = float(unit_seed or 0.0)

        if eff <= 0.0 or u <= 0.0:
            return {
                "allocated": 0.0,
                "used": 0.0,
                "remain": 0.0,
                "k": k,
            }

        allocated = eff / SAFE_FACTOR if SAFE_FACTOR > 0 else eff
        used = float(k) * u
        remain = max(0.0, allocated - used)

        return {
            "allocated": allocated,
            "used": used,
            "remain": remain,
            "k": k,
        }

    def _update_seed_triggers(
        self,
        feed: StrategyFeed,
        state: Any,
        long_size: float,
        long_pnl: float,
        short_size: float,
        short_pnl: float,
    ) -> List[int]:
        """
        [5.1 Seed 기반 Escape Trigger]

        - 어떤 라인 L_k 를 터치했을 때, 아래 조건이면 방향별 Trigger 설정:
          (1) 해당 방향 미실현 PnL < 0
          (2) remain_seed_dir ≤ unit_seed_dir
        - Long 물림: Escape_Trigger_Line = L_{k-1}
        - Short 물림: Escape_Trigger_Line = L_{k+1}
        - 한 Wave 동안 방향별 1회만 기록.
        """
        price_now = float(getattr(feed, "price", 0.0) or 0.0)
        price_prev = float(getattr(feed, "price_prev", price_now) or price_now)

        p_center = float(getattr(state, "p_center", 0.0) or 0.0)
        p_gap = float(getattr(state, "p_gap", 0.0) or 0.0)

        if p_gap <= 0.0 or price_now <= 0.0:
            return []

        # 이번 틱에서 터치한 라인 목록
        touched = detect_touched_lines(
            price_prev=price_prev,
            price_now=price_now,
            p_center=p_center,
            p_gap=p_gap,
            idx_min=min(LONG_LINE_MIN, SHORT_LINE_MIN),
            idx_max=max(LONG_LINE_MAX, SHORT_LINE_MAX),
        )
        if not touched:
            return []

        eff_long = float(getattr(state, "long_seed_total_effective", 0.0) or 0.0)
        eff_short = float(getattr(state, "short_seed_total_effective", 0.0) or 0.0)
        unit_long = float(getattr(state, "unit_seed_long", 0.0) or 0.0)
        unit_short = float(getattr(state, "unit_seed_short", 0.0) or 0.0)
        k_long = int(getattr(state, "k_long", 0) or 0)
        k_short = int(getattr(state, "k_short", 0) or 0)

        seed_long = self._compute_seed_usage_dir(eff_long, unit_long, k_long)
        seed_short = self._compute_seed_usage_dir(eff_short, unit_short, k_short)

        # LONG 방향 Seed Trigger
        if (
            long_size > 0.0
            and long_pnl < 0.0
            and unit_long > 0.0
            and seed_long["remain"] <= unit_long + 1e-9
            and getattr(state, "escape_trigger_line_long", None) is None
        ):
            # 가장 먼저 터치된 라인 기준
            k_idx = min(touched)
            trigger_idx = max(LONG_LINE_MIN, k_idx - 1)

            state.escape_trigger_line_long = trigger_idx
            state.escape_long_pending = True

            try:
                self.logger.info(
                    "[Escape] Seed trigger (LONG): L_k=%d -> trigger_line=%d "
                    "remain_seed=%.4f unit_seed=%.4f",
                    k_idx,
                    trigger_idx,
                    seed_long["remain"],
                    unit_long,
                )
                log_escape_trigger(
                    self.logger,
                    reason="SEED_TRIGGER_LONG",
                    pnl_total_pct=float(getattr(feed, "pnl_total_pct", 0.0) or 0.0),
                    atr_ratio=(
                        float(getattr(feed, "atr_4h_42", 0.0) or 0.0)
                        / float(getattr(state, "atr_value", 0.0) or 1.0)
                        if float(getattr(state, "atr_value", 0.0) or 0.0) > 0.0
                        else 0.0
                    ),
                    main_side="LONG",
                    main_qty=long_size,
                    side_seed_total=seed_long["allocated"],
                    exposure_ratio=0.0,
                )
            except Exception:
                pass

        # SHORT 방향 Seed Trigger
        if (
            short_size > 0.0
            and short_pnl < 0.0
            and unit_short > 0.0
            and seed_short["remain"] <= unit_short + 1e-9
            and getattr(state, "escape_trigger_line_short", None) is None
        ):
            k_idx = max(touched)
            trigger_idx = min(SHORT_LINE_MAX, k_idx + 1)

            state.escape_trigger_line_short = trigger_idx
            state.escape_short_pending = True

            try:
                self.logger.info(
                    "[Escape] Seed trigger (SHORT): L_k=%d -> trigger_line=%d "
                    "remain_seed=%.4f unit_seed=%.4f",
                    k_idx,
                    trigger_idx,
                    seed_short["remain"],
                    unit_short,
                )
                log_escape_trigger(
                    self.logger,
                    reason="SEED_TRIGGER_SHORT",
                    pnl_total_pct=float(getattr(feed, "pnl_total_pct", 0.0) or 0.0),
                    atr_ratio=(
                        float(getattr(feed, "atr_4h_42", 0.0) or 0.0)
                        / float(getattr(state, "atr_value", 0.0) or 1.0)
                        if float(getattr(state, "atr_value", 0.0) or 0.0) > 0.0
                        else 0.0
                    ),
                    main_side="SHORT",
                    main_qty=short_size,
                    side_seed_total=seed_short["allocated"],
                    exposure_ratio=0.0,
                )
            except Exception:
                pass

        return touched

    def _update_escape_active_from_triggers(
        self,
        state: Any,
        touched_lines: List[int],
        now_ts: float,
    ) -> None:
        """
        [5.2 ESCAPE_PENDING → ESCAPE_ACTIVE]

        - Escape_Trigger_Line 을 실제로 터치하면 ESCAPE_ACTIVE 방향 ON.
        - 방향별 플래그:
            · escape_long_active / escape_short_active
        - 글로벌 플래그:
            · escape_active = escape_long_active or escape_short_active
        """
        if not touched_lines:
            return

        # LONG 방향
        trig_long = getattr(state, "escape_trigger_line_long", None)
        if getattr(state, "escape_long_pending", False) and trig_long is not None:
            if int(trig_long) in touched_lines:
                state.escape_long_pending = False
                state.escape_long_active = True
                state.escape_enter_ts = now_ts
                self.logger.info(
                    "[Escape] ESCAPE_ACTIVE (LONG) triggered at line=%s",
                    trig_long,
                )

        # SHORT 방향
        trig_short = getattr(state, "escape_trigger_line_short", None)
        if getattr(state, "escape_short_pending", False) and trig_short is not None:
            if int(trig_short) in touched_lines:
                state.escape_short_pending = False
                state.escape_short_active = True
                state.escape_enter_ts = now_ts
                self.logger.info(
                    "[Escape] ESCAPE_ACTIVE (SHORT) triggered at line=%s",
                    trig_short,
                )

        # 글로벌 플래그
        if getattr(state, "escape_long_active", False) or getattr(state, "escape_short_active", False):
            state.escape_active = True

    # --------------------------------------------------------
    # ESCAPE_ON / ESCAPE_OFF / FULL_EXIT 조건
    # --------------------------------------------------------

    def _compute_common_metrics(self, feed: StrategyFeed, state: Any) -> Dict[str, Any]:
        """
        ESCAPE_ON / ESCAPE_OFF / FULL_EXIT / HEDGE 판단을 위한 공통 메트릭.
        """
        price = float(getattr(feed, "price", 0.0) or 0.0)
        atr_4h_42 = float(getattr(feed, "atr_4h_42", 0.0) or 0.0)
        atr_value = float(getattr(state, "atr_value", 0.0) or 0.0)

        try:
            pnl_total_pct = float(getattr(feed, "pnl_total_pct", 0.0) or 0.0)
        except Exception:
            pnl_total_pct = 0.0

        pos = _extract_position_info(feed)
        long_size = pos["long_size"]
        long_pnl = pos["long_pnl"]
        short_size = pos["short_size"]
        short_pnl = pos["short_pnl"]

        escape_long_active = bool(getattr(state, "escape_long_active", False))
        escape_short_active = bool(getattr(state, "escape_short_active", False))

        # 메인 방향:
        # - ESCAPE_LONG_ACTIVE / ESCAPE_SHORT_ACTIVE 가 켜져 있으면 해당 방향 우선
        # - 없으면 단일 방향 포지션이 존재하는 경우 그쪽을 메인으로 본다.
        if escape_long_active and long_size > 0.0:
            main_side = "LONG"
            main_qty = long_size
        elif escape_short_active and short_size > 0.0:
            main_side = "SHORT"
            main_qty = short_size
        elif long_size > 0 and short_size == 0:
            main_side = "LONG"
            main_qty = long_size
        elif short_size > 0 and long_size == 0:
            main_side = "SHORT"
            main_qty = short_size
        else:
            main_side = None
            main_qty = 0.0

        main_notional = abs(main_qty) * price

        eff_long = float(getattr(state, "long_seed_total_effective", 0.0) or 0.0)
        eff_short = float(getattr(state, "short_seed_total_effective", 0.0) or 0.0)
        if main_side == "LONG":
            side_eff = eff_long
        elif main_side == "SHORT":
            side_eff = eff_short
        else:
            side_eff = 0.0

        exposure_ratio = main_notional / side_eff if side_eff > 0.0 else 0.0
        atr_ratio = atr_4h_42 / atr_value if atr_value > 0.0 else 0.0

        hedge_side = getattr(state, "hedge_side", None)
        hedge_size = float(getattr(state, "hedge_size", 0.0) or 0.0)
        hedge_notional = abs(hedge_size) * price

        return {
            "price": price,
            "pnl_total_pct": pnl_total_pct,
            "long_size": long_size,
            "short_size": short_size,
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "main_side": main_side,
            "main_qty": main_qty,
            "main_notional": main_notional,
            "exposure_ratio": exposure_ratio,
            "atr_ratio": atr_ratio,
            "atr_value": atr_value,
            "atr_4h_42": atr_4h_42,
            "hedge_side": hedge_side,
            "hedge_size": hedge_size,
            "hedge_notional": hedge_notional,
        }

    def _compute_pair_pnl_and_exposure(
        self,
        state: Any,
        m: Dict[str, Any],
        direction: str,
    ) -> Dict[str, float]:
        """
        5.4 / 5.5 헷지 BE / +2% 동시 청산용 메트릭.

        direction: "LONG" 또는 "SHORT" (물린 메인 방향)

        반환:
          {
            "pnl_main": float,
            "pnl_hedge": float,
            "pnl_total": float,
            "N_entry": float,        # Escape Hedge 진입 시점 기준 노출 (기록 없으면 현재값)
            "N_current": float,      # 현재 시점 노출
            "main_notional": float,
            "hedge_notional": float,
            "main_size": float,
            "hedge_size": float,
          }
        """
        price = m["price"]
        if price <= 0.0:
            return {
                "pnl_main": 0.0,
                "pnl_hedge": 0.0,
                "pnl_total": 0.0,
                "N_entry": 0.0,
                "N_current": 0.0,
                "main_notional": 0.0,
                "hedge_notional": 0.0,
                "main_size": 0.0,
                "hedge_size": 0.0,
            }

        long_size = m["long_size"]
        long_pnl = m["long_pnl"]
        short_size = m["short_size"]
        short_pnl = m["short_pnl"]

        hedge_side = getattr(state, "hedge_side", None)
        hedge_size = float(getattr(state, "hedge_size", 0.0) or 0.0)

        if hedge_side is None or hedge_size <= 0.0:
            return {
                "pnl_main": 0.0,
                "pnl_hedge": 0.0,
                "pnl_total": 0.0,
                "N_entry": 0.0,
                "N_current": 0.0,
                "main_notional": 0.0,
                "hedge_notional": 0.0,
                "main_size": 0.0,
                "hedge_size": 0.0,
            }

        direction = direction.upper()
        if direction == "LONG":
            main_size = long_size
            main_pnl = long_pnl
            pair_attr = "escape_pair_exposure_long"
            expected_hedge_side = "SHORT"
        else:
            main_size = short_size
            main_pnl = short_pnl
            pair_attr = "escape_pair_exposure_short"
            expected_hedge_side = "LONG"

        # 이 방향에 대응하는 헷지가 아니면 무의미
        if main_size <= 0.0 or hedge_side != expected_hedge_side:
            return {
                "pnl_main": 0.0,
                "pnl_hedge": 0.0,
                "pnl_total": 0.0,
                "N_entry": 0.0,
                "N_current": 0.0,
                "main_notional": 0.0,
                "hedge_notional": 0.0,
                "main_size": 0.0,
                "hedge_size": hedge_size,
            }

        # 헷지 PnL 은 해당 방향 전체 포지션 PnL 에서 비율로 분배
        if hedge_side == "LONG":
            total_side_size = long_size
            total_side_pnl = long_pnl
        else:
            total_side_size = short_size
            total_side_pnl = short_pnl

        if total_side_size <= 0.0:
            hedge_pnl = 0.0
        else:
            ratio = min(1.0, abs(hedge_size) / abs(total_side_size))
            hedge_pnl = total_side_pnl * ratio

        main_notional = abs(main_size) * price
        hedge_notional = abs(hedge_size) * price
        N_current = main_notional + hedge_notional

        N_entry = float(getattr(state, pair_attr, 0.0) or 0.0)
        if N_entry <= 0.0 and N_current > 0.0:
            # 최초 진입 시점으로 보고 기록
            N_entry = N_current
            setattr(state, pair_attr, N_entry)

        pnl_total = main_pnl + hedge_pnl

        return {
            "pnl_main": main_pnl,
            "pnl_hedge": hedge_pnl,
            "pnl_total": pnl_total,
            "N_entry": N_entry,
            "N_current": N_current,
            "main_notional": main_notional,
            "hedge_notional": hedge_notional,
            "main_size": main_size,
            "hedge_size": hedge_size,
        }

    # --------------------------------------------------------
    # HEDGE_ENTRY / HEDGE_EXIT (단순화된 2배 cap + 안전 조건)
    # --------------------------------------------------------

    def _plan_hedge_orders(
        self,
        state: Any,
        m: Dict[str, Any],
    ) -> List[EscapeOrderSpec]:
        """
        ESCAPE_ACTIVE 동안 Hedge 유지/조정 / 청산 계획.

        - HEDGE_ENTRY:
            · 메인 방향이 존재하고
            · target_hedge_qty > current_hedge_qty + 최소 threshold
            · Q_hedge 는 main_notional 기준 1x, 2배수 cap (notional 기준)
        - HEDGE_EXIT:
            · ESCAPE 비활성화되었거나
            · 메인 포지션이 사라졌거나
        """
        orders: List[EscapeOrderSpec] = []

        price = m["price"]
        if price <= 0.0:
            return orders

        main_side = m["main_side"]
        main_qty = m["main_qty"]
        hedge_side = m["hedge_side"]
        hedge_size = m["hedge_size"]
        hedge_notional = m["hedge_notional"]

        escape_active = bool(getattr(state, "escape_active", False))

        # HEDGE_EXIT: ESCAPE 비활성 + 헷지 남아있음
        if not escape_active and hedge_size > 0.0:
            close_side = hedge_side
            orders.append(
                EscapeOrderSpec(
                    type="HEDGE_EXIT",
                    side=close_side,
                    qty=abs(hedge_size),
                )
            )
            try:
                log_hedge_exit_plan(
                    self.logger,
                    main_side=main_side,
                    main_qty=main_qty,
                    hedge_side=hedge_side,
                    hedge_qty=hedge_size,
                    hedge_notional=hedge_notional,
                    hedge_pnl_pct=None,
                )
            except Exception:
                pass
            return orders

        # ESCAPE 비활성 이면 신규 HEDGE_ENTRY 없음
        if not escape_active:
            return orders

        # 메인 포지션이 없는데 헷지만 남은 경우 → 헷지 청산
        if (main_side is None or main_qty <= 0.0) and hedge_size > 0.0:
            close_side = hedge_side
            orders.append(
                EscapeOrderSpec(
                    type="HEDGE_EXIT",
                    side=close_side,
                    qty=abs(hedge_size),
                )
            )
            try:
                log_hedge_exit_plan(
                    self.logger,
                    main_side=main_side,
                    main_qty=main_qty,
                    hedge_side=hedge_side,
                    hedge_qty=hedge_size,
                    hedge_notional=hedge_notional,
                    hedge_pnl_pct=None,
                )
            except Exception:
                pass
            return orders

        # 메인 포지션이 없으면 Hedge Entry 불가
        if main_side is None or main_qty <= 0.0:
            return orders

        # 2배수 cap (notional 기준)
        main_notional = m["main_notional"]
        max_hedge_notional = 2.0 * main_notional

        # 기본 타겟: 1x Hedge (보수적으로)
        target_hedge_notional = min(main_notional, max_hedge_notional)
        target_hedge_qty = target_hedge_notional / price if price > 0.0 else 0.0

        # 현재 헷지 대비 추가 필요한 수량
        add_qty = target_hedge_qty - hedge_size
        add_notional = add_qty * price

        # 너무 작으면 무시
        if add_qty <= 0.0 or add_notional < CFG.HEDGE_ENTRY_MIN_NOTIONAL_USDT:
            return orders

        hedge_dir = "SHORT" if main_side == "LONG" else "LONG"
        orders.append(
            EscapeOrderSpec(
                type="HEDGE_ENTRY",
                side=hedge_dir,
                qty=add_qty,
            )
        )

        try:
            log_hedge_plan(
                self.logger,
                main_side=main_side,
                main_qty=main_qty,
                hedge_side=hedge_dir,
                hedge_qty_before=hedge_size,
                hedge_qty_after=hedge_size + add_qty,
                hedge_notional=target_hedge_notional,
            )
        except Exception:
            pass

        return orders

    # --------------------------------------------------------
    # v10.1 메인 엔트리 포인트: evaluate(feed) → EscapeDecision
    # --------------------------------------------------------

    def evaluate(self, feed: StrategyFeed) -> EscapeDecision:
        """
        v10.1 Escape 메인 루프.

        핵심 처리 순서:

          0) BotState 런타임 필드 보정
          1) 공통 메트릭 계산 (pnl_total_pct, exposure, atr_ratio, 포지션)
          2) FULL_EXIT 조건 체크 (계정 기준 2% 수익)
          3) 뉴스/서킷 블럭 업데이트 (news_block / cb_block)
          4) Seed 기반 Escape Trigger (5.1)
          5) ESCAPE_PENDING → ESCAPE_ACTIVE (5.2)
          6) ESCAPE_ON / ESCAPE_OFF 글로벌 (PnL/Seed/ATR 기준)
          7) ESCAPE_ACTIVE 방향별 +2% 동시 청산 (5.5) 및 헷지 BE 청산 (5.4)
          8) ESCAPE_ACTIVE 플래그 / mode 정리 (5.3 Gate 용)
          9) ESCAPE_ACTIVE 동안 Hedge Entry/Exit 계획 생성
        """
        state = feed.state
        self._ensure_runtime_fields(state)

        decision = EscapeDecision(
            mode_override=None,
            orders=[],
            full_exit=False,
            state_updates={},
        )

        m = self._compute_common_metrics(feed, state)
        now_ts = time.time()

        # ------------------------------
        # 1) FULL_EXIT (+2% 계정 기준)
        # ------------------------------
        pnl_total_pct = m["pnl_total_pct"]
        if pnl_total_pct >= CFG.FULL_EXIT_PNL_PCT:
            # FULL_EXIT 트리거 → ESCAPE / HEDGE 상태 초기화
            state.escape_active = False
            state.escape_long_active = False
            state.escape_short_active = False
            state.escape_long_pending = False
            state.escape_short_pending = False
            state.escape_trigger_line_long = None
            state.escape_trigger_line_short = None
            state.escape_pair_exposure_long = 0.0
            state.escape_pair_exposure_short = 0.0
            state.hedge_pnl_positive_seen_long = False
            state.hedge_pnl_positive_seen_short = False
            state.escape_reason = "FULL_EXIT"
            state.escape_enter_ts = 0.0

            # 모드는 FULL_EXIT 이후 정상/뉴스블록에 따라 결정
            if getattr(state, "news_block", False):
                state.mode = "NEWS_BLOCK"
                decision.mode_override = "NEWS_BLOCK"
            else:
                state.mode = "NORMAL"
                decision.mode_override = "NORMAL"

            decision.full_exit = True
            self.logger.info(
                "[Escape] FULL_EXIT trigger: pnl_total_pct=%.4f >= %.4f",
                pnl_total_pct,
                CFG.FULL_EXIT_PNL_PCT,
            )
            return decision

        # ------------------------------
        # 2) 뉴스/서킷 블록 처리 (news_block / cb_block)
        # ------------------------------
        nb = bool(getattr(state, "news_block", False))
        cb = bool(getattr(state, "cb_block", False))

        news_on = bool(getattr(feed, "news_signal_on", False))
        news_off = bool(getattr(feed, "news_signal_off", False))

        if news_on:
            nb = True
        if news_off:
            nb = False

        state.news_block = nb
        state.cb_block = cb

        # 뉴스 블록은 ESCAPE_ACTIVE 아닌 경우에만 mode 를 강제로 NEWS_BLOCK 으로 변경
        if nb and not getattr(state, "escape_active", False):
            state.mode = "NEWS_BLOCK"
            decision.mode_override = "NEWS_BLOCK"
        elif not nb and state.mode == "NEWS_BLOCK" and not getattr(state, "escape_active", False):
            state.mode = "NORMAL"
            decision.mode_override = "NORMAL"

        # ------------------------------
        # 3) Seed 기반 Escape Trigger (5.1)
        # ------------------------------
        touched_lines = self._update_seed_triggers(
            feed=feed,
            state=state,
            long_size=m["long_size"],
            long_pnl=m["long_pnl"],
            short_size=m["short_size"],
            short_pnl=m["short_pnl"],
        )

        # ------------------------------
        # 4) ESCAPE_PENDING → ESCAPE_ACTIVE (5.2)
        # ------------------------------
        self._update_escape_active_from_triggers(
            state=state,
            touched_lines=touched_lines,
            now_ts=now_ts,
        )

        # ------------------------------
        # 5) ESCAPE_ON / ESCAPE_OFF (글로벌 기준)
        #     - PnL 급락 / Seed 노출 / ATR Vol-Spike
        # ------------------------------
        escape_active_prev = bool(getattr(state, "escape_active", False))
        escape_enter_ts = float(getattr(state, "escape_enter_ts", 0.0) or 0.0)

        escape_on = False
        escape_reason = None

        # PnL 기준
        if pnl_total_pct <= -CFG.ESCAPE_ON_PNL_PCT:
            escape_on = True
            escape_reason = "PNL_DROP"

        # Seed 노출 비율 기준 (메인 방향 기준)
        if not escape_on and m["exposure_ratio"] >= CFG.ESCAPE_ON_SEED_PCT:
            escape_on = True
            escape_reason = "SEED_LOSS"

        # ATR Vol-Spike 기준
        if (
            not escape_on
            and m["atr_ratio"] >= CFG.ESCAPE_ATR_SPIKE_MULT
        ):
            escape_on = True
            escape_reason = "VOL_SPIKE"

        escape_off = False
        if escape_active_prev:
            # PnL 회복
            if pnl_total_pct >= -CFG.ESCAPE_OFF_PNL_PCT:
                # ATR 정상 수준
                if m["atr_ratio"] <= CFG.ESCAPE_OFF_ATR_MULT:
                    # ESCAPE 진입 후 쿨다운 시간 경과
                    if escape_enter_ts > 0.0 and (now_ts - escape_enter_ts) >= CFG.ESCAPE_COOLDOWN_SEC:
                        # Seed 노출도 충분히 줄어든 경우
                        if m["exposure_ratio"] <= CFG.ESCAPE_OFF_MAX_EXPOSURE_RATIO:
                            escape_off = True

        # ESCAPE 진입/해제 (글로벌)
        if (not escape_active_prev) and escape_on:
            state.escape_active = True
            state.escape_reason = escape_reason or "UNKNOWN"
            state.escape_enter_ts = now_ts
            decision.mode_override = "ESCAPE"
            self.logger.info(
                "[Escape] ESCAPE mode ON: reason=%s pnl_total_pct=%.4f exposure=%.3f atr_ratio=%.3f",
                state.escape_reason,
                pnl_total_pct,
                m["exposure_ratio"],
                m["atr_ratio"],
            )
        elif escape_active_prev and escape_off:
            # Seed 기반 방향 ESCAPE_ACTIVE 가 남아 있다면 글로벌은 유지
            if not (getattr(state, "escape_long_active", False) or getattr(state, "escape_short_active", False)):
                state.escape_active = False
                state.escape_reason = None
                state.escape_enter_ts = 0.0

                nb_final = bool(getattr(state, "news_block", False))
                if nb_final:
                    state.mode = "NEWS_BLOCK"
                    decision.mode_override = "NEWS_BLOCK"
                else:
                    state.mode = "NORMAL"
                    decision.mode_override = "NORMAL"

                self.logger.info("[Escape] ESCAPE mode OFF (global)")

                # ESCAPE_CLEAR 이벤트
                try:
                    log_escape_clear(
                        self.logger,
                        pnl_total_pct=pnl_total_pct,
                        atr_ratio=m["atr_ratio"],
                        exposure_ratio=m["exposure_ratio"],
                        duration_sec=(now_ts - escape_enter_ts) if escape_enter_ts > 0 else 0.0,
                    )
                except Exception:
                    pass

        # ------------------------------
        # 6) ESCAPE_ACTIVE 방향별 +2% 동시 청산 (5.5) / 헷지 BE 청산 (5.4)
        # ------------------------------
        early_orders: List[EscapeOrderSpec] = []
        pair_exit_triggered = False
        hedge_be_triggered = False

        if getattr(state, "escape_active", False):
            # (a) +2% 동시 청산 (우선순위 가장 높음)
            for direction in ("LONG", "SHORT"):
                esc_flag = getattr(state, f"escape_{direction.lower()}_active", False)
                if not esc_flag:
                    continue

                pair = self._compute_pair_pnl_and_exposure(state, m, direction)
                N_exposure = pair["N_entry"] or pair["N_current"]
                if N_exposure <= 0.0:
                    continue

                pnl_total_pair = pair["pnl_total"]
                threshold = N_exposure * PAIR_EXIT_PNL_PCT

                if threshold > 0.0 and pnl_total_pair >= threshold:
                    main_side = direction
                    main_qty = abs(pair["main_size"])
                    hedge_side = getattr(state, "hedge_side", None)
                    hedge_size = abs(pair["hedge_size"])

                    # 기존 물린 포지션 + 헷지 포지션 동시 Market 청산
                    if hedge_side and hedge_size > 0.0:
                        early_orders.append(
                            EscapeOrderSpec(
                                type="HEDGE_EXIT",
                                side=hedge_side,
                                qty=hedge_size,
                            )
                        )
                    if main_side and main_qty > 0.0:
                        early_orders.append(
                            EscapeOrderSpec(
                                type="HEDGE_EXIT",
                                side=main_side,
                                qty=main_qty,
                            )
                        )

                    # 상태 정리: 해당 방향 ESCAPE 세트 종료
                    if direction == "LONG":
                        state.escape_long_active = False
                        state.escape_trigger_line_long = None
                        state.escape_pair_exposure_long = 0.0
                        state.hedge_pnl_positive_seen_long = False
                    else:
                        state.escape_short_active = False
                        state.escape_trigger_line_short = None
                        state.escape_pair_exposure_short = 0.0
                        state.hedge_pnl_positive_seen_short = False

                    state.escape_long_pending = False
                    state.escape_short_pending = False

                    # 다른 방향도 ESCAPE_ACTIVE 가 아니면 글로벌 ESCAPE 종료
                    if not (
                        getattr(state, "escape_long_active", False)
                        or getattr(state, "escape_short_active", False)
                    ):
                        state.escape_active = False
                        state.escape_reason = None
                        state.escape_enter_ts = 0.0

                    pair_exit_triggered = True
                    self.logger.info(
                        "[Escape] +2%% pair exit (%s): pnl_total=%.4f, N_exposure=%.4f, threshold=%.4f",
                        direction,
                        pnl_total_pair,
                        N_exposure,
                        threshold,
                    )
                    break  # +2% 는 방향당 1개 세트만 처리

            # (b) 헷지 단독 BE 청산 (5.4) – +2% 미충족 시에만
            if not pair_exit_triggered:
                for direction in ("LONG", "SHORT"):
                    esc_flag = getattr(state, f"escape_{direction.lower()}_active", False)
                    if not esc_flag:
                        continue

                    pair = self._compute_pair_pnl_and_exposure(state, m, direction)
                    hedge_size = abs(pair["hedge_size"])
                    if hedge_size <= 0.0:
                        continue

                    hedge_pnl = pair["pnl_hedge"]

                    flag_attr = f"hedge_pnl_positive_seen_{direction.lower()}"
                    positive_seen = bool(getattr(state, flag_attr, False))

                    # 한 번이라도 양수가 되면 플래그 On
                    if hedge_pnl > HEDGE_PNL_POS_EPS:
                        positive_seen = True
                        setattr(state, flag_attr, True)

                    # 양수 구간을 지난 뒤, 다시 0 근처로 복귀하면 BE 청산
                    if positive_seen and abs(hedge_pnl) <= HEDGE_BE_EPS:
                        hedge_side = getattr(state, "hedge_side", None)
                        if hedge_side:
                            early_orders.append(
                                EscapeOrderSpec(
                                    type="HEDGE_EXIT",
                                    side=hedge_side,
                                    qty=hedge_size,
                                )
                            )
                            setattr(state, flag_attr, False)

                            # 이 방향 Escape 세트는 종료 (헷지만 닫고 메인은 유지)
                            if direction == "LONG":
                                state.escape_long_active = False
                                state.escape_trigger_line_long = None
                                state.escape_pair_exposure_long = 0.0
                            else:
                                state.escape_short_active = False
                                state.escape_trigger_line_short = None
                                state.escape_pair_exposure_short = 0.0

                            # 다른 방향도 없으면 글로벌 ESCAPE 종료
                            if not (
                                getattr(state, "escape_long_active", False)
                                or getattr(state, "escape_short_active", False)
                            ):
                                state.escape_active = False
                                state.escape_reason = None
                                state.escape_enter_ts = 0.0

                            hedge_be_triggered = True
                            self.logger.info(
                                "[Escape] Hedge BE exit (%s): hedge_pnl=%.6f, eps=%.6f",
                                direction,
                                hedge_pnl,
                                HEDGE_BE_EPS,
                            )
                            break

        # ------------------------------
        # 7) ESCAPE_ACTIVE 플래그 / mode 정리 (5.3 Gate 용)
        # ------------------------------
        # 방향 ESCAPE_ACTIVE 가 하나라도 ON 이면 글로벌도 ON
        if getattr(state, "escape_long_active", False) or getattr(state, "escape_short_active", False):
            state.escape_active = True
        else:
            # 방향별 ESCAPE 가 모두 꺼져 있으면 글로벌도 OFF (이미 FULL_EXIT/BE/+2% 에서 정리했을 수 있음)
            if not getattr(state, "escape_active", False):
                state.escape_reason = None
                state.escape_enter_ts = 0.0

        if getattr(state, "escape_active", False):
            state.mode = "ESCAPE"
            if decision.mode_override is None:
                decision.mode_override = "ESCAPE"
        else:
            # ESCAPE 비활성 + 뉴스 블록 여부에 따라 모드 유지
            if getattr(state, "news_block", False):
                state.mode = "NEWS_BLOCK"
                if decision.mode_override is None:
                    decision.mode_override = "NEWS_BLOCK"
            else:
                state.mode = "NORMAL"
                if decision.mode_override is None:
                    decision.mode_override = "NORMAL"

        # Grid / Risk / OrderManager 에서 사용할 Gate:
        # - state.escape_active
        # - state.escape_long_active / state.escape_short_active
        # - state.mode ("ESCAPE"/"NEWS_BLOCK"/"NORMAL")

        # ------------------------------
        # 8) ESCAPE_ACTIVE 동안 Hedge Entry/Exit 계획 생성
        # ------------------------------
        orders: List[EscapeOrderSpec] = list(early_orders)

        # +2% / BE 청산이 이미 트리거된 틱에서는 추가 HEDGE_ENTRY/EXIT 를 만들지 않는다.
        if getattr(state, "escape_active", False) and not (pair_exit_triggered or hedge_be_triggered):
            orders.extend(self._plan_hedge_orders(state, m))

        decision.orders = orders
        return decision

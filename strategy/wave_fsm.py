from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any

from strategy.feed_types import StrategyFeed
from strategy.grid_logic import GridDecision, GridLogic, _extract_position_info
from strategy.escape_logic import EscapeDecision, EscapeLogic
from strategy.capital import CapitalManager
from strategy.state_model import AccountState
from core.order_manager import OrderManager
from core.state_manager import StateManager
from utils.logger import get_logger
import os
from decimal import Decimal

from config import DRY_RUN

try:
    from config import INITIAL_SEED_USDT  # 시뮬레이션 초기 잔고 (DRY_RUN 용)
except Exception:
    INITIAL_SEED_USDT = 0.0


@dataclass
class FinalDecision:
    """
    GridDecision + EscapeDecision 통합 결과
    """
    orders: List[Any]           # Grid OrderSpec + Escape OrderSpec
    state_updates: Dict[str, Any]


class WaveFSM:
    """
    v10.1 WaveFSM — Wave Lifecycle + GridLogic + EscapeLogic orchestration.
    """

    def __init__(
        self,
        grid_logic: GridLogic,
        escape_logic: EscapeLogic,
        order_manager: OrderManager,
        state_manager: StateManager,
        capital_manager: CapitalManager,
    ) -> None:
        self.logger = get_logger("wave_fsm")

        self.grid_logic = grid_logic
        self.escape_logic = escape_logic
        self.order_manager = order_manager
        self.state_manager = state_manager
        self.capital_manager = capital_manager

        # [DRY-RUN TEST ONLY] fake position injection flag
        self._fake_pos_for_test = bool(int(os.getenv("FSM_FAKE_POS", "0")))
        self._fake_pos_qty = Decimal(os.getenv("FSM_FAKE_POS_QTY", "0.005"))

        self.current_wave_id: int = 0
        self.current_mode: str = "NORMAL"

        self._wave_active: bool = False
        self.flat_ticks: int = 0

        self.last_price: float = 0.0

        self.last_escape_active_long: bool = False
        self.last_escape_active_short: bool = False

        self._last_grid_decision = None
        self._last_tick_ts: float = 0.0

    # ==================================================================
    # 내부: 포지션 상태 읽기
    # ==================================================================

    def _read_pos_state(self, feed: StrategyFeed) -> tuple[float, float, float]:
        """
        v10.1 포지션 상태 읽기.

        1) 기본적으로 StrategyFeed.state.long_size / short_size / hedge_size 를 사용한다.
        2) 세 값이 모두 0이면 ExchangeAPI.get_positions() 결과로 한 번 더 보정한다.
        """
        st = getattr(feed, "state", None)

        def _safe_float(v) -> float:
            try:
                return float(v or 0.0)
            except Exception:
                return 0.0

        if isinstance(st, dict):
            long_size = _safe_float(st.get("long_size", 0.0))
            short_size = _safe_float(st.get("short_size", 0.0))
            hedge_size = _safe_float(st.get("hedge_size", 0.0))
        else:
            long_size = _safe_float(getattr(st, "long_size", 0.0) if st is not None else 0.0)
            short_size = _safe_float(getattr(st, "short_size", 0.0) if st is not None else 0.0)
            hedge_size = _safe_float(getattr(st, "hedge_size", 0.0) if st is not None else 0.0)

        if (
            abs(long_size) < 1e-12
            and abs(short_size) < 1e-12
            and abs(hedge_size) < 1e-12
        ):
            try:
                exch = getattr(self.order_manager, "exchange", None)
                if exch is not None and hasattr(exch, "get_positions"):
                    pos = exch.get_positions() or {}
                    long_pos = pos.get("LONG") or pos.get("long") or {}
                    short_pos = pos.get("SHORT") or pos.get("short") or {}

                    long_size = _safe_float(long_pos.get("qty") or long_pos.get("size"))
                    short_size = _safe_float(short_pos.get("qty") or short_pos.get("size"))
                    hedge_size = 0.0
            except Exception as exc:
                self.logger.warning(
                    "[WaveFSM] _read_pos_state ExchangeAPI fallback failed: %s", exc
                )

        return long_size, short_size, hedge_size

    # ==================================================================
    # 내부: Wave Lifecycle (Start / End)
    # ==================================================================

    def _get_total_balance_snapshot(self, feed: StrategyFeed) -> float:
        """
        Wave Start 시 사용할 total_balance_snap 계산.
        """
        real_balance = 0.0

        account = getattr(feed, "account", None) or getattr(feed, "account_state", None)
        if isinstance(account, AccountState):
            try:
                real_balance = float(getattr(account, "total_balance", 0.0) or 0.0)
            except Exception:
                real_balance = 0.0

        if real_balance <= 0.0:
            st = getattr(feed, "state", None)
            if st is not None:
                try:
                    v = getattr(st, "total_balance", None)
                    if v is not None:
                        real_balance = float(v or 0.0)
                except Exception:
                    pass

        if real_balance <= 0.0:
            for attr in ("total_balance_snap", "total_balance", "balance_total"):
                if hasattr(feed, attr):
                    try:
                        v = getattr(feed, attr)
                        if v is not None:
                            real_balance = float(v)
                            if real_balance > 0.0:
                                break
                    except Exception:
                        continue

        if real_balance <= 0.0:
            try:
                from core.exchange_api import exchange as exchange_api
                bal = exchange_api.get_balance()
                if isinstance(bal, dict):
                    real_balance = float(bal.get("total") or 0.0)
            except Exception as e:
                self.logger.error(
                    "[WaveFSM] failed to fetch balance snapshot: %s", e
                )

        total_balance_snap = float(real_balance or 0.0)

        if DRY_RUN:
            sim_balance = None

            for obj in (feed, getattr(feed, "state", None)):
                if obj is None:
                    continue
                for attr in ("simulated_balance", "initial_balance", "initial_seed_usdt"):
                    if hasattr(obj, attr):
                        try:
                            val = float(getattr(obj, attr) or 0.0)
                        except Exception:
                            continue
                        if val > 0.0:
                            sim_balance = val
                            break
                if sim_balance is not None and sim_balance > 0.0:
                    break

            if (sim_balance is None or sim_balance <= 0.0) and INITIAL_SEED_USDT > 0.0:
                sim_balance = float(INITIAL_SEED_USDT)

            if sim_balance is not None and sim_balance > 0.0:
                total_balance_snap = sim_balance

        self.logger.info(
            "[Risk] total_balance_snap=%.4f (DRY_RUN=%s, real_balance=%.4f)",
            float(total_balance_snap or 0.0),
            bool(DRY_RUN),
            float(real_balance or 0.0),
        )

        return float(total_balance_snap or 0.0)

    def _get_escape_active_flags(self, feed: StrategyFeed) -> tuple[bool, bool]:
        state = getattr(feed, "state", None)
        wave_state = getattr(feed, "wave_state", None)

        long_active = False
        short_active = False

        if wave_state is not None:
            try:
                le = getattr(wave_state, "long_escape", None)
                se = getattr(wave_state, "short_escape", None)
                long_active = bool(getattr(le, "active", False))
                short_active = bool(getattr(se, "active", False))
            except Exception:
                pass

        if not (long_active or short_active) and state is not None:
            long_active = bool(
                getattr(state, "escape_long_active", False)
                or getattr(state, "escape_active_long", False)
            )
            short_active = bool(
                getattr(state, "escape_short_active", False)
                or getattr(state, "escape_active_short", False)
            )

        return long_active, short_active

    def _check_wave_start_condition(self, feed: StrategyFeed) -> bool:
        state = self.state_manager.get_state()
        feed.state = state

        pos_info = _extract_position_info(feed)
        long_size = float(pos_info.get("long_size", 0.0) or 0.0)
        short_size = float(pos_info.get("short_size", 0.0) or 0.0)

        hedge_size = 0.0
        for obj in (feed, getattr(feed, "state", None)):
            if obj is None:
                continue
            for attr in ("hedge_size", "hedge_position_size"):
                if hasattr(obj, attr):
                    try:
                        hedge_size = float(getattr(obj, attr) or 0.0)
                        break
                    except Exception:
                        hedge_size = 0.0
            if hedge_size != 0.0:
                break

        flat = (
            abs(long_size) < 1e-12
            and abs(short_size) < 1e-12
            and abs(hedge_size) < 1e-12
        )

        long_escape_active, short_escape_active = self._get_escape_active_flags(feed)
        escape_active = bool(long_escape_active or short_escape_active)

        mode = str(getattr(state, "mode", "UNKNOWN") or "UNKNOWN").upper()

        # v10.1 호환: STARTUP도 실질적으로 NORMAL로 취급 (초기 상태파일 호환)
        mode_ok_for_start = mode in ("NORMAL", "STARTUP")

        return flat and (not escape_active) and mode_ok_for_start

    def _handle_wave_lifecycle(self, feed: StrategyFeed) -> None:
        state = self.state_manager.get_state()

        st = getattr(feed, "state", None)

        def _safe_float(v) -> float:
            try:
                return float(v or 0.0)
            except Exception:
                return 0.0

        long_size = _safe_float(getattr(st, "long_size", 0.0) if st is not None else 0.0)
        short_size = _safe_float(getattr(st, "short_size", 0.0) if st is not None else 0.0)
        hedge_size = _safe_float(getattr(st, "hedge_size", 0.0) if st is not None else 0.0)

        if (
            abs(long_size) < 1e-12
            and abs(short_size) < 1e-12
            and abs(hedge_size) < 1e-12
        ):
            pos = getattr(feed, "positions", None) or {}
            try:
                long_size = _safe_float(pos.get("long_size", long_size))
                short_size = _safe_float(pos.get("short_size", short_size))
                hedge_size = _safe_float(pos.get("hedge_size", hedge_size))
            except Exception:
                pass

        is_flat = (
            abs(long_size) < 1e-12
            and abs(short_size) < 1e-12
            and abs(hedge_size) < 1e-12
        )

        long_escape_active, short_escape_active = self._get_escape_active_flags(feed)
        escape_active = bool(long_escape_active or short_escape_active)

        if is_flat and not escape_active:
            self.flat_ticks += 1
        else:
            self.flat_ticks = 0

        mode = str(getattr(state, "mode", "UNKNOWN") or "UNKNOWN").upper()
        mode_ok_for_start = mode in ("NORMAL", "STARTUP")

        if self._fake_pos_for_test and long_size == 0 and short_size == 0:
            fake_qty = float(self._fake_pos_qty)
            long_size = fake_qty
            short_size = 0.0
            is_flat = False
            self.flat_ticks = 0

        try:
            self.logger.info(
                "[WaveFSM] pos_state: long=%.4f, short=%.4f, hedge=%.4f, flat=%s, flat_ticks=%d",
                long_size,
                short_size,
                hedge_size,
                is_flat,
                self.flat_ticks,
            )
        except Exception:
            pass

        if self._wave_active and is_flat and (not escape_active):
            self._wave_active = False
            self.logger.info(
                "[WaveFSM] Wave End detected: wave_id=%s (flat & escape_inactive)",
                getattr(state, "wave_id", -1),
            )

        if (
            (not self._wave_active)
            and is_flat
            and (not escape_active)
            and mode_ok_for_start
            and self.flat_ticks >= 2
        ):
            self._start_new_wave(feed)

    def _start_new_wave(self, feed: StrategyFeed) -> None:
        state_before = self.state_manager.get_state()
        price_now = float(getattr(feed, "price", 0.0) or 0.0)
        atr_4h_42 = float(getattr(feed, "atr_4h_42", 0.0) or 0.0)

        # 계정 총 자산 스냅샷
        total_balance_snap = self._get_total_balance_snapshot(feed)

        # Seed 배분 및 unit_seed 계산
        snap = self.capital_manager.compute_wave_snapshot(total_balance_snap)

        # P_gap / P_center
        p_gap = max(atr_4h_42 * 0.15, 100.0)
        p_center = price_now

        # StateManager 에 Wave Reset 요청 (wave_id++, k_dir/steps/line_memory 초기화 포함)
        new_state = self.state_manager.reset_wave(
            p_center=p_center,
            p_gap=p_gap,
            atr_value=atr_4h_42,
            total_balance_snap=total_balance_snap,   # ✅ v10.1 핵심: 저장/영속화
            long_seed_total_effective=snap.long_seed_total_effective,
            short_seed_total_effective=snap.short_seed_total_effective,
            unit_seed_long=snap.unit_seed_long,
            unit_seed_short=snap.unit_seed_short,
            mode="NORMAL",
        )

        feed.state = new_state
        self._wave_active = True

        try:
            self.logger.info(
                "[WaveFSM] New Wave Start: prev_wave_id=%s new_wave_id=%s "
                "p_center=%.2f p_gap=%.2f atr_4h_42=%.4f "
                "total_balance_snap=%.2f long_eff=%.2f short_eff=%.2f "
                "unit_seed_long=%.4f unit_seed_short=%.4f",
                getattr(state_before, "wave_id", -1),
                getattr(new_state, "wave_id", -1),
                p_center,
                p_gap,
                atr_4h_42,
                float(total_balance_snap or 0.0),
                snap.long_seed_total_effective,
                snap.short_seed_total_effective,
                snap.unit_seed_long,
                snap.unit_seed_short,
            )
        except Exception:
            pass

    # ==================================================================
    # 메인 엔트리 포인트
    # ==================================================================

    def tick(self, feed: StrategyFeed, now_ts: float) -> None:
        if feed is None:
            self.logger.warning("[WaveFSM] tick: feed is None")
            return

        # 0) BotState 싱크
        try:
            bot_state = self.state_manager.get_state()
            feed.state = bot_state
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] failed to sync BotState from StateManager: %s",
                exc,
                exc_info=True,
            )

        # 1) 포지션 상태 읽기 + Feed에 반영
        try:
            long_size, short_size, hedge_size = self._read_pos_state(feed)

            st = getattr(feed, "state", None)
            if isinstance(st, dict):
                s_dict = dict(st)
                s_dict["long_size"] = long_size
                s_dict["short_size"] = short_size
                s_dict["hedge_size"] = hedge_size
                feed.state = s_dict
            elif st is not None:
                try:
                    setattr(st, "long_size", long_size)
                    setattr(st, "short_size", short_size)
                    setattr(st, "hedge_size", hedge_size)
                except Exception:
                    pass

            try:
                feed.positions = {
                    "long_size": long_size,
                    "short_size": short_size,
                    "hedge_size": hedge_size,
                }
            except Exception:
                pass

        except Exception as exc:
            self.logger.error(
                "[WaveFSM] _read_pos_state failed: %s", exc, exc_info=True
            )

        # 2) Wave lifecycle 처리
        try:
            self._handle_wave_lifecycle(feed)
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] Wave lifecycle handling failed: %s", exc, exc_info=True
            )

        # reset_wave 이후 BotState 인스턴스 교체 대응
        try:
            bot_state2 = self.state_manager.get_state()
            feed.state = bot_state2
        except Exception:
            pass

        # 3) EscapeLogic + OrderManager.apply_escape_decision
        try:
            esc_dec: EscapeDecision = self.escape_logic.evaluate(feed)
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] EscapeLogic.evaluate() failed: %s", exc, exc_info=True
            )
            esc_dec = EscapeDecision(
                mode_override=None,
                orders=[],
                full_exit=False,
                state_updates={},
            )

        try:
            self.order_manager.apply_escape_decision(esc_dec, feed, now_ts)
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] OrderManager.apply_escape_decision() failed: %s",
                exc,
                exc_info=True,
            )

        if esc_dec.full_exit:
            try:
                self.state_manager.save_state()
            except Exception as exc:
                self.logger.error(
                    "[WaveFSM] save_state failed after FULL_EXIT: %s",
                    exc,
                    exc_info=True,
                )
            return

        # 4) GridLogic + OrderManager.apply_decision
        try:
            grid_dec: GridDecision = self.grid_logic.process(feed)
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] GridLogic.process() failed: %s", exc, exc_info=True
            )
            return

        try:
            self.order_manager.apply_decision(grid_dec, feed, now_ts)
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] OrderManager.apply_decision() failed: %s",
                exc,
                exc_info=True,
            )

        # 5) state_updates 반영 후 저장
        try:
            final = self._merge_decisions(grid_decision=grid_dec, escape_decision=esc_dec)

            state = self.state_manager.get_state()
            for key, value in (final.state_updates or {}).items():
                try:
                    setattr(state, key, value)
                except Exception:
                    self.logger.warning(
                        "[WaveFSM] failed to apply state_update key=%s", key
                    )

            self.state_manager.save_state()
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] failed to apply state_updates / save_state: %s",
                exc,
                exc_info=True,
            )

    # ==================================================================
    # 내부: GridDecision + EscapeDecision 통합
    # ==================================================================

    def _merge_decisions(
        self,
        grid_decision: GridDecision,
        escape_decision: EscapeDecision,
    ) -> FinalDecision:
        orders: List[Any] = []
        orders.extend(grid_decision.orders)
        orders.extend(escape_decision.orders)

        state_updates: Dict[str, Any] = {}
        state_updates.update(grid_decision.state_updates or {})
        state_updates.update(escape_decision.state_updates or {})

        return FinalDecision(
            orders=orders,
            state_updates=state_updates,
        )

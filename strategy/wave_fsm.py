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
from utils.logger import logger
from config import DRY_RUN
from utils.logger import get_logger
import os
from decimal import Decimal

try:
    from config import INITIAL_SEED_USDT  # 시뮬레이션 초기 잔고 (DRY_RUN 용)
except Exception:  # INITIAL_SEED_USDT 없을 때도 안전하게 동작
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

    역할:
    - Wave Start 조건 판단 및 Wave 시작 시 BotState/Seed/LineMemory 초기화
    - Wave 종료(flat) 감지 및 다음 Wave Start 준비
    - GridLogic / EscapeLogic 실행 순서 관리
    - StateManager 를 통해 BotState 업데이트/저장
    """

    def __init__(
        self,
        grid_logic: GridLogic,
        escape_logic: EscapeLogic,
        order_manager: OrderManager,
        state_manager: StateManager,
        capital_manager: CapitalManager,
    ) -> None:
        """
        v10.1 WaveFSM 의 의존성을 주입한다.
        main_v10.WaveBot 에서 아래와 같이 호출한다:

            self.wave_fsm = WaveFSM(
                grid_logic=self.grid_logic,
                escape_logic=self.escape_logic,
                order_manager=self.order_manager,
                state_manager=self.state_manager,
                capital_manager=self.capital_manager,
            )
        """
        self.logger = get_logger("wave_fsm")

        # 주입된 컴포넌트 보관
        self.grid_logic = grid_logic
        self.escape_logic = escape_logic
        self.order_manager = order_manager
        self.state_manager = state_manager
        self.capital_manager = capital_manager

        # [DRY-RUN TEST ONLY] fake position injection flag
        self._fake_pos_for_test = bool(int(os.getenv("FSM_FAKE_POS", "0")))
        self._fake_pos_qty = Decimal(os.getenv("FSM_FAKE_POS_QTY", "0.005"))

        # FSM 내부 상태 기본값
        self.current_wave_id: int = 0
        self.current_mode: str = "NORMAL"

        # Wave 활성 여부 및 연속 flat 카운트
        self._wave_active: bool = False
        self.flat_ticks: int = 0           # 필요 시 외부에서 참조하는 용도

        self.last_price: float = 0.0

        # Escape / Hedge 관련 플래그 기본값
        self.last_escape_active_long: bool = False
        self.last_escape_active_short: bool = False

        # 기타 필요 시 사용하는 내부 버퍼들 (초기값만 세팅)
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

        # 1) StrategyFeed.state 에서 읽기 (dataclass / dict 모두 지원)
        if isinstance(st, dict):
            long_size = _safe_float(st.get("long_size", 0.0))
            short_size = _safe_float(st.get("short_size", 0.0))
            hedge_size = _safe_float(st.get("hedge_size", 0.0))
        else:
            long_size = _safe_float(getattr(st, "long_size", 0.0) if st is not None else 0.0)
            short_size = _safe_float(getattr(st, "short_size", 0.0) if st is not None else 0.0)
            hedge_size = _safe_float(getattr(st, "hedge_size", 0.0) if st is not None else 0.0)

        # 2) 세 값이 모두 0이면 ExchangeAPI 포지션으로 보정
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
                    hedge_size = 0.0  # 현재 구조상 별도 hedge 포지션은 없음
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

        REAL 모드 (DRY_RUN=False):
          - AccountState.total_balance
          - feed.state.total_balance
          - feed.total_balance_snap / feed.total_balance / feed.balance_total
          - ExchangeAPI.get_balance()["total"]
          중 하나로 real_balance 를 구한다.

        DRY_RUN 모드 (DRY_RUN=True):
          - 시뮬레이션 잔고(simulated_balance / initial_balance / initial_seed_usdt)가
            있으면 그 값을 사용
          - 없으면 INITIAL_SEED_USDT (>0 인 경우)
          - 둘 다 없으면 real_balance 를 그대로 사용

        최종적으로 total_balance_snap 을 반환하고, 로그로 남긴다.
        """
        real_balance = 0.0

        # 1) AccountState (feed.account / feed.account_state)
        account = getattr(feed, "account", None) or getattr(feed, "account_state", None)
        if isinstance(account, AccountState):
            try:
                real_balance = float(getattr(account, "total_balance", 0.0) or 0.0)
            except Exception:
                real_balance = 0.0

        # 2) feed.state.total_balance
        if real_balance <= 0.0:
            st = getattr(feed, "state", None)
            if st is not None:
                try:
                    v = getattr(st, "total_balance", None)
                    if v is not None:
                        real_balance = float(v or 0.0)
                except Exception:
                    pass

        # 3) feed 에 직접 붙어 있는 필드들
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

        # 4) 마지막 수단: ExchangeAPI 에서 직접 조회
        if real_balance <= 0.0:
            try:
                from core.exchange_api import exchange as exchange_api  # 지연 import
                bal = exchange_api.get_balance()
                if isinstance(bal, dict):
                    real_balance = float(bal.get("total") or 0.0)
            except Exception as e:
                self.logger.error(
                    "[WaveFSM] failed to fetch balance snapshot: %s", e
                )

        # ------------------------------
        # DRY_RUN / REAL 분기
        # ------------------------------
        total_balance_snap = float(real_balance or 0.0)

        if DRY_RUN:
            sim_balance = None

            # feed / feed.state 에 시뮬레이션 잔고가 있다면 우선 사용
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

            # 없으면 INITIAL_SEED_USDT 를 사용 (있다면)
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
        """
        LONG/SHORT Escape active 여부 추출.
        - feed.wave_state.{long_escape,short_escape}.active 우선
        - 없으면 feed.state.* 에서 best-effort 로 가져옴
        """
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
            # 예전/보조 필드들까지 best-effort 로 체크
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
        """
        (보조 유틸) Wave Start 조건 논리만 분리한 버전.

        실제 Start 수행은 _handle_wave_lifecycle() 에서 수행한다.
        """
        state = self.state_manager.get_state()
        feed.state = state  # BotState 단일 소스 유지

        pos_info = _extract_position_info(feed)
        long_size = float(pos_info.get("long_size", 0.0) or 0.0)
        short_size = float(pos_info.get("short_size", 0.0) or 0.0)

        # hedge_size 는 별도 위치에 있을 수 있으므로 best-effort 로 가져온다.
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

        return flat and (not escape_active) and (mode == "NORMAL")

    def _handle_wave_lifecycle(self, feed: StrategyFeed) -> None:
        """
        Wave Start / End 를 관리한다.

        - Wave End:
            · 현재 Wave 가 active 였고,
              포지션이 다시 flat 이면서 escape_active == False 이면
              내부 플래그를 비활성화 (다음 tick 에서 새 Wave Start 대상).

        - Wave Start:
            · Long/Short/Hedge 포지션이 모두 0
            · escape_active == False
            · mode == "NORMAL"
            · 그리고 현재 Wave 가 비활성 상태일 때만 수행.
        """
        # 1) BotState (mode, wave_id 등용)
        state = self.state_manager.get_state()

        # 2) 포지션: 무조건 gross 기준, feed.state(=BotState) 우선
        st = getattr(feed, "state", None)

        def _safe_float(v) -> float:
            try:
                return float(v or 0.0)
            except Exception:
                return 0.0

        long_size = _safe_float(getattr(st, "long_size", 0.0) if st is not None else 0.0)
        short_size = _safe_float(getattr(st, "short_size", 0.0) if st is not None else 0.0)
        hedge_size = _safe_float(getattr(st, "hedge_size", 0.0) if st is not None else 0.0)

        # 2-1) 모두 0 이면 feed.positions 로 한 번 더 보정 (REAL 모드 안전장치)
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

        # 3) flat 정의: LONG/SHORT/HEDGE 모두 0일 때만 flat (gross 기준)
        is_flat = (
            abs(long_size) < 1e-12
            and abs(short_size) < 1e-12
            and abs(hedge_size) < 1e-12
        )

        # 4) ESCAPE active 여부
        long_escape_active, short_escape_active = self._get_escape_active_flags(feed)
        escape_active = bool(long_escape_active or short_escape_active)

        # 5) 연속 flat tick 카운트 (escape_active=True 이면 카운트 안함)
        if is_flat and not escape_active:
            self.flat_ticks += 1
        else:
            self.flat_ticks = 0

        mode = str(getattr(state, "mode", "UNKNOWN") or "UNKNOWN").upper()

        # 6) DRY-RUN 테스트용 가짜 포지션 주입
        if self._fake_pos_for_test and long_size == 0 and short_size == 0:
            fake_qty = float(self._fake_pos_qty)
            long_size = fake_qty          # 가짜 롱 포지션 주입
            short_size = 0.0              # 숏은 0 유지
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

        # 7) Wave End 감지: 현재 Wave 가 active 였고 다시 완전 flat + escape_inactive
        if self._wave_active and is_flat and (not escape_active):
            self._wave_active = False
            self.logger.info(
                "[WaveFSM] Wave End detected: wave_id=%s (flat & escape_inactive)",
                getattr(state, "wave_id", -1),
            )

        # 8) Wave Start 조건:
        #    - 이전 Wave 가 종료된 상태(self._wave_active == False)
        #    - 최소 N tick 연속 flat 상태 유지 (여기서는 2)
        if (
            (not self._wave_active)
            and is_flat
            and (not escape_active)
            and mode == "NORMAL"
            and self.flat_ticks >= 2
        ):
            self._start_new_wave(feed)

    def _start_new_wave(self, feed: StrategyFeed) -> None:
        """
        Wave Start 시:

        - Total_Balance_snap 스냅샷 획득 (capital.py)
        - 25/25/50 + 0.9 effective seed 계산
        - unit_seed_long / unit_seed_short 계산
        - ATR_4H(42) → P_gap = max(ATR*0.15, 100)
        - P_center = 현재 price
        - state_manager.reset_wave(...) 호출로 wave_id++, k_dir/steps/line_memory 초기화
        """
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
            long_seed_total_effective=snap.long_seed_total_effective,
            short_seed_total_effective=snap.short_seed_total_effective,
            unit_seed_long=snap.unit_seed_long,
            unit_seed_short=snap.unit_seed_short,
            mode="NORMAL",
        )

        # 현재 tick 에서 바로 사용할 수 있도록 feed.state 갱신
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
                snap.total_balance_snap,
                snap.long_seed_total_effective,
                snap.short_seed_total_effective,
                snap.unit_seed_long,
                snap.unit_seed_short,
            )
        except Exception:
            pass

    # ==================================================================
    # 메인 엔트리 포인트 (실동작)
    # ==================================================================

    def tick(self, feed: StrategyFeed, now_ts: float) -> None:
        """
        WaveFSM 한 틱 실행.

        순서:
          0) BotState 싱크 (state_manager → feed.state)
          1) _read_pos_state() 로 실제 포지션을 읽고
             → feed.state / feed.positions 에 값을 강제로 싱크
          2) Wave lifecycle 처리 (_handle_wave_lifecycle)
          3) EscapeLogic.evaluate(feed) + OrderManager.apply_escape_decision (Mode B)
             · FULL_EXIT 인 경우 Grid(Maker)는 이 틱에서 실행하지 않음
          4) GridLogic.process(feed) + OrderManager.apply_decision (Mode A)
          5) Grid/Escape state_updates 를 BotState 에 반영 후 save_state()
        """
        if feed is None:
            self.logger.warning("[WaveFSM] tick: feed is None")
            return

        # 0) BotState 싱크: 항상 StateManager 의 BotState 를 feed.state 로 사용
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

            # 1-1) feed.state(long_size / short_size / hedge_size) 덮어쓰기
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
                    # dataclass / 기타 객체에서 실패해도 그냥 무시
                    pass

            # 1-2) feed.positions 도 dict 형태로 제공 (Grid / Escape 공통 사용)
            try:
                feed.positions = {
                    "long_size": long_size,
                    "short_size": short_size,
                    "hedge_size": hedge_size,
                }
            except Exception:
                # StrategyFeed 가 __slots__ 등으로 동적 속성을 막고 있어도 그냥 무시
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

        # Wave reset 이후 BotState 인스턴스가 교체되었을 수 있으므로 다시 싱크
        try:
            bot_state2 = self.state_manager.get_state()
            feed.state = bot_state2
        except Exception:
            pass

        # 3) EscapeLogic (Mode B) + OrderManager.apply_escape_decision
        try:
            esc_dec: EscapeDecision = self.escape_logic.evaluate(feed)
        except Exception as exc:
            self.logger.error(
                "[WaveFSM] EscapeLogic.evaluate() failed: %s", exc, exc_info=True
            )
            # Escape 실패 시에는 Escape 없이 Grid 만 실행 (보수적)
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

        # FULL_EXIT 가 트리거된 틱에서는 Grid Maker 를 새로 만들지 않는다.
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

        # 4) GridLogic (Mode A) + OrderManager.apply_decision
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

        # 5) Grid / Escape state_updates 를 BotState 에 반영 후 저장
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
        """
        v10.1 merge 규칙:

          - orders:
              GridDecision.orders + EscapeDecision.orders 를 단순 합치되,
              실제 개별 실행은 OrderManager 쪽에서
              A 모드 / B 모드로 나뉘어 처리한다.

          - state_updates:
              Grid 쪽 업데이트 → Escape 쪽 업데이트 순으로 덮어쓴다.
              (ESCAPE/FULL_EXIT 에서 mode/news/hedge 관련 필드 우선)
        """

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

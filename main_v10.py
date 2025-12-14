"""
main_v10.py - Entry point for Bybit Wave Bot v10.1

System Lifecycle (v10.1 7장) 을 실제 루프에 매핑:

  Wave Start  → Running → EscapePhase → Reset

- Wave Start:
    * Flat 상태 확인 (롱/숏/헷지 = 0)
    * capital / state_manager / wave_fsm 를 통해
      Total_Balance_snap / seed / unit_seed / k_dir / P_center / P_gap / wave_id 초기화

- Running Loop (tick 마다, Priority 관점):

    [P0] Escape / FULL_EXIT / Hedge (절대 우선, 절대 차단 금지)
         - EscapeLogic.evaluate(...) 내부에서 처리
         - FULL_EXIT / HEDGE_ENTRY / HEDGE_EXIT / +2% 동시청산 / 헷지 BE 청산

    [P1] RiskManager (뉴스/서킷)
         - 뉴스 필터 (T_event-60 ~ +30) → news_block
         - 서킷 브레이커 (vol_1m, Volume_1m, Volume_MA20) → cb_block
         - 여기서 Start-up / DCA / Refill 을 위한 Gate 플래그만 설정
         - ESCAPE / FULL_EXIT / HEDGE / TP / 강제청산은 절대 막지 않음

    [P2] GridLogic (EscapeActive 가 아닌 방향만)
         - Start-up / DCA / Refill / TP
         - news_block / cb_block / seed / LineMemory / last-chunk 규칙 Gate 적용

    [P3] OrderManager
         - GridDecision / EscapeDecision / FullExitDecision 을 모아서
           Mode A (Maker) / Mode B (Taker) / liquidation_slicer 로 실제 주문 실행

    [P4] Fill/Exit 이벤트
         - state_manager / capital / state_model 과 연동
         - k_dir, used_seed, remain_seed, line_memory(FREE/OPEN/LOCKED_LOSS) 업데이트

- EscapePhase & Reset:
    * EscapeLogic 이 ESCAPE_ACTIVE / ESCAPE_DONE 관리
    * Long/Short/Hedge 모두 size=0 이 되면 Wave 종료로 보고
      wave_fsm / state_manager 를 통해 다음 Wave Start 상태로 전환
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.exchange_api import exchange
from core.state_manager import StateManager
from core.order_manager import OrderManager
from strategy.capital import CapitalManager
from strategy.grid_logic import GridLogic
from strategy.escape_logic import EscapeLogic
from strategy.wave_fsm import WaveFSM
from strategy.risk_manager import RiskManager, RiskInputs
from strategy.feed_types import StrategyFeed, OrderInfo
from utils.logger import logger


# ------------------------------------------------------------
# 내부 스냅샷 구조체 (시장/계정 요약)
# ------------------------------------------------------------

@dataclass
class MarketSnapshot:
    """
    한 tick 에서 사용하는 시장/계정 스냅샷.
    """
    price: float
    atr_4h_42: float

    # 1분 OHLCV 기반
    move_1m: float              # 최근 1분 가격 변동률 절대값 (price basis)
    vol_1m: float               # 최근 1분 거래량 (Volume_1m)
    vol_ma20: float             # 최근 20분 거래량 평균 (Volume_MA20)
    last3_ranges: List[float]   # 최근 3개 1분봉 (high-low)
    last3_mids: List[float]     # 최근 3개 1분봉 mid (= (high+low)/2)

    # 1초 변동률 (loop_once 간 가격 변화 기준)
    vol_1s: float               # 직전 tick 대비 절대 변동률

    # Raw 데이터
    open_orders_raw: List[Dict[str, Any]]
    positions_raw: Dict[str, Any]
    balance_raw: Dict[str, Any]


# ------------------------------------------------------------
# WaveBot v10.1 Orchestrator
# ------------------------------------------------------------

class WaveBot:
    """
    WaveBot v10.1 메인 오케스트레이터.

    구성 요소:
      - exchange         : Bybit BTCUSDT Perp 래퍼
      - state_manager    : BotState 저장/로드 + Wave reset
      - capital          : seed / unit_seed / k_dir 회계
      - grid_logic       : Start-up / DCA / Refill / TP
      - escape_logic     : ESCAPE / HEDGE / FULL_EXIT (Priority 0)
      - risk_manager     : 뉴스 필터 / 서킷 브레이커 / Resume (Entry Gate 전용)
      - order_manager    : Mode A (Maker) / Mode B (Taker+Slicer) 실행 엔진
      - wave_fsm         : Grid + Escape + OrderManager + StateManager orchestration
    """

    def __init__(self) -> None:
        self.logger = logger
        self.exchange = exchange

        # 상태/전략 컴포넌트
        self.state_manager = StateManager()
        self.capital = CapitalManager()
        self.grid_logic = GridLogic(capital=self.capital)
        self.escape_logic = EscapeLogic(capital=self.capital)
        self.order_manager = OrderManager(exchange_instance=self.exchange)
        self.risk_manager = RiskManager()
        self.wave_fsm = WaveFSM(
            grid_logic=self.grid_logic,
            escape_logic=self.escape_logic,
            order_manager=self.order_manager,
            state_manager=self.state_manager,
            capital_manager=self.capital,
        )

        # 1초 변동률 계산을 위한 직전 가격
        self._last_price: Optional[float] = None

    # --------------------------------------------------------
    # Lifecycle hooks
    # --------------------------------------------------------

    def _update_market_state(self) -> None:
        """
        Exchange.get_positions() / get_balance() 결과를
        StateManager.state 에 반영한다.

        DRY_RUN / REAL 공통:
          - long_size / short_size
          - total_balance / free_balance
        """

        def _safe_float(v) -> float:
            try:
                return float(v or 0.0)
            except Exception:
                return 0.0

        # 현재 state 객체 (dict 또는 dataclass)
        state_obj = self.state_manager.state

        def _set_state(name: str, value) -> None:
            v = _safe_float(value)
            if isinstance(state_obj, dict):
                state_obj[name] = v
            else:
                try:
                    setattr(state_obj, name, v)
                except Exception:
                    # 없는 필드는 조용히 무시
                    pass

        # ------------------------------------------------------------------
        # 1) 포지션: get_positions()
        # ------------------------------------------------------------------
        try:
            positions = self.exchange.get_positions() or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[WaveBot] _update_market_state get_positions failed: %s", exc
            )
            positions = {}

        long_raw = positions.get("LONG") or positions.get("long") or {}
        short_raw = positions.get("SHORT") or positions.get("short") or {}

        long_size = _safe_float(long_raw.get("qty") or long_raw.get("size"))
        short_size = _safe_float(short_raw.get("qty") or short_raw.get("size"))

        self.logger.info(
            "[WaveBot] positions: long_size=%s short_size=%s",
            long_size,
            short_size,
        )

        _set_state("long_size", long_size)
        _set_state("short_size", short_size)

        # ------------------------------------------------------------------
        # 2) 잔고: get_balance()
        # ------------------------------------------------------------------
        try:
            balance = self.exchange.get_balance() or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[WaveBot] _update_market_state get_balance failed: %s", exc
            )
            balance = {}

        # Bybit UTA / 일반 케이스를 모두 커버하기 위한 유연한 키 처리
        total_balance = _safe_float(
            balance.get("total_equity")
            or balance.get("total_balance")
            or balance.get("equity")
        )
        free_balance = _safe_float(
            balance.get("available_balance")
            or balance.get("free_balance")
            or balance.get("available")
        )

        self.logger.info(
            "[WaveBot] balance: total_balance=%s free_balance=%s",
            total_balance,
            free_balance,
        )

        _set_state("total_balance", total_balance)
        _set_state("free_balance", free_balance)

    def start(self) -> None:
        """
        프로세스 시작 시 1회 호출.

        - StateManager 초기화 (파일에서 BotState 로딩이 구현되어 있다면 호출)
        - Exchange 세팅 (레버리지/헷지 모드 등은 exchange_api.set_leverage_and_mode 에서 처리)
        """
        self.logger.info("WaveBot v10.1 starting...")

        # StateManager 가 load() 또는 load_state() 를 제공한다면 best-effort 로 호출
        for meth_name in ("load", "load_state"):
            meth = getattr(self.state_manager, meth_name, None)
            if callable(meth):
                try:
                    meth()
                    self.logger.info("[WaveBot] StateManager.%s() 호출 완료", meth_name)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "[WaveBot] StateManager.%s() 호출 실패: %s", meth_name, exc
                    )
                break

        # Exchange 레버리지/포지션 모드 세팅 (Cross 7x + Hedge Mode)
        setup = getattr(self.exchange, "set_leverage_and_mode", None)
        if callable(setup):
            try:
                setup()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[WaveBot] exchange.set_leverage_and_mode() 실패: %s", exc
                )

    def shutdown(self) -> None:
        """
        프로세스 종료 시 호출. (필요시 정리 작업 추가)
        """
        self.logger.info("WaveBot v10.1 shutting down...")

        # StateManager 가 save/save_state 를 제공한다면 best-effort 로 호출
        for meth_name in ("save", "save_state"):
            meth = getattr(self.state_manager, meth_name, None)
            if callable(meth):
                try:
                    meth()
                    self.logger.info("[WaveBot] StateManager.%s() 호출 완료", meth_name)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "[WaveBot] StateManager.%s() 호출 실패: %s", meth_name, exc
                    )
                break

    # --------------------------------------------------------
    # 내부 유틸: BotState 가져오기
    # --------------------------------------------------------

    def _get_state_obj(self):
        """
        StateManager 구현이 get_state() 를 제공하면 그것을 사용,
        아니면 .state 속성을 직접 사용.
        """
        getter = getattr(self.state_manager, "get_state", None)
        if callable(getter):
            return getter()
        return getattr(self.state_manager, "state")

    # --------------------------------------------------------
    # 내부 유틸: ATR_4H(42) 계산 (간단 버전)
    # --------------------------------------------------------

    def _compute_atr_4h_42(self) -> float:
        """
        4H ATR(42) 계산.

        - exchange.fetch_ohlcv("4h", limit=42)를 사용.
        - 단순 TR 평균으로 ATR 근사 (명세상 정확한 수식보다 '규모'가 중요).
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(timeframe="4h", limit=42) or []
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[WaveBot] fetch_ohlcv(4h) failed: %s", exc)
            return 0.0

        if len(ohlcv) < 2:
            return 0.0

        trs: List[float] = []
        try:
            prev_close = float(ohlcv[0][4])
        except Exception:  # noqa: BLE001
            prev_close = 0.0

        for row in ohlcv:
            try:
                _ts, _o, h, l, c, _v = row
                h = float(h)
                l = float(l)
                c = float(c)
            except Exception:  # noqa: BLE001
                continue

            tr = max(
                h - l,
                abs(h - prev_close),
                abs(l - prev_close),
            )
            trs.append(tr)
            prev_close = c

        if not trs:
            return 0.0
        return sum(trs) / float(len(trs))

    # --------------------------------------------------------
    # 내부 유틸: 1분 OHLCV 기반 Risk 입력 계산
    # --------------------------------------------------------

    def _compute_1m_metrics(self) -> Dict[str, Any]:
        """
        1분 OHLCV 를 불러와 move_1m / vol_1m / vol_ma20 / last3_ranges / last3_mids 계산.
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(timeframe="1m", limit=20) or []
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[WaveBot] fetch_ohlcv(1m) failed: %s", exc)
            return {
                "move_1m": 0.0,
                "vol_1m": 0.0,
                "vol_ma20": 0.0,
                "last3_ranges": [],
                "last3_mids": [],
            }

        if not ohlcv:
            return {
                "move_1m": 0.0,
                "vol_1m": 0.0,
                "vol_ma20": 0.0,
                "last3_ranges": [],
                "last3_mids": [],
            }

        # 마지막 캔들 기준 move_1m / vol_1m
        last = ohlcv[-1]
        try:
            _ts, o, h, l, c, v = last
            o = float(o)
            c = float(c)
            v = float(v)
        except Exception:  # noqa: BLE001
            o = c = v = 0.0

        mid = (o + c) / 2.0 if (o + c) != 0 else c or o or 0.0
        move_1m = abs(c - o) / mid if mid > 0 else 0.0
        vol_1m = v

        # vol_ma20 = 마지막 20개 캔들의 거래량 평균
        vols: List[float] = []
        for row in ohlcv:
            try:
                vols.append(float(row[5]))
            except Exception:  # noqa: BLE001
                continue
        vol_ma20 = sum(vols) / float(len(vols)) if vols else 0.0

        # 최근 3개 캔들의 range / mid
        last3 = ohlcv[-3:]
        last3_ranges: List[float] = []
        last3_mids: List[float] = []
        for row in last3:
            try:
                _ts, _o, h, l, _c, _v = row
                h = float(h)
                l = float(l)
            except Exception:  # noqa: BLE001
                continue
            last3_ranges.append(h - l)
            last3_mids.append((h + l) / 2.0 if (h + l) != 0 else 0.0)

        return {
            "move_1m": move_1m,
            "vol_1m": vol_1m,
            "vol_ma20": vol_ma20,
            "last3_ranges": last3_ranges,
            "last3_mids": last3_mids,
        }

    # --------------------------------------------------------
    # 스냅샷 → MarketSnapshot 생성
    # --------------------------------------------------------

    def _build_market_snapshot(self, now_ts: float) -> MarketSnapshot:  # noqa: ARG002
        # 1) 가격
        try:
            price = float(self.exchange.get_ticker() or 0.0)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[WaveBot] get_ticker failed: %s", exc)
            price = 0.0

        # 2) ATR_4H(42)
        atr_4h_42 = self._compute_atr_4h_42()

        # 3) 1분 OHLCV 기반 Risk 지표
        one_min = self._compute_1m_metrics()
        move_1m = one_min["move_1m"]
        vol_1m = one_min["vol_1m"]
        vol_ma20 = one_min["vol_ma20"]
        last3_ranges = one_min["last3_ranges"]
        last3_mids = one_min["last3_mids"]

        # 4) 1초 변동률 (직전 loop vs 현재 가격)
        if self._last_price and self._last_price > 0 and price > 0:
            vol_1s = abs(price - self._last_price) / self._last_price
        else:
            vol_1s = 0.0

        # 5) Open orders / Positions / Balance
        try:
            open_orders_raw = self.exchange.get_open_orders() or []
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[WaveBot] get_open_orders failed: %s", exc)
            open_orders_raw = []

        try:
            positions_raw = self.exchange.get_positions() or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[WaveBot] get_positions failed: %s", exc)
            positions_raw = {}

        try:
            balance_raw = self.exchange.get_balance() or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[WaveBot] get_balance failed: %s", exc)
            balance_raw = {}

        return MarketSnapshot(
            price=price,
            atr_4h_42=atr_4h_42,
            move_1m=move_1m,
            vol_1m=vol_1m,
            vol_ma20=vol_ma20,
            last3_ranges=last3_ranges,
            last3_mids=last3_mids,
            vol_1s=vol_1s,
            open_orders_raw=open_orders_raw,
            positions_raw=positions_raw,
            balance_raw=balance_raw,
        )

    # --------------------------------------------------------
    # Positions/Balance → BotState 동기화
    # --------------------------------------------------------
    # --------------------------------------------------------
    # Positions → BotState 동기화

    def _update_state_from_positions(self, snap, state) -> None:
        """
        Bybit Hedge Mode 포지션에서
        LONG / SHORT 각각의 qty, avg_price 기반으로
        정확한 PnL을 계산하여 BotState 에 저장한다.

        계산식:
          롱  : (price_now - avg_price) * qty
          숏  : (avg_price - price_now) * qty
        """

        # ----------------------
        # 1) 안전한 값 빼오기
        # ----------------------
        price_now = float(getattr(snap, "price", 0.0) or 0.0)
        pos_raw = getattr(snap, "positions_raw", None)
        if pos_raw is None:
            pos_raw = getattr(snap, "positions", None)

        long_size = short_size = 0.0
        long_avg = short_avg = 0.0

        # ----------------------
        # 2) dict 형태
        # ----------------------
        if isinstance(pos_raw, dict):
            long_info = pos_raw.get("LONG") or pos_raw.get("long") or {}
            short_info = pos_raw.get("SHORT") or pos_raw.get("short") or {}

            long_size  = float(long_info.get("qty", 0.0) or 0.0)
            short_size = float(short_info.get("qty", 0.0) or 0.0)

            long_avg  = float(long_info.get("avg_price", 0.0) or 0.0)
            short_avg = float(short_info.get("avg_price", 0.0) or 0.0)

        # ----------------------
        # 3) list(ccxt) 형태
        # ----------------------
        elif isinstance(pos_raw, (list, tuple)):
            for p in pos_raw:
                if not isinstance(p, dict):
                    continue
                info = p.get("info") or {}

                qty = float(p.get("contracts") or info.get("size") or 0.0)
                if qty <= 0:
                    continue

                # 방향
                side = (p.get("side") or info.get("side") or "").lower()
                if not side:
                    idx = str(info.get("positionIdx", ""))
                    if idx == "1":
                        side = "long"
                    elif idx == "2":
                        side = "short"

                # 평균가
                avg = float(
                    p.get("entryPrice")
                    or p.get("avg_price")
                    or info.get("avgPrice")
                    or 0.0
                )

                if side in ("long", "buy"):
                    long_size += qty
                    long_avg = avg
                elif side in ("short", "sell"):
                    short_size += qty
                    short_avg = avg

        # ----------------------
        # 4) PnL 계산
        # ----------------------
        long_pnl = 0.0
        short_pnl = 0.0

        if price_now > 0:
            if long_size > 0 and long_avg > 0:
                long_pnl = (price_now - long_avg) * long_size
            if short_size > 0 and short_avg > 0:
                short_pnl = (short_avg - price_now) * short_size

        # ----------------------
        # 5) BotState에 저장
        # ----------------------
        updates = {
            "long_size": long_size,
            "short_size": short_size,
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "long_pos_nonzero": (long_size > 0),
            "short_pos_nonzero": (short_size > 0),
            "long_pnl_sign": 1 if long_pnl > 0 else -1 if long_pnl < 0 else 0,
            "short_pnl_sign": 1 if short_pnl > 0 else -1 if short_pnl < 0 else 0,
        }

        for k, v in updates.items():
            setattr(state, k, v)
            self.state_manager.update(k, v)

        self.logger.info(
            "[PnL-CORE] price=%.2f L(size=%.4f avg=%.2f pnl=%.4f) "
            "S(size=%.4f avg=%.2f pnl=%.4f)",
            price_now,
            long_size, long_avg, long_pnl,
            short_size, short_avg, short_pnl,
        )

    # --------------------------------------------------------
    # RiskInputs 구성 + RiskDecision → BotState 반영
    # --------------------------------------------------------

    def _build_risk_inputs(
        self,
        snap: MarketSnapshot,
        now_ts: float,
        state: Any,
    ) -> RiskInputs:
        p_gap = float(getattr(state, "p_gap", 0.0) or 0.0)
        p_center = float(getattr(state, "p_center", 0.0) or 0.0)

        # news_events 는 외부 스케줄러에서 주입하는 것을 전제로 함 (여기서는 None)
        return RiskInputs(
            ts=now_ts,
            is_macro=False,               # 뉴스 스케줄이 없는 경우 기본 False
            p_gap=p_gap,
            move_1m=snap.move_1m,
            vol_1m=snap.vol_1m,
            vol_ma20=snap.vol_ma20,
            last3_ranges=snap.last3_ranges,
            p_center=p_center,
            last3_mids=snap.last3_mids,
            news_events=None,
        )

    def _apply_risk_decision_to_state(self, risk_decision) -> None:
        """
        RiskManager 가 판단한 news_block / cb_block 을 BotState 에 반영.
        GridLogic / EscapeLogic 은 BotState 의 플래그를 보고 Gate 동작을 수행한다.

        *중요*: 여기서는 Entry 계열 Gate 플래그만 쓴다.
        ESCAPE / FULL_EXIT / TP / HEDGE / 강제청산은
        RiskManager 에 의해 차단되면 안 되고, 실제 행동 여부는
        EscapeLogic / OrderManager 에서 결정한다.
        """
        updates: Dict[str, Any] = {}
        updates["news_block"] = bool(getattr(risk_decision, "news_block", False))
        updates["cb_block"] = bool(getattr(risk_decision, "cb_block", False))

        for key, value in updates.items():
            try:
                self.state_manager.update(key, value)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[WaveBot] state_manager.update(%s) failed: %s", key, exc
                )

    # --------------------------------------------------------
    # StrategyFeed 구성
    # --------------------------------------------------------

    def _map_open_orders(self, open_orders_raw: List[Dict[str, Any]]) -> List[OrderInfo]:
        """
        ccxt 표준 주문 구조를 내부 OrderInfo 로 매핑.
        (필요 필드만 best-effort 로 추출)
        """
        result: List[OrderInfo] = []
        for o in open_orders_raw:
            try:
                order_id = str(o.get("id") or o.get("order_id") or "")
                side = str(o.get("side") or "").upper()  # "BUY" / "SELL"
                price = float(o.get("price") or 0.0)
                qty = float(o.get("amount") or o.get("qty") or 0.0)
                filled_qty = float(o.get("filled") or 0.0)
                reduce_only = bool(o.get("reduceOnly") or o.get("reduce_only") or False)
                order_type = str(o.get("type") or "limit").capitalize()  # "Limit", ...
                tif = str(o.get("timeInForce") or o.get("time_in_force") or "").capitalize()
                tag = o.get("clientOrderId") or o.get("tag") or None
                created_ts = float(
                    o.get("timestamp") or o.get("createdTime") or time.time() * 1000
                ) / 1000.0
            except Exception:  # noqa: BLE001
                continue

            result.append(
                OrderInfo(
                    order_id=order_id,
                    side=side,
                    price=price,
                    qty=qty,
                    filled_qty=filled_qty,
                    reduce_only=reduce_only,
                    order_type=order_type,
                    time_in_force=tif,
                    tag=tag,
                    created_ts=created_ts,
                )
            )
        return result

    def _build_strategy_feed(
        self,
        snap: MarketSnapshot,
        state: Any,
    ) -> StrategyFeed:
        """
        Running Loop 단계에서 Grid / Escape / OrderManager 에 전달할 StrategyFeed 구성.

        - price / atr_4h_42
        - state (BotState)
        - open_orders (OrderInfo 리스트)
        - pnl_total / pnl_total_pct (Wave 스냅샷 Total_Balance 기준, best-effort)
        """
        price = float(snap.price or 0.0)

        # PnL 은 BotState 에서 long_pnl / short_pnl 등을 받아 사용
        long_pnl = float(getattr(state, "long_pnl", 0.0) or 0.0)
        short_pnl = float(getattr(state, "short_pnl", 0.0) or 0.0)
        pnl_total = long_pnl + short_pnl

        # 분모: Wave 시작 시 스냅샷된 Total_Balance (있으면 사용), 없으면 현재 balance_raw
        total_balance_snap = float(
            getattr(state, "total_balance_snap", getattr(state, "total_balance", 0.0))
            or 0.0
        )

        if total_balance_snap <= 0.0:
            bal = getattr(snap, "balance_raw", None)
            if isinstance(bal, dict):
                try:
                    total_balance_snap = float(
                        bal.get("total_equity")
                        or bal.get("total_balance")
                        or bal.get("equity")
                        or 0.0
                    )
                except Exception:
                    total_balance_snap = 0.0

        pnl_total_pct = pnl_total / total_balance_snap if total_balance_snap > 0 else 0.0

        open_orders = self._map_open_orders(snap.open_orders_raw)

        feed = StrategyFeed(
            price=price,
            atr_4h_42=snap.atr_4h_42,
            state=state,
            open_orders=open_orders,
            pnl_total=pnl_total,
            pnl_total_pct=pnl_total_pct,
        )

        # Escape / Grid 에서 사용할 직전 가격
        prev_price = self._last_price if (self._last_price and self._last_price > 0) else price
        setattr(feed, "price_prev", prev_price)

        # ESCAPE / liquidation_slicer 에서 사용할 수 있도록 vol_1s / 1m price move 를 feed 에 부착
        # - vol_1s : 직전 tick vs 현재 tick 1초 변동률
        # - vol_1m : 최근 1분 price 변동률 절대값
        setattr(feed, "vol_1s", snap.vol_1s)
        setattr(feed, "vol_1m", snap.move_1m)

        # Risk / News / CB 플래그도 feed 에 복사 (grid_logic 등에서 사용)
        setattr(feed, "news_block", bool(getattr(state, "news_block", False)))
        setattr(feed, "cb_block", bool(getattr(state, "cb_block", False)))

        # (옵션) account 스냅샷이 필요한 다른 모듈을 위해 balance_raw 도 붙여둘 수 있음
        setattr(feed, "balance_raw", snap.balance_raw)

        return feed

    # --------------------------------------------------------
    # 메인 루프 한 사이클 (Start/Running/Escape/Reset 포함)
    # --------------------------------------------------------

    def loop_once(self) -> None:
        """
        WaveBot v10.1 메인 1 tick.

        순서 (실제 실행 순서 기준):

          1) Market / State snapshot
          2) RiskManager → news_block / cb_block → BotState 반영
              - 이 단계는 'Entry Gate' 만 설정하며,
                ESCAPE / FULL_EXIT / TP / 강제청산을 막지 않는다.
          3) StrategyFeed 생성 (Risk 플래그가 반영된 최신 BotState 기준)
          4) WaveFSM.tick(feed, now_ts) 호출
             - 내부에서 다음을 Priority 에 맞게 수행:
               · Wave Start 조건 확인 (flat + NORMAL + escape_inactive)
               · EscapeLogic (Priority 0):
                     ESCAPE Trigger / ESCAPE_ACTIVE / FULL_EXIT / HEDGE / +2% / BE
               · ESCAPE_ACTIVE 가 아닌 방향에 대해서만 GridLogic:
                     Start-up / DCA / Refill / TP
               · OrderManager:
                     Grid / Escape / FULL_EXIT / HEDGE 주문 실행
               · EscapePhase → Reset:
                     flat 복귀 시 Wave 종료 및 Wave reset
        """
        now_ts = time.time()

        # 1) Market / State snapshot
        state_before = self._get_state_obj()
        snap = self._build_market_snapshot(now_ts)

        # 1.5) 포지션/잔고 정보를 BotState 에 동기화
        self._update_state_from_positions(snap, state_before)

        # 2) RiskManager 평가 → news_block / cb_block 플래그 갱신
        risk_inputs = self._build_risk_inputs(snap, now_ts, state_before)
        risk_decision = self.risk_manager.process(risk_inputs)
        self._apply_risk_decision_to_state(risk_decision)

        # Risk 반영 후 최신 BotState 로 StrategyFeed 구성
        state_for_feed = self._get_state_obj()

        # 3) StrategyFeed 생성
        feed = self._build_strategy_feed(snap, state_for_feed)

        # 4) WaveFSM tick 실행
        #    - Wave Start / Running / EscapePhase / Reset 을 내부에서 처리
        #    - EscapeLogic 이 Priority 0 으로 항상 Grid / 뉴스 / 서킷 / TP 보다 우선
        try:
            self.wave_fsm.tick(feed, now_ts)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("[WaveBot] WaveFSM.tick failed: %s", exc)

        # 직전 가격 갱신 (다음 tick 에 1초 변동률/price_prev 계산용)
        if snap.price > 0:
            self._last_price = snap.price


# ------------------------------------------------------------
# CLI / main() 진입점
# ------------------------------------------------------------

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bybit Wave Bot v10.1 runner")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single loop iteration and exit (for diagnostics).",
    )
    parser.add_argument(
        "--loop-interval",
        type=float,
        default=1.0,
        help="Main loop sleep interval in seconds (default: 1.0).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    bot = WaveBot()

    # Graceful shutdown via signals
    stop = {"flag": False}

    def handle_sigterm(signum, frame):  # noqa: ARG001
        stop["flag"] = True
        bot.logger.info("Received signal %s, shutting down...", signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_sigterm)

    try:
        bot.start()
        if args.once:
            bot.loop_once()
            return 0

        bot.logger.info(
            "WaveBot v10.1 main loop started (interval=%.3fs)", args.loop_interval
        )
        while not stop["flag"]:
            bot.loop_once()
            time.sleep(args.loop_interval)

    except Exception as exc:  # noqa: BLE001 - top-level guard
        # Top-level guard: we log the exception and exit with non-zero code.
        try:
            bot.logger.exception("Fatal error in main loop: %s", exc)
        except Exception:  # noqa: BLE001
            # If logger itself fails, still print to stderr.
            print(f"[FATAL] WaveBot main loop error: {exc!r}", file=sys.stderr)
        return 1

    finally:
        try:
            bot.shutdown()
        except Exception:  # noqa: BLE001
            # Avoid masking original errors.
            pass

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

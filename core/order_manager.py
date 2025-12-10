from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Dict, Optional, Callable

from core.exchange_api import exchange
from utils.logger import logger
from utils.calculator import calc_contract_qty
from utils.escape_events import log_full_exit_trigger
from strategy.feed_types import StrategyFeed
from strategy.grid_logic import GridDecision, GridOrderSpec
from strategy.escape_logic import EscapeDecision, EscapeOrderSpec
from strategy.liquidation_slicer import LiquidationSlicer


@dataclass
class ApiOrderSpec:
    """내부 표준 주문 스펙 (Mode A / Mode B 공통 메타용)"""
    side: str
    type: str
    qty: float
    price: float | None
    time_in_force: str
    reduce_only: bool
    tag: str | None


MAX_SLICE_NOTIONAL = 70_000.0  # FULL_EXIT / ESCAPE 슬라이스 기준 (USDT)


class OrderManager:
    """
    v10.1 Order Execution Layer

    - Mode A (Grid, Maker Only):
        · GridDecision → apply_decision(...)
        · LIMIT (Maker 중심 경로)
        · 60초 후 남은 수량 있으면, 같은 가격/잔량으로 다시 LIMIT 재배치
        · Mode A 경로에서는 절대 Market 사용 금지

    - Mode B (ESCAPE/HEDGE/FULL_EXIT, Taker Fallback):
        · EscapeDecision → apply_escape_decision(...)
        · FULL_EXIT / HEDGE_EXIT:
              liquidation_slicer 를 통해
              70k 슬라이스 + high-vol override 기준으로 MARKET 청산
        · HEDGE_ENTRY:
              우선 LIMIT (최대 1초 대기) 시도 후,
              남은 수량은 liquidation_slicer 기반 MARKET 진입
    """

    def __init__(self, exchange_instance: Any | None = None) -> None:
        self.exchange = exchange_instance or exchange
        self.logger = logger
        # Mode A 주문 메타 (60초 재배치용)
        self._order_meta: Dict[str, Dict[str, Any]] = {}
        # Market 슬라이스/override용 Slicer
        self.liquidation_slicer = LiquidationSlicer(max_slice_notional=MAX_SLICE_NOTIONAL)

    # ==================================================================
    # v10.1 A 모드: GridDecision 처리 (Maker Only)
    # ==================================================================

    def apply_decision(self, decision: GridDecision, feed: StrategyFeed, now_ts: float) -> None:
        """
        Mode A — Grid Maker 전용.

        - cancel → entries → replaces 순으로 처리
        - 생성되는 모든 주문은 LIMIT 로 간주
        - Market 주문은 절대 사용하지 않음
        """
        mode = decision.mode

        self.logger.info(
            "[OrderManager] apply_decision(mode=%s, cancels=%d, entries=%d, replaces=%d)",
            mode,
            len(decision.grid_cancels),
            len(decision.grid_entries),
            len(decision.grid_replaces),
        )

        # 1) 취소 먼저 처리
        for oid in decision.grid_cancels:
            try:
                self.exchange.cancel_order(oid)
                self.logger.info("[GridCancel] Cancelled order_id=%s", oid)
            except Exception as e:
                self.logger.warning("[GridCancel] Cancel failed id=%s err=%s", oid, e)
            finally:
                # Mode A 60초 재배치 루프가 있다면 중단되도록 메타 제거
                self._order_meta.pop(oid, None)

        # 2) 신규 Grid Maker 생성
        for spec in decision.grid_entries:
            self._create_mode_a_order(spec, now_ts)

        # 3) 재배치 (실질적으로는 새 Maker 생성과 동일 취급)
        for spec in decision.grid_replaces:
            self._create_mode_a_order(spec, now_ts)

    # ==================================================================
    # v10.1 B 모드: EscapeDecision 처리 (ESCAPE/FULL_EXIT/HEDGE)
    # ==================================================================

    def apply_escape_decision(
        self,
        esc_decision: EscapeDecision,
        feed: StrategyFeed,
        now_ts: float,
    ) -> None:
        """
        Mode B — ESCAPE / HEDGE / FULL_EXIT

        - FULL_EXIT: 전체 포지션을 liquidation_slicer 기준으로 MARKET 청산
        - HEDGE_ENTRY: Mode B LIMIT + 1초 대기 → 남은 수량 slicer 기반 MARKET
        - HEDGE_EXIT : slicer 기반 MARKET 청산
        """
        if not esc_decision.full_exit and not esc_decision.orders:
            return

        self.logger.info(
            "[OrderManager] apply_escape_decision(full_exit=%s, orders=%d)",
            esc_decision.full_exit,
            len(esc_decision.orders),
        )

        price = float(feed.price or 0.0)
        if price <= 0:
            self.logger.warning(
                "[OrderManager] apply_escape_decision skipped: invalid price=%.4f",
                price,
            )
            return

        # 1) FULL_EXIT → 모든 포지션 슬라이스 종료
        if esc_decision.full_exit:
            self._execute_full_exit(feed)
            return

        # 2) ESCAPE/HEDGE 주문 처리
        for spec in esc_decision.orders:
            if spec.qty <= 0:
                continue

            if spec.type == "HEDGE_ENTRY":
                self.logger.info(
                    "[Escape] HEDGE_ENTRY side=%s qty=%.6f",
                    spec.side,
                    spec.qty,
                )
                self._execute_hedge_entry(
                    logical_side=spec.side,
                    total_qty=spec.qty,
                    feed=feed,
                    tag="ESCAPE_HEDGE_ENTRY",
                )

            elif spec.type == "HEDGE_EXIT":
                self.logger.info(
                    "[Escape] HEDGE_EXIT side=%s qty=%.6f",
                    spec.side,
                    spec.qty,
                )
                self._execute_sliced_market_exit(
                    logical_side=spec.side,
                    total_qty=spec.qty,
                    price=price,
                    tag="ESCAPE_HEDGE_EXIT",
                    feed=feed,
                )

            else:
                self.logger.warning(
                    "[OrderManager] Unknown EscapeOrderSpec.type=%s", spec.type
                )

    # ==================================================================
    # 내부: FULL_EXIT 및 슬라이스 종료 (Mode B, Market)
    # ==================================================================

    def _execute_full_exit(self, feed: StrategyFeed) -> None:
        """
        FULL_EXIT: 모든 메인 포지션을 liquidation_slicer 규칙에 따라
        70k 슬라이스 + high‑vol override 기준으로 MARKET 종료.

        - 여기서 FULL_EXIT_TRIGGER 이벤트도 함께 남긴다.
        """
        price = float(feed.price or 0.0)
        if price <= 0.0:
            self.logger.error("[FullExit] invalid price=%.4f", price)
            return

        try:
            positions = self.exchange.get_positions()
        except Exception as exc:
            self.logger.error("[FullExit] get_positions() failed: %s", exc)
            return

        long_pos = positions.get("LONG", {}) or {}
        short_pos = positions.get("SHORT", {}) or {}

        long_qty = float(long_pos.get("qty", 0.0) or 0.0)
        short_qty = float(short_pos.get("qty", 0.0) or 0.0)

        self.logger.info(
            "[FullExit] positions LONG=%.6f SHORT=%.6f price=%.2f",
            long_qty, short_qty, price,
        )

        # FULL_EXIT_TRIGGER 이벤트 로그
        positions_before = {
            "LONG": {
                "qty": long_qty,
                "avg_price": float(long_pos.get("avg_price", 0.0) or 0.0),
            },
            "SHORT": {
                "qty": short_qty,
                "avg_price": float(short_pos.get("avg_price", 0.0) or 0.0),
            },
            "HEDGE": {
                "qty": float(getattr(feed.state, "hedge_size", 0.0) or 0.0),
                "side": getattr(feed.state, "hedge_side", None),
            },
        }
        try:
            log_full_exit_trigger(
                self.logger,
                pnl_total_pct=float(getattr(feed, "pnl_total_pct", 0.0) or 0.0),
                wave_id=getattr(feed.state, "wave_id", 0),
                positions_before=positions_before,
            )
        except Exception as exc:
            self.logger.warning("[FullExit] log_full_exit_trigger failed: %s", exc)

        # LONG 포지션 종료 (SELL reduce-only)
        if long_qty > 0.0:
            self._execute_sliced_market_exit(
                logical_side="LONG",
                total_qty=long_qty,
                price=price,
                tag="FULL_EXIT",
                feed=feed,
            )

        # SHORT 포지션 종료 (BUY reduce-only)
        if short_qty > 0.0:
            self._execute_sliced_market_exit(
                logical_side="SHORT",
                total_qty=short_qty,
                price=price,
                tag="FULL_EXIT",
                feed=feed,
            )

    # ==================================================================
    # 내부: side 코드 변환 (Bybit wrapper 호환)
    # ==================================================================

    def _side_code_for_exit(self, logical_side: str) -> int:
        """
        logical_side:
          - "LONG"  → side_code=4 (Long 포지션 종료 = Sell reduce-only)
          - "SHORT" → side_code=2 (Short 포지션 종료 = Buy reduce-only)
        """
        s = (logical_side or "").upper()
        if s == "LONG":
            return 4
        else:
            return 2

    def _side_code_for_entry(self, logical_side: str) -> int:
        """
        logical_side:
          - "LONG"  → side_code=1 (Long 진입 = Buy)
          - "SHORT" → side_code=3 (Short 진입 = Sell)
        """
        s = (logical_side or "").upper()
        if s == "LONG":
            return 1
        else:
            return 3

    # ==================================================================
    # 내부: liquidation_slicer 기반 Market Exit (Mode B)
    # ==================================================================

    def _execute_sliced_market_exit(
        self,
        logical_side: str,
        total_qty: float,
        price: float,
        tag: str,
        feed: Optional[StrategyFeed] = None,
    ) -> None:
        """
        liquidation_slicer 를 사용해:

          - 70k USDT 단위 슬라이스
          - high‑vol override (vol_1s ≥ 0.001 & vol_1s ≥ 3 * vol_1m) 시 전액 한 번에

        으로 Market 청산 수행.
        """
        if total_qty <= 0.0 or price <= 0.0:
            return

        total_notional = abs(total_qty) * price
        if total_notional <= 0.0:
            return

        vol_1s = 0.0
        vol_1m = 0.0
        if feed is not None:
            # feed 에서 제공되면 사용, 없으면 0으로 둔다 (override 미발동)
            vol_1s = float(getattr(feed, "vol_1s", 0.0) or 0.0)
            vol_1m = float(getattr(feed, "vol_1m", 0.0) or 0.0)

        self.logger.info(
            "[SliceExit] %s side=%s total_qty=%.6f total_notional=%.2f vol_1s=%.4f vol_1m=%.4f",
            tag, logical_side, total_qty, total_notional, vol_1s, vol_1m,
        )

        def _market_cb(side_str: str, qty: float) -> None:
            """
            liquidation_slicer 에서 호출하는 실제 Market 주문 콜백.
            side_str 은 logical_side 와 동일 ("LONG"/"SHORT" 등).
            """
            if qty <= 0.0:
                return
            side_code = self._side_code_for_exit(side_str)
            try:
                oid = self.exchange.place_market_order(side_code, qty)
                self.logger.info(
                    "[SliceExit] %s MARKET side=%s side_code=%s qty=%.6f oid=%s",
                    tag, side_str, side_code, qty, oid,
                )
            except Exception as exc:
                self.logger.error(
                    "[SliceExit] %s MARKET failed side=%s side_code=%s qty=%.6f err=%s",
                    tag, side_str, side_code, qty, exc,
                )

        # 실제 슬라이스 실행 (슬라이스 간 1초 딜레이 포함)
        plan = self.liquidation_slicer.execute_sliced_liquidation(
            side=logical_side,
            total_notional=total_notional,
            price=price,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            place_market_order=_market_cb,
        )

        self.logger.info(
            "[SliceExit] %s completed mode=%s slices=%d reason=%s",
            tag, plan.mode, len(plan.slices), plan.reason,
        )

    # ==================================================================
    # 내부: HEDGE_ENTRY Mode B LIMIT + 1초 후 slicer fallback
    # ==================================================================

    def _execute_hedge_entry(
        self,
        logical_side: str,
        total_qty: float,
        feed: StrategyFeed,
        tag: str,
    ) -> None:
        """
        ESCAPE Hedge Entry (Mode B):

          1) LIMIT 진입 (최대 1초 대기)
          2) 남은 잔량이 있으면, liquidation_slicer 를 통해
             70k 슬라이스 + high-vol override 기준 MARKET 진입
        """
        price = float(feed.price or 0.0)
        if total_qty <= 0.0 or price <= 0.0:
            return

        side_code = self._side_code_for_entry(logical_side)

        self.logger.info(
            "[HedgeEntry] %s LIMIT side=%s side_code=%s qty=%.6f price=%.2f",
            tag, logical_side, side_code, total_qty, price,
        )

        try:
            oid = self.exchange.place_limit_order(side_code, price, total_qty)
        except Exception as exc:
            self.logger.error(
                "[HedgeEntry] %s LIMIT failed side_code=%s qty=%.6f err=%s",
                tag, side_code, total_qty, exc,
            )
            return

        if not oid:
            self.logger.error(
                "[HedgeEntry] %s LIMIT returned empty oid (side_code=%s qty=%.6f)",
                tag, side_code, total_qty,
            )
            return

        # 최대 1초 대기 후 잔량 확인
        try:
            time.sleep(1.0)
            status = self.exchange.get_order_status(oid)
            filled = float(status.get("dealVol", 0.0) or 0.0)
        except Exception as exc:
            self.logger.error(
                "[HedgeEntry] %s get_order_status failed oid=%s err=%s",
                tag, oid, exc,
            )
            filled = 0.0

        remain = max(total_qty - filled, 0.0)

        # LIMIT 주문은 정리
        try:
            self.exchange.cancel_order(oid)
        except Exception as exc:
            self.logger.warning(
                "[HedgeEntry] %s cancel_order failed oid=%s err=%s",
                tag, oid, exc,
            )

        self.logger.info(
            "[HedgeEntry] %s LIMIT filled=%.6f remain=%.6f",
            tag, filled, remain,
        )

        # 남은 잔량은 slicer 기반 MARKET 진입
        if remain > 0.0:
            self._execute_sliced_market_entry(
                logical_side=logical_side,
                total_qty=remain,
                price=price,
                tag=f"{tag}_FALLBACK",
                feed=feed,
            )

    def _execute_sliced_market_entry(
        self,
        logical_side: str,
        total_qty: float,
        price: float,
        tag: str,
        feed: Optional[StrategyFeed] = None,
    ) -> None:
        """
        HEDGE_ENTRY fallback 용 MARKET 진입 (Mode B).

        FULL_EXIT/HEDGE_EXIT 은 모두 'exit' 방향이지만,
        HEDGE_ENTRY fallback 은 'entry' 방향으로 슬라이스.
        """
        if total_qty <= 0.0 or price <= 0.0:
            return

        total_notional = abs(total_qty) * price
        if total_notional <= 0.0:
            return

        vol_1s = 0.0
        vol_1m = 0.0
        if feed is not None:
            vol_1s = float(getattr(feed, "vol_1s", 0.0) or 0.0)
            vol_1m = float(getattr(feed, "vol_1m", 0.0) or 0.0)

        self.logger.info(
            "[SliceEntry] %s side=%s total_qty=%.6f total_notional=%.2f vol_1s=%.4f vol_1m=%.4f",
            tag, logical_side, total_qty, total_notional, vol_1s, vol_1m,
        )

        def _market_cb(side_str: str, qty: float) -> None:
            if qty <= 0.0:
                return
            side_code = self._side_code_for_entry(side_str)
            try:
                oid = self.exchange.place_market_order(side_code, qty)
                self.logger.info(
                    "[SliceEntry] %s MARKET side=%s side_code=%s qty=%.6f oid=%s",
                    tag, side_str, side_code, qty, oid,
                )
            except Exception as exc:
                self.logger.error(
                    "[SliceEntry] %s MARKET failed side=%s side_code=%s qty=%.6f err=%s",
                    tag, side_str, side_code, qty, exc,
                )

        plan = self.liquidation_slicer.execute_sliced_liquidation(
            side=logical_side,
            total_notional=total_notional,
            price=price,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            place_market_order=_market_cb,
        )

        self.logger.info(
            "[SliceEntry] %s completed mode=%s slices=%d reason=%s",
            tag, plan.mode, len(plan.slices), plan.reason,
        )

    # ==================================================================
    # 내부: Mode A Maker Order 생성 + 60초 재배치
    # ==================================================================

    def _create_mode_a_order(self, spec: GridOrderSpec, now_ts: float) -> None:
        """
        GridOrderSpec(side="BUY"/"SELL") 를 받아
        Bybit side code(1/3)를 올바르게 매핑한 뒤
        ExchangeAPI.place_limit_order 에 전달한다.

        v10.1:
        - Mode A 에서는 LIMIT 기반 Maker 경로만 사용
        - 주문 생성 후 60초 뒤 잔량이 있으면 같은 가격/잔량으로 1회 재지정가
        """
        side_str = spec.side.upper()
        if side_str == "BUY":
            logical_side = "LONG"
        else:
            logical_side = "SHORT"

        side_code = self._side_code_for_entry(logical_side)
        tag = f"W{spec.wave_id}_GRID_A_{spec.grid_index}_{spec.side}"

        try:
            oid = self.exchange.place_limit_order(side_code, spec.price, spec.qty)
        except Exception as exc:
            self.logger.error(
                "[OrderManager] Mode A order failed side=%s price=%s qty=%s err=%s",
                spec.side, spec.price, spec.qty, exc
            )
            return

        if not oid:
            self.logger.error(
                "[OrderManager] Mode A order failed (empty oid) side=%s price=%s qty=%s",
                spec.side, spec.price, spec.qty,
            )
            return

        # 메타데이터 저장 (60초 재배치 + 디버깅/추적용)
        self._order_meta[oid] = {
            "order_id": oid,
            "mode": "A",
            "kind": "GRID",
            "grid_index": spec.grid_index,
            "wave_id": spec.wave_id,
            "side": spec.side,
            "price": spec.price,
            "qty": spec.qty,
            "created_ts": now_ts,
            "tag": tag,
            "reduce_only": False,
        }

        self.logger.info(
            "[GridEntry] oid=%s side=%s grid_index=%d price=%.2f qty=%.6f tag=%s",
            oid, spec.side, spec.grid_index, spec.price, spec.qty, tag
        )

        # 60초 후 재지정가 스케줄링
        self._schedule_mode_a_replacement(oid)

    def _schedule_mode_a_replacement(self, oid: str) -> None:
        """
        Mode A 주문에 대해 60초 후 잔량이 있으면 같은 자리 재지정가.
        (grid_cancels 등에 의해 _order_meta 에서 제거된 경우 재배치하지 않는다.)
        """
        try:
            t = threading.Thread(
                target=self._mode_a_replacement_worker,
                args=(oid,),
                daemon=True,
            )
            t.start()
        except Exception as exc:
            self.logger.warning(
                "[ModeA] failed to schedule replacement for oid=%s err=%s", oid, exc
            )

    def _mode_a_replacement_worker(self, oid: str) -> None:
        """
        60초 후:

          - 해당 oid 가 여전히 _order_meta 에 존재하면
            · 남은 수량 계산
            · 남은 수량 > 0 이면 cancel + 같은 price/qty 로 다시 LIMIT 주문
          - 존재하지 않으면 (이미 GridCancel 등으로 정리된 상태) → 아무 것도 안 함

        재배치는 1회만 수행한다.
        """
        try:
            time.sleep(60.0)
        except Exception:
            return

        meta = self._order_meta.get(oid)
        if meta is None:
            # 이미 외부에서 취소/정리된 주문 → 재배치 안 함
            return

        side = str(meta.get("side", "")).upper()
        price = float(meta.get("price") or 0.0)
        original_qty = float(meta.get("qty") or 0.0)
        tag = meta.get("tag")

        if price <= 0.0 or original_qty <= 0.0:
            # 이상한 메타 → 그냥 종료
            self._order_meta.pop(oid, None)
            return

        try:
            status = self.exchange.get_order_status(oid)
            filled = float(status.get("dealVol", 0.0) or 0.0)
        except Exception as exc:
            self.logger.warning(
                "[ModeA] get_order_status failed oid=%s err=%s", oid, exc
            )
            return

        remaining = max(original_qty - filled, 0.0)
        if remaining <= 0.0:
            # 전량 체결됨 → 메타 정리 후 종료
            self._order_meta.pop(oid, None)
            self.logger.info(
                "[ModeA] oid=%s fully filled before 60s, no re-post needed", oid
            )
            return

        # 기존 주문 cancel
        try:
            self.exchange.cancel_order(oid)
        except Exception as exc:
            self.logger.warning(
                "[ModeA] cancel_order failed oid=%s err=%s", oid, exc
            )

        # 예전 메타 제거
        self._order_meta.pop(oid, None)

        # side 문자열("BUY"/"SELL") → logical_side → side_code(1/3)
        if side == "BUY":
            logical_side = "LONG"
        else:
            logical_side = "SHORT"
        side_code = self._side_code_for_entry(logical_side)

        api = ApiOrderSpec(
            side=side,
            type="LIMIT",
            qty=remaining,
            price=price,
            time_in_force="PostOnly",
            reduce_only=bool(meta.get("reduce_only", False)),
            tag=tag,
        )

        try:
            new_oid = self.exchange.place_limit_order(side_code, api.price, api.qty)
        except Exception as exc:
            self.logger.error(
                "[ModeA] re-place order failed side=%s price=%s qty=%.6f err=%s",
                api.side, api.price, api.qty, exc
            )
            return

        if not new_oid:
            self.logger.error(
                "[ModeA] re-place order failed (empty oid) prev_oid=%s", oid
            )
            return

        self._order_meta[new_oid] = {
            "order_id": new_oid,
            "mode": "A",
            "kind": meta.get("kind", "GRID"),
            "grid_index": meta.get("grid_index"),
            "wave_id": meta.get("wave_id"),
            "side": api.side,
            "price": api.price,
            "qty": api.qty,
            "created_ts": time.time(),
            "tag": api.tag,
            "reduce_only": api.reduce_only,
        }

        self.logger.info(
            "[ModeA] Re-posted GRID order oid=%s prev_oid=%s side=%s price=%.2f qty=%.6f tag=%s",
            new_oid, oid, api.side, api.price, api.qty, api.tag,
        )
        # 명세상 60초 재배치는 1회만 보장하면 되므로 여기서 루프 종료.

    # ==================================================================
    # Legacy functions (KEEP) — 기존 코드 호환용
    # ==================================================================

    async def execute_atomic_order(
        self,
        side: int,
        price: float,
        usdt_amount: float,
        timeout: float,
        allow_taker: bool = True,
    ):
        """
        레거시 인터페이스 (기존 메인에서 사용할 수 있음).

        v10.1 설계에서는 Mode A/B 분리 후 거의 사용하지 않는 경로지만,
        과거 코드 호환을 위해 그대로 유지한다.

        - usdt_amount >= 5000 이면 5-way splitting
        - allow_taker=True 이면 타임아웃 이후 남은 수량 Market 으로 강제 청산
        """
        if usdt_amount >= 5000:
            self.logger.info(
                "[Split Order] Large amount (%.2f), splitting into 5...", usdt_amount
            )
            split_amt = usdt_amount / 5
            offsets = [0, -0.5, 0.5, -1.0, 1.0]

            for i in range(5):
                adj_price = price + offsets[i]
                await self._single_atomic(
                    side, adj_price, split_amt, timeout, allow_taker
                )
                await asyncio.sleep(0.2)
        else:
            await self._single_atomic(side, price, usdt_amount, timeout, allow_taker)

    async def _single_atomic(
        self,
        side: int,
        price: float,
        usdt_amount: float,
        timeout: float,
        allow_taker: bool,
    ):
        qty = calc_contract_qty(usdt_amount, price)
        if qty <= 0:
            return

        from utils.calculator import to_int_price
        price = to_int_price(price)

        mode_msg = "All-in Taker" if allow_taker else "Maker Only"
        self.logger.info("[Order] %s: Side=%s P=%s Q=%.6f", mode_msg, side, price, qty)

        oid = await asyncio.to_thread(
            self.exchange.place_limit_order, side, price, qty
        )
        if not oid:
            self.logger.error("지정가 주문 실패")
            if allow_taker:
                await self._force_market_order(side, qty)
            return

        await asyncio.sleep(timeout)

        status = await asyncio.to_thread(self.exchange.get_order_status, oid)
        filled = status.get("dealVol", 0.0)
        remain = qty - filled

        if remain > 0:
            await asyncio.to_thread(self.exchange.cancel_order, oid)
            await asyncio.sleep(0.2)

            if allow_taker:
                self.logger.info("[Timeout] 잔량 %.6f 시장가", remain)
                await self._force_market_order(side, remain)
            else:
                self.logger.info("[Timeout] 잔량 %.6f 취소 (Maker Only)", remain)
        else:
            self.logger.info("[Success] 지정가 전량 체결")

    async def _force_market_order(self, side: int, qty: float):
        res = await asyncio.to_thread(self.exchange.place_market_order, side, qty)
        if res:
            self.logger.info("시장가 성공: Q=%.6f", qty)
        else:
            self.logger.critical("시장가 실패! Q=%.6f", qty)


order_manager = OrderManager()


# === WaveBot v10.1: SYMBOL_INFO_get 최종 런타임 정의 (BTCUSDT 전용) ===

class _SymbolInfoRuntime(dict):
    __getattr__ = dict.get  # info.min_qty, info["min_qty"] 둘 다 허용


def SYMBOL_INFO_get(symbol: str):
    """
    WaveBot v10.1용 심볼 메타 정보 헬퍼 (레거시 호환 스텁).

    현재는 BTCUSDT Perp만 사용하므로 상수값을 반환한다.
    실제 거래정밀도는 utils.calculator.SYMBOL_INFO 를 기준으로 맞춘다.
    """
    return _SymbolInfoRuntime(
        min_qty=0.001,   # 최소 수량
        qty_step=0.001,  # 수량 스텝
        price_tick=0.1,  # 가격 틱 (대략값; 실 정밀도는 calculator 기준)
    )

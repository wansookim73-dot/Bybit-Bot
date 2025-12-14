from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, Tuple, Set, List

from core.exchange_api import exchange
from utils.logger import logger
from utils.calculator import calc_contract_qty
from utils.escape_events import log_full_exit_trigger
from strategy.feed_types import StrategyFeed
from strategy.grid_logic import GridDecision, GridOrderSpec
from strategy.escape_logic import EscapeDecision, EscapeOrderSpec
from strategy.liquidation_slicer import LiquidationSlicer


MAX_SLICE_NOTIONAL = 70_000.0  # FULL_EXIT / ESCAPE 슬라이스 기준 (USDT)
DEDUP_TTL_SEC = 15.0           # 동일 fingerprint 중복 발주 방지(재기동/연속실행 보호)


@dataclass
class ApiOrderSpec:
    """내부 표준 주문 스펙 (메타/로그용)"""
    side: str
    qty: float
    price: Optional[float]
    reduce_only: bool = False
    position_idx: Optional[int] = None
    tag: Optional[str] = None


class OrderManager:
    """
    v10.1 Order Execution Layer (정리본)

    목표:
    - TP(reduceOnly=True)는 반드시 "청산 side_code(2/4)" + positionIdx(1/2)로만 발주
    - DCA/엔트리(reduceOnly=False)는 "진입 side_code(1/3)"로만 발주
    - ESCAPE(HEDGE/FULL_EXIT) 실행 경로 구현
    - Exchange open orders 기반 DEDUP로, --once 반복 실행 시 중복 발주 방지
    """

    def __init__(self, exchange_instance: Any | None = None) -> None:
        self.exchange = exchange_instance or exchange
        self.logger = logger

        # Mode A 주문 메타 (60초 재배치용: 엔트리/그리드 주문만)
        self._order_meta: Dict[str, Dict[str, Any]] = {}

        # 중복 방지(단기 TTL): fp -> last_ts
        self._recent_fp: Dict[Tuple[str, float, int, bool], float] = {}

        # Market 슬라이스/override용 Slicer
        self.liquidation_slicer = LiquidationSlicer(max_slice_notional=MAX_SLICE_NOTIONAL)

    # ==========================================================
    # Public: Mode A (GridDecision) — Maker 중심
    # ==========================================================

    def apply_decision(self, decision: GridDecision, feed: StrategyFeed, now_ts: float) -> None:
        """
        Mode A — Grid Maker 중심(기본 LIMIT).

        처리 순서:
          1) cancels
          2) entries
          3) replaces

        주의:
          - spec.reduce_only=True 인 주문은 TP 전용 경로로 분기(청산 side_code 사용)
          - DEDUP: 거래소 open orders에 동일 fp가 있으면 발주 스킵
        """
        mode = getattr(decision, "mode", "UNKNOWN")

        self.logger.info(
            "[OrderManager] apply_decision(mode=%s, cancels=%d, entries=%d, replaces=%d)",
            mode,
            len(getattr(decision, "grid_cancels", []) or []),
            len(getattr(decision, "grid_entries", []) or []),
            len(getattr(decision, "grid_replaces", []) or []),
        )

        # 0) open orders fingerprint 로드 (entries/replaces 있을 때만)
        open_fps: Set[Tuple[str, float, int, bool]] = set()
        if (getattr(decision, "grid_entries", None) or getattr(decision, "grid_replaces", None)):
            open_fps = self._load_open_order_fps()

        # 1) 취소 먼저 처리
        for oid in getattr(decision, "grid_cancels", []) or []:
            try:
                self.exchange.cancel_order(oid)
                self.logger.info("[GridCancel] Cancelled order_id=%s", oid)
            except Exception as e:
                self.logger.warning("[GridCancel] Cancel failed id=%s err=%s", oid, e)
            finally:
                # 60초 재배치 루프 중단되도록 메타 제거
                self._order_meta.pop(oid, None)

        # 2) 신규 Grid/TP 주문 생성
        for spec in getattr(decision, "grid_entries", []) or []:
            self._create_mode_a_order(spec, now_ts, open_fps=open_fps)

        # 3) 재배치 생성 (실질적으로 새 주문 생성과 동일 취급)
        for spec in getattr(decision, "grid_replaces", []) or []:
            self._create_mode_a_order(spec, now_ts, open_fps=open_fps)

    # ==========================================================
    # Public: Mode B (EscapeDecision) — ESCAPE/HEDGE/FULL_EXIT
    # ==========================================================

    def apply_escape_decision(
        self,
        esc_decision: EscapeDecision,
        feed: StrategyFeed,
        now_ts: float,
    ) -> None:
        """
        Mode B — ESCAPE / HEDGE / FULL_EXIT

        - FULL_EXIT: 슬라이스 기준 MARKET 청산
        - HEDGE_ENTRY: LIMIT(최대 1초 대기) → 남은 수량 MARKET 슬라이스 진입
        - HEDGE_EXIT : MARKET 슬라이스 청산
        """
        if not esc_decision:
            return

        full_exit = bool(getattr(esc_decision, "full_exit", False))
        orders = getattr(esc_decision, "orders", []) or []

        if (not full_exit) and (not orders):
            return

        self.logger.info(
            "[OrderManager] apply_escape_decision(full_exit=%s, orders=%d)",
            full_exit,
            len(orders),
        )

        price = float(getattr(feed, "price", 0.0) or 0.0)
        if price <= 0:
            self.logger.warning("[OrderManager] apply_escape_decision skipped: invalid price=%.4f", price)
            return

        # 1) FULL_EXIT
        if full_exit:
            self._execute_full_exit(feed)
            return

        # 2) ESCAPE/HEDGE 주문 처리
        for spec in orders:
            try:
                if not spec:
                    continue
                qty = float(getattr(spec, "qty", 0.0) or 0.0)
                if qty <= 0:
                    continue

                typ = str(getattr(spec, "type", "") or "").upper().strip()
                side = str(getattr(spec, "side", "") or "").upper().strip()

                if typ == "HEDGE_ENTRY":
                    self.logger.info("[Escape] HEDGE_ENTRY side=%s qty=%.6f", side, qty)
                    self._execute_hedge_entry(
                        logical_side=side,
                        total_qty=qty,
                        feed=feed,
                        tag="ESCAPE_HEDGE_ENTRY",
                    )

                elif typ == "HEDGE_EXIT":
                    self.logger.info("[Escape] HEDGE_EXIT side=%s qty=%.6f", side, qty)
                    self._execute_sliced_market_exit(
                        logical_side=side,
                        total_qty=qty,
                        price=price,
                        tag="ESCAPE_HEDGE_EXIT",
                        feed=feed,
                    )

                else:
                    self.logger.warning("[OrderManager] Unknown EscapeOrderSpec.type=%s", typ)
            except Exception as exc:
                self.logger.error("[OrderManager] apply_escape_decision failed: %s", exc)

    # ==========================================================
    # 내부: open orders -> fingerprint 로드 (DEDUP)
    # ==========================================================

    def _load_open_order_fps(self) -> Set[Tuple[str, float, int, bool]]:
        """
        fingerprint = (side_str, price, positionIdx, reduceOnly)
        - side_str: "buy" / "sell"
        - price: tick 반영된 가격(부동소수 오차 방지 위해 2자리 반올림)
        - positionIdx: 1/2
        - reduceOnly: bool
        """
        fps: Set[Tuple[str, float, int, bool]] = set()
        try:
            orders = self.exchange.get_open_orders()
        except Exception as exc:
            self.logger.warning("[DEDUP] get_open_orders failed: %s", exc)
            return fps

        for o in orders or []:
            try:
                if not isinstance(o, dict):
                    continue
                info = o.get("info") or {}
                side_raw = (o.get("side") or info.get("side") or "").lower()
                if side_raw not in ("buy", "sell"):
                    # CCXT는 보통 buy/sell로 오지만 방어
                    side_raw = "buy" if "buy" in side_raw else ("sell" if "sell" in side_raw else "")
                if not side_raw:
                    continue

                px = o.get("price")
                if px is None:
                    px = info.get("price")
                price = float(px or 0.0)
                if price <= 0:
                    continue

                pidx = info.get("positionIdx") or info.get("position_idx") or 0
                try:
                    position_idx = int(pidx)
                except Exception:
                    position_idx = 0
                if position_idx not in (1, 2):
                    continue

                reduce_only = bool(info.get("reduceOnly") or info.get("isReduceOnly") or False)

                fps.add((side_raw, round(price, 2), position_idx, reduce_only))
            except Exception:
                continue

        return fps

    def _recent_dedup_hit(self, fp: Tuple[str, float, int, bool], now_ts: float) -> bool:
        """
        단기 TTL DEDUP:
        - 동일 fp를 짧은 시간 내(DEDUP_TTL_SEC)에 또 발주하는 것을 방지
        """
        last = self._recent_fp.get(fp)
        if last is not None and (now_ts - last) < DEDUP_TTL_SEC:
            return True
        self._recent_fp[fp] = now_ts
        return False

    # ==========================================================
    # 내부: side_code 매핑
    # ==========================================================

    def _side_code_for_exit(self, logical_side: str) -> int:
        """
        청산(Exit)용 side_code 매핑.

        허용 입력:
          - "LONG"  또는 "SELL"  -> 4 (롱 청산 = sell reduce-only)
          - "SHORT" 또는 "BUY"   -> 2 (숏 청산 = buy reduce-only)

        그 외는 위험하니 '찍지 않고' 0을 반환(주문 스킵 유도).
        """
        s = (logical_side or "").upper().strip()

        if s in ("LONG", "SELL"):
            return 4
        if s in ("SHORT", "BUY"):
            return 2

        self.logger.critical(
            "[OrderManager] _side_code_for_exit invalid logical_side=%r "
            "(expected LONG/SHORT or BUY/SELL) -> SKIP",
            logical_side,
        )
        return 0


    def _side_code_for_entry(self, logical_side: str) -> int:
        """
        진입(Entry)용 side_code 매핑.

        허용 입력:
          - "LONG"  또는 "BUY"   -> 1 (롱 진입 = buy)
          - "SHORT" 또는 "SELL"  -> 3 (숏 진입 = sell)

        그 외는 위험하니 '찍지 않고' 0을 반환(주문 스킵 유도).
        """
        s = (logical_side or "").upper().strip()

        if s in ("LONG", "BUY"):
            return 1
        if s in ("SHORT", "SELL"):
            return 3

        self.logger.critical(
            "[OrderManager] _side_code_for_entry invalid logical_side=%r "
            "(expected LONG/SHORT or BUY/SELL) -> SKIP",
            logical_side,
        )
        return 0

    def _map_side_int(self, side_code: int) -> Tuple[str, int, bool]:
        """
        ExchangeAPI 내부 매핑(_side_int_to_ccxt)이 있으면 그대로 사용.
        없으면 로컬 fallback.
        """
        if hasattr(self.exchange, "_side_int_to_ccxt"):
            side_str, position_idx, reduce_only = self.exchange._side_int_to_ccxt(side_code)  # type: ignore[attr-defined]
            return str(side_str), int(position_idx), bool(reduce_only)

        # fallback
        side_str = "buy" if side_code in (1, 2) else "sell"
        position_idx = 1 if side_code in (1, 4) else 2
        reduce_only = side_code in (2, 4)
        return side_str, position_idx, reduce_only

    def _prepare_price_qty(self, price: float, qty: float) -> Tuple[float, float]:
        """
        ExchangeAPI의 tick/minQty/stepSize 처리 결과와 최대한 일치시키기 위해
        내부 helper가 있으면 사용. 없으면 raw 반환.
        """
        if hasattr(self.exchange, "_prepare_price_and_qty_from_qty"):
            try:
                p, q = self.exchange._prepare_price_and_qty_from_qty(price, qty)  # type: ignore[attr-defined]
                return float(p), float(q)
            except Exception:
                pass
        return float(price), float(qty)

    def _fp_for_new_order(
        self,
        side_code: int,
        price: float,
        qty: float,
        *,
        position_idx_override: Optional[int] = None,
        reduce_only_override: Optional[bool] = None,
    ) -> Tuple[str, float, int, bool]:
        side_str, position_idx, reduce_only = self._map_side_int(side_code)
        if position_idx_override is not None:
            position_idx = int(position_idx_override)
        if reduce_only_override is not None:
            reduce_only = bool(reduce_only_override)

        floored_price, _final_qty = self._prepare_price_qty(price, qty)
        return (str(side_str).lower(), round(float(floored_price), 2), int(position_idx), bool(reduce_only))

    # ==========================================================
    # 내부: Mode A 주문 생성 (TP / Entry 분기)
    # ==========================================================

    def _create_mode_a_order(self, spec: GridOrderSpec, now_ts: float, *, open_fps: Set[Tuple[str, float, int, bool]]) -> None:
        """
        GridOrderSpec 기반 LIMIT 주문 생성.

        - reduce_only=False: 엔트리(OPEN) side_code=1/3
        - reduce_only=True : TP(청산) side_code=2/4 + position_idx=1/2 반드시 필요
        """
        side_str = str(getattr(spec, "side", "") or "").upper().strip()
        if side_str not in ("BUY", "SELL"):
            self.logger.error("[OrderManager] invalid spec.side=%r", getattr(spec, "side", None))
            return

        price = float(getattr(spec, "price", 0.0) or 0.0)
        qty = float(getattr(spec, "qty", 0.0) or 0.0)
        if price <= 0.0 or qty <= 0.0:
            self.logger.warning("[OrderManager] skip invalid spec price/qty price=%s qty=%s", price, qty)
            return

        wave_id = int(getattr(spec, "wave_id", 0) or 0)
        grid_index = int(getattr(spec, "grid_index", -1) or -1)

        reduce_only = bool(getattr(spec, "reduce_only", False))
        position_idx = getattr(spec, "position_idx", None)

        tag = f"W{wave_id}_GRID_A_{grid_index}_{side_str}"

        # --------------------------
        # TP 경로 (reduce_only=True)
        # --------------------------
        if reduce_only:
            # position_idx 검증
            if position_idx not in (1, 2):
                self.logger.error(
                    "[OrderManager] TP(reduce_only=True) but invalid position_idx=%r (expected 1 or 2)",
                    position_idx,
                )
                return

            if int(position_idx) == 1:
                logical = "CLOSE_LONG"
                side_code = 4  # sell reduce-only
            else:
                logical = "CLOSE_SHORT"
                side_code = 2  # buy reduce-only

            # DEDUP (open orders)
            fp = self._fp_for_new_order(side_code, price, qty, position_idx_override=int(position_idx), reduce_only_override=True)
            if fp in open_fps:
                self.logger.info("[DEDUP] skip TP (already open) fp=%s tag=%s", fp, tag)
                return
            if self._recent_dedup_hit(fp, now_ts):
                self.logger.info("[DEDUP] skip TP (ttl-hit) fp=%s tag=%s", fp, tag)
                return

            self.logger.info(
                "[TP-LIMIT] logical=%s side=%s side_code=%s price=%.2f qty=%.6f position_idx=%s reduce_only=True tag=%s",
                logical, side_str, side_code, price, qty, position_idx, tag,
            )

            try:
                oid = self._place_tp_limit_order(side_code=side_code, price=price, qty=qty, position_idx=int(position_idx))
            except Exception as exc:
                self.logger.error(
                    "[OrderManager] TP LIMIT failed logical=%s side=%s side_code=%s price=%s qty=%s position_idx=%s err=%s",
                    logical, side_str, side_code, price, qty, position_idx, exc,
                )
                return

            if not oid:
                self.logger.error(
                    "[OrderManager] TP LIMIT returned empty oid logical=%s side=%s side_code=%s price=%s qty=%s position_idx=%s",
                    logical, side_str, side_code, price, qty, position_idx,
                )
                return

            # TP는 60초 재배치(자동 cancel/repost) 하지 않음: 보호성 주문이므로 보수적으로 유지
            self.logger.info(
                "[GridEntry] oid=%s side=%s logical=%s grid_index=%d price=%.2f qty=%.6f tag=%s reduce_only=True position_idx=%s",
                oid, side_str, logical, grid_index, price, qty, tag, position_idx,
            )
            return

        # --------------------------
        # Entry/DCA 경로 (reduce_only=False)
        # --------------------------
        if side_str == "BUY":
            logical_side = "LONG"
            logical = "OPEN_LONG"
        else:
            logical_side = "SHORT"
            logical = "OPEN_SHORT"

        side_code = self._side_code_for_entry(logical_side)
        if side_code not in (1, 3):
            self.logger.critical(
                "[HedgeEntry] %s INVALID logical_side=%r -> side_code=%r "
                "(expected 1 or 3) -> SKIP",
                tag,
                logical_side,
                side_code,
            )
            return

        fp = self._fp_for_new_order(side_code, price, qty)
        if fp in open_fps:
            self.logger.info("[DEDUP] skip ENTRY (already open) fp=%s tag=%s", fp, tag)
            return
        if self._recent_dedup_hit(fp, now_ts):
            self.logger.info("[DEDUP] skip ENTRY (ttl-hit) fp=%s tag=%s", fp, tag)
            return

        try:
            oid = self.exchange.place_limit_order(side_code, price, qty)
        except Exception as exc:
            self.logger.error(
                "[OrderManager] Mode A order failed side=%s logical=%s side_code=%s price=%s qty=%s err=%s",
                side_str, logical, side_code, price, qty, exc,
            )
            return

        if not oid:
            self.logger.error(
                "[OrderManager] Mode A order failed (empty oid) side=%s logical=%s side_code=%s price=%s qty=%s",
                side_str, logical, side_code, price, qty,
            )
            return

        # 메타 저장(엔트리만) + 60초 재배치
        self._order_meta[oid] = {
            "order_id": oid,
            "mode": "A",
            "kind": "GRID",
            "grid_index": grid_index,
            "wave_id": wave_id,
            "side": side_str,
            "price": price,
            "qty": qty,
            "created_ts": now_ts,
            "tag": tag,
            "reduce_only": False,
        }

        self.logger.info(
            "[GridEntry] oid=%s side=%s logical=%s grid_index=%d price=%.2f qty=%.6f tag=%s",
            oid, side_str, logical, grid_index, price, qty, tag,
        )

        self._schedule_mode_a_replacement(oid)

    def _place_tp_limit_order(self, *, side_code: int, price: float, qty: float, position_idx: int) -> str:
        """
        TP 전용 LIMIT 발주:
        - 가능하면 ExchangeAPI의 TP 전용 함수를 우선 사용
        - 없으면 close side_code(2/4) 기반 place_limit_order로 fallback
        """
        # [SAFETY] TP는 반드시 close side_code(2/4) + position_idx(1/2) 여야 한다.
        if side_code not in (2, 4) or position_idx not in (1, 2):
            self.logger.critical(
                "[TP] invalid side_code=%r position_idx=%r (expected close 2/4 + idx 1/2) -> REFUSE",
                side_code,
                position_idx,
            )
            return ""

        # 여러 구현명을 방어적으로 지원
        for name in ("place_tp_limit_order", "place_tp_limit", "place_limit_order_tp"):
            if hasattr(self.exchange, name):
                fn = getattr(self.exchange, name)
                try:
                    return str(fn(side_code, price, qty, position_idx=position_idx, reduce_only=True))
                except TypeError:
                    try:
                        return str(fn(side_code, price, qty, position_idx))
                    except TypeError:
                        return str(fn(side_code, price, qty))

        # fallback: side_code=2/4면 ExchangeAPI가 reduceOnly/positionIdx를 자동 매핑하는 구조여야 함
        return str(self.exchange.place_limit_order(side_code, price, qty))

    # ==========================================================
    # 내부: Mode A 60초 재배치(엔트리만)
    # ==========================================================

    def _schedule_mode_a_replacement(self, oid: str) -> None:
        try:
            t = threading.Thread(target=self._mode_a_replacement_worker, args=(oid,), daemon=True)
            t.start()
        except Exception as exc:
            self.logger.warning("[ModeA] failed to schedule replacement for oid=%s err=%s", oid, exc)

    def _mode_a_replacement_worker(self, oid: str) -> None:
        try:
            time.sleep(60.0)
        except Exception:
            return

        meta = self._order_meta.get(oid)
        if meta is None:
            return

        side = str(meta.get("side", "")).upper()
        price = float(meta.get("price") or 0.0)
        original_qty = float(meta.get("qty") or 0.0)
        tag = meta.get("tag")

        if price <= 0.0 or original_qty <= 0.0:
            self._order_meta.pop(oid, None)
            return

        try:
            status = self.exchange.get_order_status(oid)
            filled = float(status.get("dealVol", 0.0) or 0.0)
        except Exception as exc:
            self.logger.warning("[ModeA] get_order_status failed oid=%s err=%s", oid, exc)
            return

        remaining = max(original_qty - filled, 0.0)
        if remaining <= 0.0:
            self._order_meta.pop(oid, None)
            self.logger.info("[ModeA] oid=%s fully filled before 60s, no re-post needed", oid)
            return

        # 기존 주문 cancel
        try:
            self.exchange.cancel_order(oid)
        except Exception as exc:
            self.logger.warning("[ModeA] cancel_order failed oid=%s err=%s", oid, exc)

        self._order_meta.pop(oid, None)

        logical_side = "LONG" if side == "BUY" else "SHORT"
        side_code = self._side_code_for_entry(logical_side)
        if side_code not in (1, 3):
            self.logger.critical(
                "[ModeA] INVALID logical_side=%r -> side_code=%r (expected 1 or 3) -> SKIP",
                logical_side,
                side_code,
            )
            return

        try:
            new_oid = self.exchange.place_limit_order(side_code, price, remaining)
        except Exception as exc:
            self.logger.error(
                "[ModeA] re-place order failed side=%s price=%s qty=%.6f err=%s",
                side, price, remaining, exc,
            )
            return

        if not new_oid:
            self.logger.error("[ModeA] re-place order failed (empty oid) prev_oid=%s", oid)
            return

        self._order_meta[new_oid] = {
            "order_id": new_oid,
            "mode": "A",
            "kind": meta.get("kind", "GRID"),
            "grid_index": meta.get("grid_index"),
            "wave_id": meta.get("wave_id"),
            "side": side,
            "price": price,
            "qty": remaining,
            "created_ts": time.time(),
            "tag": tag,
            "reduce_only": False,
        }

        self.logger.info(
            "[ModeA] Re-posted GRID order oid=%s prev_oid=%s side=%s price=%.2f qty=%.6f tag=%s",
            new_oid, oid, side, price, remaining, tag,
        )

    # ==========================================================
    # 내부: FULL_EXIT + Market slicer (Mode B)
    # ==========================================================

    def _execute_full_exit(self, feed: StrategyFeed) -> None:
        price = float(getattr(feed, "price", 0.0) or 0.0)
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

        self.logger.info("[FullExit] positions LONG=%.6f SHORT=%.6f price=%.2f", long_qty, short_qty, price)

        positions_before = {
            "LONG": {"qty": long_qty, "avg_price": float(long_pos.get("avg_price", 0.0) or 0.0)},
            "SHORT": {"qty": short_qty, "avg_price": float(short_pos.get("avg_price", 0.0) or 0.0)},
            "HEDGE": {"qty": float(getattr(getattr(feed, "state", None), "hedge_size", 0.0) or 0.0),
                      "side": getattr(getattr(feed, "state", None), "hedge_side", None)},
        }

        try:
            log_full_exit_trigger(
                self.logger,
                pnl_total_pct=float(getattr(feed, "pnl_total_pct", 0.0) or 0.0),
                wave_id=getattr(getattr(feed, "state", None), "wave_id", 0),
                positions_before=positions_before,
            )
        except Exception as exc:
            self.logger.warning("[FullExit] log_full_exit_trigger failed: %s", exc)

        if long_qty > 0.0:
            self._execute_sliced_market_exit("LONG", long_qty, price, tag="FULL_EXIT", feed=feed)
        if short_qty > 0.0:
            self._execute_sliced_market_exit("SHORT", short_qty, price, tag="FULL_EXIT", feed=feed)

    def _execute_sliced_market_exit(
        self,
        logical_side: str,
        total_qty: float,
        price: float,
        tag: str,
        feed: Optional[StrategyFeed] = None,
    ) -> None:
        if total_qty <= 0.0 or price <= 0.0:
            return

        total_notional = abs(total_qty) * price
        if total_notional <= 0.0:
            return

        vol_1s = float(getattr(feed, "vol_1s", 0.0) or 0.0) if feed is not None else 0.0
        vol_1m = float(getattr(feed, "vol_1m", 0.0) or 0.0) if feed is not None else 0.0

        self.logger.info(
            "[SliceExit] %s side=%s total_qty=%.6f total_notional=%.2f vol_1s=%.4f vol_1m=%.4f",
            tag, logical_side, total_qty, total_notional, vol_1s, vol_1m,
        )

        def _market_cb(side_str: str, qty: float) -> None:
            if qty <= 0.0:
                return
            side_code = self._side_code_for_exit(side_str)
            if side_code not in (2, 4):
                self.logger.critical(
                    "[SliceExit] %s INVALID side_str=%r -> side_code=%r "
                    "(expected 2 or 4) -> SKIP",
                    tag,
                    side_str,
                    side_code,
                )
                return

            try:
                oid = self.exchange.place_market_order(side_code, qty, price_for_calc=price)
                self.logger.info("[SliceExit] %s MARKET side=%s side_code=%s qty=%.6f oid=%s", tag, side_str, side_code, qty, oid)
            except Exception as exc:
                self.logger.error("[SliceExit] %s MARKET failed side=%s side_code=%s qty=%.6f err=%s", tag, side_str, side_code, qty, exc)

        plan = self.liquidation_slicer.execute_sliced_liquidation(
            side=logical_side,
            total_notional=total_notional,
            price=price,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            place_market_order=_market_cb,
        )

        self.logger.info("[SliceExit] %s completed mode=%s slices=%d reason=%s", tag, plan.mode, len(plan.slices), plan.reason)

    def _execute_hedge_entry(self, logical_side: str, total_qty: float, feed: StrategyFeed, tag: str) -> None:
        price = float(getattr(feed, "price", 0.0) or 0.0)
        if total_qty <= 0.0 or price <= 0.0:
            return

        side_code = self._side_code_for_entry(logical_side)
        if side_code not in (1, 3):
            self.logger.critical(
                "[HedgeEntry] %s INVALID logical_side=%r -> side_code=%r "
                "(expected 1 or 3) -> SKIP",
                tag,
                logical_side,
                side_code,
            )
            return

        self.logger.info("[HedgeEntry] %s LIMIT side=%s side_code=%s qty=%.6f price=%.2f", tag, logical_side, side_code, total_qty, price)

        try:
            oid = self.exchange.place_limit_order(side_code, price, total_qty)
        except Exception as exc:
            self.logger.error("[HedgeEntry] %s LIMIT failed side_code=%s qty=%.6f err=%s", tag, side_code, total_qty, exc)
            return

        if not oid:
            self.logger.error("[HedgeEntry] %s LIMIT returned empty oid (side_code=%s qty=%.6f)", tag, side_code, total_qty)
            return

        # 최대 1초 대기 후 잔량 확인
        try:
            time.sleep(1.0)
            status = self.exchange.get_order_status(oid)
            filled = float(status.get("dealVol", 0.0) or 0.0)
        except Exception as exc:
            self.logger.error("[HedgeEntry] %s get_order_status failed oid=%s err=%s", tag, oid, exc)
            filled = 0.0

        remain = max(total_qty - filled, 0.0)

        # LIMIT 주문은 정리
        try:
            self.exchange.cancel_order(oid)
        except Exception as exc:
            self.logger.warning("[HedgeEntry] %s cancel_order failed oid=%s err=%s", tag, oid, exc)

        self.logger.info("[HedgeEntry] %s LIMIT filled=%.6f remain=%.6f", tag, filled, remain)

        # 남은 잔량은 MARKET 슬라이스 진입
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
        if total_qty <= 0.0 or price <= 0.0:
            return

        total_notional = abs(total_qty) * price
        if total_notional <= 0.0:
            return

        vol_1s = float(getattr(feed, "vol_1s", 0.0) or 0.0) if feed is not None else 0.0
        vol_1m = float(getattr(feed, "vol_1m", 0.0) or 0.0) if feed is not None else 0.0

        self.logger.info(
            "[SliceEntry] %s side=%s total_qty=%.6f total_notional=%.2f vol_1s=%.4f vol_1m=%.4f",
            tag, logical_side, total_qty, total_notional, vol_1s, vol_1m,
        )

        def _market_cb(side_str: str, qty: float) -> None:
            if qty <= 0.0:
                return
            side_code = self._side_code_for_entry(side_str)
            if side_code not in (1, 3):
                self.logger.critical(
                    "[SliceEntry] %s INVALID side_str=%r -> side_code=%r "
                    "(expected 1 or 3) -> SKIP",
                    tag,
                    side_str,
                    side_code,
                )
                return
            try:
                oid = self.exchange.place_market_order(side_code, qty, price_for_calc=price)
                self.logger.info("[SliceEntry] %s MARKET side=%s side_code=%s qty=%.6f oid=%s", tag, side_str, side_code, qty, oid)
            except Exception as exc:
                self.logger.error("[SliceEntry] %s MARKET failed side=%s side_code=%s qty=%.6f err=%s", tag, side_str, side_code, qty, exc)

        plan = self.liquidation_slicer.execute_sliced_liquidation(
            side=logical_side,
            total_notional=total_notional,
            price=price,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            place_market_order=_market_cb,
        )

        self.logger.info("[SliceEntry] %s completed mode=%s slices=%d reason=%s", tag, plan.mode, len(plan.slices), plan.reason)

    # ==========================================================
    # Legacy (optional): atomic order (호환용)
    # ==========================================================

    async def execute_atomic_order(
        self,
        side: int,
        price: float,
        usdt_amount: float,
        timeout: float,
        allow_taker: bool = True,
    ) -> Dict[str, Any]:
        """
        (호환용) usdt_amount 기반으로 qty 산출 후 LIMIT → (필요시) MARKET.
        반환: {"limit_oid": str|None, "market_oid": str|None, "filled": float}
        """
        if price <= 0.0 or usdt_amount <= 0.0:
            return {"limit_oid": None, "market_oid": None, "filled": 0.0}
        if price <= 0.0 or usdt_amount <= 0.0:
            return {"limit_oid": None, "market_oid": None, "filled": 0.0}

        if side not in (1, 2, 3, 4):
            self.logger.critical(
                "[AtomicOrder] invalid side=%r (expected 1/2/3/4) -> SKIP",
                side,
            )
            return {"limit_oid": None, "market_oid": None, "filled": 0.0}

        qty = calc_contract_qty(...)

        qty = calc_contract_qty(usdt_amount=usdt_amount, price=price, symbol=getattr(self.exchange, "symbol", "BTCUSDT"), dry_run=getattr(self.exchange, "dry_run", False))
        if qty <= 0.0:
            return {"limit_oid": None, "market_oid": None, "filled": 0.0}

        limit_oid = self.exchange.place_limit_order(side, price, qty)
        filled = 0.0

        t0 = time.time()
        while time.time() - t0 < float(timeout or 0.0):
            await asyncio.sleep(0.2)
            st = self.exchange.get_order_status(limit_oid)
            filled = float(st.get("dealVol", 0.0) or 0.0)
            if filled >= qty:
                return {"limit_oid": limit_oid, "market_oid": None, "filled": filled}

        try:
            self.exchange.cancel_order(limit_oid)
        except Exception:
            pass

        remain = max(qty - filled, 0.0)
        if (not allow_taker) or remain <= 0.0:
            return {"limit_oid": limit_oid, "market_oid": None, "filled": filled}

        market_oid = self.exchange.place_market_order(side, remain, price_for_calc=price)
        return {"limit_oid": limit_oid, "market_oid": market_oid, "filled": qty}


# Global instance (기존 import 패턴 호환)
order_manager = OrderManager()

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Set, List

from core.exchange_api import exchange
from core.order_intent_registry import order_intent_registry
from utils.logger import logger
from utils.escape_events import log_full_exit_trigger
from strategy.feed_types import StrategyFeed
from strategy.grid_logic import GridDecision, GridOrderSpec
from strategy.escape_logic import EscapeDecision
from strategy.liquidation_slicer import LiquidationSlicer


MAX_SLICE_NOTIONAL = 70_000.0
DEDUP_TTL_SEC = 15.0

SEED_STEP_MAX: int = 13
ENTRY_STEP_COST: int = 2
TP_STEP_COST: int = 1


@dataclass
class ApiOrderSpec:
    side: str
    qty: float
    price: Optional[float]
    reduce_only: bool = False
    position_idx: Optional[int] = None
    tag: Optional[str] = None


class OrderManager:
    """
    v10.1 Order Execution Layer
    """

    def __init__(self, exchange_instance: Any | None = None) -> None:
        self.exchange = exchange_instance or exchange
        self.logger = logger

        self._order_meta: Dict[str, Dict[str, Any]] = {}
        self._recent_fp: Dict[Tuple[str, float, int, bool], float] = {}

        self.liquidation_slicer = LiquidationSlicer(max_slice_notional=MAX_SLICE_NOTIONAL)

        self._escape_lockdown: bool = False
        self._escape_lockdown_reason: str = ""
        self._escape_lockdown_ts: float = 0.0

        self._trend_strength: str = "STRONG_TREND"
        self._trend_bias: str = "NONE"
        self._allow_entry_long: bool = False
        self._allow_entry_short: bool = False
        self._trend_cache_ts: float = 0.0

        # ✅ intent registry (authoritative 등록)
        self._intent = order_intent_registry

    # -------------------- internal utils --------------------

    @staticmethod
    def _u(x: Any) -> str:
        return str(x or "").upper().strip()

    @staticmethod
    def _norm_upper(v: Any, default: str) -> str:
        try:
            s = str(v).strip().upper()
            return s if s else default
        except Exception:
            return default

    @staticmethod
    def _safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
        try:
            if v is None:
                return default
            return int(v)
        except Exception:
            return default

    @staticmethod
    def _safe_bool(v: Any, default: bool = False) -> bool:
        """
        Bybit/CCXT 응답에서 reduceOnly 등이 bool/str/int 혼재할 수 있어 fail-open/오탐을 줄이기 위한 파서.
        """
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v
        try:
            if isinstance(v, (int, float)):
                return bool(int(v))
        except Exception:
            pass
        try:
            s = str(v).strip().lower()
            if s in ("1", "true", "t", "yes", "y", "on"):
                return True
            if s in ("0", "false", "f", "no", "n", "off", ""):
                return False
        except Exception:
            pass
        return bool(v)

    @staticmethod
    def _infer_position_idx(side_raw: str, reduce_only: bool) -> Optional[int]:
        """
        positionIdx 누락 시, Bybit hedge 규칙(본 프로젝트의 side_code 매핑과 동일)을 기반으로 추정.
          - ENTRY:  buy -> 1, sell -> 2
          - EXIT :  buy(reduce) -> 2, sell(reduce) -> 1
        """
        s = str(side_raw or "").strip().lower()
        if s not in ("buy", "sell"):
            return None
        if not reduce_only:
            return 1 if s == "buy" else 2
        return 2 if s == "buy" else 1

    @staticmethod
    def _now_ms(ts: Optional[float] = None) -> int:
        t = float(ts if ts is not None else time.time())
        return int(t * 1000)

    @classmethod
    def _make_order_link_id(cls, base_tag: str, now_ts: float) -> str:
        return f"{base_tag}_{cls._now_ms(now_ts)}"

    def _intent_register(self, *, order_id: Optional[str], order_link_id: Optional[str], source: str) -> None:
        try:
            self._intent.register(order_id=order_id, order_link_id=order_link_id, source=source, authoritative=True)
        except Exception:
            pass

    # -------------------- ESCAPE 判정 --------------------

    def _is_escape_active(self, decision_mode: str, feed: Optional[StrategyFeed]) -> Tuple[bool, str]:
        reasons: List[str] = []

        if self._u(decision_mode) == "ESCAPE":
            reasons.append("decision.mode")

        if feed is None:
            return (bool(reasons), ",".join(reasons))

        state = getattr(feed, "state", None)
        wave_state = getattr(feed, "wave_state", None)

        if state is not None:
            try:
                if self._u(getattr(state, "mode", "")) == "ESCAPE":
                    reasons.append("state.mode")
            except Exception:
                pass

            for k in (
                "escape_active",
                "escape_long_active",
                "escape_short_active",
                "escape_active_long",
                "escape_active_short",
                "escape_long_pending",
                "escape_short_pending",
            ):
                try:
                    if bool(getattr(state, k, False)):
                        reasons.append(f"state.{k}")
                except Exception:
                    pass

        if wave_state is not None:
            try:
                le = getattr(wave_state, "long_escape", None)
                se = getattr(wave_state, "short_escape", None)
                if bool(getattr(le, "active", False)):
                    reasons.append("wave_state.long_escape.active")
                if bool(getattr(se, "active", False)):
                    reasons.append("wave_state.short_escape.active")
            except Exception:
                pass

        return (bool(reasons), ",".join(reasons))

    def _set_escape_lockdown(self, on: bool, *, reason: str, now_ts: float) -> None:
        on = bool(on)
        if on:
            if not self._escape_lockdown:
                self.logger.warning("[ESCAPE-LOCKDOWN] ON (%s)", reason or "unknown")
            self._escape_lockdown = True
            self._escape_lockdown_reason = str(reason or "unknown")
            self._escape_lockdown_ts = float(now_ts or time.time())
            try:
                self._order_meta.clear()
            except Exception:
                pass
        else:
            if self._escape_lockdown:
                self.logger.warning("[ESCAPE-LOCKDOWN] OFF (%s)", reason or "unknown")
            self._escape_lockdown = False
            self._escape_lockdown_reason = ""
            self._escape_lockdown_ts = float(now_ts or time.time())

    # -------------------- Trend Gate cache --------------------

    def _trend_gate_allow_flags_from_feed(self, feed: Optional[StrategyFeed]) -> Tuple[bool, bool, str, str]:
        strength = "STRONG_TREND"
        bias = "NONE"
        if feed is not None:
            strength = self._norm_upper(getattr(feed, "trend_strength", None), "STRONG_TREND")
            bias = self._norm_upper(getattr(feed, "trend_bias", None), "NONE")

        allow_long = True
        allow_short = True

        if strength == "STRONG_TREND":
            allow_long = False
            allow_short = False
        elif strength == "WEAK_TREND":
            if bias == "UP":
                allow_long = False
                allow_short = True
            elif bias == "DOWN":
                allow_long = True
                allow_short = False
            else:
                allow_long = True
                allow_short = True
        else:
            allow_long = True
            allow_short = True

        return allow_long, allow_short, strength, bias

    def _update_trend_cache(self, feed: Optional[StrategyFeed], now_ts: float) -> None:
        allow_long, allow_short, strength, bias = self._trend_gate_allow_flags_from_feed(feed)
        self._allow_entry_long = bool(allow_long)
        self._allow_entry_short = bool(allow_short)
        self._trend_strength = str(strength)
        self._trend_bias = str(bias)
        self._trend_cache_ts = float(now_ts or time.time())

    def _entry_allowed_cached(self, logical_side: str) -> bool:
        s = self._u(logical_side)
        if s == "LONG":
            return bool(self._allow_entry_long)
        if s == "SHORT":
            return bool(self._allow_entry_short)
        return False

    # -------------------- helpers --------------------

    @staticmethod
    def _is_entry_family_open_order(o: Any) -> bool:
        try:
            if bool(getattr(o, "reduce_only", False)):
                return False
        except Exception:
            return False

        try:
            tag = str(getattr(o, "tag", "") or "").strip()
        except Exception:
            tag = ""
        if not tag:
            return False

        tu = tag.upper()
        if tu.startswith("STARTUP") or tu.startswith("DCA") or tu.startswith("REENTRY"):
            return True
        if "_GRID_A_" in tu:
            return True
        return False

    def _get_k_dir(self, feed: Optional[StrategyFeed], logical_side: str) -> Optional[int]:
        if feed is None:
            return None

        state = getattr(feed, "state", None)
        is_dict = isinstance(state, dict)

        def _get(name: str) -> Any:
            if is_dict:
                return (state or {}).get(name)
            return getattr(state, name, None)

        s = self._u(logical_side)
        if s == "LONG":
            for name in ("k_long", "k_dir_long", "long_k", "long_steps_filled"):
                iv = self._safe_int(_get(name), None)
                if iv is not None:
                    return max(0, iv)
            return None
        if s == "SHORT":
            for name in ("k_short", "k_dir_short", "short_k", "short_steps_filled"):
                iv = self._safe_int(_get(name), None)
                if iv is not None:
                    return max(0, iv)
            return None
        return None

    def _load_feed_open_order_fps(self, feed: Optional[StrategyFeed]) -> Set[Tuple[str, float, int, bool]]:
        """
        StrategyFeed.open_orders(내부 표준 형태)가 제공되는 경우, 이를 dedup 소스로 사용.
        API 호출 없이 중복 주문 폭주를 차단하는 1차 방어선.
        """
        fps: Set[Tuple[str, float, int, bool]] = set()
        if feed is None:
            return fps

        open_orders = getattr(feed, "open_orders", None) or []
        for o in open_orders:
            try:
                side_raw = str(getattr(o, "side", "") or "").strip().lower()
                if side_raw not in ("buy", "sell"):
                    # "BUY"/"SELL" 등
                    if side_raw == "buy":
                        pass
                    elif side_raw == "sell":
                        pass
                    else:
                        # 일부 구현은 "BUY"/"SELL" 대문자로 들어올 수 있음
                        side_raw2 = str(getattr(o, "side", "") or "").strip().upper()
                        if side_raw2 == "BUY":
                            side_raw = "buy"
                        elif side_raw2 == "SELL":
                            side_raw = "sell"
                        else:
                            continue

                px = getattr(o, "price", None)
                if px is None:
                    px = getattr(o, "order_price", None)
                price = float(px or 0.0)
                if price <= 0:
                    continue

                ro_val = getattr(o, "reduce_only", None)
                if ro_val is None:
                    ro_val = getattr(o, "reduceOnly", None)
                reduce_only = self._safe_bool(ro_val, default=False)

                pidx_val = getattr(o, "position_idx", None)
                if pidx_val is None:
                    pidx_val = getattr(o, "positionIdx", None)
                position_idx = self._safe_int(pidx_val, default=None)
                if position_idx not in (1, 2):
                    position_idx = self._infer_position_idx(side_raw, reduce_only)

                if position_idx not in (1, 2):
                    continue

                fps.add((side_raw, round(price, 2), int(position_idx), bool(reduce_only)))
            except Exception:
                continue

        return fps

    def _load_open_order_fps(self) -> Set[Tuple[str, float, int, bool]]:
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

                side_raw = (o.get("side") or info.get("side") or "").strip().lower()
                if side_raw not in ("buy", "sell"):
                    continue

                px = o.get("price")
                if px is None:
                    px = info.get("price")
                price = float(px or 0.0)
                if price <= 0:
                    continue

                # positionIdx: info/top-level 혼재 가능
                pidx = (
                    info.get("positionIdx")
                    if info.get("positionIdx") is not None
                    else info.get("position_idx")
                )
                if pidx is None:
                    pidx = o.get("positionIdx", o.get("position_idx", None))

                position_idx = self._safe_int(pidx, default=None)

                # reduceOnly: bool/str 혼재 가능
                ro_val = None
                if isinstance(info, dict):
                    if "reduceOnly" in info:
                        ro_val = info.get("reduceOnly")
                    elif "isReduceOnly" in info:
                        ro_val = info.get("isReduceOnly")
                    elif "reduce_only" in info:
                        ro_val = info.get("reduce_only")

                if ro_val is None:
                    ro_val = o.get("reduceOnly", o.get("reduce_only", None))

                reduce_only = self._safe_bool(ro_val, default=False)

                if position_idx not in (1, 2):
                    position_idx = self._infer_position_idx(side_raw, reduce_only)

                if position_idx not in (1, 2):
                    continue

                fps.add((side_raw, round(price, 2), int(position_idx), bool(reduce_only)))
            except Exception:
                continue

        return fps

    def _recent_dedup_hit(self, fp: Tuple[str, float, int, bool], now_ts: float) -> bool:
        last = self._recent_fp.get(fp)
        if last is not None and (now_ts - last) < DEDUP_TTL_SEC:
            return True
        self._recent_fp[fp] = now_ts
        return False

    def _side_code_for_exit(self, logical_side: str) -> int:
        s = self._u(logical_side)
        if s in ("LONG", "SELL"):
            return 4
        if s in ("SHORT", "BUY"):
            return 2
        self.logger.critical("[OrderManager] _side_code_for_exit invalid logical_side=%r -> SKIP", logical_side)
        return 0

    def _side_code_for_entry(self, logical_side: str) -> int:
        s = self._u(logical_side)
        if s in ("LONG", "BUY"):
            return 1
        if s in ("SHORT", "SELL"):
            return 3
        self.logger.critical("[OrderManager] _side_code_for_entry invalid logical_side=%r -> SKIP", logical_side)
        return 0

    def _map_side_int(self, side_code: int) -> Tuple[str, int, bool]:
        if hasattr(self.exchange, "_side_int_to_ccxt"):
            side_str, position_idx, reduce_only = self.exchange._side_int_to_ccxt(side_code)  # type: ignore[attr-defined]
            return str(side_str), int(position_idx), bool(reduce_only)

        side_str = "buy" if side_code in (1, 2) else "sell"
        position_idx = 1 if side_code in (1, 4) else 2
        reduce_only = side_code in (2, 4)
        return side_str, position_idx, reduce_only

    def _prepare_price_qty(self, price: float, qty: float) -> Tuple[float, float]:
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

    def _place_limit_order_compat(
        self,
        side_code: int,
        price: float,
        qty: float,
        *,
        tag: Optional[str],
        position_idx: Optional[int],
        reduce_only: Optional[bool],
    ) -> Any:
        fn = getattr(self.exchange, "place_limit_order", None)
        if not callable(fn):
            self.logger.error("[OrderManager] exchange.place_limit_order not callable")
            return None

        attempts: List[Dict[str, Any]] = []

        if tag is not None:
            attempts.append({"tag": tag})
            attempts.append({"order_link_id": tag})
            attempts.append({"client_order_id": tag})
            attempts.append({"orderLinkId": tag})

        if tag is not None and position_idx is not None:
            attempts.append({"tag": tag, "position_idx": int(position_idx)})
            attempts.append({"tag": tag, "positionIdx": int(position_idx)})

        if tag is not None and reduce_only is not None:
            attempts.append({"tag": tag, "reduce_only": bool(reduce_only)})
            attempts.append({"tag": tag, "reduceOnly": bool(reduce_only)})

        if tag is not None and position_idx is not None and reduce_only is not None:
            attempts.append({"tag": tag, "position_idx": int(position_idx), "reduce_only": bool(reduce_only)})
            attempts.append({"tag": tag, "positionIdx": int(position_idx), "reduceOnly": bool(reduce_only)})

        for kw in attempts:
            try:
                return fn(side_code, price, qty, **kw)
            except TypeError:
                continue
            except Exception as exc:
                self.logger.error("[OrderManager] place_limit_order failed kw=%s err=%s", kw, exc)
                return None

        try:
            return fn(side_code, price, qty)
        except Exception as exc:
            self.logger.error("[OrderManager] place_limit_order failed (no-kw) err=%s", exc)
            return None

    def _place_tp_limit_order_compat(
        self,
        side_code: int,
        price: float,
        qty: float,
        *,
        position_idx: int,
        tag: Optional[str],
    ) -> Any:
        fn = getattr(self.exchange, "place_tp_limit_order", None)
        if callable(fn):
            try:
                return fn(side_code, price, qty, position_idx=int(position_idx), reduce_only=True, tag=tag)
            except TypeError:
                pass
            except Exception as exc:
                self.logger.error("[OrderManager] place_tp_limit_order failed err=%s", exc)
                return None

        return self._place_limit_order_compat(
            side_code,
            price,
            qty,
            tag=tag,
            position_idx=int(position_idx),
            reduce_only=True,
        )

    def _place_market_order_compat(
        self,
        side_code: int,
        qty: float,
        *,
        price_for_calc: float,
        tag: Optional[str],
    ) -> Any:
        fn = getattr(self.exchange, "place_market_order", None)
        if not callable(fn):
            self.logger.error("[OrderManager] exchange.place_market_order not callable")
            return None

        if tag is not None:
            try:
                return fn(side_code, qty, price_for_calc=price_for_calc, tag=tag)
            except TypeError:
                pass
            except Exception as exc:
                self.logger.error("[OrderManager] place_market_order(tag) failed err=%s", exc)
                return None

        try:
            return fn(side_code, qty, price_for_calc=price_for_calc)
        except Exception as exc:
            self.logger.error("[OrderManager] place_market_order failed err=%s", exc)
            return None

    # -------------------- Mode A --------------------

    def apply_decision(self, decision: GridDecision, feed: StrategyFeed, now_ts: float) -> None:
        decision_mode = self._u(getattr(decision, "mode", "UNKNOWN"))
        cancels_in = getattr(decision, "grid_cancels", []) or []
        # normalize cancels: accept order_id str or OrderInfo/dict
        def _cancel_oid(x: Any) -> str:
            if x is None:
                return ""
            if isinstance(x, str):
                return x
            if isinstance(x, dict):
                info = x.get("info") if isinstance(x.get("info"), dict) else {}
                return str(
                    x.get("order_id")
                    or x.get("orderId")
                    or x.get("orderID")
                    or x.get("id")
                    or info.get("orderId")
                    or info.get("order_id")
                    or info.get("id")
                    or ""
                )
            return str(
                getattr(x, "order_id", None)
                or getattr(x, "orderId", None)
                or getattr(x, "id", None)
                or ""
            )
        cancels_in_ids: List[str] = [
            oid for oid in (_cancel_oid(x) for x in (cancels_in or [])) if oid
        ]
        entries = getattr(decision, "grid_entries", []) or []
        replaces = getattr(decision, "grid_replaces", []) or []

        self.logger.info(
            "[OrderManager] apply_decision(mode=%s, cancels=%d, entries=%d, replaces=%d)",
            decision_mode,
            len(cancels_in),
            len(entries),
            len(replaces),
        )
        self.logger.info("[OM_CANCEL_DEBUG] cancels_in_raw=%s", list(cancels_in or []))

        esc, reason = self._is_escape_active(decision_mode, feed)
        self._set_escape_lockdown(esc, reason=reason or "none", now_ts=now_ts)

        self._update_trend_cache(feed, now_ts)

        if self._escape_lockdown:
            for oid in cancels_in_ids:
                 try:
                     self.logger.info("[GridCancel] TRY order_id=%r", oid)
                     self.exchange.cancel_order(oid)
                     self.logger.info("[GridCancel] DONE order_id=%r", oid)
                 except Exception as e:
                     self.logger.warning("[GridCancel] FAIL order_id=%r err=%s", oid, e)
                 finally:
                     self._order_meta.pop(str(oid), None)

            if entries or replaces:
                self.logger.warning(
                    "[HARD-GATE] ESCAPE lockdown(%s): blocked grid_entries=%d grid_replaces=%d",
                    self._escape_lockdown_reason or "unknown",
                    len(entries),
                    len(replaces),
                )
            return

        cancels_set: Set[str] = set(cancels_in_ids)

        if (not self._allow_entry_long) or (not self._allow_entry_short):
            open_orders = getattr(feed, "open_orders", []) or []
            extra = 0
            for o in open_orders:
                if not self._is_entry_family_open_order(o):
                    continue

                oid = str(getattr(o, "order_id", "") or "")
                if not oid or oid in cancels_set:
                    continue

                side = self._u(getattr(o, "side", ""))
                if side == "BUY" and (not self._allow_entry_long):
                    cancels_set.add(oid)
                    extra += 1
                elif side == "SELL" and (not self._allow_entry_short):
                    cancels_set.add(oid)
                    extra += 1

            if extra > 0:
                self.logger.info(
                    "[TrendGate] cancel entry open_orders=%d (allow_long=%s allow_short=%s strength=%s bias=%s)",
                    extra,
                    bool(self._allow_entry_long),
                    bool(self._allow_entry_short),
                    self._trend_strength,
                    self._trend_bias,
                )

        cancels: List[str] = list(cancels_set)

        # DEBUG(C-SESSION): cancels_in vs cancels_final visibility (no behavior change)
        try:
            self.logger.info("[OM_CANCEL_DEBUG] cancels_in_raw=%r", cancels_in)
            self.logger.info("[OM_CANCEL_DEBUG] cancels_final=%r", cancels)
        except Exception:
            pass

        open_fps: Set[Tuple[str, float, int, bool]] = set()
        if entries or replaces:
            # feed.open_orders + exchange.get_open_orders 를 합쳐 2중 방어
            open_fps = self._load_feed_open_order_fps(feed) | self._load_open_order_fps()

        for oid in cancels:
            try:
                self.logger.info("[GridCancel] TRY order_id=%r", oid)
                self.exchange.cancel_order(oid)
                self.logger.info("[GridCancel] DONE order_id=%r", oid)
            except Exception as e:
                self.logger.warning("[GridCancel] FAIL order_id=%r err=%s", oid, e)
            finally:
                self._order_meta.pop(str(oid), None)

        for spec in entries:
            self._create_mode_a_order(spec, now_ts, feed=feed, open_fps=open_fps)
        for spec in replaces:
            self._create_mode_a_order(spec, now_ts, feed=feed, open_fps=open_fps)

    def apply_escape_decision(self, esc_decision: EscapeDecision, feed: StrategyFeed, now_ts: float) -> None:
        if not esc_decision:
            return

        full_exit = bool(getattr(esc_decision, "full_exit", False))
        orders = getattr(esc_decision, "orders", []) or []

        if (not full_exit) and (not orders):
            return

        self._set_escape_lockdown(True, reason="apply_escape_decision", now_ts=now_ts)

        self.logger.info(
            "[OrderManager] apply_escape_decision(full_exit=%s, orders=%d)",
            full_exit,
            len(orders),
        )

        price = float(getattr(feed, "price", 0.0) or 0.0)
        if price <= 0:
            self.logger.warning("[OrderManager] apply_escape_decision skipped: invalid price=%.4f", price)
            return

        if full_exit:
            self._execute_full_exit(feed)
            return

        for spec in orders:
            try:
                if not spec:
                    continue
                qty = float(getattr(spec, "qty", 0.0) or 0.0)
                if qty <= 0:
                    continue

                typ = self._u(getattr(spec, "type", ""))
                side = self._u(getattr(spec, "side", ""))

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
                        source="hedge",
                    )

                else:
                    self.logger.warning("[OrderManager] Unknown EscapeOrderSpec.type=%s", typ)
            except Exception as exc:
                self.logger.error("[OrderManager] apply_escape_decision failed: %s", exc)

    # -------------------- internal: Mode A create --------------------

    def _create_mode_a_order(
        self,
        spec: GridOrderSpec,
        now_ts: float,
        *,
        feed: Optional[StrategyFeed],
        open_fps: Set[Tuple[str, float, int, bool]],
    ) -> None:
        if self._escape_lockdown:
            self.logger.warning(
                "[HARD-GATE] ESCAPE lockdown(%s): refused ModeA create",
                self._escape_lockdown_reason or "unknown",
            )
            return

        side_str = str(getattr(spec, "side", "") or "").upper().strip()
        if side_str not in ("BUY", "SELL"):
            self.logger.error("[OrderManager] invalid spec.side=%r", getattr(spec, "side", None))
            return

        price = float(getattr(spec, "price", 0.0) or 0.0)
        qty = float(getattr(spec, "qty", 0.0) or 0.0)
        if price <= 0.0 or qty <= 0.0:
            return

        wave_id = int(getattr(spec, "wave_id", 0) or 0)
        grid_index = int(getattr(spec, "grid_index", -1) or -1)

        reduce_only = bool(getattr(spec, "reduce_only", False))
        position_idx_in = getattr(spec, "position_idx", None)

        if reduce_only:
            step_cost = TP_STEP_COST
        else:
            _sc = self._safe_int(getattr(spec, "step_cost", None), default=None)
            step_cost = ENTRY_STEP_COST if (_sc is None or _sc < ENTRY_STEP_COST) else int(_sc)

        base_tag = f"W{wave_id}_GRID_A_{grid_index}_{side_str}"
        order_link_id = self._make_order_link_id(base_tag, now_ts)

        if reduce_only:
            if self._escape_lockdown:
                self.logger.warning("[HARD-GATE] TP suppressed by policy (ESCAPE-only TP ban). tag=%s", base_tag)
                return

            if position_idx_in in (1, 2):
                position_idx = int(position_idx_in)
            else:
                position_idx = 1 if side_str == "SELL" else 2

            logical_exit = "LONG" if side_str == "SELL" else "SHORT"
            side_code = self._side_code_for_exit(logical_exit)
            if side_code not in (2, 4):
                return

            fp = self._fp_for_new_order(
                side_code,
                price,
                qty,
                position_idx_override=position_idx,
                reduce_only_override=True,
            )
            if fp in open_fps:
                self.logger.info("[DEDUP] skip TP (already open) fp=%s tag=%s", fp, base_tag)
                return
            if self._recent_dedup_hit(fp, now_ts):
                self.logger.info("[DEDUP] skip TP (ttl-hit) fp=%s tag=%s", fp, base_tag)
                return

            oid = self._place_tp_limit_order_compat(
                side_code,
                price,
                qty,
                position_idx=position_idx,
                tag=order_link_id,
            )
            if not oid:
                return

            # ✅ intent 등록 (tp)
            self._intent_register(order_id=str(oid), order_link_id=str(order_link_id), source="tp")

            # ✅ 같은 apply_decision 내 중복 방지(동일 fp 추가)
            open_fps.add(fp)

            self.logger.info(
                "[GridTP] oid=%s side=%s logical_exit=%s grid_index=%d price=%.2f qty=%.6f tag=%s step_cost=%d",
                oid,
                side_str,
                logical_exit,
                grid_index,
                price,
                qty,
                order_link_id,
                step_cost,
            )
            return

        spec_mode = self._u(getattr(spec, "mode", "A"))
        if spec_mode == "B":
            self.logger.warning(
                "[HARD-GATE] ModeB context: blocked ENTRY (Start-up/DCA/Reentry must not execute in Mode B). tag=%s",
                base_tag,
            )
            return

        logical_side = "LONG" if side_str == "BUY" else "SHORT"

        if not self._entry_allowed_cached(logical_side):
            self.logger.info(
                "[TrendGate] blocked ENTRY logical=%s tag=%s (strength=%s bias=%s allow_long=%s allow_short=%s)",
                logical_side,
                base_tag,
                self._trend_strength,
                self._trend_bias,
                self._allow_entry_long,
                self._allow_entry_short,
            )
            return

        k_dir = self._get_k_dir(feed, logical_side)
        if k_dir is None:
            self.logger.warning(
                "[HARD-GATE] blocked ENTRY: k_dir missing (fail-closed) logical=%s tag=%s step_cost=%d",
                logical_side,
                base_tag,
                step_cost,
            )
            return
        if (int(k_dir) + ENTRY_STEP_COST) > SEED_STEP_MAX:
            self.logger.warning(
                "[HARD-GATE] blocked ENTRY: seed step overflow (k_dir=%d +2 > %d) logical=%s tag=%s",
                int(k_dir),
                SEED_STEP_MAX,
                logical_side,
                base_tag,
            )
            return

        side_code = self._side_code_for_entry(logical_side)
        if side_code not in (1, 3):
            return

        if position_idx_in in (1, 2):
            position_idx = int(position_idx_in)
        else:
            position_idx = 1 if side_str == "BUY" else 2

        fp = self._fp_for_new_order(
            side_code,
            price,
            qty,
            position_idx_override=position_idx,
            reduce_only_override=False,
        )
        if fp in open_fps:
            self.logger.info("[DEDUP] skip ENTRY (already open) fp=%s tag=%s", fp, base_tag)
            return
        if self._recent_dedup_hit(fp, now_ts):
            self.logger.info("[DEDUP] skip ENTRY (ttl-hit) fp=%s tag=%s", fp, base_tag)
            return

        oid = self._place_limit_order_compat(
            side_code,
            price,
            qty,
            tag=order_link_id,
            position_idx=position_idx,
            reduce_only=False,
        )
        if not oid:
            return

        # ✅ intent 등록 (grid)
        self._intent_register(order_id=str(oid), order_link_id=str(order_link_id), source="grid")

        # ✅ 같은 apply_decision 내 중복 방지(동일 fp 추가)
        open_fps.add(fp)

        self._order_meta[str(oid)] = {
            "order_id": str(oid),
            "mode": "A",
            "kind": "GRID",
            "grid_index": grid_index,
            "wave_id": wave_id,
            "side": side_str,
            "price": price,
            "qty": qty,
            "created_ts": float(now_ts or time.time()),
            "base_tag": base_tag,
            "tag": order_link_id,
            "reduce_only": False,
            "step_cost": int(step_cost),
            "logical_side": logical_side,
            "position_idx": int(position_idx),
        }

        self.logger.info(
            "[GridEntry] oid=%s side=%s logical=%s grid_index=%d price=%.2f qty=%.6f tag=%s step_cost=%d",
            oid,
            side_str,
            "OPEN_LONG" if side_str == "BUY" else "OPEN_SHORT",
            grid_index,
            price,
            qty,
            order_link_id,
            step_cost,
        )

        self._schedule_mode_a_replacement(str(oid))

    # -------------------- Mode A replacement worker --------------------

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

        if self._escape_lockdown:
            self._order_meta.pop(oid, None)
            self.logger.warning("[ModeA] ESCAPE lockdown active -> drop replacement oid=%s", oid)
            return

        meta = self._order_meta.get(oid)
        if meta is None:
            return

        side = str(meta.get("side", "")).upper()
        logical_side = "LONG" if side == "BUY" else "SHORT"

        if not self._entry_allowed_cached(logical_side):
            try:
                self.exchange.cancel_order(oid)
            except Exception:
                pass
            self._order_meta.pop(oid, None)
            self.logger.warning(
                "[ModeA] TrendGate blocked -> cancel & drop repost oid=%s logical=%s (strength=%s bias=%s)",
                oid,
                logical_side,
                self._trend_strength,
                self._trend_bias,
            )
            return

        price = float(meta.get("price") or 0.0)
        original_qty = float(meta.get("qty") or 0.0)
        base_tag = str(meta.get("base_tag") or "")
        position_idx = int(meta.get("position_idx") or 0) if int(meta.get("position_idx") or 0) in (1, 2) else (1 if side == "BUY" else 2)

        if price <= 0.0 or original_qty <= 0.0 or not base_tag:
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
            return

        try:
            self.exchange.cancel_order(oid)
        except Exception:
            pass

        self._order_meta.pop(oid, None)

        side_code = self._side_code_for_entry(logical_side)
        if side_code not in (1, 3):
            return

        # ✅ cancel이 실패/무효(no-op)였을 경우 기존 주문이 남아 있을 수 있으므로,
        #    오픈오더 재조회 후 동일 fp가 남아 있으면 repost를 스킵(중복 폭주 차단)
        now2 = time.time()
        fp = self._fp_for_new_order(
            side_code,
            price,
            remaining,
            position_idx_override=position_idx,
            reduce_only_override=False,
        )
        open_fps = self._load_open_order_fps()
        if fp in open_fps:
            self.logger.info("[DEDUP] skip ModeA repost (already open) fp=%s tag=%s", fp, base_tag)
            return
        if self._recent_dedup_hit(fp, now2):
            self.logger.info("[DEDUP] skip ModeA repost (ttl-hit) fp=%s tag=%s", fp, base_tag)
            return

        tag = self._make_order_link_id(base_tag, now2)

        new_oid = self._place_limit_order_compat(
            side_code,
            price,
            remaining,
            tag=tag,
            position_idx=position_idx,
            reduce_only=False,
        )
        if not new_oid:
            return

        # ✅ intent 등록 (grid)
        self._intent_register(order_id=str(new_oid), order_link_id=str(tag), source="grid")

        self._order_meta[str(new_oid)] = {
            **meta,
            "order_id": str(new_oid),
            "qty": remaining,
            "created_ts": time.time(),
            "tag": tag,
        }

        self.logger.info(
            "[ModeA] Re-posted GRID order oid=%s prev_oid=%s side=%s price=%.2f qty=%.6f tag=%s",
            new_oid, oid, side, price, remaining, tag
        )

    # -------------------- FULL_EXIT + slicer --------------------

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

        try:
            log_full_exit_trigger(
                self.logger,
                pnl_total_pct=float(getattr(feed, "pnl_total_pct", 0.0) or 0.0),
                wave_id=getattr(getattr(feed, "state", None), "wave_id", 0),
                positions_before={"LONG": {"qty": long_qty}, "SHORT": {"qty": short_qty}},
            )
        except Exception:
            pass

        if long_qty > 0.0:
            self._execute_sliced_market_exit("LONG", long_qty, price, tag="FULL_EXIT", feed=feed, source="escape")
        if short_qty > 0.0:
            self._execute_sliced_market_exit("SHORT", short_qty, price, tag="FULL_EXIT", feed=feed, source="escape")

    def _execute_sliced_market_exit(
        self,
        logical_side: str,
        total_qty: float,
        price: float,
        tag: str,
        feed: Optional[StrategyFeed] = None,
        *,
        source: str,
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
            tag, logical_side, total_qty, total_notional, vol_1s, vol_1m
        )

        def _market_cb(side_str: str, qty: float) -> None:
            if qty <= 0.0:
                return
            side_code = self._side_code_for_exit(side_str)
            if side_code not in (2, 4):
                return

            link_id = self._make_order_link_id(tag, time.time())
            try:
                oid = self._place_market_order_compat(side_code, qty, price_for_calc=price, tag=link_id)
                self.logger.info("[SliceExit] %s MARKET side=%s side_code=%s qty=%.6f oid=%s", tag, side_str, side_code, qty, oid)
                if oid:
                    self._intent_register(order_id=str(oid), order_link_id=str(link_id), source=source)
            except Exception as exc:
                self.logger.error("[SliceExit] %s MARKET failed side=%s qty=%.6f err=%s", tag, side_str, qty, exc)

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
            return

        link_id = self._make_order_link_id(tag, time.time())
        self.logger.info("[HedgeEntry] %s LIMIT side=%s side_code=%s qty=%.6f price=%.2f", tag, logical_side, side_code, total_qty, price)

        try:
            oid = self._place_limit_order_compat(side_code, price, total_qty, tag=link_id, position_idx=None, reduce_only=False)
        except Exception as exc:
            self.logger.error("[HedgeEntry] %s LIMIT failed side_code=%s qty=%.6f err=%s", tag, side_code, total_qty, exc)
            return

        if not oid:
            return

        # ✅ intent 등록 (hedge)
        self._intent_register(order_id=str(oid), order_link_id=str(link_id), source="hedge")

        try:
            time.sleep(1.0)
            status = self.exchange.get_order_status(oid)
            filled = float(status.get("dealVol", 0.0) or 0.0)
        except Exception:
            filled = 0.0

        remain = max(total_qty - filled, 0.0)

        try:
            self.exchange.cancel_order(oid)
        except Exception:
            pass

        self.logger.info("[HedgeEntry] %s LIMIT filled=%.6f remain=%.6f", tag, filled, remain)

        if remain > 0.0:
            self._execute_sliced_market_entry(logical_side, remain, price, tag=f"{tag}_FALLBACK", feed=feed, source="hedge")

    def _execute_sliced_market_entry(
        self,
        logical_side: str,
        total_qty: float,
        price: float,
        tag: str,
        feed: Optional[StrategyFeed] = None,
        *,
        source: str,
    ) -> None:
        if total_qty <= 0.0 or price <= 0.0:
            return

        total_notional = abs(total_qty) * price
        if total_notional <= 0.0:
            return

        vol_1s = float(getattr(feed, "vol_1s", 0.0) or 0.0) if feed is not None else 0.0
        vol_1m = float(getattr(feed, "vol_1m", 0.0) or 0.0) if feed is not None else 0.0

        self.logger.info("[SliceEntry] %s side=%s total_qty=%.6f total_notional=%.2f vol_1s=%.4f vol_1m=%.4f", tag, logical_side, total_qty, total_notional, vol_1s, vol_1m)

        def _market_cb(side_str: str, qty: float) -> None:
            if qty <= 0.0:
                return
            side_code = self._side_code_for_entry(side_str)
            if side_code not in (1, 3):
                return

            link_id = self._make_order_link_id(tag, time.time())
            try:
                oid = self._place_market_order_compat(side_code, qty, price_for_calc=price, tag=link_id)
                self.logger.info("[SliceEntry] %s MARKET side=%s side_code=%s qty=%.6f oid=%s", tag, side_str, side_code, qty, oid)
                if oid:
                    self._intent_register(order_id=str(oid), order_link_id=str(link_id), source=source)
            except Exception as exc:
                self.logger.error("[SliceEntry] %s MARKET failed side=%s qty=%.6f err=%s", tag, side_str, qty, exc)

        plan = self.liquidation_slicer.execute_sliced_liquidation(
            side=logical_side,
            total_notional=total_notional,
            price=price,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            place_market_order=_market_cb,
        )
        self.logger.info("[SliceEntry] %s completed mode=%s slices=%d reason=%s", tag, plan.mode, len(plan.slices), plan.reason)


order_manager = OrderManager()

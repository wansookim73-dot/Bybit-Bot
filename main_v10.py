"""
main_v10.py - Entry point for Bybit Wave Bot v10.1

[패치세트2 핵심]
- balance_raw에서 total/free를 확실히 파싱하여 BotState.total_balance/free_balance에 반영
- BotState.total_balance_snap이 0이면 1회 부트스트랩(현재 total_balance로 세팅)
  → pnl_total_pct 분모 0 문제 해결 (EscapeLogic 트리거 정확도 회복)
- state_manager.update(save=False) 사용 → tick 중 디스크 저장 폭발 방지
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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


@dataclass
class MarketSnapshot:
    price: float
    atr_4h_42: float

    move_1m: float
    vol_1m: float
    vol_ma20: float
    last3_ranges: List[float]
    last3_mids: List[float]

    vol_1s: float

    open_orders_raw: List[Dict[str, Any]]
    positions_raw: Any
    balance_raw: Dict[str, Any]


class WaveBot:
    def __init__(self) -> None:
        self.logger = logger
        self.exchange = exchange

        # 싱글톤 StateManager로 일원화됨 (core/state_manager.py 패치세트2)
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

        self._last_price: Optional[float] = None

    # --------------------------------------------------------
    # 작은 유틸
    # --------------------------------------------------------

    def _get_state_obj(self):
        getter = getattr(self.state_manager, "get_state", None)
        if callable(getter):
            return getter()
        return getattr(self.state_manager, "state")

    def _safe_float(self, v: Any) -> float:
        try:
            return float(v or 0.0)
        except Exception:
            return 0.0

    def _parse_balance(self, bal: Any) -> Tuple[float, float]:
        """
        ExchangeAPI.get_balance() 반환 dict에서 total/free를 최대한 robust하게 파싱한다.
        """
        if not isinstance(bal, dict):
            return 0.0, 0.0

        def pick(keys: Tuple[str, ...]) -> float:
            for k in keys:
                if k in bal and bal.get(k) is not None:
                    return self._safe_float(bal.get(k))
            return 0.0

        total = pick(("total", "total_equity", "total_balance", "equity", "wallet_balance", "walletBalance"))
        free = pick(("free", "available", "available_balance", "free_balance", "availableBalance", "availableToWithdraw"))

        # 일부 거래소/래퍼에서 free를 별도로 안 주면 total로 근사
        if free <= 0.0 and total > 0.0:
            free = total

        return float(total or 0.0), float(free or 0.0)

    # --------------------------------------------------------
    # ATR / OHLCV 계산
    # --------------------------------------------------------

    def _compute_atr_4h_42(self) -> float:
        try:
            ohlcv = self.exchange.fetch_ohlcv(timeframe="4h", limit=42) or []
        except Exception as exc:
            self.logger.warning("[WaveBot] fetch_ohlcv(4h) failed: %s", exc)
            return 0.0

        if len(ohlcv) < 2:
            return 0.0

        trs: List[float] = []
        try:
            prev_close = float(ohlcv[0][4])
        except Exception:
            prev_close = 0.0

        for row in ohlcv:
            try:
                _ts, _o, h, l, c, _v = row
                h = float(h)
                l = float(l)
                c = float(c)
            except Exception:
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

    def _compute_1m_metrics(self) -> Dict[str, Any]:
        try:
            ohlcv = self.exchange.fetch_ohlcv(timeframe="1m", limit=20) or []
        except Exception as exc:
            self.logger.warning("[WaveBot] fetch_ohlcv(1m) failed: %s", exc)
            return {"move_1m": 0.0, "vol_1m": 0.0, "vol_ma20": 0.0, "last3_ranges": [], "last3_mids": []}

        if not ohlcv:
            return {"move_1m": 0.0, "vol_1m": 0.0, "vol_ma20": 0.0, "last3_ranges": [], "last3_mids": []}

        last = ohlcv[-1]
        try:
            _ts, o, h, l, c, v = last
            o = float(o)
            c = float(c)
            v = float(v)
        except Exception:
            o = c = v = 0.0

        mid = (o + c) / 2.0 if (o + c) != 0 else (c or o or 0.0)
        move_1m = abs(c - o) / mid if mid > 0 else 0.0
        vol_1m = v

        vols: List[float] = []
        for row in ohlcv:
            try:
                vols.append(float(row[5]))
            except Exception:
                continue
        vol_ma20 = sum(vols) / float(len(vols)) if vols else 0.0

        last3 = ohlcv[-3:]
        last3_ranges: List[float] = []
        last3_mids: List[float] = []
        for row in last3:
            try:
                _ts, _o, h, l, _c, _v = row
                h = float(h)
                l = float(l)
            except Exception:
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
    # Snapshot 생성
    # --------------------------------------------------------

    def _build_market_snapshot(self) -> MarketSnapshot:
        try:
            price = float(self.exchange.get_ticker() or 0.0)
        except Exception as exc:
            self.logger.error("[WaveBot] get_ticker failed: %s", exc)
            price = 0.0

        atr_4h_42 = self._compute_atr_4h_42()

        one_min = self._compute_1m_metrics()
        move_1m = one_min["move_1m"]
        vol_1m = one_min["vol_1m"]
        vol_ma20 = one_min["vol_ma20"]
        last3_ranges = one_min["last3_ranges"]
        last3_mids = one_min["last3_mids"]

        if self._last_price and self._last_price > 0 and price > 0:
            vol_1s = abs(price - self._last_price) / self._last_price
        else:
            vol_1s = 0.0

        try:
            open_orders_raw = self.exchange.get_open_orders() or []
        except Exception as exc:
            self.logger.error("[WaveBot] get_open_orders failed: %s", exc)
            open_orders_raw = []

        try:
            positions_raw = self.exchange.get_positions()
        except Exception as exc:
            self.logger.error("[WaveBot] get_positions failed: %s", exc)
            positions_raw = {}

        try:
            balance_raw = self.exchange.get_balance() or {}
        except Exception as exc:
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

    def _update_state_from_positions_and_balance(self, snap: MarketSnapshot, state: Any) -> None:
        """
        - positions_raw 기반으로 long/short size/avg/pnl 계산
        - balance_raw 기반으로 total/free 반영
        - total_balance_snap이 0이면 1회 부트스트랩
        - state_manager.update(save=False)로 디스크 I/O 폭발 방지
        """
        price_now = float(getattr(snap, "price", 0.0) or 0.0)
        pos_raw = getattr(snap, "positions_raw", None)

        long_size = short_size = 0.0
        long_avg = short_avg = 0.0

        if isinstance(pos_raw, dict):
            long_info = pos_raw.get("LONG") or pos_raw.get("long") or {}
            short_info = pos_raw.get("SHORT") or pos_raw.get("short") or {}

            long_size = float(long_info.get("qty", 0.0) or 0.0)
            short_size = float(short_info.get("qty", 0.0) or 0.0)

            long_avg = float(long_info.get("avg_price", 0.0) or 0.0)
            short_avg = float(short_info.get("avg_price", 0.0) or 0.0)

        elif isinstance(pos_raw, (list, tuple)):
            for p in pos_raw:
                if not isinstance(p, dict):
                    continue
                info = p.get("info") or {}

                qty = float(p.get("contracts") or info.get("size") or 0.0)
                if qty <= 0:
                    continue

                side = (p.get("side") or info.get("side") or "").lower()
                if not side:
                    idx = str(info.get("positionIdx", ""))
                    if idx == "1":
                        side = "long"
                    elif idx == "2":
                        side = "short"

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

        long_pnl = 0.0
        short_pnl = 0.0
        if price_now > 0:
            if long_size > 0 and long_avg > 0:
                long_pnl = (price_now - long_avg) * long_size
            if short_size > 0 and short_avg > 0:
                short_pnl = (short_avg - price_now) * short_size

        total_balance, free_balance = self._parse_balance(getattr(snap, "balance_raw", {}) or {})

        self.logger.info("[BAL] parsed balance: total=%.8f free=%.8f", total_balance, free_balance)

        # total_balance_snap이 0이면 1회 부트스트랩(기존 state.json 호환)
        try:
            tb_snap = float(getattr(state, "total_balance_snap", 0.0) or 0.0)
        except Exception:
            tb_snap = 0.0

        if tb_snap <= 0.0 and total_balance > 0.0:
            self.logger.info("[BAL] bootstrap total_balance_snap -> %.8f (was %.8f)", total_balance, tb_snap)
            self.state_manager.update("total_balance_snap", total_balance, save=False)

        updates = {
            "long_size": long_size,
            "short_size": short_size,
            "hedge_size": 0.0,  # 구조상 별도 hedge 포지션이 없다면 0
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "long_pos_nonzero": (long_size > 0),
            "short_pos_nonzero": (short_size > 0),
            "long_pnl_sign": 1 if long_pnl > 0 else -1 if long_pnl < 0 else 0,
            "short_pnl_sign": 1 if short_pnl > 0 else -1 if short_pnl < 0 else 0,
            "total_balance": total_balance,
            "free_balance": free_balance,
        }

        for k, v in updates.items():
            # state 객체에도 반영(메모리상 참조 일관성)
            try:
                setattr(state, k, v)
            except Exception:
                pass
            # 저장은 지연(save=False)
            self.state_manager.update(k, v, save=False)

        self.logger.info(
            "[PnL-CORE] price=%.2f L(size=%.4f avg=%.2f pnl=%.4f) "
            "S(size=%.4f avg=%.2f pnl=%.4f) pnl_total=%.4f",
            price_now,
            long_size, long_avg, long_pnl,
            short_size, short_avg, short_pnl,
            (long_pnl + short_pnl),
        )

    # --------------------------------------------------------
    # RiskInputs 구성 + RiskDecision → BotState 반영
    # --------------------------------------------------------

    def _build_risk_inputs(self, snap: MarketSnapshot, now_ts: float, state: Any) -> RiskInputs:
        p_gap = float(getattr(state, "p_gap", 0.0) or 0.0)
        p_center = float(getattr(state, "p_center", 0.0) or 0.0)

        return RiskInputs(
            ts=now_ts,
            is_macro=False,
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
        updates: Dict[str, Any] = {
            "news_block": bool(getattr(risk_decision, "news_block", False)),
            "cb_block": bool(getattr(risk_decision, "cb_block", False)),
        }

        self.logger.info(
            "[RISK] pause=%s reason=%s news_block=%s cb_block=%s until=%.0f",
            bool(getattr(risk_decision, "pause_active", False)),
            str(getattr(risk_decision, "reason", "NONE")),
            updates["news_block"],
            updates["cb_block"],
            float(getattr(risk_decision, "entry_pause_until_ts", 0.0) or 0.0),
        )

        for k, v in updates.items():
            self.state_manager.update(k, v, save=False)

    # --------------------------------------------------------
    # OrderInfo 매핑
    # --------------------------------------------------------

    def _map_open_orders(self, open_orders_raw: List[Dict[str, Any]]) -> List[OrderInfo]:
        result: List[OrderInfo] = []
        for o in open_orders_raw:
            try:
                order_id = str(o.get("id") or o.get("order_id") or "")
                side = str(o.get("side") or "").upper()
                price = float(o.get("price") or 0.0)
                qty = float(o.get("amount") or o.get("qty") or 0.0)
                filled_qty = float(o.get("filled") or 0.0)
                reduce_only = bool(o.get("reduceOnly") or o.get("reduce_only") or False)
                order_type = str(o.get("type") or "limit").capitalize()
                tif = str(o.get("timeInForce") or o.get("time_in_force") or "").capitalize()
                tag = o.get("clientOrderId") or o.get("tag") or None
                created_ts = float(
                    o.get("timestamp") or o.get("createdTime") or time.time() * 1000
                ) / 1000.0
            except Exception:
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

    # --------------------------------------------------------
    # StrategyFeed 구성
    # --------------------------------------------------------

    def _build_strategy_feed(self, snap: MarketSnapshot, state: Any) -> StrategyFeed:
        price = float(snap.price or 0.0)

        long_pnl = float(getattr(state, "long_pnl", 0.0) or 0.0)
        short_pnl = float(getattr(state, "short_pnl", 0.0) or 0.0)
        pnl_total = long_pnl + short_pnl

        denom = float(getattr(state, "total_balance_snap", 0.0) or 0.0)
        if denom <= 0.0:
            denom = float(getattr(state, "total_balance", 0.0) or 0.0)
        if denom <= 0.0:
            # 최후: snap.balance_raw에서 파싱
            total_balance, _free = self._parse_balance(getattr(snap, "balance_raw", {}) or {})
            denom = float(total_balance or 0.0)

        pnl_total_pct = pnl_total / denom if denom > 0 else 0.0

        open_orders = self._map_open_orders(snap.open_orders_raw)

        feed = StrategyFeed(
            price=price,
            atr_4h_42=snap.atr_4h_42,
            state=state,
            open_orders=open_orders,
            pnl_total=pnl_total,
            pnl_total_pct=pnl_total_pct,
        )

        prev_price = self._last_price if (self._last_price and self._last_price > 0) else price
        setattr(feed, "price_prev", prev_price)

        setattr(feed, "vol_1s", snap.vol_1s)
        setattr(feed, "vol_1m", snap.move_1m)

        setattr(feed, "news_block", bool(getattr(state, "news_block", False)))
        setattr(feed, "cb_block", bool(getattr(state, "cb_block", False)))
        setattr(feed, "balance_raw", snap.balance_raw)

        self.logger.info("[PNL] pnl_total=%.4f denom=%.4f pnl_total_pct=%.5f", pnl_total, denom, pnl_total_pct)

        return feed

    # --------------------------------------------------------
    # Lifecycle hooks
    # --------------------------------------------------------

    def start(self) -> None:
        self.logger.info("WaveBot v10.1 starting...")

        setup = getattr(self.exchange, "set_leverage_and_mode", None)
        if callable(setup):
            try:
                setup()
            except Exception as exc:
                self.logger.warning("[WaveBot] exchange.set_leverage_and_mode() 실패: %s", exc)

    def shutdown(self) -> None:
        self.logger.info("WaveBot v10.1 shutting down...")
        try:
            self.state_manager.save_state()
            self.logger.info("[WaveBot] StateManager.save_state() 호출 완료")
        except Exception as exc:
            self.logger.warning("[WaveBot] StateManager.save_state() 호출 실패: %s", exc)

    # --------------------------------------------------------
    # 메인 루프
    # --------------------------------------------------------

    def loop_once(self) -> None:
        now_ts = time.time()

        state_before = self._get_state_obj()
        snap = self._build_market_snapshot()

        # 1) 포지션 + 잔고 동기화 (저장은 지연)
        self._update_state_from_positions_and_balance(snap, state_before)

        # 2) RiskManager 평가 → news_block/cb_block만 업데이트(저장은 지연)
        risk_inputs = self._build_risk_inputs(snap, now_ts, state_before)
        risk_decision = self.risk_manager.process(risk_inputs)
        self._apply_risk_decision_to_state(risk_decision)

        # 3) 최신 state로 feed 구성
        state_for_feed = self._get_state_obj()
        feed = self._build_strategy_feed(snap, state_for_feed)

        # 4) WaveFSM tick (내부에서 save_state 수행)
        try:
            self.wave_fsm.tick(feed, now_ts)
        except Exception as exc:
            self.logger.exception("[WaveBot] WaveFSM.tick failed: %s", exc)

        if snap.price > 0:
            self._last_price = snap.price

        # 패치세트2: tick 중 update(save=False)로 지연된 저장을 최종 1회 커밋
        # (WaveFSM가 이미 저장했더라도, 예외/early-return 대비 안전장치)
        try:
            self.state_manager.save_state()
        except Exception:
            pass


# ------------------------------------------------------------
# CLI / main()
# ------------------------------------------------------------

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bybit Wave Bot v10.1 runner")
    parser.add_argument("--once", action="store_true", help="Run a single loop iteration and exit.")
    parser.add_argument("--loop-interval", type=float, default=1.0, help="Main loop sleep interval in seconds.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    bot = WaveBot()

    stop = {"flag": False}

    def handle_sigterm(signum, frame):
        stop["flag"] = True
        bot.logger.info("Received signal %s, shutting down...", signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_sigterm)

    try:
        bot.start()
        if args.once:
            bot.loop_once()
            return 0

        bot.logger.info("WaveBot v10.1 main loop started (interval=%.3fs)", args.loop_interval)
        while not stop["flag"]:
            bot.loop_once()
            time.sleep(args.loop_interval)

    except Exception as exc:
        try:
            bot.logger.exception("Fatal error in main loop: %s", exc)
        except Exception:
            print(f"[FATAL] WaveBot main loop error: {exc!r}", file=sys.stderr)
        return 1

    finally:
        try:
            bot.shutdown()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

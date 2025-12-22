from __future__ import annotations

import ccxt
import time
import hmac
import hashlib
import logging
from typing import Dict, Any, Optional, Tuple, List

from config import (
    BYBIT_API_KEY,
    BYBIT_SECRET_KEY,
    SYMBOL as CONFIG_SYMBOL,
    LEVERAGE as CONFIG_LEVERAGE,
    DRY_RUN,
)
from utils.logger import logger
from utils.calculator import price_floor_to_tick, calc_contract_qty, SYMBOL_INFO


# ==========================================================
# v10.1 시스템 기본 환경 (1장) - 고정 값
# ==========================================================

EXCHANGE_SYMBOL = "BTCUSDT"
POSITION_MODE_HEDGED = True
CROSS_LEVERAGE = 7.0
BYBIT_CATEGORY = "linear"


class ExchangeAPI:
    """
    v10.1 WaveBot 시스템 환경 래퍼.

    [C-Session 버그 수정]
    - main_v10.py가 사용 중인 메서드가 ExchangeAPI에 누락되어 AttributeError/경고 폭주가 발생.
      => fetch_ohlcv(), get_ticker() 를 추가하여 wrapper 일관성 복구.
    - 설계/전략 변경 없음. (단순 위임 wrapper)
    """

    def __init__(self) -> None:
        self.symbol: str = EXCHANGE_SYMBOL
        self.leverage: float = CROSS_LEVERAGE
        self.dry_run: bool = bool(DRY_RUN)

        if CONFIG_SYMBOL != EXCHANGE_SYMBOL:
            logger.warning(
                "[ExchangeAPI] config.SYMBOL=%s 이지만, v10.1 명세에 따라 %s 로 고정합니다.",
                CONFIG_SYMBOL,
                EXCHANGE_SYMBOL,
            )
        if float(CONFIG_LEVERAGE) != CROSS_LEVERAGE:
            logger.warning(
                "[ExchangeAPI] config.LEVERAGE=%s 이지만, v10.1 명세에 따라 %.1fx Cross 로 고정합니다.",
                CONFIG_LEVERAGE,
                CROSS_LEVERAGE,
            )

        exchange_options = {
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_SECRET_KEY,
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",
                "adjustForTimeDifference": True,
                "createMarketBuyOrderRequiresPrice": False,
            },
        }
        self.exchange = ccxt.bybit(exchange_options)
        self._markets_loaded: bool = False

        if self.dry_run:
            logger.warning("🧪 [DRY_RUN] 모드로 ExchangeAPI 초기화 (실 거래 없음)")
        else:
            logger.warning("🚀 [REAL] Trade Mode - Bybit BTCUSDT Perp (Cross 7x, Hedge)")
            self.set_leverage_and_mode()

    # ==========================================================
    # 유틸
    # ==========================================================

    def _ensure_markets_loaded(self) -> None:
        if self._markets_loaded:
            return
        try:
            self.exchange.load_markets()
            self._markets_loaded = True
        except Exception as exc:
            logger.warning("[ExchangeAPI] load_markets failed: %s", exc)

    # ==========================================================
    # 서버 시간 / 인증 (기존 유지)
    # ==========================================================

    def get_server_time(self) -> int:
        try:
            return self.exchange.fetch_time()
        except Exception as e:
            logger.error(f"[ExchangeAPI] Server Time Fetch Failed: {e}")
            return int(time.time() * 1000)

    def generate_auth_signature(self, expires: int) -> Tuple[str, str]:
        param_string = f"GET/realtime{expires}"
        signature = hmac.new(
            BYBIT_SECRET_KEY.encode("utf-8"),
            param_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return param_string, signature

    # ==========================================================
    # Market (가격/ohlcv)  ★ 버그 수정 핵심
    # ==========================================================

    def get_ticker(self) -> float:
        """
        main_v10.py 호환: 현재가(last) 반환.
        (기존에 다른 이름을 쓰더라도, main_v10.py가 get_ticker를 호출하므로 alias 제공)
        """
        return float(self.fetch_ticker_last() or 0.0)

    def fetch_ticker_last(self) -> float:
        """
        실제 구현: Bybit ticker(last) 조회.
        """
        try:
            self._ensure_markets_loaded()
            ticker = self.exchange.fetch_ticker(
                self.symbol,
                params={"category": BYBIT_CATEGORY},
            )
            return float(ticker.get("last") or 0.0)
        except Exception as e:
            logger.error(f"[ExchangeAPI] Ticker Fail: {e}")
            return 0.0

    def fetch_ohlcv(self, timeframe: str = "1m", limit: int = 200):
        """
        main_v10.py 호환: ATR/1m 메트릭 계산을 위한 OHLCV 조회.
        - 누락되어 있던 wrapper를 추가(버그 수정)
        - 실패 시 [] 반환(상위 로직 방어)
        """
        self._ensure_markets_loaded()
        try:
            return self.exchange.fetch_ohlcv(
                self.symbol,
                timeframe=timeframe,
                limit=int(limit),
                params={"category": BYBIT_CATEGORY},
            )
        except Exception as exc:
            logger.warning("[ExchangeAPI] fetch_ohlcv(%s,%s) failed: %s", timeframe, limit, exc)
            return []

    # ==========================================================
    # Balance (미실현 PnL 제외 walletBalance 우선) - 기존 유지
    # ==========================================================

    def get_balance(self) -> Dict[str, float]:
        total = 0.0
        available = 0.0

        def _try_wallet_balance_v5() -> tuple[float, float]:
            try:
                fn = getattr(self.exchange, "privateGetV5AccountWalletBalance", None)
                if not callable(fn):
                    return 0.0, 0.0
                resp = fn({"accountType": "UNIFIED"})
                if not isinstance(resp, dict):
                    return 0.0, 0.0
                result = resp.get("result") or {}
                lst = result.get("list") or []
                if not isinstance(lst, list) or not lst:
                    return 0.0, 0.0
                first = lst[0] if isinstance(lst[0], dict) else {}
                coins = first.get("coin") or []
                if not isinstance(coins, list):
                    return 0.0, 0.0
                for c in coins:
                    if not isinstance(c, dict):
                        continue
                    if str(c.get("coin") or "").upper() != "USDT":
                        continue
                    wb = c.get("walletBalance")
                    if wb is None:
                        wb = c.get("totalWalletBalance")
                    av = c.get("availableToWithdraw")
                    if av is None:
                        av = c.get("availableBalance")
                    try:
                        t = float(wb or 0.0)
                    except Exception:
                        t = 0.0
                    try:
                        a = float(av or 0.0)
                    except Exception:
                        a = t
                    return t, a
                return 0.0, 0.0
            except Exception as exc:
                logger.debug("[ExchangeAPI] wallet-balance(v5) parse failed: %s", exc)
                return 0.0, 0.0

        def _extract_usdt(bal) -> tuple[float, float]:
            t = 0.0
            a = 0.0
            if not isinstance(bal, dict):
                return t, a
            usdt = bal.get("USDT") or {}
            if not isinstance(usdt, dict):
                return t, a
            try:
                t = float(usdt.get("total") or 0.0)
            except Exception:
                t = 0.0
            try:
                free = usdt.get("free")
                if free is None:
                    free = usdt.get("total")
                a = float(free or 0.0)
            except Exception:
                a = t
            return t, a

        try:
            t0, a0 = _try_wallet_balance_v5()
            if t0 or a0:
                total, available = t0, a0
                raise StopIteration

            bal = self.exchange.fetch_balance({"accountType": "UNIFIED"})
            t, a = _extract_usdt(bal)
            total, available = t, a
        except StopIteration:
            pass
        except Exception as exc:
            logger.warning(
                "[ExchangeAPI] fetch_balance(accountType='UNIFIED') failed: %s", exc
            )

        if total == 0.0 and available == 0.0:
            try:
                bal2 = self.exchange.fetch_balance()
                t2, a2 = _extract_usdt(bal2)
                if t2 or a2:
                    total, available = t2, a2
            except Exception as exc:
                logger.warning("[ExchangeAPI] fetch_balance() fallback failed: %s", exc)

        logger.info(
            "[ExchangeAPI] balance snapshot: total=%.8f, free=%.8f",
            float(total or 0.0),
            float(available or 0.0),
        )

        return {
            "total": float(total or 0.0),
            "available": float(available or 0.0),
        }

    # ==========================================================
    # Positions
    # ==========================================================

    def get_positions(self) -> Dict[str, Dict[str, float]]:
        result: Dict[str, Dict[str, float]] = {
            "LONG": {"qty": 0.0, "avg_price": 0.0},
            "SHORT": {"qty": 0.0, "avg_price": 0.0},
        }

        try:
            self._ensure_markets_loaded()
            positions = self.exchange.fetch_positions(
                [self.symbol],
                params={"category": BYBIT_CATEGORY},
            )
        except Exception as exc:
            logger.warning("[ExchangeAPI] get_positions error (fetch_positions): %s", exc)
            return result

        try:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[ExchangeAPI] raw positions: %s", positions)
        except Exception:
            pass

        if not isinstance(positions, (list, tuple)):
            return result

        long_qty = 0.0
        short_qty = 0.0
        long_avg = 0.0
        short_avg = 0.0

        for pos in positions:
            try:
                if not isinstance(pos, dict):
                    continue

                info = pos.get("info") or {}

                contracts = pos.get("contracts")
                if contracts is None:
                    contracts = pos.get("size") or info.get("size")
                qty = float(contracts or 0.0)
                if qty <= 0.0:
                    continue

                side = (pos.get("side") or info.get("side") or "").lower()
                if not side:
                    idx = str(info.get("positionIdx", ""))
                    if idx == "1":
                        side = "long"
                    elif idx == "2":
                        side = "short"

                avg_price = pos.get("entryPrice")
                if not avg_price:
                    avg_price = pos.get("avgPrice") or info.get("avgPrice")
                avg = float(avg_price or 0.0)

                if side in ("long", "buy"):
                    long_qty += qty
                    long_avg = avg
                elif side in ("short", "sell"):
                    short_qty += qty
                    short_avg = avg
            except Exception:
                continue

        result["LONG"]["qty"] = float(long_qty or 0.0)
        result["SHORT"]["qty"] = float(short_qty or 0.0)
        result["LONG"]["avg_price"] = float(long_avg or 0.0)
        result["SHORT"]["avg_price"] = float(short_avg or 0.0)

        return result

    # ==========================================================
    # Leverage / Mode
    # ==========================================================

    def set_leverage_and_mode(self) -> None:
        try:
            self.exchange.set_leverage(
                int(self.leverage),
                self.symbol,
                params={"category": BYBIT_CATEGORY},
            )
        except Exception as e:
            logger.warning(f"[ExchangeAPI] Setup Error (set_leverage): {e}")

        try:
            if hasattr(self.exchange, "set_position_mode"):
                self.exchange.set_position_mode(
                    True,
                    self.symbol,
                    params={"category": BYBIT_CATEGORY},
                )
        except Exception as e:
            logger.warning(f"[ExchangeAPI] Setup Error (set_position_mode): {e}")

    # ==========================================================
    # Orders
    # ==========================================================

    def create_limit_order(
        self,
        side: str,
        price: float,
        qty: float,
        *,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.dry_run:
            logger.warning(
                f"[DRY_RUN] create_limit_order: side={side}, price={price}, qty={qty}, reduce_only={reduce_only}"
            )
            return {"id": f"dry_{int(time.time() * 1000)}"}

        try:
            price = price_floor_to_tick(price, SYMBOL_INFO["tick_size"])
        except Exception:
            pass
        try:
            qty = calc_contract_qty(qty, SYMBOL_INFO["qty_step"])
        except Exception:
            pass

        params: Dict[str, Any] = {
            "category": BYBIT_CATEGORY,
            "reduceOnly": bool(reduce_only),
        }
        if client_order_id:
            params["clientOrderId"] = client_order_id

        try:
            order = self.exchange.create_order(
                self.symbol,
                "limit",
                side.lower(),
                qty,
                price,
                params=params,
            )
            return order
        except Exception as e:
            logger.error(f"[ExchangeAPI] create_limit_order fail: {e}")
            return {}

    def create_market_order(
        self,
        side: str,
        qty: float,
        *,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.dry_run:
            logger.warning(
                f"[DRY_RUN] create_market_order: side={side}, qty={qty}, reduce_only={reduce_only}"
            )
            return {"id": f"dry_{int(time.time() * 1000)}"}

        try:
            qty = calc_contract_qty(qty, SYMBOL_INFO["qty_step"])
        except Exception:
            pass

        params: Dict[str, Any] = {
            "category": BYBIT_CATEGORY,
            "reduceOnly": bool(reduce_only),
        }
        if client_order_id:
            params["clientOrderId"] = client_order_id

        try:
            order = self.exchange.create_order(
                self.symbol,
                "market",
                side.lower(),
                qty,
                None,
                params=params,
            )
            return order
        except Exception as e:
            logger.error(f"[ExchangeAPI] create_market_order fail: {e}")
            return {}

    def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            logger.warning(f"[DRY_RUN] cancel_order: {order_id}")
            return
        try:
            self.exchange.cancel_order(
                order_id,
                self.symbol,
                params={"category": BYBIT_CATEGORY},
            )
        except Exception as e:
            logger.warning(f"[ExchangeAPI] cancel_order fail: {e}")

    def get_open_orders(self) -> List[Dict[str, Any]]:
        try:
            orders = self.exchange.fetch_open_orders(
                self.symbol,
                params={"category": BYBIT_CATEGORY},
            )
            return orders or []
        except Exception as e:
            logger.warning(f"[ExchangeAPI] get_open_orders fail: {e}")
            return []


exchange = ExchangeAPI()

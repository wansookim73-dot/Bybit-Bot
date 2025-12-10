from __future__ import annotations

import ccxt
import time
import hmac
import hashlib
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

# 심볼: BTCUSDT Perpetual (Bybit Linear Perp)
EXCHANGE_SYMBOL = "BTCUSDT"

# 포지션 모드: Hedge Mode (롱/숏 동시 보유)
POSITION_MODE_HEDGED = True

# 레버리지: Cross 7x
CROSS_LEVERAGE = 7.0

# Bybit Linear (USDT Perp) 카테고리
BYBIT_CATEGORY = "linear"


class ExchangeAPI:
    """
    v10.1 WaveBot 시스템 환경 래퍼.

    역할:
    - Bybit BTCUSDT Perpetual 에 대한 REST 호출 래핑
    - Cross 7x + Hedge Mode 고정
    - price/qty 계산 시 utils.calculator 모듈 사용
    - 모든 금액/잔고/Notional 은 USDT 기준으로 관리
    """

    def __init__(self) -> None:
        # ---- 환경 고정 ----
        self.symbol: str = EXCHANGE_SYMBOL
        self.leverage: float = CROSS_LEVERAGE
        self.dry_run: bool = bool(DRY_RUN)

        # config 값을 참고는 하되, 여기서 환경을 강제 고정
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
                "defaultType": "linear",  # USDT Perp
                "adjustForTimeDifference": True,
                "createMarketBuyOrderRequiresPrice": False,
            },
        }
        self.exchange = ccxt.bybit(exchange_options)

        # 마켓 메타데이터 lazy-load 플래그
        self._markets_loaded: bool = False

        if self.dry_run:
            logger.warning("🧪 [DRY_RUN] 모드로 ExchangeAPI 초기화 (실 거래 없음)")
        else:
            logger.warning("🚀 [REAL] Trade Mode - Bybit BTCUSDT Perp (Cross 7x, Hedge)")
            # v10.1: 초기화 시 Hedge Mode + Cross 7x 강제 세팅
            self.set_leverage_and_mode()

    # ==========================================================
    # 인증/시간 유틸
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
    # Market / Balance / Positions
    # ==========================================================

    def _ensure_markets_loaded(self) -> None:
        if self._markets_loaded:
            return
        try:
            self.exchange.load_markets()
            self._markets_loaded = True
        except Exception as exc:
            logger.warning("[ExchangeAPI] load_markets failed: %s", exc)

    def get_ticker(self) -> float:
        """
        현재 BTCUSDT Perp 가격 (last, USDT 기준).
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

    def get_balance(self) -> Dict[str, float]:
        """
        USDT 기준 total / available 잔고 리턴.

        - UTA(UNIFIED) 계정의 USDT 잔고를 우선 사용
        - 필요시 일반 fetch_balance() 를 fallback 으로 사용
        - 예외가 발생한 경우에만 0.0 으로 fallback

        반환:
            {
              "total": float,      # 총 잔고 (USDT)
              "available": float,  # 사용 가능 잔고 (USDT)
            }
        """
        total = 0.0
        available = 0.0

        def _extract_usdt(bal) -> tuple[float, float]:
            t = 0.0
            a = 0.0
            if not isinstance(bal, dict):
                return t, a
            usdt = bal.get("USDT") or {}
            if not isinstance(usdt, dict):
                return t, a
            # total
            try:
                t = float(usdt.get("total") or 0.0)
            except Exception:
                t = 0.0
            # free / available
            try:
                free = usdt.get("free")
                if free is None:
                    free = usdt.get("total")
                a = float(free or 0.0)
            except Exception:
                a = t
            return t, a

        # 1) UTA (UNIFIED) 우선
        try:
            bal = self.exchange.fetch_balance({"accountType": "UNIFIED"})
            t, a = _extract_usdt(bal)
            total, available = t, a
        except Exception as exc:
            logger.warning(
                "[ExchangeAPI] fetch_balance(accountType='UNIFIED') failed: %s", exc
            )

        # 2) 필요 시 일반 fetch_balance() fallback
        if total == 0.0 and available == 0.0:
            try:
                bal2 = self.exchange.fetch_balance()
                t2, a2 = _extract_usdt(bal2)
                if t2 or a2:
                    total, available = t2, a2
            except Exception as exc:
                logger.warning(
                    "[ExchangeAPI] fetch_balance() fallback failed: %s", exc
                )

        logger.info(
            "[ExchangeAPI] balance snapshot: total=%.8f, free=%.8f",
            float(total or 0.0),
            float(available or 0.0),
        )

        return {
            "total": float(total or 0.0),
            "available": float(available or 0.0),
        }

    def get_positions(self) -> Dict[str, Dict[str, float]]:
        """
        현재 LONG / SHORT 포지션 정보 (수량, 평균 진입가)를 Bybit UTA 헤지 모드 기준으로 반환한다.

        반환 형태:
            {
              "LONG":  {"qty": float, "avg_price": float},
              "SHORT": {"qty": float, "avg_price": float},
            }
        """
        result: Dict[str, Dict[str, float]] = {
            "LONG": {"qty": 0.0, "avg_price": 0.0},
            "SHORT": {"qty": 0.0, "avg_price": 0.0},
        }

        try:
            # 마켓 정보는 한 번만 로드되도록 _ensure_markets_loaded 사용
            self._ensure_markets_loaded()

            # Bybit UTA: symbol 필터 + category 지정
            positions = self.exchange.fetch_positions(
                [self.symbol],
                params={"category": BYBIT_CATEGORY},
            )
            logger.info("[ExchangeAPI] raw positions: %s", positions)
        except Exception as exc:
            logger.warning("[ExchangeAPI] get_positions error (fetch_positions): %s", exc)
            return result

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

                # 수량 (contracts / size / info.size 중 하나 사용)
                contracts = pos.get("contracts")
                if contracts is None:
                    contracts = pos.get("size") or info.get("size")
                qty = float(contracts or 0.0)
                if qty <= 0.0:
                    continue

                # 방향 (side / info.side / info.positionIdx)
                side = (pos.get("side") or info.get("side") or "").lower()
                if not side:
                    # Bybit positionIdx: 1=long, 2=short
                    idx = str(info.get("positionIdx", ""))
                    if idx == "1":
                        side = "long"
                    elif idx == "2":
                        side = "short"

                # 평균 진입가
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
            except Exception as exc:
                logger.warning("[ExchangeAPI] get_positions parse error: %s", exc)

        result["LONG"]["qty"] = float(long_qty)
        result["LONG"]["avg_price"] = float(long_avg)
        result["SHORT"]["qty"] = float(short_qty)
        result["SHORT"]["avg_price"] = float(short_avg)

        logger.info(
            "[ExchangeAPI] mapped positions: LONG={qty=%.6f, avg=%.2f}, SHORT={qty=%.6f, avg=%.2f}",
            result["LONG"]["qty"],
            result["LONG"]["avg_price"],
            result["SHORT"]["qty"],
            result["SHORT"]["avg_price"],
        )

        return result

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        현재 심볼의 미체결 주문 목록.
        """
        if self.dry_run:
            return []
        try:
            return self.exchange.fetch_open_orders(
                symbol=self.symbol,
                params={"category": BYBIT_CATEGORY},
            )
        except Exception as e:
            logger.error(f"[ExchangeAPI] OpenOrders Fail: {e}")
            return []

    # ==========================================================
    # Setup (Cross 7x + Hedge Mode 고정)
    # ==========================================================

    def _safe_request(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"[ExchangeAPI] Setup Error ({func.__name__}): {e}")
            return None

    def set_leverage_and_mode(self) -> None:
        """
        v10.1 명세:
        - Cross 7x
        - Hedge Mode
        를 Bybit 계정/심볼에 강제 세팅한다.
        """
        if self.dry_run:
            logger.info("[ExchangeAPI] DRY_RUN - set_leverage_and_mode 스킵")
            return

        # 마진 모드: Cross
        self._safe_request(
            self.exchange.set_margin_mode,
            "cross",
            self.symbol,
            params={"category": BYBIT_CATEGORY},
        )

        # 레버리지 설정 (Cross 7x)
        self._safe_request(
            self.exchange.set_leverage,
            self.leverage,
            self.symbol,
            params={"category": BYBIT_CATEGORY},
        )

        # 포지션 모드: Hedge Mode
        self._safe_request(
            self.exchange.set_position_mode,
            hedged=POSITION_MODE_HEDGED,
            symbol=self.symbol,
        )

    # ==========================================================
    # OHLCV
    # ==========================================================

    def fetch_ohlcv(self, timeframe: str = "1m", limit: int = 200):
        """
        OHLCV 데이터 (캔들) 조회.
        """
        try:
            self._ensure_markets_loaded()
            return self.exchange.fetch_ohlcv(
                self.symbol,
                timeframe=timeframe,
                limit=limit,
                params={"category": BYBIT_CATEGORY},
            )
        except Exception as e:
            logger.error(f"[ExchangeAPI] fetch_ohlcv fail: {e}")
            return []

    # ==========================================================
    # 내부 side/positionIdx 매핑 유틸
    # ==========================================================

    def _side_int_to_ccxt(
        self,
        side: int,
    ) -> Tuple[str, int, bool]:
        """
        side 코드 ↔ Bybit/ccxt 파라미터 매핑.

        side:
          1: Open LONG
          2: Close SHORT
          3: Open SHORT
          4: Close LONG
        """
        # 방향 (buy/sell)
        side_str = "buy" if side in (1, 2) else "sell"

        # positionIdx: 1=LONG, 2=SHORT
        position_idx = 1 if side in (1, 4) else 2

        # reduceOnly 여부
        reduce_only = side in (2, 4)

        return side_str, position_idx, reduce_only

    # ==========================================================
    # 주문: v10.1 정밀도 규칙 적용 (qty 기반)
    # ==========================================================

    def _prepare_price_and_qty_from_qty(
        self,
        price: float,
        qty: float,
    ) -> Tuple[float, float]:
        """
        v10.1 정밀도 규칙 (qty 기반):

        - price: price_floor_to_tick 로 tickSize 기준 floor
        - qty  : 이미 GridLogic 등에서 calc_contract_qty 로 한 번 계산된 값이지만,
                 여기서도 notional = price * qty 를 기준으로 다시 calc_contract_qty 를 거쳐
                 minQty/stepSize 를 재검증한다 (이중 floor는 안전 측면에서 OK).
        """
        info = SYMBOL_INFO.get(self.symbol, {})
        tick_size = float(info.get("tick_size", 0.0))

        floored_price = price_floor_to_tick(
            price,
            tick_size=tick_size,
            symbol=self.symbol,
        )

        # qty → notional(USDT) → 다시 calc_contract_qty 로 step/minQty 재검증
        notional = float(qty) * floored_price
        checked_qty = calc_contract_qty(
            usdt_amount=notional,
            price=floored_price,
            symbol=self.symbol,
            dry_run=self.dry_run,
        )

        return floored_price, checked_qty

    def place_limit_order(
        self,
        side: int,
        price: float,
        qty: float,
    ) -> str:
        """
        v10.1 기준 Limit 주문 (qty 기반).

        - side : 1/2/3/4 (Open Long / Close Short / Open Short / Close Long)
        - price: 희망 주문 가격 (raw), 내부에서 tickSize 기준 floor 처리
        - qty  : BTC 수량 (GridLogic 등에서 calc_contract_qty 로 계산된 값)

        내부 규칙:
        - price_floor_to_tick() 로 가격 floor
        - qty 는 notional = price * qty 를 기준으로 다시 calc_contract_qty() 를 거쳐
          minQty/stepSize 재검증 (이중 floor는 보수적 안전장치)
        """
        if self.dry_run:
            logger.info(
                "[DRY_RUN] place_limit_order(side=%s, price=%.2f, qty=%.6f)",
                side,
                price,
                qty,
            )
            return "dry_id"

        try:
            side_str, position_idx, reduce_only = self._side_int_to_ccxt(side)

            floored_price, final_qty = self._prepare_price_and_qty_from_qty(price, qty)

            if final_qty <= 0.0:
                logger.warning(
                    "[ExchangeAPI] place_limit_order: qty=0 (minQty/stepSize 미만) → 주문 스킵 (req=%.6f)",
                    qty,
                )
                return ""

            params: Dict[str, Any] = {
                "category": BYBIT_CATEGORY,
                "positionIdx": position_idx,
            }
            if reduce_only:
                params["reduceOnly"] = True

            order = self.exchange.create_order(
                self.symbol,
                type="limit",
                side=side_str,
                amount=final_qty,
                price=floored_price,
                params=params,
            )
            order_id = str(order.get("id", ""))
            logger.info(
                "[ExchangeAPI] Limit Order Created: id=%s side=%s qty=%.6f price=%.2f",
                order_id,
                side_str,
                final_qty,
                floored_price,
            )
            return order_id
        except Exception as e:
            logger.error(f"[ExchangeAPI] Limit Order Fail: {e}")
            return ""

    def place_market_order(
        self,
        side: int,
        qty: float,
        *,
        price_for_calc: Optional[float] = None,
    ) -> str:
        """
        v10.1 기준 Market 주문 (qty 기반).

        - side : 1/2/3/4 (Open Long / Close Short / Open Short / Close Long)
        - qty  : BTC 수량
        - price_for_calc : qty 검증용 가격 (None 이면 get_ticker() 사용)

        내부 규칙:
        - price_for_calc 기준 notional = price * qty 계산
        - calc_contract_qty() 로 minQty/stepSize 재검증 후 그 qty 로 Market 주문
        """
        if self.dry_run:
            logger.info(
                "[DRY_RUN] place_market_order(side=%s, qty=%.6f, price_for_calc=%s)",
                side,
                qty,
                price_for_calc,
            )
            return "dry_id"

        try:
            side_str, position_idx, reduce_only = self._side_int_to_ccxt(side)

            price_used = (
                float(price_for_calc)
                if price_for_calc is not None and float(price_for_calc) > 0.0
                else self.get_ticker()
            )
            if price_used <= 0.0:
                logger.error(
                    "[ExchangeAPI] place_market_order: price_for_calc 불가 (ticker=0) → 주문 스킵"
                )
                return ""

            notional = float(qty) * price_used
            final_qty = calc_contract_qty(
                usdt_amount=notional,
                price=price_used,
                symbol=self.symbol,
                dry_run=self.dry_run,
            )

            if final_qty <= 0.0:
                logger.warning(
                    "[ExchangeAPI] place_market_order: qty=0 (minQty/stepSize 미만) → 주문 스킵 (req=%.6f)",
                    qty,
                )
                return ""

            params: Dict[str, Any] = {
                "category": BYBIT_CATEGORY,
                "positionIdx": position_idx,
            }
            if reduce_only:
                params["reduceOnly"] = True

            order = self.exchange.create_order(
                self.symbol,
                type="market",
                side=side_str,
                amount=final_qty,
                price=None,
                params=params,
            )
            order_id = str(order.get("id", ""))
            logger.info(
                "[ExchangeAPI] Market Order Created: id=%s side=%s qty=%.6f (px=%.2f)",
                order_id,
                side_str,
                final_qty,
                price_used,
            )
            return order_id
        except Exception as e:
            logger.critical(f"[ExchangeAPI] Market Order Fail: {e}")
            return ""

    # ==========================================================
    # 주문 취소 / 상태 조회
    # ==========================================================

    def cancel_order(self, order_id: str) -> None:
        """
        특정 주문 취소.
        """
        if self.dry_run:
            logger.info("[DRY_RUN] cancel_order(%s)", order_id)
            return
        self._safe_request(
            self.exchange.cancel_order,
            order_id,
            self.symbol,
            params={"category": BYBIT_CATEGORY},
        )

    def get_order_status(self, order_id: str) -> Dict[str, float]:
        """
        filled 수량(Deal Volume)만 단순히 조회해서 리턴.

        반환:
            {"dealVol": float}
        """
        if self.dry_run:
            # DRY_RUN 에서는 충분히 큰 값으로 가정 (상위 로직 검증용)
            return {"dealVol": 999999.0}

        try:
            order = self.exchange.fetch_order(
                order_id,
                self.symbol,
                params={"category": BYBIT_CATEGORY},
            )
            filled = float(order.get("filled", 0.0))
            return {"dealVol": filled}
        except Exception as e:
            logger.error(f"[ExchangeAPI] get_order_status fail: {e}")
            return {"dealVol": 0.0}


# 전역 인스턴스 및 상수 (기존 호환용)
exchange = ExchangeAPI()

SIDE_BUY = "Buy"
SIDE_SELL = "Sell"

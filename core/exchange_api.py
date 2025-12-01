import ccxt
import time
from typing import Dict, Any
from config import BYBIT_API_KEY, BYBIT_SECRET_KEY, SYMBOL, LEVERAGE, DRY_RUN
from utils.logger import logger

class BybitAPI:
    def __init__(self):
        self.symbol = SYMBOL
        options = {
            'apiKey': BYBIT_API_KEY,
            'secret': BYBIT_SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'linear',
                'adjustForTimeDifference': True,
                'createMarketBuyOrderRequiresPrice': False,
            }
        }
        self.exchange = ccxt.bybit(options)
        
        if DRY_RUN:
            logger.warning("🧪 [DRY_RUN] 모드로 실행 중입니다.")
        else:
            logger.warning("🚀 [REAL] 실전 매매 모드입니다.")

    def _safe_request(self, func, *args, **kwargs):
        """API 요청 래퍼 (에러 핸들링 공통화)"""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"API Error ({func.__name__}): {e}")
            return None

    def get_ticker(self) -> float:
        try:
            ticker = self.exchange.fetch_ticker(self.symbol, params={'category': 'linear'})
            return float(ticker['last'])
        except Exception as e:
            logger.error(f"Ticker Fail: {e}")
            return 0.0

    def get_balance(self) -> Dict[str, float]:
        try:
            # Unified Account 우선 시도
            bal = self.exchange.fetch_balance({'type': 'unified'})
            if 'USDT' in bal:
                return {
                    'total': float(bal['USDT'].get('total', 0.0)),
                    'available': float(bal['USDT'].get('free', 0.0))
                }
            
            # Fallback to Swap
            bal = self.exchange.fetch_balance({'type': 'swap'})
            if 'USDT' in bal:
                return {
                    'total': float(bal['USDT'].get('total', 0.0)),
                    'available': float(bal['USDT'].get('free', 0.0))
                }
            return {'total': 0.0, 'available': 0.0}
        except Exception:
            return {'total': 0.0, 'available': 0.0}

    def get_positions(self) -> Dict[str, Dict]:
        res = {'LONG': {'qty': 0.0, 'avg_price': 0.0}, 'SHORT': {'qty': 0.0, 'avg_price': 0.0}}
        try:
            positions = self.exchange.fetch_positions([self.symbol], {'category': 'linear'})
            for p in positions:
                # 심볼명 매칭 (BTC/USDT:USDT 대응)
                if self.symbol.replace('/','') not in p['symbol'].replace('/',''):
                    continue
                
                side = p['side'].upper()
                qty = float(p['contracts'])
                avg = float(p['entryPrice'] or 0)
                
                if side in res:
                    res[side] = {'qty': qty, 'avg_price': avg}
            return res
        except Exception as e:
            logger.error(f"Pos Error: {e}")
            return res

    def set_leverage(self):
        """초기 설정: 레버리지 및 헤지 모드"""
        self._safe_request(self.exchange.set_leverage, LEVERAGE, self.symbol, params={'category': 'linear'})
        self._safe_request(self.exchange.set_position_mode, hedged=True, symbol=self.symbol)

    def fetch_ohlcv(self, timeframe='1m', limit=50):
        res = self._safe_request(self.exchange.fetch_ohlcv, self.symbol, timeframe, limit=limit, params={'category': 'linear'})
        return res if res else []

    def place_limit_order(self, side: int, price: float, qty: float) -> str:
        if DRY_RUN: return "dry_id"
        try:
            # Side: 1(OpenL), 2(CloseS), 3(OpenS), 4(CloseL)
            # CCXT: side='buy'/'sell', positionIdx=1(L)/2(S), reduceOnly
            side_str = 'buy' if side in [1, 2] else 'sell'
            params = {'category': 'linear'}
            
            if side in [1, 4]: params['positionIdx'] = 1  # Hedge Long
            else: params['positionIdx'] = 2  # Hedge Short
            
            if side in [2, 4]: params['reduceOnly'] = True

            order = self.exchange.create_order(self.symbol, 'limit', side_str, qty, price, params)
            return str(order['id'])
        except Exception as e:
            logger.error(f"Limit Order Fail: {e}")
            return ""

    def place_market_order(self, side: int, qty: float) -> str:
        if DRY_RUN: return "dry_id"
        try:
            side_str = 'buy' if side in [1, 2] else 'sell'
            params = {'category': 'linear'}
            
            if side in [1, 4]: params['positionIdx'] = 1
            else: params['positionIdx'] = 2
            
            if side in [2, 4]: params['reduceOnly'] = True

            order = self.exchange.create_order(self.symbol, 'market', side_str, qty, None, params)
            return str(order['id'])
        except Exception as e:
            logger.critical(f"Market Order Fail: {e}")
            return ""

    def cancel_order(self, order_id: str):
        if DRY_RUN: return
        self._safe_request(self.exchange.cancel_order, order_id, self.symbol, params={'category': 'linear'})

    def get_order_status(self, order_id: str) -> Dict:
        if DRY_RUN: return {'dealVol': 999999.0}
        try:
            order = self.exchange.fetch_order(order_id, self.symbol, params={'category': 'linear'})
            return {'dealVol': float(order.get('filled', 0.0))}
        except Exception:
            # 주문을 못 찾으면(이미 체결됨 or 취소됨), 0.0 리턴하여 안전하게 처리
            return {'dealVol': 0.0}

exchange = BybitAPI()

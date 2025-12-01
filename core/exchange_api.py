import ccxt
import time
import hmac
import hashlib
from typing import Dict, Any, Optional, Tuple
from config import BYBIT_API_KEY, BYBIT_SECRET_KEY, SYMBOL, LEVERAGE, DRY_RUN
from utils.logger import logger

class BybitAPI:
    def __init__(self):
        self.symbol = SYMBOL
        self.secret = BYBIT_SECRET_KEY
        options = {
            'apiKey': BYBIT_API_KEY, 'secret': BYBIT_SECRET_KEY, 'enableRateLimit': True,
            'options': {'defaultType': 'linear', 'adjustForTimeDifference': True, 'createMarketBuyOrderRequiresPrice': False}
        }
        self.exchange = ccxt.bybit(options)
        if DRY_RUN: logger.warning("🧪 [DRY_RUN] 모드")
        else: logger.warning("🚀 [REAL] Trade Mode")

    # [추가됨] 서버 시간 조회 (인증 시간 오차 해결용)
    def get_server_time(self) -> int:
        """UTC 밀리초 단위 서버 시간"""
        try:
            return self.exchange.fetch_time()
        except Exception as e:
            logger.error(f"Server Time Fetch Failed: {e}")
            return int(time.time() * 1000) # 실패 시 로컬 시간 반환

    # [수정] WS 인증용 서명 헬퍼
    def generate_auth_signature(self, expires: int) -> Tuple[str, str]: 
        param_string = f"GET/realtime{expires}"
        signature = hmac.new(self.secret.encode('utf-8'), param_string.encode('utf-8'), hashlib.sha256).hexdigest()
        return param_string, signature
    
    # ... (rest of get_ticker, get_balance, etc. remains unchanged) ...
    def get_ticker(self) -> float:
        try: return float(self.exchange.fetch_ticker(self.symbol, params={'category': 'linear'})['last'])
        except Exception as e:
            logger.error(f"Ticker Fail: {e}"); return 0.0

    def get_balance(self) -> Dict[str, float]:
        try:
            bal = self.exchange.fetch_balance({'type': 'unified'})
            if 'USDT' in bal: return {'total': float(bal['USDT'].get('total', 0)), 'available': float(bal['USDT'].get('free', 0))}
            bal_swap = self.exchange.fetch_balance({'type': 'swap'})
            if 'USDT' in bal_swap: return {'total': float(bal_swap['USDT'].get('total', 0)), 'available': float(bal_swap['USDT'].get('free', 0))}
            return {'total': 0.0, 'available': 0.0}
        except Exception: return {'total': 0.0, 'available': 0.0}

    def get_positions(self) -> Dict[str, Dict]:
        res = {'LONG': {'qty': 0.0, 'avg_price': 0.0}, 'SHORT': {'qty': 0.0, 'avg_price': 0.0}}
        try:
            positions = self.exchange.fetch_positions([self.symbol], {'category': 'linear'})
            for p in positions:
                if self.symbol not in p['symbol'].replace('/',''): continue
                side = p['side'].upper()
                if side in res: res[side] = {'qty': float(p['contracts']), 'avg_price': float(p['entryPrice'] or 0)}
            return res
        except Exception: return res

    def set_leverage(self):
        self._safe_request(self.exchange.set_leverage, LEVERAGE, self.symbol, params={'category': 'linear'})
        self._safe_request(self.exchange.set_position_mode, hedged=True, symbol=self.symbol)
        
    def _safe_request(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Setup Error ({func.__name__}): {e}")
            return None

    def fetch_ohlcv(self, timeframe='1m', limit=50):
        try: return self.exchange.fetch_ohlcv(self.symbol, timeframe, limit=limit, params={'category': 'linear'})
        except Exception: return []

    def place_limit_order(self, side: int, price: float, qty: float) -> str:
        if DRY_RUN: return "dry_id"
        try:
            side_str = 'buy' if side in [1, 2] else 'sell'
            params = {'category': 'linear', 'positionIdx': 1 if side in [1, 4] else 2}
            if side in [2, 4]: params['reduceOnly'] = True
            return str(self.exchange.create_order(self.symbol, 'limit', side_str, qty, price, params)['id'])
        except Exception as e: logger.error(f"Limit Fail: {e}"); return ""

    def place_market_order(self, side: int, qty: float) -> str:
        if DRY_RUN: return "dry_id"
        try:
            side_str = 'buy' if side in [1, 2] else 'sell'
            params = {'category': 'linear', 'positionIdx': 1 if side in [1, 4] else 2}
            if side in [2, 4]: params['reduceOnly'] = True
            return str(self.exchange.create_order(self.symbol, 'market', side_str, qty, None, params)['id'])
        except Exception as e: logger.critical(f"Market Order Fail: {e}"); return ""

    def cancel_order(self, order_id: str):
        if DRY_RUN: return
        self._safe_request(self.exchange.cancel_order, order_id, self.symbol, params={'category': 'linear'})

    def get_order_status(self, order_id: str) -> Dict:
        if DRY_RUN: return {'dealVol': 999999.0}
        try: return {'dealVol': float(self.exchange.fetch_order(order_id, self.symbol, params={'category': 'linear'}).get('filled', 0.0))}
        except Exception: return {'dealVol': 0.0}

exchange = BybitAPI()

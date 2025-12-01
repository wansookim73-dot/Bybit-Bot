import ccxt
import time
from typing import Dict, Any
from config import BYBIT_API_KEY, BYBIT_SECRET_KEY, SYMBOL, LEVERAGE, DRY_RUN
from utils.logger import logger

class BybitAPI:
    def __init__(self):
        self.symbol = SYMBOL
        options = {
            'apiKey': BYBIT_API_KEY, 'secret': BYBIT_SECRET_KEY, 'enableRateLimit': True,
            'options': {'defaultType': 'linear', 'adjustForTimeDifference': True, 'createMarketBuyOrderRequiresPrice': False}
        }
        self.exchange = ccxt.bybit(options)
        if DRY_RUN: logger.warning("🧪 [DRY_RUN] Mode")
        else: logger.warning("🚀 [REAL] Trade Mode")

    def get_ticker(self) -> float:
        try: return float(self.exchange.fetch_ticker(self.symbol, params={'category': 'linear'})['last'])
        except Exception as e:
            logger.error(f"Ticker Fail: {e}")
            return 0.0

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
        
        # [수정] 포지션 업데이트 지연을 위한 잠시 대기 (최대 0.5초)
        time.sleep(0.05) 
        
        try:
            positions = self.exchange.fetch_positions([self.symbol], {'category': 'linear'})
            for p in positions:
                if self.symbol not in p['symbol'].replace(':USDT', ''): continue
                side = p['side'].upper()
                if side in res: res[side] = {'qty': float(p['contracts']), 'avg_price': float(p['entryPrice'] or 0)}
            return res
        except Exception as e:
            logger.error(f"Pos Error: {e}")
            return res

    def set_leverage(self):
        try: self.exchange.set_leverage(LEVERAGE, self.symbol, params={'category': 'linear'})
        except: pass
        try: self.exchange.set_position_mode(hedged=True, symbol=self.symbol)
        except: pass

    def fetch_ohlcv(self, timeframe='1m', limit=50):
        try: return self.exchange.fetch_ohlcv(self.symbol, timeframe, limit=limit, params={'category': 'linear'})
        except: return []

    def place_limit_order(self, side: int, price: float, qty: float) -> str:
        if DRY_RUN: return "dry_id"
        try:
            side_str = 'buy' if side in [1, 2] else 'sell'
            params = {'category': 'linear', 'positionIdx': 1 if side in [1, 4] else 2}
            if side in [2, 4]: params['reduceOnly'] = True
            order = self.exchange.create_order(self.symbol, 'limit', side_str, qty, price, params)
            return str(order['id'])
        except Exception as e: logger.error(f"Limit Fail: {e}"); return ""

    def place_market_order(self, side: int, qty: float) -> str:
        if DRY_RUN: return "dry_id"
        try:
            side_str = 'buy' if side in [1, 2] else 'sell'
            params = {'category': 'linear', 'positionIdx': 1 if side in [1, 4] else 2}
            if side in [2, 4]: params['reduceOnly'] = True
            return str(self.exchange.create_order(self.symbol, 'market', side_str, qty, None, params)['id'])
        except Exception as e: logger.critical(f"Market Fail: {e}"); return ""

    def cancel_order(self, order_id: str):
        if DRY_RUN: return
        self._safe_request(self.exchange.cancel_order, order_id, self.symbol, params={'category': 'linear'})

    def get_order_status(self, order_id: str) -> Dict:
        if DRY_RUN: return {'dealVol': 999999.0}
        try:
            order = self.exchange.fetch_order(order_id, self.symbol, params={'category': 'linear'})
            return {'dealVol': float(order.get('filled', 0.0))}
        except Exception: return {'dealVol': 0.0}

exchange = BybitAPI()

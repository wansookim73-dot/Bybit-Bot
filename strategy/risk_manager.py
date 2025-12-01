import pandas as pd
from datetime import datetime, timedelta
from utils.logger import logger

class RiskManager:
    def __init__(self): self.pause_until = None

    def check_macro_guard(self):
        # 뉴스 필터는 필요 시 여기에 날짜/시간 추가
        return False

    def check_circuit_breaker(self, df):
        """[명세서 6.2] 가격 0.6% or 거래량 4배"""
        if len(df) < 21: return False
        last = df.iloc[-1]
        
        # 1. 가격 변동폭
        volatility = (last['high'] - last['low']) / last['open']
        if volatility >= 0.006:
            self.pause_until = datetime.now() + timedelta(minutes=15)
            logger.warning(f"🚨 Circuit Breaker: High Volatility ({volatility*100:.2f}%)")
            return True
            
        # 2. 거래량 폭발
        vol_ma = df['volume'].iloc[-21:-1].mean()
        if vol_ma > 0 and last['volume'] >= (vol_ma * 4):
            self.pause_until = datetime.now() + timedelta(minutes=15)
            logger.warning(f"🚨 Circuit Breaker: Volume Spike (x{last['volume']/vol_ma:.1f})")
            return True
            
        return False

    def check_resume_conditions(self, df, gap):
        """[명세서 6.3] 재개 조건"""
        if df.empty: return False
        # 최근 3분 캔들 안정
        for _, c in df.iloc[-3:].iterrows():
            if (c['high']-c['low']) >= gap: return False
        
        # 거래량 안정 (1.5배 미만)
        last_vol = df.iloc[-1]['volume']
        vol_ma = df.iloc[-21:-1].mean()
        if vol_ma > 0 and last_vol >= (vol_ma * 1.5): return False
        
        return True

    def get_status(self):
        if self.pause_until and datetime.now() < self.pause_until: return "PAUSE"
        return "NORMAL"

risk_manager = RiskManager()

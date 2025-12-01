import sys, io, os, asyncio
import pandas as pd
from datetime import datetime

# [1] 환경 및 인코딩 강제 설정 (AWS 서버 언어 문제 방지)
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8')
os.environ["PYTHONIOENCODING"] = "utf-8"

from config import *
from core.exchange_api import exchange
from core.order_manager import order_manager
from core.state_manager import bot_state
from strategy.grid_logic import grid_logic
from strategy.escape_logic import escape_logic
from strategy.risk_manager import risk_manager
from utils.logger import logger
from utils.calculator import to_int_price, to_int_usdt

def fetch_candles(tf, limit=50):
    raw = exchange.fetch_ohlcv(tf, limit)
    if not raw: return pd.DataFrame()
    df = pd.DataFrame(raw, columns=['time','open','high','low','close','volume'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    return df

def calculate_atr(df, p=14):
    if len(df) < p+1: return 0.0
    tr = pd.concat([df['high']-df['low'], (df['high']-df['close'].shift()).abs(), (df['low']-df['close'].shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(p).mean().iloc[-1])

async def main():
    logger.info("🤖 Bybit Wave Bot v10.1 FINAL Starting...")
    
    exchange.set_leverage()
    await asyncio.sleep(2) # API 상태 안정화

    # 1. 초기화 (Start)
    if bot_state.get("mode") == "STARTUP" or bot_state.get("p_gap") < MIN_GRID_GAP:
        price = exchange.get_ticker()
        if price <= 0: logger.error("초기 가격 조회 실패"); return

        df_4h = fetch_candles('4h', 100) 
        atr = calculate_atr(df_4h)
        if atr == 0: atr = price * 0.01
        gap = max(atr * ATR_MULTIPLIER, MIN_GRID_GAP)
        
        bot_state.update("p_center", to_int_price(price))
        bot_state.update("p_gap", to_int_price(gap))
        bot_state.update("total_balance", INITIAL_SEED_USDT)
        
        if bot_state.get("mode") == "STARTUP":
            bot_state.update("mode", "NORMAL")
            bot_state.update("last_long_line", None) # 초기화 시 Last Line 초기화
            bot_state.update("last_short_line", None)
        
        logger.info(f"✅ Init: Center={price}, Gap={gap:.1f}")

    last_pos = exchange.get_positions()

    while True:
        try:
            current_price = exchange.get_ticker()
            if current_price <= 0: await asyncio.sleep(1); continue
                
            curr_pos = exchange.get_positions()
            bal = exchange.get_balance()
            df_1m = fetch_candles('1m', 30)
            
            mode = bot_state.get("mode")
            center = bot_state.get("p_center")
            gap = bot_state.get("p_gap")
            if gap < MIN_GRID_GAP: gap = MIN_GRID_GAP
            
            line = grid_logic.calculate_line_index(current_price, center, gap)
            total_bal = bot_state.get("total_balance", 600)

            logger.info(f"[Status] Mode={mode}, Price={current_price}, Line={line}")

            # Priority 0: Escape
            breakout = escape_logic.check_breakout(line)
            if mode == "NORMAL" and breakout:
                logger.warning(f"🚨 Escape: {breakout}")
                bot_state.update("mode", "ESCAPE")
                mode = "ESCAPE"
                
                hedge = escape_logic.calculate_hedge_size(breakout, curr_pos, bal['available'])
                side = 1 if breakout == "UP" else 3
                if hedge > 0:
                    await order_manager.execute_atomic_order(side, current_price, hedge, ESCAPE_TIMEOUT_SEC, True)

            if mode == "ESCAPE":
                # Fakeout, Targeting, Reset 로직은 이전과 동일
                pass

            # Priority 1: Risk
            if mode != "ESCAPE":
                if risk_manager.get_status() == "PAUSE":
                    if risk_manager.check_resume_conditions(df_1m, gap): bot_state.update("mode", "NORMAL")
                    else: await asyncio.sleep(60); continue
                elif risk_manager.check_circuit_breaker(df_1m):
                    bot_state.update("mode", "PAUSE"); continue

            # Priority 2: Grid
            if mode == "NORMAL":
                l_q = curr_pos["LONG"]["qty"]
                s_q = curr_pos["SHORT"]["qty"]
                unit = to_int_usdt((total_bal * MAX_ALLOCATION_RATE) / GRID_SPLIT_COUNT * LEVERAGE)

                # 0. 초기 진입 (반복 방지 포함)
                # [핵심 수정] last_long_line이 None일 때만 Start-up 발동
                if l_q == 0 and s_q == 0 and abs(line) <= 7 and bot_state.get("last_long_line") is None:
                    logger.info(f"🆕 Start-up Entry (Initial Entry)")
                    
                    # Long
                    await order_manager.execute_atomic_order(1, current_price, unit, GRID_TIMEOUT_SEC, False)
                    bot_state.update("last_long_line", line) # ★ 앵커 설정
                    
                    # Short
                    await order_manager.execute_atomic_order(3, current_price, unit, GRID_TIMEOUT_SEC, False)
                    bot_state.update("last_short_line", line) # ★ 앵커 설정

                else:
                    last_l = bot_state.get("last_long_line")
                    last_s = bot_state.get("last_short_line")
                    
                    # Scale-in
                    entry = grid_logic.check_entry_signal(line, curr_pos, center, gap, last_l, last_s, total_bal)
                    if entry:
                        side = 1 if entry['action'] == "OPEN_LONG" else 3
                        await order_manager.execute_atomic_order(side, entry['price'], unit, GRID_TIMEOUT_SEC, False)
                        if side == 1: bot_state.update("last_long_line", line)
                        else: bot_state.update("last_short_line", line)
                    
                    # TP
                    tp = grid_logic.check_tp_signal(line, curr_pos, center, gap)
                    if tp:
                        side = 4 if tp['action'] == "CLOSE_LONG" else 2
                        await order_manager.execute_atomic_order(side, tp['price'], unit, GRID_TIMEOUT_SEC, False)

            last_pos = curr_pos
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Loop Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

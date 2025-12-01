import sys, io, os, asyncio
import pandas as pd
from datetime import datetime

# 인코딩 강제 설정 (AWS 서버 언어 문제 방지)
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
    logger.info("🤖 Bybit Wave Bot v10.5 FINAL Starting...")
    exchange.set_leverage()
    await asyncio.sleep(2)

    if bot_state.get("mode") == "STARTUP" or bot_state.get("p_gap") < MIN_GRID_GAP:
        price = exchange.get_ticker()
        if price > 0:
            df_4h = fetch_candles('4h', 100) 
            atr = calculate_atr(df_4h)
            if atr == 0: atr = price * 0.01
            gap = max(atr * ATR_MULTIPLIER, MIN_GRID_GAP)
            
            bot_state.update("p_center", to_int_price(price))
            bot_state.update("p_gap", to_int_price(gap))
            bot_state.update("total_balance", INITIAL_SEED_USDT)
            
            if bot_state.get("mode") == "STARTUP":
                bot_state.update("mode", "NORMAL")
            
            logger.info(f"✅ Init: Center={to_int_price(price)}, Gap={gap:.1f}")

    last_pos = exchange.get_positions()

    while True:
        try:
            current_price = exchange.get_ticker()
            if current_price <= 0: 
                await asyncio.sleep(1); continue
                
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

            # --- State Cleanup Check (Loop Breaker) ---
            if curr_pos["LONG"]["qty"] == 0 and curr_pos["SHORT"]["qty"] == 0 and bot_state.get("last_long_line") is not None:
                logger.warning("🧹 State Cleanup: Positions are zero, clearing old line memory.")
                bot_state.update("last_long_line", None)
                bot_state.update("last_short_line", None)
            # --- End State Cleanup Check ---


            # Priority 0: Escape
            # ...

            # Priority 1: Risk
            # ...
            
            # Priority 2: Grid
            if mode == "NORMAL":
                l_q = curr_pos["LONG"]["qty"]
                s_q = curr_pos["SHORT"]["qty"]
                unit = to_int_usdt((total_bal * MAX_ALLOCATION_RATE) / GRID_SPLIT_COUNT * LEVERAGE)

                # 0. 초기 진입 (반복 방지 포함)
                if l_q == 0 and s_q == 0 and abs(line) <= 7 and bot_state.get("last_long_line") is None:
                    
                    # [Step 1] 상태 선행 업데이트 (메모리 가드)
                    bot_state.update("last_long_line", line)
                    bot_state.update("last_short_line", line)

                    logger.info(f"🆕 Start-up Entry (Initial Entry)")
                    
                    # Long Order
                    await order_manager.execute_atomic_order(1, current_price, unit, GRID_TIMEOUT_SEC, False)
                    # Short Order
                    await order_manager.execute_atomic_order(3, current_price, unit, GRID_TIMEOUT_SEC, False)
                    
                    # [Final Fix] 포지션 업데이트 대기 루프 (Latency Check)
                    logger.info("⏱️ Waiting for API Position Update...")
                    pos_check_count = 0
                    while pos_check_count < 10: # 최대 10초 대기
                        pos_check = exchange.get_positions()
                        if pos_check["LONG"]["qty"] > 0 or pos_check["SHORT"]["qty"] > 0:
                            logger.info("✅ Position confirmed. Exiting startup sequence.")
                            break
                        await asyncio.sleep(1)
                        pos_check_count += 1

                    continue # 다음 루프로 즉시 이동

                else:
                    last_l = bot_state.get("last_long_line")
                    last_s = bot_state.get("last_short_line")
                    
                    # Scale-in, TP logic (unchanged)
                    entry = grid_logic.check_entry_signal(line, curr_pos, center, gap, last_l, last_s, total_bal)
                    if entry:
                        side = 1 if entry['action'] == "OPEN_LONG" else 3
                        await order_manager.execute_atomic_order(side, entry['price'], unit, GRID_TIMEOUT_SEC, False)
                        if side == 1: bot_state.update("last_long_line", line)
                        else: bot_state.update("last_short_line", line)
                    
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

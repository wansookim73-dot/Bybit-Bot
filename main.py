import sys, io, os, asyncio
import pandas as pd
import time
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
from core.websocket_service import WebSocketService 

# === Shared Memory Structure (WSS Data) ===
SHARED_STATE = {
    "positions": {'LONG': {'qty': 0.0, 'avg_price': 0.0}, 'SHORT': {'qty': 0.0, 'avg_price': 0.0}},
    "balance": {'total': 0.0, 'available': 0.0},
    "positions_last_update": 0.0 
}

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
    logger.info("🤖 Bybit Wave Bot v11.3 (Final WSS Trade Mode) Starting...")
    
    # 1. WebSocket Service 초기화 및 비동기 실행 (가장 먼저 시작)
    ws_service = WebSocketService(SHARED_STATE)
    asyncio.create_task(ws_service.connect_and_listen())
    logger.info("🔌 WebSocket Private Stream initiated.")

    # 2. REST 셋업
    exchange.set_leverage()
    await asyncio.sleep(2) # API 상태 안정화

    # 3. 초기화 (Start)
    if bot_state.get("mode") == "STARTUP" or bot_state.get("p_gap") < MIN_GRID_GAP:
        price = exchange.get_ticker() 
        if price <= 0: logger.critical("초기 가격 조회 실패"); return

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

    while True:
        try:
            # === WSS Data Use Logic (CRITICAL FIX) ===
            current_time = time.time()
            wss_latency = current_time - SHARED_STATE['positions_last_update']
            
            # WSS 데이터가 초기화에 실패했거나 (0.0), 5초 이상 업데이트가 없을 경우 (stale)
            if SHARED_STATE['positions_last_update'] == 0.0 or wss_latency > 5.0: 
                # WSS가 안 될 때만 REST를 사용 (느린 경로)
                curr_pos = exchange.get_positions()
                balance_data = exchange.get_balance()
                loop_wait_time = 1.0
                
                if SHARED_STATE['positions_last_update'] == 0.0:
                     logger.warning("⚠️ WSS Initial Snapshot Failed. Using REST fallback.")
            else:
                # WSS Data is fresh (Fast Path)
                curr_pos = SHARED_STATE["positions"] 
                balance_data = SHARED_STATE["balance"] 
                loop_wait_time = 0.3 # WSS 성공 시 루프 속도 유지
            
            # --- Main Loop Execution ---
            current_price = exchange.get_ticker()
            if current_price <= 0: 
                await asyncio.sleep(1); continue
                
            df_1m = fetch_candles('1m', 30)
            
            mode = bot_state.get("mode")
            center = bot_state.get("p_center")
            gap = bot_state.get("p_gap")
            if gap < MIN_GRID_GAP: gap = MIN_GRID_GAP
            
            line = grid_logic.calculate_line_index(current_price, center, gap)
            total_bal = bot_state.get("total_balance", 600)

            logger.info(f"[Status] Mode={mode}, Price={current_price}, Line={line} | WSS Latency: {wss_latency:.1f}s")

            # --- State Cleanup Check (Loop Breaker) ---
            l_q = curr_pos["LONG"]["qty"]
            s_q = curr_pos["SHORT"]["qty"]

            if l_q == 0 and s_q == 0:
                if bot_state.get("last_long_line") is not None or bot_state.get("last_short_line") is not None:
                    logger.warning("🧹 State Cleanup: Clearing old line memory.")
                    bot_state.update("last_long_line", None)
                    bot_state.update("last_short_line", None)
            # --- End State Cleanup Check ---


            # Priority 0: Escape
            breakout = escape_logic.check_breakout(line)
            if mode == "NORMAL" and breakout:
                logger.warning(f"🚨 Escape: {breakout}")
                bot_state.update("mode", "ESCAPE")
                mode = "ESCAPE"
                
                avail_bal = balance_data['available']
                hedge_usdt = escape_logic.calculate_hedge_size(breakout, curr_pos, avail_bal)
                if hedge_usdt > 0:
                    side = 1 if breakout == "UP" else 3
                    await order_manager.execute_atomic_order(side, current_price, hedge_usdt, ESCAPE_TIMEOUT_SEC, True)

            # Priority 1: Risk
            if mode != "ESCAPE":
                if risk_manager.get_status() == "PAUSE":
                    if risk_manager.check_resume_conditions(df_1m, gap): bot_state.update("mode", "NORMAL")
                    else: await asyncio.sleep(60); continue
                elif risk_manager.check_circuit_breaker(df_1m):
                    bot_state.update("mode", "PAUSE"); continue

            # Priority 2: Grid
            if mode == "NORMAL":
                unit = to_int_usdt((total_bal * MAX_ALLOCATION_RATE) / GRID_SPLIT_COUNT * LEVERAGE)

                # 0. 초기 진입 
                if l_q == 0 and s_q == 0 and abs(line) <= 7 and bot_state.get("last_long_line") is None:
                    
                    bot_state.update("last_long_line", line)
                    bot_state.update("last_short_line", line)

                    logger.info(f"🆕 Start-up Entry (Initial Entry)")
                    
                    await order_manager.execute_atomic_order(1, current_price, unit, GRID_TIMEOUT_SEC, False)
                    await order_manager.execute_atomic_order(3, current_price, unit, GRID_TIMEOUT_SEC, False)
                    
                    await asyncio.sleep(3) 
                    continue

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

            await asyncio.sleep(loop_wait_time)
        
        except Exception as e:
            logger.error(f"Main Loop Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")

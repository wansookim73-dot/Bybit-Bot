import asyncio
import json
import time
import websockets
import hmac
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from core.exchange_api import exchange
from utils.logger import logger
from config import BYBIT_API_KEY, BYBIT_SECRET_KEY, SYMBOL # <--- FIX: Added missing imports from config!

# Bybit Private WS V5 Endpoint
WS_URL = "wss://stream.bybit.com/v5/private"
AUTH_TIMEOUT = 30 

class WebSocketService:
    """
    [프라이빗 WebSocket 서비스]
    - REST API 대신 실시간으로 포지션 및 잔고 업데이트를 수신하여 공유 상태 갱신
    """
    def __init__(self, shared_state: Dict):
        self.ws_url = WS_URL
        self.shared_state = shared_state
        self.api_key = BYBIT_API_KEY # Now accessible
        self.secret = BYBIT_SECRET_KEY # Now accessible
        self.symbol = SYMBOL # Now accessible
        self.running = True
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        
    async def _authenticate(self):
        """인증 메시지 생성 및 전송 (서버 시간 기반)"""
        
        # [핵심 수정] 서버 시간 조회 후 expires 계산
        server_time_ms = exchange.get_server_time()
        if server_time_ms == 0:
            raise RuntimeError("Cannot fetch server time for authentication.")
        
        expires = server_time_ms + 10000 # 서버 시간에 10초 여유 추가
        param_str, signature = exchange.generate_auth_signature(expires)
        
        auth_msg = {
            "op": "auth",
            "args": [self.api_key, expires, signature]
        }
        await self.ws.send(json.dumps(auth_msg))
        logger.info("WS: Sent auth message.")

    async def _subscribe(self):
        """포지션 및 잔고 구독"""
        sub_msg = {"op": "subscribe", "args": ["position", "order", "wallet"]}
        await self.ws.send(json.dumps(sub_msg))
        logger.info("WS: Subscribed to position, order, and wallet streams.")

    def _update_shared_state(self, data: Dict):
        """수신된 데이터를 기반으로 메모리 내 상태 즉시 업데이트"""
        topic = data.get("topic")
        data_list = data.get("data", [])

        if topic == "position":
            for item in data_list:
                if item.get('symbol') != self.symbol: continue
                side = item.get('positionIdx')
                side_str = "LONG" if side == 1 else "SHORT"
                qty = float(item.get('size', 0))
                avg = float(item.get('entryPrice', 0))
                
                self.shared_state["positions"][side_str] = {"qty": qty, "avg_price": avg}
                self.shared_state["positions_last_update"] = time.time()
                
                logger.debug(f"WS: Position {side_str} updated. Qty={qty}")

        elif topic == "wallet":
            for item in data_list:
                if item.get('accountType') == 'UNIFIED':
                    total = float(item.get('equity', 0))
                    available = float(item.get('availableBalance', 0))
                    self.shared_state["balance"] = {"total": total, "available": available}
                    self.shared_state["positions_last_update"] = time.time()
                    logger.debug(f"WS: Balance updated. Available={available}")
                    break
        
        elif topic == "order":
             logger.debug(f"WS: Order execution update.")


    async def connect_and_listen(self):
        """메인 연결 및 리스너 루프"""
        while self.running:
            try:
                logger.info("WS: Connecting to Private Stream...")
                async with websockets.connect(self.ws_url, ping_interval=20, close_timeout=AUTH_TIMEOUT) as websocket:
                    self.ws = websocket
                    
                    await self._authenticate()
                    await self._subscribe()
                    
                    while self.running:
                        message = await asyncio.wait_for(websocket.recv(), timeout=35)
                        data = json.loads(message)
                        
                        self._update_shared_state(data)

            except websockets.exceptions.ConnectionClosedOK:
                logger.info("WS: Connection closed normally. Restarting...")
            except Exception as e:
                logger.error(f"WS Fatal Error: {e.__class__.__name__} - {e}. Reconnecting in 10s.")
                await asyncio.sleep(10)


# 이 클래스는 main.py에서 asyncio.create_task로 실행됩니다.

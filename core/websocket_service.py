import asyncio
import json
import time
import websockets
from typing import Dict, Any

from core.exchange_api import exchange
from utils.logger import logger
from config import BYBIT_API_KEY, BYBIT_SECRET_KEY, SYMBOL

# Bybit WS v5 endpoints
PRIVATE_WS_URL = "wss://stream.bybit.com/v5/private"
PUBLIC_WS_URL  = "wss://stream.bybit.com/v5/public/linear"  # BTCUSDT(USDT perpetual) 기준
AUTH_TIMEOUT = 30

class WebSocketService:
    """
    - Private: position / order / wallet
    - Public : publicTrade.<SYMBOL> 로 가격 업데이트
    """
    def __init__(self, shared_state: Dict):
        self.shared_state = shared_state
        # shared_state 기본 구조 보강
        self.shared_state.setdefault("positions", {
            "LONG": {"qty": 0.0, "avg_price": 0.0},
            "SHORT": {"qty": 0.0, "avg_price": 0.0},
        })
        self.shared_state.setdefault("balance", {"total": 0.0, "available": 0.0})
        self.shared_state.setdefault("positions_last_update", 0.0)
        self.shared_state.setdefault("price", {"value": 0.0, "last_update_ts": 0.0})

        self.api_key = BYBIT_API_KEY
        self.secret = BYBIT_SECRET_KEY
        self.symbol = SYMBOL
        self.running = True
        self._last_price_info_ts = 0.0

    async def start_services(self):
        logger.info("WS Service: All streams started as concurrent tasks.")
        await asyncio.gather(
            self._run_private(),
            self._run_public(),
        )

    async def _run_private(self):
        while self.running:
            try:
                logger.info("WS: Connecting to Private Stream...")
                async with websockets.connect(
                    PRIVATE_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=AUTH_TIMEOUT,
                ) as ws:
                    await self._authenticate(ws)
                    await self._private_subscribe(ws)

                    while self.running:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        # auth/subscribe ack는 topic이 없을 수 있으니 과감히 무시/통과
                        if "success" in data and data.get("op") in ("auth", "subscribe"):
                            logger.info(f"WS (Private) ACK: {data}")
                            continue

                        self._handle_private(data)

            except websockets.exceptions.ConnectionClosedOK:
                logger.info("WS: Private closed normally. Restarting...")
            except Exception as e:
                logger.error(f"WS Fatal Error (Private): {e.__class__.__name__} - {e}. Reconnecting in 10s.")
                await asyncio.sleep(10)

    async def _run_public(self):
        ws_symbol = self.symbol
        ws_symbol = ws_symbol.split(":", 1)[0]  # "BTC/USDT:USDT" -> "BTC/USDT"
        ws_symbol = ws_symbol.replace("/", "")  # "BTC/USDT" -> "BTCUSDT"
        topic = f"publicTrade.{ws_symbol}"
        while self.running:
            try:
                logger.info("WS: Connecting to Public Stream...")
                async with websockets.connect(
                    PUBLIC_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=AUTH_TIMEOUT,
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                    logger.info(f"WS: Subscribed to topics: [{topic}]")

                    while self.running:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        if "success" in data and data.get("op") == "subscribe":
                            logger.info(f"WS (Public) ACK: {data}")
                            continue

                        self._handle_public(data)

            except websockets.exceptions.ConnectionClosedOK:
                logger.info("WS: Public closed normally. Restarting...")
            except Exception as e:
                logger.error(f"WS Fatal Error (Public): {e.__class__.__name__} - {e}. Reconnecting in 10s.")
                await asyncio.sleep(10)

    async def _authenticate(self, ws):
        server_time_ms = exchange.get_server_time()
        if not server_time_ms:
            raise RuntimeError("Cannot fetch server time for authentication.")

        expires = server_time_ms + 10_000
        _, signature = exchange.generate_auth_signature(expires)

        auth_msg = {"op": "auth", "args": [self.api_key, expires, signature]}
        await ws.send(json.dumps(auth_msg))
        logger.info("WS: Sent auth message.")

    async def _private_subscribe(self, ws):
        sub_msg = {"op": "subscribe", "args": ["position", "order", "wallet"]}
        await ws.send(json.dumps(sub_msg))
        logger.info("WS: Subscribed to position, order, and wallet streams.")

    def _handle_private(self, msg: Dict[str, Any]):
        topic = msg.get("topic")
        data_list = msg.get("data") or []

        if topic == "position":
            # Bybit v5 position payload 처리
            # data_list 안에 여러 포지션이 들어올 수 있으므로, 동일 symbol만 필터링
            for item in data_list:
                if item.get("symbol") != self.symbol:
                    continue

                # 수량/평단 안전 파싱
                size_raw = item.get("size", 0) or 0
                entry_raw = item.get("entryPrice", item.get("avgPrice", 0)) or 0

                qty = float(size_raw)
                avg = float(entry_raw)

                # side/positionIdx 로 LONG/SHORT 판별
                side = item.get("side")
                idx = item.get("positionIdx")

                if side == "Buy":
                    side_str = "LONG"
                elif side == "Sell":
                    side_str = "SHORT"
                elif idx == 1:
                    side_str = "LONG"
                elif idx == 2:
                    side_str = "SHORT"
                else:
                    # 알 수 없는 포지션 인덱스는 무시
                    continue

                # 수량이 0이면 평단도 0으로 정리
                if qty == 0:
                    avg = 0.0

                self.shared_state["positions"][side_str] = {"qty": qty, "avg_price": avg}
                self.shared_state["positions_last_update"] = time.time()
                logger.info(f"WS: Position {side_str} updated. Qty={qty}, Avg={avg}")

            logger.info(f"WS: Position snapshot = {self.shared_state['positions']}")

        elif topic == "wallet":
            # Bybit v5 wallet payload는 coin별로 여러 개가 올 수 있으므로 USDT 항목만 선택
            for item in data_list:
                if item.get("coin") == "USDT":
                    total = float(item.get("equity", 0) or 0)
                    avail = float(item.get("availableBalance", 0) or 0)
                    self.shared_state["balance"] = {"total": total, "available": avail}
                    self.shared_state["positions_last_update"] = time.time()
                    logger.info(f"WS: Wallet updated. Total={total}, Avail={avail}")
                    break


        elif topic == "order":
            # 주문 이벤트도 private activity로 간주
            self.shared_state["positions_last_update"] = time.time()
            logger.info("WS: Order update received.")

    def _handle_public(self, msg: Dict[str, Any]):
        topic = msg.get("topic")
        if not topic or not topic.startswith("publicTrade."):
            return

        data_list = msg.get("data") or []
        if not data_list:
            return

        last = data_list[-1]
        p = last.get("p") or last.get("price")
        if p is None:
            return

        price = float(p)
        self.shared_state["price"]["value"] = price
        self.shared_state["price"]["last_update_ts"] = time.time()

        # “살아있음” 확인용: 10초에 한 번만 출력
        now = time.time()
        if now - self._last_price_info_ts > 10:
            self._last_price_info_ts = now
            logger.info(f"WS: Public price tick {self.symbol}={price}")

# v10.1 WaveBot 호환용 래퍼 클래스 (WebsocketService ← WebSocketService)
class WebsocketService(WebSocketService):
    """
    WaveBot 에서 `WebsocketService(self.exchange)` 형태로 호출해도 동작하도록 하는
    얇은 래퍼 클래스.

    - 첫 번째 인자가 dict 이면: shared_state 로 보고 그대로 사용.
    - 첫 번째 인자가 dict 이 아니면(ExchangeAPI 등):
        → 새로운 shared_state dict 를 만들어 WebSocketService 에 전달.
    """

    def __init__(self, *args, **kwargs):
        # 1) shared_state 가 명시적으로 dict 로 들어온 경우
        if args and isinstance(args[0], dict):
            shared_state = args[0]
        elif isinstance(kwargs.get("shared_state"), dict):
            shared_state = kwargs["shared_state"]
        else:
            # 2) WaveBot 처럼 Exchange 인스턴스를 넘긴 경우:
            #    인자를 무시하고 새로운 shared_state dict 생성
            shared_state = {}

        super().__init__(shared_state)

import asyncio
from core.exchange_api import exchange
from utils.logger import logger
from utils.calculator import calc_contract_qty, to_int_price

class OrderManager:
    async def execute_atomic_order(self, side: int, price: float, usdt_amount: float, timeout: float, allow_taker: bool = True):
        """
        주문 실행 마스터 키
        - 5,000불 이상 분할 주문
        - 지정가 -> 대기 -> (취소 or 시장가)
        """
        # [명세서 4] 대규모 주문 5분할
        if usdt_amount >= 5000:
            logger.info(f"[Split Order] Large amount ({usdt_amount}), splitting into 5...")
            split_amt = usdt_amount / 5
            offsets = [0, -0.5, 0.5, -1.0, 1.0] # 가격 분산
            
            for i in range(5):
                adj_price = price + offsets[i]
                await self._single_atomic(side, adj_price, split_amt, timeout, allow_taker)
                await asyncio.sleep(0.2) # API Rate Limit 고려
        else:
            await self._single_atomic(side, price, usdt_amount, timeout, allow_taker)

    async def _single_atomic(self, side: int, price: float, usdt_amount: float, timeout: float, allow_taker: bool):
        qty = calc_contract_qty(usdt_amount, price)
        price = to_int_price(price)
        
        if qty <= 0: return

        mode_msg = "All-in Taker" if allow_taker else "Maker Only"
        logger.info(f"[Order] {mode_msg}: Side={side}, P={price}, Q={qty}")

        # 1. 지정가 주문
        oid = await asyncio.to_thread(exchange.place_limit_order, side, price, qty)
        if not oid:
            # 주문 실패 시
            logger.error("지정가 주문 실패")
            if allow_taker:
                await self._force_market_order(side, qty)
            return

        # 2. 타임아웃 대기
        await asyncio.sleep(timeout)
        
        # 3. 상태 확인
        status = await asyncio.to_thread(exchange.get_order_status, oid)
        filled = status.get('dealVol', 0.0)
        remain = qty - filled

        # 4. 미체결 처리
        if remain > 0:
            # 일단 취소 시도
            await asyncio.to_thread(exchange.cancel_order, oid)
            await asyncio.sleep(0.2) # 취소 전파 대기
            
            if allow_taker:
                logger.info(f"[Timeout] 잔량 {remain:.4f}개 시장가 강제 집행")
                await self._force_market_order(side, remain)
            else:
                logger.info(f"[Timeout] 잔량 {remain:.4f}개 취소 (Maker Only)")
        else:
            logger.info("[Success] 지정가 전량 체결 완료")

    async def _force_market_order(self, side: int, qty: float):
        res = await asyncio.to_thread(exchange.place_market_order, side, qty)
        if res:
            logger.info(f"시장가 성공: Q={qty}")
        else:
            logger.critical(f"시장가 실패! Q={qty}")

order_manager = OrderManager()

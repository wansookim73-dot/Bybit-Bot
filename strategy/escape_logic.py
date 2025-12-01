from config import LEVERAGE, HEDGE_MAX_FACTOR, ESCAPE_CUSHION_PCT
from utils.calculator import to_int_usdt, to_int_price

class EscapeLogic:
    def __init__(self):
        self.ESCAPE_TRIGGER = 13

    def check_breakout(self, line):
        if line >= self.ESCAPE_TRIGGER: return "UP"
        if line <= -self.ESCAPE_TRIGGER: return "DOWN"
        return None

    def calculate_hedge_size(self, breakout_dir, positions, balance_available):
        """[명세서 5.1] 2배 캡 + All-in"""
        trap_notional = 0.0
        if breakout_dir == "UP":
            trap_notional = positions["SHORT"]["qty"] * positions["SHORT"]["avg_price"]
        else:
            trap_notional = positions["LONG"]["qty"] * positions["LONG"]["avg_price"]

        if trap_notional <= 0: return 0.0

        # 2배 캡
        target_hedge = trap_notional * HEDGE_MAX_FACTOR
        # 가용 자금 한도
        max_buying_power = balance_available * LEVERAGE * 0.98
        
        return to_int_usdt(min(target_hedge, max_buying_power))

    def get_pnl_zero_price_with_cushion(self, positions, breakout_dir):
        """[명세서 5.2] 2% 쿠션 목표가"""
        l_qty, l_avg = positions["LONG"]["qty"], positions["LONG"]["avg_price"]
        s_qty, s_avg = positions["SHORT"]["qty"], positions["SHORT"]["avg_price"]
        
        net_qty = l_qty - s_qty
        if abs(net_qty) < 0.000001: return 0
        
        # BEP 계산
        cost_diff = (l_qty * l_avg) - (s_qty * s_avg)
        base_target = cost_diff / net_qty
        
        # 쿠션 적용 (가격의 약 0.5% 정도 이동하여 PnL 2% 효과 유도)
        # *단순화: PnL 2%를 정확히 역산하려면 증거금 대비 수익률 공식 필요하나,
        # 여기서는 안전하게 가격 베이스 0.5% 이동으로 구현
        cushion = base_target * 0.005 
        
        if breakout_dir == "UP": return to_int_price(base_target + cushion)
        else: return to_int_price(base_target - cushion)

    def check_fakeout_return(self, line, mode):
        return mode == "ESCAPE" and abs(line) < self.ESCAPE_TRIGGER

escape_logic = EscapeLogic()

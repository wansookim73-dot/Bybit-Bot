from config import MAX_ALLOCATION_RATE
from utils.calculator import to_int_price

class GridLogic:
    def __init__(self):
        # 진입 허용 범위 (-12 ~ +12)
        # Short: -7 이상 / Long: +7 이하
        self.SHORT_ENTRY_RANGE = range(-7, 13) 
        self.LONG_ENTRY_RANGE = range(-12, 8)

    def calculate_line_index(self, price, center, gap):
        if gap <= 0: return 0
        return int(round((price - center) / gap))

    def check_max_allocation(self, side, positions, total_balance):
        """[명세서 2] 25% 차단 룰"""
        pos = positions.get(side, {"qty": 0, "avg_price": 0})
        current_val = pos["qty"] * pos["avg_price"]
        limit_val = total_balance * MAX_ALLOCATION_RATE
        
        if current_val >= limit_val:
            # logger.warning(f"{side} Max Allocation Reached ({current_val:.1f}/{limit_val:.1f})")
            return False # 차단
        return True

    def check_entry_signal(self, line, pos, center, gap, last_l, last_s, total_bal):
        price = to_int_price(center + line * gap)
        
        # Long Scale-in
        if line in self.LONG_ENTRY_RANGE and (last_l is None or line != last_l):
            if self.check_max_allocation("LONG", pos, total_bal):
                l_avg = pos["LONG"].get("avg_price", 0)
                if pos["LONG"]["qty"] > 0 and price < l_avg:
                    return {"action": "OPEN_LONG", "price": price, "comment": f"Long Scale-in L{line}"}

        # Short Scale-in
        if line in self.SHORT_ENTRY_RANGE and (last_s is None or line != last_s):
            if self.check_max_allocation("SHORT", pos, total_bal):
                s_avg = pos["SHORT"].get("avg_price", 0)
                if pos["SHORT"]["qty"] > 0 and price > s_avg:
                    return {"action": "OPEN_SHORT", "price": price, "comment": f"Short Scale-in L{line}"}
        return None

    def check_tp_signal(self, line, pos, center, gap):
        price = to_int_price(center + line * gap)
        
        # Long TP
        l_pos = pos.get("LONG", {})
        if l_pos.get("qty", 0) > 0 and price >= (l_pos["avg_price"] + gap * 0.8):
            return {"action": "CLOSE_LONG", "price": price, "comment": f"Long TP L{line}"}

        # Short TP
        s_pos = pos.get("SHORT", {})
        if s_pos.get("qty", 0) > 0 and price <= (s_pos["avg_price"] - gap * 0.8):
            return {"action": "CLOSE_SHORT", "price": price, "comment": f"Short TP L{line}"}
        return None

    def check_refill_needed(self, line, prev_pos, curr_pos):
        """[명세서 3.4] 재고 관리 + 위치 체크"""
        # Long이 없어졌고, 현재 위치가 Long 진입 구간이면 리필
        if prev_pos["LONG"]["qty"] > 0 and curr_pos["LONG"]["qty"] == 0:
            if line in self.LONG_ENTRY_RANGE: return "LONG"
            
        # Short이 없어졌고, 현재 위치가 Short 진입 구간이면 리필
        if prev_pos["SHORT"]["qty"] > 0 and curr_pos["SHORT"]["qty"] == 0:
            if line in self.SHORT_ENTRY_RANGE: return "SHORT"
            
        return None

grid_logic = GridLogic()

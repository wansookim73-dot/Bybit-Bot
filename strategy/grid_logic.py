from typing import Dict, Any, Optional
from config import GRID_SPLIT_COUNT, MAX_ALLOCATION_RATE
from utils.calculator import to_int_price

class GridLogic:
    def __init__(self):
        self.OVERLAP = 7
        self.SHORT_ENTRY_RANGE = range(-7, 13) 
        self.LONG_ENTRY_RANGE = range(-12, 8)
        self.DCA_THRESHOLD_MULTIPLIER = 1.0 # [수정] 최소 1 Grid Gap 이탈 시에만 DCA 허용

    def calculate_line_index(self, price, center, gap):
        if gap <= 0: return 0
        return int(round((price - center) / gap)) if gap > 0 else 0

    def get_grid_price(self, line, center, gap):
        return to_int_price(center + line * gap)

    def check_max_allocation(self, side, positions, total_balance):
        pos = positions.get(side, {"qty": 0, "avg_price": 0})
        current_val = pos["qty"] * pos["avg_price"]
        limit_val = total_balance * MAX_ALLOCATION_RATE
        return current_val < limit_val

    # [최종 수정] 1x Gap Threshold 적용
    def check_entry_signal(self, line_index: int, positions: Dict, p_center: float, p_gap: float, 
                           last_l: Optional[int], last_s: Optional[int], total_bal: float) -> Dict[str, Any]:
        grid_p = self.get_grid_price(line_index, p_center, p_gap)

        # Long Scale-in
        if line_index in self.LONG_ENTRY_RANGE and (last_l is None or line_index < last_l):
            if self.check_max_allocation("LONG", positions, total_bal):
                l_avg = positions.get("LONG", {"qty": 0, "avg_price": 0})["avg_price"]
                # 조건: 현재 가격이 평단가보다 1.0 Gap 이상 낮아야 함 (새로운 라인으로 확정)
                if positions["LONG"].get("qty", 0) > 0 and grid_p <= (l_avg - p_gap * self.DCA_THRESHOLD_MULTIPLIER):
                    return {"action": "OPEN_LONG", "price": grid_p, "comment": f"Long Scale-in L{line_index}"}

        # Short Scale-in
        if line_index in self.SHORT_ENTRY_RANGE and (last_s is None or line_index > last_s):
            if self.check_max_allocation("SHORT", positions, total_bal):
                s_avg = positions.get("SHORT", {"qty": 0, "avg_price": 0})["avg_price"]
                # 조건: 현재 가격이 평단가보다 1.0 Gap 이상 높아야 함
                if positions["SHORT"].get("qty", 0) > 0 and grid_p >= (s_avg + p_gap * self.DCA_THRESHOLD_MULTIPLIER):
                    return {"action": "OPEN_SHORT", "price": grid_p, "comment": f"Short Scale-in L{line_index}"}
        return None

    def check_tp_signal(self, line_index: int, positions: Dict, p_center: float, p_gap: float) -> Dict[str, Any]:
        grid_p = self.get_grid_price(line_index, p_center, p_gap)
        
        # TP: 3.0 Gap 최종 확정
        long_pos = positions.get("LONG", {})
        if long_pos.get("qty", 0) > 0 and grid_p >= (long_pos.get("avg_price", 0) + p_gap * 3.0):
            return {"action": "CLOSE_LONG", "price": grid_p, "comment": f"Long TP L{line_index}"}

        short_pos = positions.get("SHORT", {})
        if short_pos.get("qty", 0) > 0 and grid_p <= (short_pos.get("avg_price", 0) - p_gap * 3.0):
            return {"action": "CLOSE_SHORT", "price": grid_p, "comment": f"Short TP L{line_index}"}
        return None

    def check_refill_needed(self, line, prev_pos, curr_pos):
        if prev_pos["LONG"].get("qty", 0) > 0 and curr_pos["LONG"].get("qty", 0) == 0:
            if line in self.LONG_ENTRY_RANGE: return "LONG"
        if prev_pos["SHORT"].get("qty", 0) > 0 and curr_pos["SHORT"].get("qty", 0) == 0:
            if line in self.SHORT_ENTRY_RANGE: return "SHORT"
        return None

grid_logic = GridLogic()

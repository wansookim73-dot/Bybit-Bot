from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Tuple, Optional

from strategy.feed_types import StrategyFeed, OrderInfo
from strategy.state_model import LineState
from utils.logger import logger, get_logger
from utils.calculator import calc_contract_qty
from strategy.capital import CapitalManager
from core.state_manager import get_state_manager
import time
import os
from decimal import Decimal

# ------------------------------
# GridOrderSpec
# ------------------------------

@dataclass
class GridOrderSpec:
    side: str              # "BUY" | "SELL"
    price: float
    qty: float
    grid_index: int        # 라인 index (엔트리: -12..+12, TP: profit_line_index)
    wave_id: int
    mode: str = "A"        # Grid Maker는 항상 Mode A

    # ✅ TP safety (Hedge Mode)
    reduce_only: bool = False
    position_idx: Optional[int] = None  # 1=LONG, 2=SHORT

# ------------------------------
# GridDecision
# ------------------------------


@dataclass
class GridDecision:
    mode: str                               # feed.state.mode ("NORMAL"/"ESCAPE"/"NEWS_BLOCK"/...)
    grid_entries: List[GridOrderSpec]       # 신규 Maker (Entry/TP 포함)
    grid_replaces: List[GridOrderSpec]      # 재배치 Maker (v10.1에서는 거의 사용 안 함)
    grid_cancels: List[str]                 # 취소할 order_id 리스트
    state_updates: Dict[str, Any]           # BotState 필드 업데이트 요청 (k_dir, line_memory 등)

    @property
    def orders(self) -> List[GridOrderSpec]:
        return list(self.grid_entries) + list(self.grid_replaces)


# ------------------------------
# 상수: 라인 범위 / 분할
# ------------------------------

GRID_LEVERAGE: float = 7.0  # v10.1: Bybit BTCUSDT Perp Cross 7x 고정 레버리지
SPLIT_COUNT: int = 13       # 13분할 고정
SAFE_FACTOR: float = 0.9    # v10.1: effective seed = nominal * 0.9

# 라인 운용 범위
LONG_LINE_MIN = -12
LONG_LINE_MAX = +7
SHORT_LINE_MIN = -7
SHORT_LINE_MAX = +12
OVERLAP_MIN = -7
OVERLAP_MAX = +7

# DCA 발동 최소 손실 기준 (USDT 단위)
#   예) 3.0 으로 설정하면, PnL 이 -3 USDT 이하일 때만 DCA 시도
#   기본값은 0.0 → 기존 동작과 완전히 동일
DCA_MIN_LOSS_USDT_LONG: float = 0.0   # 롱 기준
DCA_MIN_LOSS_USDT_SHORT: float = 0.0  # 숏 기준


# ------------------------------
# Helper dataclasses
# ------------------------------


@dataclass
class SeedGateState:
    unit_seed: float
    allocated_seed: float
    effective_seed_total: float
    used_seed: float
    remain_seed: float
    k_dir: int
    can_open_unit: bool


# ------------------------------
# Helper functions
# ------------------------------


def line_price(p_center: float, p_gap: float, index: int) -> float:
    return float(p_center) + float(index) * float(p_gap)


def classify_order(order: OrderInfo, current_wave_id: int) -> Tuple[str, Optional[int], Optional[str]]:
    """
    GRID 태그 패턴 예:
    - "W4_GRID_A_-3_BUY"
    """
    tag = order.tag or ""

    prefix = f"W{current_wave_id}_GRID_A_"
    if tag.startswith(prefix):
        parts = tag.split("_")
        try:
            grid_index = int(parts[3])
        except Exception:
            return ("FOREIGN", None, None)
        side = parts[4] if len(parts) > 4 else order.side.upper()
        return ("GRID_CURRENT", grid_index, side)

    # 다른 Wave 의 GRID 주문
    if tag.startswith("W") and "_GRID_A_" in tag:
        try:
            wave_num = int(tag.split("_")[0][1:])
        except Exception:
            return ("FOREIGN", None, None)
        if wave_num != current_wave_id:
            return ("GRID_OLD_WAVE", None, None)

    # Escape / Manual / 기타 특수 주문
    if tag.startswith("MB_PRE_") or tag.startswith("MB_SLICE_") \
       or tag in ("FULL_EXIT", "ESCAPE_HEDGE_EXIT", "HEDGE_EXIT"):
        return ("NON_GRID_MANAGED", None, None)

    return ("FOREIGN", None, None)


def choose_main_order(orders: List[OrderInfo]) -> Optional[OrderInfo]:
    """
    동일 grid_index / side 에 여러 주문이 있을 경우,
    가장 최근(created_ts 최대)을 메인으로 사용.
    """
    if not orders:
        return None
    return max(orders, key=lambda o: o.created_ts)


def detect_touched_lines(
    price_prev: float,
    price_now: float,
    p_center: float,
    p_gap: float,
    idx_min: int = -12,
    idx_max: int = +12,
) -> List[int]:
    """
    price_prev/price_now 기반 라인 터치 검출.

    규칙 (3.1.6):
    - 상승 구간 (price_now >= price_prev):
        price_prev <  Line_k_price <= price_now
        → 인덱스 오름차순 처리 (작은 인덱스 → 큰 인덱스)
    - 하락 구간 (price_now < price_prev):
        price_prev >  Line_k_price >= price_now
        → 인덱스 내림차순 처리 (큰 인덱스 → 작은 인덱스)
    """
    try:
        price_prev = float(price_prev)
        price_now = float(price_now)
        p_center = float(p_center)
        p_gap = float(p_gap)
    except (TypeError, ValueError):
        return []

    if p_gap <= 0:
        return []

    ascending = price_now >= price_prev
    touched: List[int] = []

    if ascending:
        for idx in range(idx_min, idx_max + 1):
            lp = line_price(p_center, p_gap, idx)
            if price_prev < lp <= price_now:
                touched.append(idx)
        touched.sort()
    else:
        for idx in range(idx_max, idx_min - 1, -1):
            lp = line_price(p_center, p_gap, idx)
            if price_prev > lp >= price_now:
                touched.append(idx)

    return touched


def _extract_position_info(feed: StrategyFeed) -> Dict[str, Any]:
    """
    StrategyFeed / BotState / (옵션) feed.positions 에서
    현재 계좌 포지션 정보를 추출한다.
    """
    _log = get_logger("grid_logic")

    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    long_size = short_size = hedge_size = 0.0
    long_pnl = short_pnl = 0.0

    pos_from_feed = getattr(feed, "positions", None)
    state = getattr(feed, "state", None)

    if isinstance(pos_from_feed, dict):
        long_size = _to_float(pos_from_feed.get("long_size", 0.0))
        short_size = _to_float(pos_from_feed.get("short_size", 0.0))
        hedge_size = _to_float(pos_from_feed.get("hedge_size", 0.0))
        long_pnl = _to_float(pos_from_feed.get("long_pnl", 0.0))
        short_pnl = _to_float(pos_from_feed.get("short_pnl", 0.0))

        if state is not None and (abs(long_pnl) < 1e-9 or abs(short_pnl) < 1e-9):
            if isinstance(state, dict):
                if abs(long_pnl) < 1e-9:
                    long_pnl = _to_float(state.get("long_pnl", long_pnl))
                if abs(short_pnl) < 1e-9:
                    short_pnl = _to_float(state.get("short_pnl", short_pnl))
            else:
                if abs(long_pnl) < 1e-9:
                    long_pnl = _to_float(getattr(state, "long_pnl", long_pnl))
                if abs(short_pnl) < 1e-9:
                    short_pnl = _to_float(getattr(state, "short_pnl", short_pnl))
    else:
        if isinstance(state, dict):
            long_size = _to_float(state.get("long_size", 0.0))
            short_size = _to_float(state.get("short_size", 0.0))
            hedge_size = _to_float(state.get("hedge_size", 0.0))
            long_pnl = _to_float(state.get("long_pnl", 0.0))
            short_pnl = _to_float(state.get("short_pnl", 0.0))
        else:
            long_size = _to_float(getattr(state, "long_size", 0.0))
            short_size = _to_float(getattr(state, "short_size", 0.0))
            hedge_size = _to_float(getattr(state, "hedge_size", 0.0))
            long_pnl = _to_float(getattr(state, "long_pnl", 0.0))
            short_pnl = _to_float(getattr(state, "short_pnl", 0.0))

    has_size = (abs(long_size) > 0.0) or (abs(short_size) > 0.0)
    pnl_missing = (abs(long_pnl) < 1e-9 and abs(short_pnl) < 1e-9)

    if has_size and pnl_missing:
        mark_price = 0.0
        candidate_prices: list[float] = []
        for attr in (
            "mark_price", "last_price", "last", "price", "mid_price", "mid", "close",
            "best_bid", "best_ask", "bid", "ask",
        ):
            candidate_prices.append(_to_float(getattr(feed, attr, None), 0.0))

        ticker = getattr(feed, "ticker", None)
        if isinstance(ticker, dict):
            for key in ("mark_price", "markPrice", "last", "close", "price", "mid", "bid", "ask"):
                candidate_prices.append(_to_float(ticker.get(key), 0.0))

        for p in candidate_prices:
            if p and p > 0:
                mark_price = p
                break

        def _get_avg(src: Any, prefix: str) -> float:
            if isinstance(src, dict):
                for key in (
                    f"{prefix}_avg", f"{prefix}_avg_price",
                    f"{prefix}_entry", f"{prefix}_entry_price", f"{prefix}_price",
                ):
                    if key in src:
                        return _to_float(src.get(key), 0.0)
            else:
                for attr in (
                    f"{prefix}_avg", f"{prefix}_avg_price",
                    f"{prefix}_entry", f"{prefix}_entry_price", f"{prefix}_price",
                ):
                    if hasattr(src, attr):
                        return _to_float(getattr(src, attr, 0.0), 0.0)
            return 0.0

        src_for_avg = pos_from_feed if isinstance(pos_from_feed, dict) else getattr(feed, "state", None)
        long_avg = _get_avg(src_for_avg, "long")
        short_avg = _get_avg(src_for_avg, "short")

        if mark_price > 0.0:
            if abs(long_size) > 0.0 and long_avg > 0.0:
                long_pnl = (mark_price - long_avg) * long_size
            if abs(short_size) > 0.0 and short_avg > 0.0:
                short_pnl = (short_avg - mark_price) * short_size

    # DRY-RUN fake injection (keep)
    try:
        fake_pos_flag = bool(int(os.getenv("FSM_FAKE_POS", "0")))
        fake_pos_qty = _to_float(os.getenv("FSM_FAKE_POS_QTY", "0.005"))
    except Exception:
        fake_pos_flag = False
        fake_pos_qty = 0.0

    if fake_pos_flag and long_size == 0 and short_size == 0 and hedge_size == 0:
        long_size = fake_pos_qty

    try:
        fake_pnl_flag = bool(int(os.getenv("FSM_FAKE_PNL", "0")))
        fake_pnl_value = _to_float(os.getenv("FSM_FAKE_PNL_VALUE", "0.0"))
    except Exception:
        fake_pnl_flag = False
        fake_pnl_value = 0.0

    if fake_pnl_flag and long_size > 0 and long_pnl == 0.0:
        long_pnl = fake_pnl_value

    pos_info: Dict[str, Any] = {
        "long_size": long_size,
        "short_size": short_size,
        "hedge_size": hedge_size,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
    }

    _log.info(
        "[Feed] pos_for_fsm: long=%.6f short=%.6f hedge=%.6f long_pnl=%.6f short_pnl=%.6f",
        pos_info.get("long_size", 0.0),
        pos_info.get("short_size", 0.0),
        pos_info.get("hedge_size", 0.0),
        pos_info.get("long_pnl", 0.0),
        pos_info.get("short_pnl", 0.0),
    )

    return pos_info


def _compute_profit_line_index(pnl: float, size: float, p_gap: float) -> int:
    try:
        pnl = float(pnl)
        size = float(size)
        p_gap = float(p_gap)
    except (TypeError, ValueError):
        return 0
    if size == 0 or p_gap <= 0:
        return 0
    delta_price = pnl / abs(size)
    lines = delta_price / p_gap
    if lines <= 0:
        return 0
    return int(lines // 1)


def _pnl_sign(x: float) -> int:
    x = float(x or 0.0)
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _compute_avg_entry_from_pnl_long(price_now: float, size: float, pnl: float) -> Optional[float]:
    try:
        price_now = float(price_now)
        size = float(size)
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None
    if size == 0:
        return None
    return price_now - pnl / abs(size)


def _compute_avg_entry_from_pnl_short(price_now: float, size: float, pnl: float) -> Optional[float]:
    try:
        price_now = float(price_now)
        size = float(size)
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None
    if size == 0:
        return None
    return price_now + pnl / abs(size)


# ------------------------------
# GridLogic
# ------------------------------

class GridLogic:
    def __init__(self, capital: CapitalManager | None = None) -> None:
        self.capital = capital
        self.safe_factor = SAFE_FACTOR

    def _apply_news_cb_block(
        self,
        decision,
        grid_orders_by_side_idx,
        news_block: bool,
        cb_block: bool,
        mode: str,
    ) -> tuple[bool, bool]:
        """
        Returns: (news_active, block_new_entries)
        - 뉴스/CB 구간에서는 신규 진입(리스크 증가)을 차단한다.
        - TP/Reduce(리스크 감소)는 허용한다.
        - 기존 Maker(Grid) 주문은 취소한다.
        """
        news_active = bool(news_block or cb_block or mode == "NEWS_BLOCK")
        if not news_active:
            return False, False

        # cancel existing maker/grid orders
        for orders in grid_orders_by_side_idx.values():
            for o in orders:
                decision.grid_cancels.append(o.order_id)

        logger.info("[GRID] NEWS/CB Block active → Grid 신규 엔트리 차단, TP/Reduce 허용, 기존 Maker 취소")

        # block new entries, keep reduce-only only
        try:
            decision.grid_entries = [
                e for e in decision.grid_entries
                if bool(getattr(e, "reduce_only", False))
            ]
        except Exception:
            pass

        return True, True

    def _seed_gate(self, state, side: str, override_k_dir: Optional[int] = None) -> SeedGateState:
        s = state
        side_u = side.upper()
        if side_u == "LONG":
            eff = float(getattr(s, "long_seed_total_effective", 0.0) or 0.0)
            unit = float(getattr(s, "unit_seed_long", 0.0) or 0.0)
            k_dir_state = int(getattr(s, "k_long", 0) or 0)
        else:
            eff = float(getattr(s, "short_seed_total_effective", 0.0) or 0.0)
            unit = float(getattr(s, "unit_seed_short", 0.0) or 0.0)
            k_dir_state = int(getattr(s, "k_short", 0) or 0)

        if override_k_dir is not None:
            k_dir = max(0, min(int(override_k_dir), SPLIT_COUNT))
        else:
            k_dir = max(0, min(int(k_dir_state), SPLIT_COUNT))

        if eff <= 0.0 or unit <= 0.0:
            return SeedGateState(
                unit_seed=0.0,
                allocated_seed=0.0,
                effective_seed_total=eff,
                used_seed=0.0,
                remain_seed=0.0,
                k_dir=k_dir,
                can_open_unit=False,
            )

        allocated = eff / self.safe_factor if self.safe_factor > 0 else eff
        used = float(k_dir) * unit
        remain = max(0.0, allocated - used)
        can_open = (k_dir < SPLIT_COUNT) and (remain + 1e-9 >= unit)

        return SeedGateState(
            unit_seed=unit,
            allocated_seed=allocated,
            effective_seed_total=eff,
            used_seed=used,
            remain_seed=remain,
            k_dir=k_dir,
            can_open_unit=can_open,
        )

    def process(self, feed: StrategyFeed) -> GridDecision:
        now_ts = time.time()  # injected for DCA cooldown
        state = feed.state
        mode = getattr(state, "mode", "NORMAL")

        decision = GridDecision(
            mode=mode,
            grid_entries=[],
            grid_replaces=[],
            grid_cancels=[],
            state_updates={},
        )

        wave_id = int(getattr(state, "wave_id", 0) or 0)
        p_center = float(getattr(state, "p_center", 0.0) or 0.0)
        p_gap = float(getattr(state, "p_gap", 0.0) or 0.0)

        price_now = float(getattr(feed, "price", 0.0) or 0.0)
        price_prev = float(getattr(feed, "price_prev", price_now) or price_now)

        line_memory_long: Dict[int, LineState] = dict(getattr(state, "line_memory_long", {}) or {})
        line_memory_short: Dict[int, LineState] = dict(getattr(state, "line_memory_short", {}) or {})

        wave_state = getattr(feed, "wave_state", None)
        if wave_state is not None:
            escape_long_active = bool(getattr(getattr(wave_state, "long_escape", wave_state), "active", False))
            escape_short_active = bool(getattr(getattr(wave_state, "short_escape", wave_state), "active", False))
        else:
            escape_long_active = bool(getattr(state, "escape_long_active", False) or getattr(state, "escape_active_long", False))
            escape_short_active = bool(getattr(state, "escape_short_active", False) or getattr(state, "escape_active_short", False))

        news_block = bool(getattr(feed, "news_block", False) or getattr(state, "news_block", False))
        cb_block = bool(getattr(feed, "cb_block", False) or getattr(state, "cb_block", False))

        pos_info = _extract_position_info(feed)
        long_size = float(pos_info.get("long_size", 0.0) or 0.0)
        short_size = float(pos_info.get("short_size", 0.0) or 0.0)
        long_pnl = float(pos_info.get("long_pnl", 0.0) or 0.0)
        short_pnl = float(pos_info.get("short_pnl", 0.0) or 0.0)

        # ------------------------------------------------------------
        # [REENTRY_RESET_V10_1]
        # TP 1분할 체결(수량 감소) 감지 시 dca_used_indices(진입기록) 리셋 → 동일 라인 재진입 허용
        # - LONG TP fill: 음수 idx(롱 진입 라인)만 제거
        # - SHORT TP fill: 양수 idx(숏 진입 라인)만 제거
        # ------------------------------------------------------------
        try:
            prev_long_size = float(getattr(state, "prev_long_size", long_size) or long_size)
            prev_short_size = float(getattr(state, "prev_short_size", short_size) or short_size)
            prev_long_tp_active = bool(getattr(state, "prev_long_tp_active", False))
            prev_short_tp_active = bool(getattr(state, "prev_short_tp_active", False))
        except Exception:
            prev_long_size, prev_short_size = long_size, short_size
            prev_long_tp_active, prev_short_tp_active = False, False

        # NOTE:
        #  - 운영에서 TP active 신호가 누락/불안정할 수 있어(prev_*_tp_active가 False로 유지되는 케이스),
        #    "수량 감소"를 1차 트리거로 사용한다.
        #  - prev_*_tp_active는 'TP로 인한 감소인지' 라벨링/관측용 힌트로만 사용.
        _tp_fill_eps = 1e-9

        long_reduced = (prev_long_size > 0.0) and (long_size < (prev_long_size - _tp_fill_eps))
        short_reduced = (prev_short_size > 0.0) and (short_size < (prev_short_size - _tp_fill_eps))

        tp_filled_long = bool(long_reduced)
        tp_filled_short = bool(short_reduced)

        if tp_filled_long or tp_filled_short:
            used = set(getattr(state, "dca_used_indices", []) or [])
            last_idx = int(getattr(state, "dca_last_idx", 10**9) or 10**9)
            last_ts = float(getattr(state, "dca_last_ts", 0.0) or 0.0)
            last_price = float(getattr(state, "dca_last_price", 0.0) or 0.0)

            if tp_filled_long:
                used = {i for i in used if i >= 0}
                if last_idx < 0:
                    last_idx, last_ts, last_price = 10**9, 0.0, 0.0

                if prev_long_tp_active:
                    logger.info(
                        "[REENTRY] LONG TP filled -> reset dca_used_indices(negative cleared). prev=%.6f now=%.6f",
                        prev_long_size, long_size
                    )
                else:
                    logger.info(
                        "[REENTRY] LONG size reduced -> reset dca_used_indices(negative cleared). prev_tp_active=False prev=%.6f now=%.6f",
                        prev_long_size, long_size
                    )

            if tp_filled_short:
                used = {i for i in used if i <= 0}
                if last_idx > 0:
                    last_idx, last_ts, last_price = 10**9, 0.0, 0.0

                if prev_short_tp_active:
                    logger.info(
                        "[REENTRY] SHORT TP filled -> reset dca_used_indices(positive cleared). prev=%.6f now=%.6f",
                        prev_short_size, short_size
                    )
                else:
                    logger.info(
                        "[REENTRY] SHORT size reduced -> reset dca_used_indices(positive cleared). prev_tp_active=False prev=%.6f now=%.6f",
                        prev_short_size, short_size
                    )

            try:
                setattr(state, "dca_used_indices", sorted(used))
                setattr(state, "dca_last_idx", last_idx)
                setattr(state, "dca_last_ts", last_ts)
                setattr(state, "dca_last_price", last_price)
            except Exception:
                pass

            try:
                decision.state_updates["dca_used_indices"] = sorted(used)
                decision.state_updates["dca_last_idx"] = last_idx
                decision.state_updates["dca_last_ts"] = last_ts
                decision.state_updates["dca_last_price"] = last_price
            except Exception:
                pass

        prev_long_nonzero = bool(getattr(state, "long_pos_nonzero", long_size != 0.0))
        prev_short_nonzero = bool(getattr(state, "short_pos_nonzero", short_size != 0.0))
        prev_long_pnl_sign = int(getattr(state, "long_pnl_sign", 0) or 0)
        prev_short_pnl_sign = int(getattr(state, "short_pnl_sign", 0) or 0)

        long_pnl_sign = _pnl_sign(long_pnl)
        short_pnl_sign = _pnl_sign(short_pnl)

        reset_long_lines = False
        reset_short_lines = False
        reset_long_seed = False
        reset_short_seed = False

        if long_size == 0.0 and prev_long_nonzero:
            reset_long_lines = True
            reset_long_seed = True
        if short_size == 0.0 and prev_short_nonzero:
            reset_short_lines = True
            reset_short_seed = True

        if long_size != 0.0 and prev_long_pnl_sign >= 0 and long_pnl_sign < 0:
            reset_long_lines = True
        if short_size != 0.0 and prev_short_pnl_sign >= 0 and short_pnl_sign < 0:
            reset_short_lines = True

        if reset_long_lines:
            line_memory_long = {}
            decision.state_updates["line_memory_long"] = line_memory_long
            decision.state_updates["long_tp_active"] = False
            decision.state_updates["long_tp_max_index"] = 0

        if reset_short_lines:
            line_memory_short = {}
            decision.state_updates["line_memory_short"] = line_memory_short
            decision.state_updates["short_tp_active"] = False
            decision.state_updates["short_tp_max_index"] = 0

        k_long_state = int(getattr(state, "k_long", 0) or 0)
        k_short_state = int(getattr(state, "k_short", 0) or 0)

        k_long_effective = 0 if reset_long_seed else k_long_state
        k_short_effective = 0 if reset_short_seed else k_short_state

        if reset_long_seed and k_long_state != 0:
            decision.state_updates["k_long"] = 0
        if reset_short_seed and k_short_state != 0:
            decision.state_updates["k_short"] = 0

        decision.state_updates["long_pos_nonzero"] = (long_size != 0.0)
        decision.state_updates["short_pos_nonzero"] = (short_size != 0.0)
        decision.state_updates["long_pnl_sign"] = long_pnl_sign
        decision.state_updates["short_pnl_sign"] = short_pnl_sign

        seed_long = self._seed_gate(state, "LONG", override_k_dir=k_long_effective)
        seed_short = self._seed_gate(state, "SHORT", override_k_dir=k_short_effective)

        grid_orders_by_side_idx: Dict[Tuple[str, int], List[OrderInfo]] = {}
        old_wave_orders: List[str] = []

        for o in feed.open_orders:
            kind, idx, side = classify_order(o, wave_id)
            if kind == "GRID_CURRENT" and idx is not None and side is not None:
                key = (side.upper(), int(idx))
                grid_orders_by_side_idx.setdefault(key, []).append(o)
            elif kind == "GRID_OLD_WAVE":
                old_wave_orders.append(o.order_id)

        for oid in old_wave_orders:
            decision.grid_cancels.append(oid)

        news_active, block_new_entries = self._apply_news_cb_block(
            decision, grid_orders_by_side_idx, news_block, cb_block, mode
        )

        # -----------
        # DCA touch window (crossing-based, but over a rolling high/low window)
        # ------------------------------
        try:
            dca_low = float(getattr(state, "dca_win_low", price_now))
            dca_high = float(getattr(state, "dca_win_high", price_now))
            dca_ts = float(getattr(state, "dca_win_ts", 0.0))
        except Exception:
            dca_low, dca_high, dca_ts = price_now, price_now, 0.0

        # 윈도우 만료(예: 20초) 시 리셋
        if (now_ts - dca_ts) > 20.0:
            dca_low, dca_high = price_now, price_now
            dca_ts = now_ts

        # 윈도우 업데이트
        dca_low = min(dca_low, price_now)
        dca_high = max(dca_high, price_now)

        setattr(state, "dca_win_low", dca_low)
        setattr(state, "dca_win_high", dca_high)
        setattr(state, "dca_win_ts", dca_ts)

        # 교차 기반 터치: (윈도우 low~high) 구간을 기준으로 라인 관통 여부 검사
        touched_indices = detect_touched_lines(
            price_prev=dca_low,
            price_now=dca_high,
            p_center=p_center,
            p_gap=p_gap,
            idx_min=min(LONG_LINE_MIN, SHORT_LINE_MIN),
            idx_max=max(LONG_LINE_MAX, SHORT_LINE_MAX),
        )
        dca_indices: list[int] = []  # A-PLAN safety init (avoid UnboundLocalError)

        # ------------------------------
        # [A-PLAN] DCA trigger: touched OR near-line
        # ------------------------------
        dca_near_eps = float(getattr(state, 'dca_near_eps', 0.18) or 0.18)
        dca_cooldown_sec = float(getattr(state, 'dca_cooldown_sec', 20.0) or 20.0)
        dca_repeat_guard = float(getattr(state, 'dca_repeat_guard', 0.50) or 0.50)

        idx_float = (price_now - p_center) / p_gap if p_gap > 0 else 0.0
        idx_near = int(round(idx_float))
        line_near = line_price(p_center, p_gap, idx_near) if p_gap > 0 else price_now
        dist_near = abs(price_now - line_near)
        dist_ratio = (dist_near / p_gap) if p_gap > 0 else 999.0
        near_touch = (dist_ratio <= dca_near_eps)

        dca_indices = list(dca_indices or [])
        if (not dca_indices) and near_touch:
            dca_indices = [idx_near]

        logger.info(
            "[DCA-DBG] dca_indices=%s touched=%s near_touch=%s dist_ratio=%.3f eps=%.3f",
            dca_indices, touched_indices, near_touch, dist_ratio, dca_near_eps
        )

        logger.info(
            "[DCA-DBG] price_prev=%.2f price_now=%.2f touched=%s",
            price_prev, price_now, touched_indices,
        )
        # [DCA-DBG] nearest_line (why touched==[])
        try:
            pc = float(p_center)
            pg = float(p_gap)
            pn = float(price_now)
            cand = []
            for gi in range(-12, 13):
                lp = pc + pg * gi
                cand.append((abs(pn - lp), gi, lp))
            cand.sort(key=lambda x: x[0])
            d0, gi0, lp0 = cand[0]
            logger.info(
                "[DCA-DBG] nearest_line: price_now=%.2f nearest_idx=%d line=%.2f dist=%.2f p_center=%.2f p_gap=%.2f",
                pn, gi0, lp0, d0, pc, pg,
            )
        except Exception as _e:
            logger.info("[DCA-DBG] nearest_line: skipped (%s)", _e)

        # ------------------------------
        # Start-up Entry (진입) — reduce_only 금지
        # ------------------------------
        k_long = k_long_effective
        k_short = k_short_effective

        startup_done = bool(getattr(state, "startup_done", False))
        startup_entries_created = 0

        existing_long_startup = bool(grid_orders_by_side_idx.get(("BUY", 0), []))
        existing_short_startup = bool(grid_orders_by_side_idx.get(("SELL", 0), []))

        if wave_id > 0 and (not startup_done) and p_center > 0.0 and p_gap > 0.0:
            if (long_size == 0.0 and k_long == 0 and (not escape_long_active) and seed_long.can_open_unit and (not existing_long_startup)):
                long_idx = 0
                if LONG_LINE_MIN <= long_idx <= LONG_LINE_MAX:
                    if line_memory_long.get(long_idx, LineState.FREE) == LineState.FREE:
                        price_l = line_price(p_center, p_gap, long_idx)
                        notional_l = seed_long.unit_seed * GRID_LEVERAGE
                        qty_l = calc_contract_qty(usdt_amount=notional_l, price=price_l, symbol="BTCUSDT")
                        if qty_l > 0.0:
                            key_l = ("BUY", long_idx)
                            existing_l = choose_main_order(grid_orders_by_side_idx.get(key_l, []))
                            if existing_l is None:
                                decision.grid_entries.append(
                                    GridOrderSpec(side="BUY", price=price_l, qty=qty_l, grid_index=long_idx, wave_id=wave_id, mode="A")
                                )
                                startup_entries_created += 1

            if (short_size == 0.0 and k_short == 0 and (not escape_short_active) and seed_short.can_open_unit and (not existing_short_startup)):
                short_idx = 0
                if SHORT_LINE_MIN <= short_idx <= SHORT_LINE_MAX:
                    if line_memory_short.get(short_idx, LineState.FREE) == LineState.FREE:
                        price_s = line_price(p_center, p_gap, short_idx)
                        notional_s = seed_short.unit_seed * GRID_LEVERAGE
                        qty_s = calc_contract_qty(usdt_amount=notional_s, price=price_s, symbol="BTCUSDT")
                        if qty_s > 0.0:
                            key_s = ("SELL", short_idx)
                            existing_s = choose_main_order(grid_orders_by_side_idx.get(key_s, []))
                            if existing_s is None:
                                decision.grid_entries.append(
                                    GridOrderSpec(side="SELL", price=price_s, qty=qty_s, grid_index=short_idx, wave_id=wave_id, mode="A")
                                )
                                startup_entries_created += 1

            if startup_entries_created > 0:
                decision.state_updates["startup_done"] = True
                logger.info("[GRID] Start-up Entry 실행 완료: wave_id=%s, entries=%s", wave_id, startup_entries_created)

        # ------------------------------
        # DCA (Scale-in) — unchanged
        # ------------------------------
        if (
            long_pnl < 0.0
            and long_size != 0.0
            and price_now < price_prev
            and not escape_long_active
            and seed_long.can_open_unit
            and (-long_pnl) >= DCA_MIN_LOSS_USDT_LONG
        ):
            remaining_seed = seed_long.remain_seed
            available_steps = max(0, SPLIT_COUNT - seed_long.k_dir)
            for idx in dca_indices:
                # [A-PLAN] anti-repeat guard
                # 같은 wave에서 같은 idx 반복 진입을 막는다.
                try:
                    _wave_id_now = int(getattr(state, 'wave_id', 0) or 0)
                except Exception:
                    _wave_id_now = 0
                try:
                    _guard_wave = int(getattr(state, 'dca_guard_wave_id', -1) or -1)
                except Exception:
                    _guard_wave = -1
                if _guard_wave != _wave_id_now:
                    dca_used_indices = set()
                    dca_last_ts = 0.0
                    dca_last_idx = 10**9
                    dca_last_price = 0.0
                    try:
                        setattr(state, 'dca_guard_wave_id', _wave_id_now)
                        setattr(state, 'dca_used_indices', [])
                        setattr(state, 'dca_last_ts', 0.0)
                        setattr(state, 'dca_last_idx', 10**9)
                        setattr(state, 'dca_last_price', 0.0)
                    except Exception:
                        pass
                else:
                    dca_used_indices = set(getattr(state, 'dca_used_indices', []) or [])
                    dca_last_ts = float(getattr(state, 'dca_last_ts', 0.0) or 0.0)
                    dca_last_idx = int(getattr(state, 'dca_last_idx', 10**9) or 10**9)
                    dca_last_price = float(getattr(state, 'dca_last_price', 0.0) or 0.0)
                # idx 1회 사용 + 동일 idx 재시도 쿨다운 + 동일 idx 가격근접 반복 방지
                if idx in dca_used_indices:
                    logger.info('[DCA-DBG] guard_block: idx=%s already used (wave=%s)', idx, _wave_id_now)
                    continue
                if (idx == dca_last_idx) and ((now_ts - dca_last_ts) < dca_cooldown_sec):
                    logger.info('[DCA-DBG] guard_block: cooldown idx=%s dt=%.1f < %.1f', idx, (now_ts - dca_last_ts), dca_cooldown_sec)
                    continue
                if (idx == dca_last_idx) and (dca_last_price > 0.0) and (abs(price_now - dca_last_price) < (dca_repeat_guard * p_gap)):
                    logger.info('[DCA-DBG] guard_block: repeat_price idx=%s dist=%.2f < %.2f', idx, abs(price_now - dca_last_price), (dca_repeat_guard * p_gap))
                    continue
                # 보수적으로: 루프에 들어온 순간 mark (같은 tick/다음 tick 반복 진입을 강하게 억제)
                dca_used_indices.add(idx)
                dca_last_ts = now_ts
                dca_last_idx = idx
                dca_last_price = price_now
                try:
                    setattr(state, 'dca_used_indices', sorted(dca_used_indices))
                    setattr(state, 'dca_last_ts', dca_last_ts)
                    setattr(state, 'dca_last_idx', dca_last_idx)
                    setattr(state, 'dca_last_price', dca_last_price)
                except Exception:
                    pass
                if idx < LONG_LINE_MIN or idx > LONG_LINE_MAX:
                    continue
                if available_steps <= 0:
                    break
                if line_memory_long.get(idx, LineState.FREE) != LineState.FREE:
                    continue
                if remaining_seed + 1e-9 < seed_long.unit_seed:
                    break
                price_i = line_price(p_center, p_gap, idx)
                notional = seed_long.unit_seed * GRID_LEVERAGE
                qty = calc_contract_qty(usdt_amount=notional, price=price_i, symbol="BTCUSDT")
                if qty <= 0.0:
                    continue
                key = ("BUY", idx)
                existing = choose_main_order(grid_orders_by_side_idx.get(key, []))
                if existing is None:
                    decision.grid_entries.append(GridOrderSpec(side="BUY", price=price_i, qty=qty, grid_index=idx, wave_id=wave_id, mode="A"))
                    remaining_seed -= seed_long.unit_seed
                    available_steps -= 1

        if (
            short_pnl < 0.0
            and short_size != 0.0
            and price_now > price_prev
            and not escape_short_active
            and seed_short.can_open_unit
            and (-short_pnl) >= DCA_MIN_LOSS_USDT_SHORT
        ):
            remaining_seed = seed_short.remain_seed
            available_steps = max(0, SPLIT_COUNT - seed_short.k_dir)
            for idx in dca_indices:
                # [A-PLAN] anti-repeat guard
                # 같은 wave에서 같은 idx 반복 진입을 막는다.
                try:
                    _wave_id_now = int(getattr(state, 'wave_id', 0) or 0)
                except Exception:
                    _wave_id_now = 0
                try:
                    _guard_wave = int(getattr(state, 'dca_guard_wave_id', -1) or -1)
                except Exception:
                    _guard_wave = -1
                if _guard_wave != _wave_id_now:
                    dca_used_indices = set()
                    dca_last_ts = 0.0
                    dca_last_idx = 10**9
                    dca_last_price = 0.0
                    try:
                        setattr(state, 'dca_guard_wave_id', _wave_id_now)
                        setattr(state, 'dca_used_indices', [])
                        setattr(state, 'dca_last_ts', 0.0)
                        setattr(state, 'dca_last_idx', 10**9)
                        setattr(state, 'dca_last_price', 0.0)
                    except Exception:
                        pass
                else:
                    dca_used_indices = set(getattr(state, 'dca_used_indices', []) or [])
                    dca_last_ts = float(getattr(state, 'dca_last_ts', 0.0) or 0.0)
                    dca_last_idx = int(getattr(state, 'dca_last_idx', 10**9) or 10**9)
                    dca_last_price = float(getattr(state, 'dca_last_price', 0.0) or 0.0)
                # idx 1회 사용 + 동일 idx 재시도 쿨다운 + 동일 idx 가격근접 반복 방지
                if idx in dca_used_indices:
                    logger.info('[DCA-DBG] guard_block: idx=%s already used (wave=%s)', idx, _wave_id_now)
                    continue
                if (idx == dca_last_idx) and ((now_ts - dca_last_ts) < dca_cooldown_sec):
                    logger.info('[DCA-DBG] guard_block: cooldown idx=%s dt=%.1f < %.1f', idx, (now_ts - dca_last_ts), dca_cooldown_sec)
                    continue
                if (idx == dca_last_idx) and (dca_last_price > 0.0) and (abs(price_now - dca_last_price) < (dca_repeat_guard * p_gap)):
                    logger.info('[DCA-DBG] guard_block: repeat_price idx=%s dist=%.2f < %.2f', idx, abs(price_now - dca_last_price), (dca_repeat_guard * p_gap))
                    continue
                # 보수적으로: 루프에 들어온 순간 mark (같은 tick/다음 tick 반복 진입을 강하게 억제)
                dca_used_indices.add(idx)
                dca_last_ts = now_ts
                dca_last_idx = idx
                dca_last_price = price_now
                try:
                    setattr(state, 'dca_used_indices', sorted(dca_used_indices))
                    setattr(state, 'dca_last_ts', dca_last_ts)
                    setattr(state, 'dca_last_idx', dca_last_idx)
                    setattr(state, 'dca_last_price', dca_last_price)
                except Exception:
                    pass
                if idx < SHORT_LINE_MIN or idx > SHORT_LINE_MAX:
                    continue
                if available_steps <= 0:
                    break
                if line_memory_short.get(idx, LineState.FREE) != LineState.FREE:
                    continue
                if remaining_seed + 1e-9 < seed_short.unit_seed:
                    break
                price_j = line_price(p_center, p_gap, idx)
                notional = seed_short.unit_seed * GRID_LEVERAGE
                qty = calc_contract_qty(usdt_amount=notional, price=price_j, symbol="BTCUSDT")
                if qty <= 0.0:
                    continue
                key = ("SELL", idx)
                existing = choose_main_order(grid_orders_by_side_idx.get(key, []))
                if existing is None:
                    decision.grid_entries.append(GridOrderSpec(side="SELL", price=price_j, qty=qty, grid_index=idx, wave_id=wave_id, mode="A"))
                    remaining_seed -= seed_short.unit_seed
                    available_steps -= 1

        # ------------------------------
        # TP (익절)
        # ------------------------------
        profit_idx_long = _compute_profit_line_index(long_pnl, long_size, p_gap)
        profit_idx_short = _compute_profit_line_index(short_pnl, short_size, p_gap)

        long_tp_active = bool(getattr(state, "long_tp_active", False))
        short_tp_active = bool(getattr(state, "short_tp_active", False))

        long_tp_max_index = int(getattr(state, "long_tp_max_index", 0) or 0)
        short_tp_max_index = int(getattr(state, "short_tp_max_index", 0) or 0)

        logger.info(
            "[TP-DBG] pre: price_now=%.2f p_gap=%.2f "
            "long_size=%.6f long_pnl=%.6f profit_idx_long=%d long_tp_active=%s long_tp_max_index=%d "
            "short_size=%.6f short_pnl=%.6f profit_idx_short=%d short_tp_active=%s short_tp_max_index=%d "
            "escape_long_active=%s escape_short_active=%s",
            price_now, p_gap,
            long_size, long_pnl, profit_idx_long, long_tp_active, long_tp_max_index,
            short_size, short_pnl, profit_idx_short, short_tp_active, short_tp_max_index,
            bool(escape_long_active), bool(escape_short_active),
        )

        # [TP_STATE_RESET_V10_1] reset TP state when flat on each side
        if long_size == 0.0 and (long_tp_active or long_tp_max_index != 0):
            long_tp_active = False
            long_tp_max_index = 0
            decision.state_updates["long_tp_active"] = False
            decision.state_updates["long_tp_max_index"] = 0
            logger.info("[TP-DBG] LONG TP reset: long_size=0 -> tp_active=False tpmax=0")
        if short_size == 0.0 and (short_tp_active or short_tp_max_index != 0):
            short_tp_active = False
            short_tp_max_index = 0
            decision.state_updates["short_tp_active"] = False
            decision.state_updates["short_tp_max_index"] = 0
            logger.info("[TP-DBG] SHORT TP reset: short_size=0 -> tp_active=False tpmax=0")

        if long_size != 0.0 and long_pnl > 0.0 and profit_idx_long >= 3 and (not long_tp_active):
            long_tp_active = True
            long_tp_max_index = profit_idx_long - 1
            decision.state_updates["long_tp_active"] = True
            decision.state_updates["long_tp_max_index"] = long_tp_max_index
            logger.info("[TP-DBG] LONG TP 활성화: profit_idx_long=%d -> long_tp_max_index=%d", profit_idx_long, long_tp_max_index)

        if short_size != 0.0 and short_pnl > 0.0 and profit_idx_short >= 3 and (not short_tp_active):
            short_tp_active = True
            short_tp_max_index = profit_idx_short - 1
            decision.state_updates["short_tp_active"] = True
            decision.state_updates["short_tp_max_index"] = short_tp_max_index
            logger.info("[TP-DBG] SHORT activate: idx=%d pnl=%.6f size=%.6f -> tpmax=%d", profit_idx_short, short_pnl, short_size, short_tp_max_index)

        # LONG TP 실행 (SELL reduce-only, posIdx=1)
        if long_size != 0.0 and long_pnl > 0.0 and long_tp_active and (not escape_long_active):
            avg_entry_long = _compute_avg_entry_from_pnl_long(price_now, long_size, long_pnl)
            if avg_entry_long is not None and p_gap > 0:
                new_max = profit_idx_long
                upper_bound = min(new_max, long_tp_max_index + SPLIT_COUNT)
                for idx in range(long_tp_max_index + 1, upper_bound + 1):
                    if idx < 3:
                        continue
                    price_tp = avg_entry_long + idx * p_gap
                    _tick = 0.1
                    if float(price_tp) <= float(avg_entry_long) + _tick:
                        logger.info("[TP-DBG] LONG TP skip: price_tp=%.2f <= avg_entry_long=%.2f (tick=%.1f)", price_tp, avg_entry_long, _tick)
                        continue
                    notional = seed_long.unit_seed * GRID_LEVERAGE if seed_long.unit_seed > 0 else 0.0
                    if notional <= 0.0:
                        break
                    qty_tp = calc_contract_qty(usdt_amount=notional, price=price_tp, symbol="BTCUSDT")
                    if qty_tp <= 0.0:
                        break
                    if qty_tp > abs(long_size):
                        qty_tp = abs(long_size)
                    if qty_tp <= 0.0:
                        break
                    key = ("SELL", idx)
                    existing = choose_main_order(grid_orders_by_side_idx.get(key, []))
                    if existing is None:
                        decision.grid_entries.append(
                            GridOrderSpec(
                                side="SELL",
                                price=price_tp,
                                qty=qty_tp,
                                grid_index=idx,
                                wave_id=wave_id,
                                mode="A",
                                reduce_only=True,
                                position_idx=1,
                            )
                        )
                        long_tp_max_index = idx
                        logger.info(
                            "[TP-DBG] LONG TP 주문 생성: idx=%d price_tp=%.2f qty_tp=%.6f reduce_only=%s position_idx=%s",
                            idx, price_tp, qty_tp, True, 1
                        )
                decision.state_updates["long_tp_max_index"] = long_tp_max_index

        # SHORT TP 실행 (BUY reduce-only, posIdx=2)
        if short_size != 0.0 and short_pnl > 0.0 and short_tp_active and (not escape_short_active):
            avg_entry_short = _compute_avg_entry_from_pnl_short(price_now, short_size, short_pnl)
            if avg_entry_short is not None and p_gap > 0:
                new_max = profit_idx_short
                upper_bound = min(new_max, short_tp_max_index + SPLIT_COUNT)
                for idx in range(short_tp_max_index + 1, upper_bound + 1):
                    if idx < 3:
                        continue
                    price_tp = avg_entry_short - idx * p_gap
                    _tick = 0.1
                    if float(price_tp) >= float(avg_entry_short) - _tick:
                        logger.info("[TP-DBG] SHORT TP skip: price_tp=%.2f >= avg_entry_short=%.2f (tick=%.1f)", price_tp, avg_entry_short, _tick)
                        continue
                    notional = seed_short.unit_seed * GRID_LEVERAGE if seed_short.unit_seed > 0 else 0.0
                    if notional <= 0.0:
                        break
                    qty_tp = calc_contract_qty(usdt_amount=notional, price=price_tp, symbol="BTCUSDT")
                    if qty_tp <= 0.0:
                        break
                    if qty_tp > abs(short_size):
                        qty_tp = abs(short_size)
                    if qty_tp <= 0.0:
                        break
                    key = ("BUY", idx)
                    existing = choose_main_order(grid_orders_by_side_idx.get(key, []))
                    if existing is None:
                        decision.grid_entries.append(
                            GridOrderSpec(
                                side="BUY",
                                price=price_tp,
                                qty=qty_tp,
                                grid_index=idx,
                                wave_id=wave_id,
                                mode="A",
                                reduce_only=True,
                                position_idx=2,
                            )
                        )
                        short_tp_max_index = idx
                        logger.info(
                            "[TP-DBG] SHORT TP 주문 생성: idx=%d price_tp=%.2f qty_tp=%.6f reduce_only=%s position_idx=%s",
                            idx, price_tp, qty_tp, True, 2
                        )
                decision.state_updates["short_tp_max_index"] = short_tp_max_index

        # [REENTRY_PREV_STATE_V10_1] 다음 tick에서 TP 체결(수량 감소) 감지용 prev 저장
        try:
            setattr(state, 'prev_long_size', float(long_size))
            setattr(state, 'prev_short_size', float(short_size))
            setattr(state, 'prev_long_tp_active', bool(long_tp_active))
            setattr(state, 'prev_short_tp_active', bool(short_tp_active))
        except Exception:
            pass
        try:
            decision.state_updates['prev_long_size'] = float(long_size)
            decision.state_updates['prev_short_size'] = float(short_size)
            decision.state_updates['prev_long_tp_active'] = bool(long_tp_active)
            decision.state_updates['prev_short_tp_active'] = bool(short_tp_active)
        except Exception:
            pass

        return decision

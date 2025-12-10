from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Tuple, Optional

from strategy.feed_types import StrategyFeed, OrderInfo
from strategy.state_model import LineState
from utils.logger import logger, get_logger
from utils.calculator import calc_contract_qty
from strategy.capital import CapitalManager
from core.state_manager import get_state_manager
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
       or tag in ("FULL_EXIT", "ESCAPE_HEDGE_EXIT"):
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
        # 이미 내림차순

    return touched


def _extract_position_info(feed: StrategyFeed) -> Dict[str, Any]:
    """
    StrategyFeed / BotState / (옵션) feed.positions 에서
    현재 계좌 포지션 정보를 읽어온다.

    - dict / dataclass 모두 지원
    - KeyError 방지를 위해 항상 dict.get() 사용
    """
    logger = get_logger("grid_logic")

    long_size = short_size = hedge_size = 0.0
    long_pnl = short_pnl = 0.0

    # 1) 우선 feed.positions 가 있으면 그것을 사용
    pos_from_feed = getattr(feed, "positions", None)
    if isinstance(pos_from_feed, dict):
        long_size = float(pos_from_feed.get("long_size", 0.0) or 0.0)
        short_size = float(pos_from_feed.get("short_size", 0.0) or 0.0)
        hedge_size = float(pos_from_feed.get("hedge_size", 0.0) or 0.0)
        long_pnl = float(pos_from_feed.get("long_pnl", 0.0) or 0.0)
        short_pnl = float(pos_from_feed.get("short_pnl", 0.0) or 0.0)
    else:
        # 2) fallback: feed.state (BotState dataclass 또는 dict)
        state = feed.state
        if isinstance(state, dict):
            long_size = float(state.get("long_size", 0.0) or 0.0)
            short_size = float(state.get("short_size", 0.0) or 0.0)
            hedge_size = float(state.get("hedge_size", 0.0) or 0.0)
            long_pnl = float(state.get("long_pnl", 0.0) or 0.0)
            short_pnl = float(state.get("short_pnl", 0.0) or 0.0)
        else:
            long_size = float(getattr(state, "long_size", 0.0) or 0.0)
            short_size = float(getattr(state, "short_size", 0.0) or 0.0)
            hedge_size = float(getattr(state, "hedge_size", 0.0) or 0.0)
            long_pnl = float(getattr(state, "long_pnl", 0.0) or 0.0)
            short_pnl = float(getattr(state, "short_pnl", 0.0) or 0.0)

    # [DRY-RUN TEST ONLY]
    # DRY_RUN 환경에서 실 포지션이 항상 0이기 때문에,
    # WaveFSM 테스트와 동일하게 GridLogic에도 "가짜 포지션"을 주입한다.
    try:
        fake_pos_flag = bool(int(os.getenv("FSM_FAKE_POS", "0")))
        fake_pos_qty = float(os.getenv("FSM_FAKE_POS_QTY", "0.005"))
    except Exception:
        fake_pos_flag = False
        fake_pos_qty = 0.0

    if fake_pos_flag and long_size == 0 and short_size == 0 and hedge_size == 0:
        long_size = fake_pos_qty    # 가짜 롱 포지션
        short_size = 0.0
        hedge_size = 0.0

    # [DRY-RUN TEST ONLY] fake PnL injection for EXIT/ESCAPE test
    # 환경 변수 FSM_FAKE_PNL=1 이고, 포지션이 있고(long_size>0 등) 실제 PnL이 0일 때만,
    # long_pnl / short_pnl 에 테스트용 값을 넣어 EXIT/ESCAPE 로직을 밟아본다.
    try:
        fake_pnl_flag = bool(int(os.getenv("FSM_FAKE_PNL", "0")))
        fake_pnl_value = float(os.getenv("FSM_FAKE_PNL_VALUE", "0.0"))
    except Exception:
        fake_pnl_flag = False
        fake_pnl_value = 0.0

    if fake_pnl_flag and long_size > 0 and long_pnl == 0.0:
        # 롱 포지션 기준 테스트: 양수면 익절, 음수면 손절/ESCAPE 쪽을 강제로 밟아볼 수 있다.
        long_pnl = fake_pnl_value

    pos_info: Dict[str, Any] = {
        "long_size": long_size,
        "short_size": short_size,
        "hedge_size": hedge_size,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
    }

    logger.info(
        "[Feed] pos_for_fsm: long=%.6f short=%.6f hedge=%.6f long_pnl=%.6f short_pnl=%.6f",
        pos_info.get("long_size", 0.0),
        pos_info.get("short_size", 0.0),
        pos_info.get("hedge_size", 0.0),
        pos_info.get("long_pnl", 0.0),
        pos_info.get("short_pnl", 0.0),
    )

    return pos_info


def _compute_profit_line_index(pnl: float, size: float, p_gap: float) -> int:
    """
    TP 단계 진입/진행을 위한 profit_line_index 계산.

    명세:
      - Long:  floor((price_now - avg_entry) / P_gap)
      - Short: floor((avg_entry - price_now) / P_gap)

    여기서는 PnL과 size, p_gap 만으로 등가 계산:
      - PnL = |size| * Δprice
      - Δprice = PnL / |size|
      - profit_line_index = floor(max(0, Δprice / P_gap))
    """
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
    return int(lines // 1)  # floor


def _pnl_sign(x: float) -> int:
    x = float(x or 0.0)
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _compute_avg_entry_from_pnl_long(price_now: float, size: float, pnl: float) -> Optional[float]:
    """
    Long 기준 avg_entry 추정:
      pnl = (price_now - avg_entry) * size
      avg_entry = price_now - pnl / size
    """
    try:
        price_now = float(price_now)
        size = float(size)
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None

    if size == 0:
        return None
    return price_now - pnl / size


def _compute_avg_entry_from_pnl_short(price_now: float, size: float, pnl: float) -> Optional[float]:
    """
    Short 기준 avg_entry 추정:
      pnl = (avg_entry - price_now) * size
      avg_entry = price_now + pnl / size
    """
    try:
        price_now = float(price_now)
        size = float(size)
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None

    if size == 0:
        return None
    return price_now + pnl / size


# ------------------------------
# GridLogic
# ------------------------------


class GridLogic:
    """
    v10.1 Grid 엔진.

    기능:
    - P_center/P_gap 기반 라인 정의 (-12..+12)
    - Overlap Zone / 허용 라인 (Long: -12~+7, Short: -7~+12)
    - price_prev/price_now 기반 Line Touch 검출
    - Start-up Entry / DCA(Scale-in) / TP 판단
    - remain_seed_dir ≥ unit_seed_dir last-chunk 규칙 적용
    - ESCAPE/NEWS/CB Gate
    - LineMemory(FREE/OPEN/LOCKED_LOSS) Gate + 리셋 (3.2.5, 3.3.5, 3.3.6)
    """

    def __init__(self, capital: CapitalManager | None = None) -> None:
        # capital 은 현재 직접 사용하지 않지만,
        # v10.1 seed/step 계산 설계와의 정합성을 위해 보관한다.
        self.capital = capital
        # capital.py 의 SAFE_FACTOR(0.9)와 split_count(13)를 그대로 따른다.
        self.safe_factor = SAFE_FACTOR

    # --------------------------
    # Seed / last-chunk 계산
    # --------------------------

    def _seed_gate(self, state, side: str, override_k_dir: Optional[int] = None) -> SeedGateState:
        """
        BotState 에서 해당 방향 seed 사용 가능 여부 계산.

        명세:
        - unit_seed_dir        : state.unit_seed_long/short
        - effective_seed_total : state.long/short_seed_total_effective
        - allocated_seed_dir   = effective_seed_total / SAFE_FACTOR
        - used_seed_dir        = k_dir * unit_seed_dir
        - remain_seed_dir      = allocated_seed_dir - used_seed_dir
        - remain_seed_dir ≥ unit_seed_dir 이면 새로운 1분할 진입 허용
        - k_dir 는 0 ~ 13 범위로 클램프
        """
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

    # --------------------------
    # 메인 프로세스
    # --------------------------

    def process(self, feed: StrategyFeed) -> GridDecision:
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

        # LineMemory (dict[int, LineState])
        line_memory_long: Dict[int, LineState] = dict(getattr(state, "line_memory_long", {}) or {})
        line_memory_short: Dict[int, LineState] = dict(getattr(state, "line_memory_short", {}) or {})

        # ESCAPE / NEWS / CB 상태
        wave_state = getattr(feed, "wave_state", None)
        if wave_state is not None:
            escape_long_active = bool(getattr(getattr(wave_state, "long_escape", wave_state), "active", False))
            escape_short_active = bool(getattr(getattr(wave_state, "short_escape", wave_state), "active", False))
        else:
            escape_long_active = bool(
                getattr(state, "escape_long_active", False)
                or getattr(state, "escape_active_long", False)
            )
            escape_short_active = bool(
                getattr(state, "escape_short_active", False)
                or getattr(state, "escape_active_short", False)
            )

        news_block = bool(
            getattr(feed, "news_block", False)
            or getattr(state, "news_block", False)
        )
        cb_block = bool(
            getattr(feed, "cb_block", False)
            or getattr(state, "cb_block", False)
        )

        # 포지션 / PnL 정보
        pos_info = _extract_position_info(feed)
        long_size = float(pos_info.get("long_size", 0.0) or 0.0)
        short_size = float(pos_info.get("short_size", 0.0) or 0.0)
        long_pnl = float(pos_info.get("long_pnl", 0.0) or 0.0)
        short_pnl = float(pos_info.get("short_pnl", 0.0) or 0.0)

        # ------------------------------
        # LineMemory Reset 규칙 (3.2.5, 3.3.5 / 3.3.6)
        # ------------------------------
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

        # (A) 전량 익절(또는 손절 등) 후 size=0 이 된 경우 → line_memory 전체 리셋 + k_dir=0
        if long_size == 0.0 and prev_long_nonzero:
            reset_long_lines = True
            reset_long_seed = True
        if short_size == 0.0 and prev_short_nonzero:
            reset_short_lines = True
            reset_short_seed = True

        # (B) 부분익절 이후 PnL >= 0 상태 유지 → 다시 손실 구간(PnL<0)으로 돌아오는 순간 → line_memory 리셋
        if long_size != 0.0 and prev_long_pnl_sign >= 0 and long_pnl_sign < 0:
            reset_long_lines = True
        if short_size != 0.0 and prev_short_pnl_sign >= 0 and short_pnl_sign < 0:
            reset_short_lines = True

        if reset_long_lines:
            line_memory_long = {}
            decision.state_updates["line_memory_long"] = line_memory_long
            # TP 단계도 초기화
            decision.state_updates["long_tp_active"] = False
            decision.state_updates["long_tp_max_index"] = 0

        if reset_short_lines:
            line_memory_short = {}
            decision.state_updates["line_memory_short"] = line_memory_short
            decision.state_updates["short_tp_active"] = False
            decision.state_updates["short_tp_max_index"] = 0

        # 전량 익절 후 seed 측면 초기화 (k_dir=0)
        k_long_state = int(getattr(state, "k_long", 0) or 0)
        k_short_state = int(getattr(state, "k_short", 0) or 0)

        k_long_effective = 0 if reset_long_seed else k_long_state
        k_short_effective = 0 if reset_short_seed else k_short_state

        if reset_long_seed and k_long_state != 0:
            decision.state_updates["k_long"] = 0
        if reset_short_seed and k_short_state != 0:
            decision.state_updates["k_short"] = 0

        # 현재 tick 기준 상태 저장 (size≠0 여부, pnl sign)
        decision.state_updates["long_pos_nonzero"] = (long_size != 0.0)
        decision.state_updates["short_pos_nonzero"] = (short_size != 0.0)
        decision.state_updates["long_pnl_sign"] = long_pnl_sign
        decision.state_updates["short_pnl_sign"] = short_pnl_sign

        # ------------------------------
        # Seed Gate (last-chunk 규칙, remain_seed_dir ≥ unit_seed_dir)
        # ------------------------------
        seed_long = self._seed_gate(state, "LONG", override_k_dir=k_long_effective)
        seed_short = self._seed_gate(state, "SHORT", override_k_dir=k_short_effective)

        # ------------------------------
        # OPEN ORDERS 분류
        # ------------------------------
        grid_orders_by_side_idx: Dict[Tuple[str, int], List[OrderInfo]] = {}
        old_wave_orders: List[str] = []

        for o in feed.open_orders:
            kind, idx, side = classify_order(o, wave_id)
            if kind == "GRID_CURRENT" and idx is not None and side is not None:
                key = (side.upper(), int(idx))
                grid_orders_by_side_idx.setdefault(key, []).append(o)
            elif kind == "GRID_OLD_WAVE":
                old_wave_orders.append(o.order_id)

        # 이전 wave 의 GRID 주문은 전부 취소
        for oid in old_wave_orders:
            decision.grid_cancels.append(oid)

        # ------------------------------
        # ESCAPE / NEWS / CB 글로벌 게이트
        # ------------------------------
        if news_block or cb_block or mode == "NEWS_BLOCK":
            # 모든 GRID Maker 주문 취소, 신규 생성 없음
            for orders in grid_orders_by_side_idx.values():
                for o in orders:
                    decision.grid_cancels.append(o.order_id)
            logger.info(
                "[GRID] NEWS/CB Block active → 모든 Grid 엔트리/TP 차단 및 기존 Maker 취소"
            )
            return decision

        # ------------------------------
        # 1) 라인 터치 검출
        # ------------------------------
        touched_indices = detect_touched_lines(
            price_prev=price_prev,
            price_now=price_now,
            p_center=p_center,
            p_gap=p_gap,
            idx_min=min(LONG_LINE_MIN, SHORT_LINE_MIN),
            idx_max=max(LONG_LINE_MAX, SHORT_LINE_MAX),
        )

        # ------------------------------
        # 2) Start-up Entry (3.2.3)
        #   - Wave 시작 시 BUY/SELL 한 개씩 Maker 배치 (grid_index=0)
        #   - qty = unit_seed_long / unit_seed_short (1유닛)
        #   - startup_done 플래그로 1회만 실행
        # ------------------------------
        k_long = k_long_effective
        k_short = k_short_effective

        startup_done = bool(getattr(state, "startup_done", False))
        startup_entries_created = 0

        if (
            wave_id > 0
            and not startup_done
            and p_center > 0.0
            and p_gap > 0.0
        ):
            # LONG Start-up at grid_index = 0
            if (
                long_size == 0.0
                and k_long == 0
                and not escape_long_active
                and seed_long.can_open_unit
            ):
                long_idx = 0
                # LONG 허용 라인 범위 내에서만
                if LONG_LINE_MIN <= long_idx <= LONG_LINE_MAX:
                    line_state = line_memory_long.get(long_idx, LineState.FREE)
                    if line_state == LineState.FREE:
                        price_l = line_price(p_center, p_gap, long_idx)
                        notional_l = seed_long.unit_seed * GRID_LEVERAGE
                        qty_l = calc_contract_qty(
                            usdt_amount=notional_l,
                            price=price_l,
                            symbol="BTCUSDT",
                        )
                        if qty_l > 0.0:
                            key_l = ("BUY", long_idx)
                            existing_l = choose_main_order(grid_orders_by_side_idx.get(key_l, []))
                            if existing_l is None:
                                decision.grid_entries.append(
                                    GridOrderSpec(
                                        side="BUY",
                                        price=price_l,
                                        qty=qty_l,
                                        grid_index=long_idx,
                                        wave_id=wave_id,
                                        mode="A",
                                    )
                                )
                                startup_entries_created += 1

            # SHORT Start-up at grid_index = 0
            if (
                short_size == 0.0
                and k_short == 0
                and not escape_short_active
                and seed_short.can_open_unit
            ):
                short_idx = 0
                # SHORT 허용 라인 범위 내에서만
                if SHORT_LINE_MIN <= short_idx <= SHORT_LINE_MAX:
                    line_state = line_memory_short.get(short_idx, LineState.FREE)
                    if line_state == LineState.FREE:
                        price_s = line_price(p_center, p_gap, short_idx)
                        notional_s = seed_short.unit_seed * GRID_LEVERAGE
                        qty_s = calc_contract_qty(
                            usdt_amount=notional_s,
                            price=price_s,
                            symbol="BTCUSDT",
                        )
                        if qty_s > 0.0:
                            key_s = ("SELL", short_idx)
                            existing_s = choose_main_order(grid_orders_by_side_idx.get(key_s, []))
                            if existing_s is None:
                                decision.grid_entries.append(
                                    GridOrderSpec(
                                        side="SELL",
                                        price=price_s,
                                        qty=qty_s,
                                        grid_index=short_idx,
                                        wave_id=wave_id,
                                        mode="A",
                                    )
                                )
                                startup_entries_created += 1

            if startup_entries_created > 0:
                decision.state_updates["startup_done"] = True
                logger.info(
                    "[GRID] Start-up Entry 실행 완료: wave_id=%s, entries=%s",
                    wave_id,
                    startup_entries_created,
                )

        # ------------------------------
        # 3) DCA (Scale-in) – 4 조건 (3.2.4)
        # ------------------------------
        # LONG DCA:
        #  (1) long_pnl < 0 (손실 구간)
        #  (2) 손실 방향(가격 하락)으로 새로운 라인 터치
        #  (3) 해당 라인 line_state == FREE
        #  (4) remain_seed >= unit_seed (last-chunk)
        if (
            long_pnl < 0.0
            and long_size != 0.0
            and price_now < price_prev
            and not escape_long_active
            and seed_long.can_open_unit
        ):
            remaining_seed = seed_long.remain_seed
            available_steps = max(0, SPLIT_COUNT - seed_long.k_dir)
            for idx in touched_indices:
                if idx < LONG_LINE_MIN or idx > LONG_LINE_MAX:
                    continue
                if available_steps <= 0:
                    break
                line_state = line_memory_long.get(idx, LineState.FREE)
                if line_state != LineState.FREE:
                    continue
                if remaining_seed + 1e-9 < seed_long.unit_seed:
                    break
                price_i = line_price(p_center, p_gap, idx)
                notional = seed_long.unit_seed * GRID_LEVERAGE
                qty = calc_contract_qty(
                    usdt_amount=notional,
                    price=price_i,
                    symbol="BTCUSDT",
                )
                if qty <= 0.0:
                    continue
                key = ("BUY", idx)
                existing = choose_main_order(grid_orders_by_side_idx.get(key, []))
                if existing is None:
                    decision.grid_entries.append(
                        GridOrderSpec(
                            side="BUY",
                            price=price_i,
                            qty=qty,
                            grid_index=idx,
                            wave_id=wave_id,
                            mode="A",
                        )
                    )
                    remaining_seed -= seed_long.unit_seed
                    available_steps -= 1

        # SHORT DCA (손실 방향: 가격 상승)
        if (
            short_pnl < 0.0
            and short_size != 0.0
            and price_now > price_prev
            and not escape_short_active
            and seed_short.can_open_unit
        ):
            remaining_seed = seed_short.remain_seed
            available_steps = max(0, SPLIT_COUNT - seed_short.k_dir)
            for idx in touched_indices:
                if idx < SHORT_LINE_MIN or idx > SHORT_LINE_MAX:
                    continue
                if available_steps <= 0:
                    break
                line_state = line_memory_short.get(idx, LineState.FREE)
                if line_state != LineState.FREE:
                    continue
                if remaining_seed + 1e-9 < seed_short.unit_seed:
                    break
                price_j = line_price(p_center, p_gap, idx)
                notional = seed_short.unit_seed * GRID_LEVERAGE
                qty = calc_contract_qty(
                    usdt_amount=notional,
                    price=price_j,
                    symbol="BTCUSDT",
                )
                if qty <= 0.0:
                    continue
                key = ("SELL", idx)
                existing = choose_main_order(grid_orders_by_side_idx.get(key, []))
                if existing is None:
                    decision.grid_entries.append(
                        GridOrderSpec(
                            side="SELL",
                            price=price_j,
                            qty=qty,
                            grid_index=idx,
                            wave_id=wave_id,
                            mode="A",
                        )
                    )
                    remaining_seed -= seed_short.unit_seed
                    available_steps -= 1

        # ------------------------------
        # 4) TP (익절) & Refill (3.3)
        # ------------------------------
        profit_idx_long = _compute_profit_line_index(long_pnl, long_size, p_gap)
        profit_idx_short = _compute_profit_line_index(short_pnl, short_size, p_gap)

        long_tp_active = bool(getattr(state, "long_tp_active", False))
        short_tp_active = bool(getattr(state, "short_tp_active", False))

        long_tp_max_index = int(getattr(state, "long_tp_max_index", 0) or 0)
        short_tp_max_index = int(getattr(state, "short_tp_max_index", 0) or 0)

        # LONG TP 단계 진입 (profit_line_index 가 처음으로 3 이상이 되는 순간)
        if (
            long_size != 0.0
            and long_pnl > 0.0
            and profit_idx_long >= 3
            and not long_tp_active
        ):
            long_tp_active = True
            long_tp_max_index = profit_idx_long - 1
            decision.state_updates["long_tp_active"] = True
            decision.state_updates["long_tp_max_index"] = long_tp_max_index

        # SHORT TP 단계 진입
        if (
            short_size != 0.0
            and short_pnl > 0.0
            and profit_idx_short >= 3
            and not short_tp_active
        ):
            short_tp_active = True
            short_tp_max_index = profit_idx_short - 1
            decision.state_updates["short_tp_active"] = True
            decision.state_updates["short_tp_max_index"] = short_tp_max_index

        # LONG TP 실행
        if (
            long_size != 0.0
            and long_pnl > 0.0
            and long_tp_active
            and not escape_long_active
        ):
            avg_entry_long = _compute_avg_entry_from_pnl_long(price_now, long_size, long_pnl)
            if avg_entry_long is not None and p_gap > 0:
                new_max = profit_idx_long
                # 한 tick 에 여러 profit line 을 통과한 경우 모두 처리 (최대 SPLIT_COUNT 단계만)
                upper_bound = min(new_max, long_tp_max_index + SPLIT_COUNT)
                for idx in range(long_tp_max_index + 1, upper_bound + 1):
                    if idx < 3:
                        continue
                    price_tp = avg_entry_long + idx * p_gap
                    notional = seed_long.unit_seed * GRID_LEVERAGE if seed_long.unit_seed > 0 else 0.0
                    if notional <= 0.0:
                        break
                    qty_tp = calc_contract_qty(
                        usdt_amount=notional,
                        price=price_tp,
                        symbol="BTCUSDT",
                    )
                    # 포지션 크기를 초과하지 않도록 마지막 분할에서 clamp
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
                            )
                        )
                        long_tp_max_index = idx

                decision.state_updates["long_tp_max_index"] = long_tp_max_index

        # SHORT TP 실행
        if (
            short_size != 0.0
            and short_pnl > 0.0
            and short_tp_active
            and not escape_short_active
        ):
            avg_entry_short = _compute_avg_entry_from_pnl_short(price_now, short_size, short_pnl)
            if avg_entry_short is not None and p_gap > 0:
                new_max = profit_idx_short
                upper_bound = min(new_max, short_tp_max_index + SPLIT_COUNT)
                for idx in range(short_tp_max_index + 1, upper_bound + 1):
                    if idx < 3:
                        continue
                    price_tp = avg_entry_short - idx * p_gap
                    notional = seed_short.unit_seed * GRID_LEVERAGE if seed_short.unit_seed > 0 else 0.0
                    if notional <= 0.0:
                        break
                    qty_tp = calc_contract_qty(
                        usdt_amount=notional,
                        price=price_tp,
                        symbol="BTCUSDT",
                    )
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
                            )
                        )
                        short_tp_max_index = idx

                decision.state_updates["short_tp_max_index"] = short_tp_max_index

        return decision

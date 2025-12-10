# 파일: strategy/liquidation_slicer.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Callable
import time


# ============================================================
# SlicePlan: 청산 계획 표현
# ============================================================

@dataclass
class SlicePlan:
    mode: str                 # "ALL_AT_ONCE" or "SLICED"
    slices: List[float]       # 각 슬라이스 notional (USDT 기준)
    reason: str               # 사람이 이해할 수 있는 설명
    total_notional: float     # 전체 청산 예정 노티오널
    max_slice_notional: float # 슬라이스 기준값 (기본 70,000)
    side: Optional[str] = None  # "LONG" / "SHORT" / "BUY" / "SELL" 등 (옵션)


# ============================================================
# Core planner: 70k 슬라이스 + high-vol override
# ============================================================

def plan_sliced_close_notional(
    total_notional: float,
    max_slice_notional: float = 70_000.0,
    *,
    # v10.1 기준 high-vol 입력 (절대 변동률, 예: 0.001 = 0.1%)
    vol_1s: float | None = None,   # 직전 1초 변동률 절대값
    vol_1m: float | None = None,   # 최근 1분 변동률 절대값
    # 레거시 호환용 alias (기존 spike_* 이름으로 호출하더라도 동작)
    spike_1s_abs_pct: float | None = None,
    range_1m_abs_pct: float | None = None,
    side: Optional[str] = None,
) -> SlicePlan:
    """
    WaveBot v10.1 4.3 Market 청산 슬라이싱 규칙.

    입력:
      - total_notional : USDT 기준 전체 청산 노티오널 N
      - vol_1s         : 직전 1초 변동률 절대값 (예: 0.001 = 0.1%)
      - vol_1m         : 최근 1분 변동률 절대값

    규칙:

    1) 기본 슬라이스 (70k)
       - N ≤ 70,000 → 한 번에 Market 청산 (ALL_AT_ONCE)
       - N > 70,000 →
            while N > 70,000:
                70,000 슬라이스 추가
                N -= 70,000
            마지막 N(≤70,000)은 한 번에 추가
         → mode="SLICED", slices=[70k, 70k, ..., last]

    2) high-vol override (4.3.2)
       - vol_1s ≥ 0.001      (0.1% 이상)
       - vol_1s ≥ 3 * vol_1m (최근 1분 변동폭의 3배 이상)
       두 조건을 동시에 만족하면:
         → 슬라이스 반복 없이 남은 N 전액을 한 번에 청산
         → mode="ALL_AT_ONCE", slices=[N], reason="high_vol_override_all_at_once"
    """
    total = float(total_notional)
    max_slice = float(max_slice_notional)

    # 레거시 인자 alias 처리 (기존 spike_*, range_* 이름으로 호출해도 호환)
    if vol_1s is None and spike_1s_abs_pct is not None:
        vol_1s = float(spike_1s_abs_pct)
    if vol_1m is None and range_1m_abs_pct is not None:
        vol_1m = float(range_1m_abs_pct)

    vol_1s = float(vol_1s or 0.0)
    vol_1m = float(vol_1m or 0.0)

    # 청산할 Notional 이 없으면 no-op
    if total <= 0.0:
        return SlicePlan(
            mode="ALL_AT_ONCE",
            slices=[],
            reason="no_notional_to_close",
            total_notional=total,
            max_slice_notional=max_slice,
            side=side,
        )

    # -----------------------------
    # 2) high-vol override
    #   - vol_1s ≥ 0.001
    #   - vol_1s ≥ 3 * vol_1m
    # -----------------------------
    if vol_1s >= 0.001 and vol_1s >= 3.0 * vol_1m:
        return SlicePlan(
            mode="ALL_AT_ONCE",
            slices=[total],
            reason="high_vol_override_all_at_once",
            total_notional=total,
            max_slice_notional=max_slice,
            side=side,
        )

    # -----------------------------
    # 1) 기본 슬라이싱 규칙 (70k 단위)
    # -----------------------------
    if total <= max_slice:
        # 임계값 이하 → 한 번에 청산
        return SlicePlan(
            mode="ALL_AT_ONCE",
            slices=[total],
            reason="below_or_equal_threshold_all_at_once",
            total_notional=total,
            max_slice_notional=max_slice,
            side=side,
        )

    # N > max_slice → 70k 단위로 슬라이스
    remaining = total
    slices: List[float] = []

    while remaining > max_slice:
        slices.append(max_slice)
        remaining -= max_slice

    if remaining > 0:
        slices.append(remaining)

    return SlicePlan(
        mode="SLICED",
        slices=slices,
        reason="sliced_by_70k_threshold",
        total_notional=total,
        max_slice_notional=max_slice,
        side=side,
    )


# ============================================================
# LiquidationSlicer: OrderManager 에서 쓰기 위한 래퍼
# ============================================================

class LiquidationSlicer:
    """
    v10.1 Liquidation Slicer.

    역할:
      - 총 Market 청산 Notional N 에 대해 70k 단위 슬라이스 계획 수립
      - high-vol override 판단 (vol_1s / vol_1m 기준)
      - (옵션) place_market_order 콜백을 사용해 실제 슬라이스 실행 + 1초 sleep

    OrderManager 연동 가이드:

      1) FULL_EXIT / HEDGE_EXIT 등에서 청산해야 할 총 notional(N) 계산
         - 예: N = abs(position_size) * price

      2) slicer.plan_close_notional(...) 으로 SlicePlan 을 만든 뒤,
         각 slice 를 순회하면서:
            qty = slice_notional / price
            exchange_api.place_market_order(side, qty)
            if plan.mode == "SLICED": time.sleep(1)

         또는 아래 execute_sliced_liquidation(...) 메서드를 사용해
         sleep 포함 실행을 맡길 수 있다.
    """

    def __init__(self, max_slice_notional: float = 70_000.0) -> None:
        self.max_slice_notional = float(max_slice_notional)

    # ---- 단순 Plan 생성용 ----
    def plan_close_notional(
        self,
        total_notional: float,
        *,
        vol_1s: float = 0.0,
        vol_1m: float = 0.0,
        side: Optional[str] = None,
    ) -> SlicePlan:
        return plan_sliced_close_notional(
            total_notional=total_notional,
            max_slice_notional=self.max_slice_notional,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            side=side,
        )

    # ---- 실제 슬라이스 실행용 (옵션) ----
    def execute_sliced_liquidation(
        self,
        side: str,
        total_notional: float,
        *,
        price: float,
        vol_1s: float = 0.0,
        vol_1m: float = 0.0,
        place_market_order: Callable[[str, float], None],
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> SlicePlan:
        """
        70k 슬라이스 + 1초 지연 실행까지 포함한 helper.

        인자:
          - side              : "BUY"/"SELL" 또는 "LONG"/"SHORT"
          - total_notional    : USDT 기준 청산 노티오널 N
          - price             : 현재 가격 (USDT)
          - vol_1s, vol_1m    : high-vol override 판단용 변동성
          - place_market_order: (side, qty) 를 받아 실제 Market 주문을 내는 콜백
                                예: lambda s, q: exchange.place_market_order(s, q)
          - sleep_fn          : 1초 지연을 대체하고 싶을 때 주입 (기본 time.sleep)

        동작:
          1) plan_close_notional(...) 으로 SlicePlan 생성
          2) 각 slice_notional 에 대해 qty = slice_notional / price 계산
          3) place_market_order(side, qty) 호출
          4) plan.mode == "SLICED" 인 경우, 슬라이스 사이에 1초 sleep
        """
        if price <= 0.0 or total_notional <= 0.0:
            # 청산할 게 없거나 가격 비정상 → 아무 것도 안 함
            return SlicePlan(
                mode="ALL_AT_ONCE",
                slices=[],
                reason="invalid_price_or_notional",
                total_notional=float(total_notional),
                max_slice_notional=self.max_slice_notional,
                side=side,
            )

        plan = self.plan_close_notional(
            total_notional=total_notional,
            vol_1s=vol_1s,
            vol_1m=vol_1m,
            side=side,
        )

        if sleep_fn is None:
            sleep_fn = time.sleep

        # 슬라이스 순회 + 1초 지연
        for idx, notional in enumerate(plan.slices):
            if notional <= 0.0:
                continue
            qty = notional / price
            if qty <= 0.0:
                continue

            # 실제 Market 청산 콜백 호출
            place_market_order(side, qty)

            # SLICED 모드에서 마지막 슬라이스가 아니면 1초 sleep
            if plan.mode == "SLICED" and idx < len(plan.slices) - 1:
                sleep_fn(1.0)

        return plan

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


# ==============================
# Wave 자본 스냅샷 / Seed 구조
# ==============================

# v10.1 명세 기본값
SAFE_FACTOR_DEFAULT = 0.9       # effective seed 안전계수 (0.9)
SIDE_ALLOC_RATIO_DEFAULT = 0.25 # Long/Short 각 25%
SPLIT_COUNT_DEFAULT = 13        # 13분할 (동시 보유 최대 13개)


@dataclass
class WaveCapitalSnapshot:
    """
    Wave 시작 시점의 자본/seed 스냅샷 (2.1, 2.2, 2.3).

    모든 값의 단위는 USDT (자기자본/마진 기준) 이다.

    - total_balance_snap        : Wave 시작 시 계정 총 자산 (USDT)
    - allocated_seed_long       : Long 방향 nominal seed (Total_Balance_snap * 0.25)
    - allocated_seed_short      : Short 방향 nominal seed (Total_Balance_snap * 0.25)
    - reserve_seed              : 나머지 nominal reserve seed (≈ 50%)
    - long_seed_total_effective : Long 방향 effective seed 총액 (= allocated_long * SAFE_FACTOR)
    - short_seed_total_effective: Short 방향 effective seed 총액 (= allocated_short * SAFE_FACTOR)
    - reserve_effective         : effective 기준 남는 USDT
                                  (= total_balance_snap - (long_eff + short_eff))
    - unit_seed_long            : Long 방향 1분할 unit seed (= long_seed_total_effective / 13)
    - unit_seed_short           : Short 방향 1분할 unit seed (= short_seed_total_effective / 13)
    """
    total_balance_snap: float

    allocated_seed_long: float
    allocated_seed_short: float
    reserve_seed: float

    long_seed_total_effective: float
    short_seed_total_effective: float
    reserve_effective: float

    unit_seed_long: float
    unit_seed_short: float


@dataclass
class SeedUsage:
    """
    한 방향(Long/Short)에 대한 seed 사용량 요약 구조 (2.3, 2.3.4, 2.3.5, 2.3.6).

    단위는 모두 USDT.

    - allocated_seed       : 해당 방향에 배정된 nominal seed (Total_Balance_snap * 0.25)
    - unit_seed            : 13분할 기준 1분할 effective seed (= effective_seed_total / 13)
    - used_seed            : k_dir * unit_seed (dust 무시)
    - remain_seed          : allocated_seed - used_seed  (음수면 0으로 클램프)
    - k_dir                : '로직 상 1분할 Step을 한 번 시도하고 마무리한 횟수' (정수)
                             = 현재 동시에 열려 있는 분할 수 개념
    - effective_seed_total : 해당 방향 전체 effective seed 총액 (= allocated_seed * SAFE_FACTOR)
    """
    allocated_seed: float
    unit_seed: float
    used_seed: float
    remain_seed: float
    k_dir: int
    effective_seed_total: float = 0.0


class CapitalManager:
    """
    WaveBot v10.1 자금 관리 명세 (2장) 구현체.

    핵심 원칙
    ----------
    - Wave 시작/Reset 시 `total_balance_snap` 를 계정 총 자산(USDT)으로 스냅샷.
    - Wave 진행 동안 seed/리스크 계산은 **항상 이 스냅샷 값**만 사용.
    - 25/25/50 nominal seed 배분 + SAFE_FACTOR(0.9) 적용한 effective seed.
    - 방향별 13분할(unit_seed_long/unit_seed_short)과 k_dir 기반 used / remain / last-chunk 관리.
    - 모든 금액(seed/used/remain/reserve)은 USDT 기준 자기자본(마진) 단위로 관리.
    - 펀딩비는 장기적으로 상쇄된다고 가정하고 여기서는 별도 모델링하지 않음.
    - 레버리지는 Cross 7x 를 기본 전제로, unit_notional = unit_seed * leverage.

    파라미터
    ----------
    side_alloc_ratio : 한 방향에 배정되는 nominal seed 비율 (기본 0.25 = 25%)
    split_count      : 분할 개수 (기본 13분할)
    leverage         : Cross 레버리지 (기본 7x) → unit_notional 계산에 사용
    safe_factor      : effective seed 안전 계수 (기본 0.9)
    """

    def __init__(
        self,
        side_alloc_ratio: float = SIDE_ALLOC_RATIO_DEFAULT,
        split_count: int = SPLIT_COUNT_DEFAULT,
        leverage: float = 7.0,
        safe_factor: float = SAFE_FACTOR_DEFAULT,
    ) -> None:
        # 1) 자산 배분 비율 (기본 25/25/50)
        self.side_alloc_ratio = float(side_alloc_ratio)
        self.split_count = int(split_count)

        # 2) Cross 레버리지 (기본 7x) - notional 계산에만 사용
        self.leverage = float(leverage)

        # 3) SAFE_FACTOR (0 ~ 1 로 클램프)
        sf = float(safe_factor)
        if sf < 0.0:
            sf = 0.0
        if sf > 1.0:
            sf = 1.0
        self.safe_factor = sf

    # --------------------------
    # Wave 스냅샷 / seed 배분 (2.1, 2.2, 2.4)
    # --------------------------

    def compute_wave_snapshot(self, total_balance_snap: float) -> WaveCapitalSnapshot:
        """
        Wave 시작/Reset 시 "한 번만" 호출해야 하는 함수.

        - total_balance_snap : Wave 시작 시점 계정 총 자산 (USDT)
        - 이 값은 Wave 종료 전까지 seed 계산에서 변하지 않는다고 가정한다.
        """
        # 음수 방지
        tb = max(float(total_balance_snap), 0.0)

        # 25/25/남은 거(≈50) nominal 배분 (USDT 기준)
        allocated_seed_long = tb * self.side_alloc_ratio
        allocated_seed_short = tb * self.side_alloc_ratio
        reserve_seed = max(0.0, tb - (allocated_seed_long + allocated_seed_short))

        # effective seed (SAFE_FACTOR 적용) - USDT 기준
        long_seed_total_effective = allocated_seed_long * self.safe_factor
        short_seed_total_effective = allocated_seed_short * self.safe_factor

        # effective 기준 reserve (남는 USDT)
        reserve_effective = max(
            0.0,
            tb - (long_seed_total_effective + short_seed_total_effective),
        )

        # 방향별 13분할 unit seed
        if self.split_count > 0:
            unit_seed_long = long_seed_total_effective / float(self.split_count)
            unit_seed_short = short_seed_total_effective / float(self.split_count)
        else:
            unit_seed_long = 0.0
            unit_seed_short = 0.0

        return WaveCapitalSnapshot(
            total_balance_snap=tb,
            allocated_seed_long=allocated_seed_long,
            allocated_seed_short=allocated_seed_short,
            reserve_seed=reserve_seed,
            long_seed_total_effective=long_seed_total_effective,
            short_seed_total_effective=short_seed_total_effective,
            reserve_effective=reserve_effective,
            unit_seed_long=unit_seed_long,
            unit_seed_short=unit_seed_short,
        )

    # --------------------------
    # 기본 seed / unit 계산 (한 방향 기준) (2.2, 2.3)
    # --------------------------

    def allocated_seed(self, total_balance_snap: float) -> float:
        """
        한 방향(Long 또는 Short)에 배정되는 nominal seed (USDT).

        v10.1 명세:
        - allocated_seed_dir = Total_Balance_snap * 0.25
        """
        tb = max(float(total_balance_snap), 0.0)
        return tb * self.side_alloc_ratio

    def effective_seed_total(self, total_balance_snap: float) -> float:
        """
        SAFE_FACTOR(예: 0.9)를 곱한 한 방향 effective seed 총액 (USDT).

        v10.1 명세:
        - seed_total_effective_dir = allocated_seed_dir * SAFE_FACTOR
        """
        alloc = self.allocated_seed(total_balance_snap)
        return alloc * self.safe_factor

    def unit_seed(self, total_balance_snap: float) -> float:
        """
        13분할 기준 1분할 effective seed (USDT).

        v10.1 명세:
        - unit_seed_dir = seed_total_effective_dir / 13
        """
        eff_total = self.effective_seed_total(total_balance_snap)
        if self.split_count <= 0:
            return 0.0
        return eff_total / float(self.split_count)

    def unit_notional(self, total_balance_snap: float) -> float:
        """
        1분할 주문 Notional (USDT).

        v10.1 명세 (Cross 7x 가정):
        - notional_per_step ≈ unit_seed_dir * 7
        - 여기서는 leverage 파라미터를 곱해 계산한다.

        실제 주문 수량(qty) 계산은
        utils.calculator.calc_contract_qty 를 사용하는 쪽(주문 모듈)에서
        unit_notional 과 price 를 이용해 수행해야 한다.
        """
        return self.unit_seed(total_balance_snap) * self.leverage

    # --------------------------
    # 현재 k_dir 기준 seed 사용량 계산 (2.3.4, 2.3.6)
    # --------------------------

    def compute_seed_usage(
        self,
        total_balance_snap: float,
        k_dir: int,  # <-- 외부에서 관리하는 확정된 k_dir (현재 열린 분할 수)
    ) -> SeedUsage:
        """
        [v10.1 명세 반영]

        - total_balance_snap : Wave 시작 시 스냅샷 잔고 (Wave 동안 고정된다고 가정)
        - k_dir              : '로직 상 1분할 Step을 한 번 시도하고 마무리한 횟수'
                               = 현재 방향에서 동시에 열려 있는 분할 수

        정의:
        - unit_seed_dir      = seed_total_effective_dir / 13
        - used_seed_dir      = k_dir * unit_seed_dir
        - remain_seed_dir    = allocated_seed_dir - used_seed_dir
        - 실제 체결 USDT와 used_seed_dir 차이는 dust 로 간주하고
          seed 회계에서는 조정하지 않는다.
        """
        # nominal / effective / unit 계산
        allocated = self.allocated_seed(total_balance_snap)
        effective_total = self.effective_seed_total(total_balance_snap)
        unit = self.unit_seed(total_balance_snap)

        # k_dir은 0 이상, split_count 이하로 제한 → 동시 보유 분할 수 최대 13 확보
        k_dir_int = max(0, min(int(k_dir), self.split_count))

        used = float(k_dir_int) * unit
        # remain_seed_dir = allocated_seed_dir - used_seed_dir, 음수 방지
        remain = max(0.0, allocated - used)

        return SeedUsage(
            allocated_seed=allocated,
            unit_seed=unit,
            used_seed=used,
            remain_seed=remain,
            k_dir=k_dir_int,
            effective_seed_total=effective_total,
        )

    # --------------------------
    # 진입 가능 여부 / last-chunk 규칙 (2.3.5, 2.3.6)
    # --------------------------

    def can_open_new_unit(
        self,
        total_balance_snap: float,
        k_dir: int,  # <-- 외부 k_dir (현재 열린 분할 수)
    ) -> Tuple[bool, SeedUsage]:
        """
        [v10.1 명세 반영]
        현재 방향 k_dir 기준으로 '새로운 1분할 진입이 가능한지' 여부를 판단한다.

        규칙:
        - remain_seed_dir ≥ unit_seed_dir 일 때만 새로운 1분할 진입 허용.
        - remain_seed_dir < unit_seed_dir 이면:
            · 해당 Wave 동안 더 이상의 신규 1분할 진입은 금지.
            · 남은 seed 는 dust 로 남겨두고 진입에 사용하지 않음.
            · 마지막 조각을 줄여서 진입하는 로직은 허용하지 않는다.
        - 추가 안전장치: k_dir >= split_count(예: 13) 이면 동시 보유 분할 상한에 도달한 것으로 보고
          더 이상 신규 진입을 허용하지 않는다.
        """
        usage = self.compute_seed_usage(total_balance_snap, k_dir)

        # unit_seed 가 0이면 어떤 경우에도 진입할 수 없음
        if usage.unit_seed <= 0.0:
            return False, usage

        # 동시 보유 분할 상한 체크 (13분할)
        if usage.k_dir >= self.split_count:
            return False, usage

        # last-chunk 규칙: remain_seed_dir ≥ unit_seed_dir 이어야 새로운 1분할 진입 허용
        if usage.remain_seed >= usage.unit_seed:
            return True, usage

        # remain_seed < unit_seed → 남은 seed 는 dust 로 취급하고 더 이상 진입 금지
        return False, usage

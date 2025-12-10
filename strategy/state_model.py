from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Literal, List, Dict
from enum import Enum


# ==============================
# 포지션 / 마켓 / 계정 상태 모델
# ==============================


@dataclass
class MarketState:
    """
    마켓 상태 요약.

    - price      : 현재 가격 (Mark 또는 Last, 프로젝트에서 일관되게 사용할 것)
    - line_index : 현재 가격이 위치한 그리드 라인 인덱스 (정수)
    - p_gap      : 그리드 간격 (P_gap)
    """
    price: float
    line_index: int
    p_gap: float


@dataclass
class PositionSide:
    """
    한 방향(Long 또는 Short)의 포지션 상태.

    - size : 수량 (BTC, Long>0 / Short<0 또는 '방향별 절대값'으로 쓸 수 있음)
    - pnl  : 해당 방향 포지션의 미실현 PnL (USDT)
    """
    size: float
    pnl: float

    @property
    def has_position(self) -> bool:
        return abs(self.size) > 1e-12

    def notional(self, price: float) -> float:
        """
        현재 가격 기준 노출 Notional (USDT 절대값).
        """
        return abs(self.size) * price


@dataclass
class PositionState:
    """
    롱/숏 포지션 상태를 묶은 구조체.
    """
    long: PositionSide
    short: PositionSide


@dataclass
class AccountState:
    """
    계정 상태.

    - free_balance  : 사용 가능한 잔고 (USDT)
    - total_balance : 계정 총 자산 (USDT, UTA 기준)
    """
    free_balance: float
    total_balance: float


# ==============================
# Wave / Escape 상태 모델
# ==============================


@dataclass
class DirectionEscapeState:
    """
    한 방향(Long 또는 Short)에 대한 Escape 상태.

    - active            : 현재 Escape 모드가 활성화 되어 있는지 여부
    - trigger_line      : 기록된 Escape Trigger Line (라인 인덱스)
    - exposure_notional : Escape 시작 시점의 총 노출 Notional
                          (기존 물린 포지션 + 헷지 포지션 절대값 합)
    - created_ts        : Escape 상태가 시작된 시각 (epoch seconds)
    """
    active: bool = False
    trigger_line: Optional[int] = None
    exposure_notional: float = 0.0
    created_ts: float = 0.0


@dataclass
class WaveState:
    """
    Wave 전체 상태.

    - wave_id       : 현재 Wave 게임 번호 (Reset/New Game 시 1씩 증가)
    - long_escape   : Long 방향 Escape 상태
    - short_escape  : Short 방향 Escape 상태
    """
    wave_id: int = 0
    long_escape: DirectionEscapeState = field(default_factory=DirectionEscapeState)
    short_escape: DirectionEscapeState = field(default_factory=DirectionEscapeState)


# ==============================
# LineMemory / Grid Slot 상태
# ==============================


class LineState(str, Enum):
    """
    Grid 라인 메모리 상태 (v10.1 명세 1:1).

    - FREE        : 아직 진입 안 했거나, 이익(PROFIT)으로 완전히 종료된 후 재진입 가능한 상태
    - OPEN        : 해당 idx에서 시작된 slot 이 살아 있는 상태 (qty > 0)
    - LOCKED_LOSS : 손절/ESCAPE/강제청산 등으로 손실 종료 → 해당 Wave 동안 재진입 금지
    """
    FREE = "FREE"
    OPEN = "OPEN"
    LOCKED_LOSS = "LOCKED_LOSS"


# grid_index(int) → LineState 맵 구조 (LineMemory)
LineMemory = Dict[int, LineState]


# ==============================
# Bot 상태 모델 (Wave/Grid/Seed/LineMemory)
# ==============================


@dataclass
class BotState:
    """
    WaveBot v10.1 전략 Bot 상태.

    Grid/Wave/Seed/LineMemory 관련 핵심 상태를 한 곳에 모아,
    StrategyFeed 및 각 Logic(Grid/Escape 등)에서 공통으로 참조한다.

    필수 필드 (명세 기준):
    - mode                          : 현재 모드 ("NORMAL" / "ESCAPE" / 기타)
    - wave_id                       : 현재 Wave 게임 번호
    - p_center                      : 현재 Wave 기준 중심 가격
    - p_gap                         : Grid 간격
    - atr_value                     : Wave 시작 시 ATR_4H(42) 스냅샷
    - long_seed_total_effective     : Long 방향 유효 Seed 총액
    - short_seed_total_effective    : Short 방향 유효 Seed 총액
    - unit_seed_long                : Long 방향 1단계 unit seed
    - unit_seed_short               : Short 방향 1단계 unit seed
    - k_long                        : Long 방향 분할 카운터
    - k_short                       : Short 방향 분할 카운터
    - long_steps_filled             : Long 방향 실제 fill된 스텝 수
    - short_steps_filled            : Short 방향 실제 fill된 스텝 수
    - line_memory_long              : grid_index → LineState 맵 (Long)
    - line_memory_short             : grid_index → LineState 맵 (Short)
    """
    # 현재 모드 ("NORMAL" / "ESCAPE" / "NEWS_BLOCK" / 기타)
    mode: str

    # Wave ID
    wave_id: int

    # Grid 기준값
    p_center: float
    p_gap: float

    # Wave 시작 시 ATR_4H(42) 스냅샷
    atr_value: float

    # Seed 관련 유효 총액
    long_seed_total_effective: float
    short_seed_total_effective: float

    # 방향별 unit seed 크기
    unit_seed_long: float
    unit_seed_short: float

    # 방향별 분할 카운터 (k)
    k_long: int
    k_short: int

    # Start-up 단계 완료 여부 (명세 내 Start-up Rule용)
    startup_done: bool = False

    # --- v10.1 runtime position snapshot ---
    long_size: float = 0.0
    short_size: float = 0.0
    long_pnl: float = 0.0
    short_pnl: float = 0.0

    # 포지션 존재 여부/손익 방향 (Wave End, Escape 판단용)
    long_pos_nonzero: bool = False
    short_pos_nonzero: bool = False
    long_pnl_sign: int = 0
    short_pnl_sign: int = 0

    # 실제 fill된 스텝 수 (k_* 와 정합성 유지 필요)
    long_steps_filled: int = 0
    short_steps_filled: int = 0

    # 리스크 블록 상태 (뉴스 / 서킷 브레이커)
    news_block: bool = False
    cb_block: bool = False

    # grid_index → LineState 맵
    line_memory_long: LineMemory = field(default_factory=dict)
    line_memory_short: LineMemory = field(default_factory=dict)


# ==============================
# 주문 스펙 (전략 -> 주문 엔진)
# ==============================


@dataclass
class OrderSpec:
    """
    전략 엔진(WaveFSM / Grid / Escape)이 주문 엔진(core.order_manager)에 넘기는 공통 스펙.

    이 레벨에서는 거래소 API 세부 타입(클라이언트 오더 ID, timeInForce 문자열 등)에
    종속되지 않도록 최대한 단순한 필드만 사용한다.

    - side        : "LONG" 또는 "SHORT"
    - order_type  : "LIMIT" 또는 "MARKET"
    - qty         : 주문 수량 (BTC)
    - price       : 지정가 주문일 경우 가격, 시장가일 경우 None
    - reduce_only : Reduce-Only 여부
    - post_only   : Maker-Only (PostOnly) 여부
    - mode        : "A" (Maker Mode) / "B" (Escape/Taker Mode) 등 전략 모드 태그
    - comment     : 디버그/로그용 설명 문자열
    """
    side: str                   # "LONG" / "SHORT"
    order_type: str             # "LIMIT" / "MARKET"
    qty: float
    price: Optional[float] = None
    reduce_only: bool = False
    post_only: bool = False
    mode: str = "A"
    comment: str = ""


# 타입 별칭 (필요 시 다른 모듈에서 사용)
Side = Literal["LONG", "SHORT"]

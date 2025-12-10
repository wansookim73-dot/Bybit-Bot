from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List


# ============================================================
# 입력 / 출력 Dataclass
# ============================================================

@dataclass
class RiskInputs:
    """
    RiskGuard 평가에 필요한 요약 입력 값들.

    - ts            : 현재 시각 (epoch seconds, 단조 증가)
    - is_macro      : (레거시) 이미 계산된 '뉴스 구간 여부' 플래그
                      news_events 가 None 인 경우에만 fallback 으로 사용
    - p_gap         : 현재 그리드 간격 (가격 단위)
    - move_1m       : 최근 1분 가격 변동률 절대값 (예: 0.006 = 0.6%)
    - vol_1m        : 최근 1분 거래량 (Volume_1m)
    - vol_ma20      : 최근 20분 거래량 평균 (Volume_MA20)
    - last3_ranges  : 최근 3개 1분봉의 (high - low) 값 리스트, 길이 3
    - p_center      : 현재 Wave 기준 P_center 가격 (없으면 0.0)
    - last3_mids    : 최근 3개 1분봉의 mid 가격 리스트 (high+low)/2 등, 길이 3
                      제공되지 않으면 p_center 로 대체해 근사
    - news_events   : 주요 뉴스 이벤트 시각 리스트 [T_event...], epoch seconds
    """
    ts: float
    is_macro: bool
    p_gap: float
    move_1m: float
    vol_1m: float
    vol_ma20: float
    last3_ranges: List[float]

    # v10.1 6장 기준 확장 입력
    p_center: float = 0.0
    last3_mids: Optional[List[float]] = None
    news_events: Optional[List[float]] = None


@dataclass
class RiskDecision:
    """
    RiskGuard 결과.

    - allow_entry         : Start-up 진입 허용 여부
    - allow_dca           : DCA 진입 허용 여부
    - allow_refill        : Refill 허용 여부
    - cancel_open_orders  : 이번 tick에서 '진입 계열 Maker'를 취소해야 하는지
    - pause_active        : 뉴스/서킷 등으로 진입 PAUSE 상태인지
    - reason              : "NONE" / "MACRO" / "CB" / "MACRO+CB"
    - news_block          : 뉴스 필터에 의한 block 여부 (GridLogic/State 에 전달용)
    - cb_block            : 서킷 브레이커에 의한 block 여부
    - entry_pause_until_ts: CB 발동 시점 기준 entry 재개 예정 시각 (epoch)
    """
    allow_entry: bool
    allow_dca: bool
    allow_refill: bool
    cancel_open_orders: bool
    pause_active: bool
    reason: str
    news_block: bool = False
    cb_block: bool = False
    entry_pause_until_ts: float = 0.0


# ============================================================
# RiskGuard 본체
# ============================================================

class RiskGuard:
    """
    WaveBot v10.1 6장 기준 리스크 레이어.

    - 뉴스 필터 (Macro Guard; news_events 기반)
    - 변동성/거래량 서킷 브레이커 (Volatility Circuit Breaker)
    - Resume Protocol (캔들/거래량 안정성 기준)

    역할은 **오직**:
      - Start-up / DCA / Refill 계열 신규 진입 허용/차단
      - 진입 Maker 주문 취소 플래그 결정

    ESCAPE / FULL_EXIT / TP / 기타 리스크 청산용 Market 주문은
    여기에서 절대 막지 않는다.
    """

    def __init__(self, cb_pause_minutes: float = 15.0) -> None:
        # 서킷 브레이커 상태
        self.cb_active: bool = False
        self.cb_started_ts: float = 0.0
        self.cb_pause_until_ts: float = 0.0

        # 뉴스 상태 (news_block 플래그)
        self.news_block_active: bool = False

        # 서킷 브레이커 PAUSE 기본 지속 시간 (초 단위)
        self.cb_pause_seconds: float = cb_pause_minutes * 60.0

    # --------------------------
    # 헬퍼: 뉴스 필터 (6.1)
    # --------------------------

    def _is_in_news_window(self, inp: RiskInputs) -> bool:
        """
        뉴스 이벤트 스케줄 기반 news_block 여부 계산.

        - 입력: inp.news_events = [T_event...]
        - now = inp.ts 가 T_event-60분 ~ T_event+30분 범위에 있으면 True.

        news_events 가 None 또는 빈 리스트면:
        - inp.is_macro (레거시 플래그)를 그대로 사용.
        """
        if inp.news_events:
            now = inp.ts
            for t in inp.news_events:
                if t is None:
                    continue
                try:
                    te = float(t)
                except (TypeError, ValueError):
                    continue
                if (te - 60 * 60) <= now <= (te + 30 * 60):
                    return True
            return False

        # 스케줄이 없으면 기존 is_macro 플래그를 그대로 사용
        return bool(inp.is_macro)

    # --------------------------
    # 헬퍼: 서킷 브레이커 트리거/유지/해제 (6.2)
    # --------------------------

    def _check_cb_trigger(self, inp: RiskInputs) -> bool:
        """
        서킷 브레이커 트리거 조건 (둘 중 하나):

        1) 최근 1분 가격 변동률 절대값 ≥ 0.6%  (move_1m ≥ 0.006)
        2) 최근 1분 거래량 ≥ 최근 20분 평균의 4배 (vol_1m ≥ vol_ma20 * 4)
        """
        cond_price = inp.move_1m >= 0.006
        cond_volume = inp.vol_ma20 > 0 and inp.vol_1m >= inp.vol_ma20 * 4.0
        return cond_price or cond_volume

    def _check_resume_ok(self, inp: RiskInputs) -> bool:
        """
        Resume 조건 (6.3) — 둘 다 충족해야 재개:

        1) Candle Stability:
           최근 3개 1분봉에 대해
               (high - low) / mid <= P_gap / P_center

           P_center / P_gap 이 없으면
               (high - low) <= P_gap  으로 근사.

        2) Volume Stability:
           Volume_1m < Volume_MA20 * 1.5
        """
        # P_center / P_gap 유효성 체크
        if inp.p_gap <= 0.0 or inp.p_center <= 0.0:
            # P_center 정보가 없으면 레거시 방식으로 근사:
            #  (high - low) <= P_gap
            if inp.last3_ranges and inp.p_gap > 0:
                stable_candle = all(r <= inp.p_gap for r in inp.last3_ranges)
            else:
                stable_candle = False
        else:
            # 비율 기반 안정성 판단
            threshold = inp.p_gap / inp.p_center
            ranges = inp.last3_ranges or []
            mids = inp.last3_mids or []
            # mids 가 제공되지 않으면 P_center 로 대체
            if not mids or len(mids) < len(ranges):
                mids = [inp.p_center] * len(ranges)

            stable_candle = True
            for r, m in zip(ranges, mids):
                if m <= 0:
                    stable_candle = False
                    break
                ratio = (r / m)
                if ratio > threshold:
                    stable_candle = False
                    break

        # Volume Stability
        if inp.vol_ma20 > 0:
            stable_volume = inp.vol_1m < inp.vol_ma20 * 1.5
        else:
            # vol_ma20 이 0 이면 안정성 판단 불가 → 재개 불허
            stable_volume = False

        return stable_candle and stable_volume

    def _update_cb_state(self, inp: RiskInputs) -> bool:
        """
        서킷 브레이커 내부 상태를 갱신하고,
        이번 tick에서 '미체결 지정가(진입 계열 Maker)를 취소해야 하는지' 여부를 반환.

        - 트리거 순간:
            cb_active=True, cb_pause_until_ts = now + 15분
            cancel_open_orders=True
        - active 상태에서 재트리거:
            필요 시 cb_pause_until_ts 를 연장
        - ts >= cb_pause_until_ts 이고, Resume 조건이 만족되면
            cb_active=False 로 해제 (entry 재개)
        """
        cancel_open_orders = False
        ts = inp.ts

        triggered = self._check_cb_trigger(inp)

        # 새로운 트리거
        if triggered and not self.cb_active:
            self.cb_active = True
            self.cb_started_ts = ts
            self.cb_pause_until_ts = ts + self.cb_pause_seconds
            cancel_open_orders = True

        # 이미 active 인 상태에서 재트리거 → PAUSE 기간 연장 (보수적)
        elif triggered and self.cb_active:
            new_pause_until = ts + self.cb_pause_seconds
            if new_pause_until > self.cb_pause_until_ts:
                self.cb_pause_until_ts = new_pause_until

        # 해제 조건: 시간 + Resume 안정성 조건
        if self.cb_active:
            if ts >= self.cb_pause_until_ts and self._check_resume_ok(inp):
                self.cb_active = False
                self.cb_started_ts = 0.0
                self.cb_pause_until_ts = 0.0

        return cancel_open_orders

    # --------------------------
    # Public: 메인 엔트리 포인트
    # --------------------------

    def process(self, inp: RiskInputs) -> RiskDecision:
        """
        한 tick에 대해:

        1) 뉴스 필터 (news_events 기반 news_block on/off)
        2) 서킷 브레이커 (volatility/volume 기반 cb_block on/off)
        3) 두 플래그를 결합해 Entry 계열 허용/차단 및
           기존 진입 Maker 주문 취소 여부를 결정.

        Escape / FULL_EXIT / TP / 강제청산은 여기서 막지 않고,
        WaveFSM / GridLogic / OrderManager 에서 RiskDecision을 참고하여
        '진입 계열(Start-up/DCA/Refill)'만 차단하는 구조를 가정한다.
        """
        ts = inp.ts  # 현재는 내부 상태 업데이트용으로만 사용
        cancel_open_orders = False

        # 1) 뉴스 필터 (6.1)
        in_news = self._is_in_news_window(inp)

        # news_block 상태 변화 감지
        if in_news and not self.news_block_active:
            # 뉴스 구간 진입 → 진입 Maker 전부 취소
            self.news_block_active = True
            cancel_open_orders = True
        elif not in_news and self.news_block_active:
            # 뉴스 구간에서 벗어남
            self.news_block_active = False

        # 2) 서킷 브레이커 상태 갱신 (6.2) + 오더 취소 플래그
        cb_cancel = self._update_cb_state(inp)
        if cb_cancel:
            cancel_open_orders = True

        # 3) 최종 PAUSE 상태 및 Reason 계산 (뉴스 + CB 결합)
        pause_by_news = self.news_block_active
        pause_by_cb = self.cb_active

        pause_active = pause_by_news or pause_by_cb

        if pause_by_news and pause_by_cb:
            reason = "MACRO+CB"
        elif pause_by_news:
            reason = "MACRO"
        elif pause_by_cb:
            reason = "CB"
        else:
            reason = "NONE"

        # Entry 계열 허용 여부:
        # - pause_active 동안 Start-up / DCA / Refill 전면 금지
        # - TP / ESCAPE / FULL_EXIT / 기타 리스크 청산은 상위 레이어에서 항상 허용
        allow_entry = not pause_active
        allow_dca = not pause_active
        allow_refill = not pause_active

        return RiskDecision(
            allow_entry=allow_entry,
            allow_dca=allow_dca,
            allow_refill=allow_refill,
            cancel_open_orders=cancel_open_orders,
            pause_active=pause_active,
            reason=reason,
            news_block=pause_by_news,
            cb_block=pause_by_cb,
            entry_pause_until_ts=self.cb_pause_until_ts,
        )


# ============================================================
# 기존 코드와의 호환성을 위한 RiskManager 래퍼
# ============================================================

class RiskManager:
    """
    main_v10 / WaveFSM 등에서 사용하는 얇은 래퍼.

    - RiskGuard.process(...) 를 호출해 RiskDecision 을 받고,
      GridLogic / OrderManager / StateManager 쪽에서
      decision.news_block / decision.cb_block 값을 BotState/Feed 에
      반영해서 Gate 로 사용할 수 있다.

    이 레이어는 **ESCAPE/FULL_EXIT/TP 를 절대 차단하지 않는다.**
    """

    def __init__(self, cb_pause_minutes: float = 15.0) -> None:
        self.guard = RiskGuard(cb_pause_minutes=cb_pause_minutes)

    def process(self, inp: RiskInputs) -> RiskDecision:
        return self.guard.process(inp)

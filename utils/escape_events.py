from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional


def _now_ts() -> float:
    return time.time()


def _log_event(logger, event: str, extra: Dict[str, Any]) -> None:
    """
    공통 이벤트 로거.

    - logger : utils.logger.get_logger(...) 로 받은 로거 인스턴스
    - event  : "ESCAPE_TRIGGER" / "ESCAPE_CLEAR" / ...
    - extra  : 본문 필드 (dict)

    출력 형식:
      {"ts": 1234567890.123, "event": "...", "extra": {...}}
    """
    payload = {
        "ts": _now_ts(),
        "event": event,
        "extra": extra,
    }
    try:
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        # logger 가 깨져도 main 로직을 막지 않게 방어
        logger.info(f"[escape_events:{event}] {extra}")
    

# ----------------------------------------------------------------------
# ESCAPE 관련 이벤트
# ----------------------------------------------------------------------

def log_escape_trigger(
    logger,
    *,
    reason: str,
    pnl_total_pct: float,
    atr_ratio: float,
    main_side: Optional[str],
    main_qty: float,
    side_seed_total: float,
    exposure_ratio: float,
) -> None:
    """
    ESCAPE_TRIGGER 이벤트 로그.

    reason: "PNL_DROP" | "SEED_LOSS" | "VOL_SPIKE"
    """
    extra = {
        "reason": reason,
        "pnl_total_pct": pnl_total_pct,
        "atr_ratio": atr_ratio,
        "main_side": main_side,
        "main_qty": main_qty,
        "side_seed_total": side_seed_total,
        "exposure_ratio": exposure_ratio,
    }
    _log_event(logger, "ESCAPE_TRIGGER", extra)


def log_escape_clear(
    logger,
    *,
    pnl_total_pct: float,
    atr_ratio: float,
    exposure_ratio: float,
    duration_sec: float,
) -> None:
    """
    ESCAPE_CLEAR 이벤트 로그.
    """
    extra = {
        "pnl_total_pct": pnl_total_pct,
        "atr_ratio": atr_ratio,
        "exposure_ratio": exposure_ratio,
        "duration_sec": duration_sec,
    }
    _log_event(logger, "ESCAPE_CLEAR", extra)


# ----------------------------------------------------------------------
# FULL_EXIT 이벤트
# ----------------------------------------------------------------------

def log_full_exit_trigger(
    logger,
    *,
    pnl_total_pct: float,
    wave_id: int,
    positions_before: Dict[str, Any],
) -> None:
    """
    FULL_EXIT 트리거 이벤트.

    positions_before:
      {
        "LONG": {"qty": ..., "avg_price": ...},
        "SHORT": {...},
        "HEDGE": {...}  # 있으면
      }
    """
    extra = {
        "pnl_total_pct": pnl_total_pct,
        "wave_id": wave_id,
        "positions_before": positions_before,
    }
    _log_event(logger, "FULLEXIT_TRIGGER", extra)


# ----------------------------------------------------------------------
# HEDGE 이벤트
# ----------------------------------------------------------------------

def log_hedge_plan(
    logger,
    *,
    main_side: Optional[str],
    main_qty: float,
    hedge_side: Optional[str],
    hedge_qty_before: float,
    hedge_qty_after: float,
    hedge_notional: float,
) -> None:
    """
    HEDGE_ENTRY / 증액 계획 이벤트 (HEDGE_PLAN).
    """
    extra = {
        "main_side": main_side,
        "main_qty": main_qty,
        "hedge_side": hedge_side,
        "hedge_qty_before": hedge_qty_before,
        "hedge_qty_after": hedge_qty_after,
        "hedge_notional": hedge_notional,
    }
    _log_event(logger, "HEDGE_PLAN", extra)


def log_hedge_exit_plan(
    logger,
    *,
    main_side: Optional[str],
    main_qty: float,
    hedge_side: Optional[str],
    hedge_qty: float,
    hedge_notional: float,
    hedge_pnl_pct: Optional[float] = None,
) -> None:
    """
    HEDGE_EXIT 계획 이벤트 (HEDGE_EXIT_PLAN).
    """
    extra = {
        "main_side": main_side,
        "main_qty": main_qty,
        "hedge_side": hedge_side,
        "hedge_qty": hedge_qty,
        "hedge_notional": hedge_notional,
        "hedge_pnl_pct": hedge_pnl_pct,
    }
    _log_event(logger, "HEDGE_EXIT_PLAN", extra)


# ----------------------------------------------------------------------
# Wave / Tick Summary (선택적)
# ----------------------------------------------------------------------

def log_wave_new(
    logger,
    *,
    wave_id: int,
    p_center: float,
    p_gap: float,
    atr_value: float,
) -> None:
    extra = {
        "wave_id": wave_id,
        "p_center": p_center,
        "p_gap": p_gap,
        "atr_value": atr_value,
    }
    _log_event(logger, "WAVE_NEW", extra)


def log_tick_summary(
    logger,
    *,
    wave_id: int,
    price: float,
    pnl_total: float,
    pnl_total_pct: float,
    long_size: float,
    short_size: float,
    escape_active: bool,
    news_block: bool,
) -> None:
    extra = {
        "wave_id": wave_id,
        "price": price,
        "pnl_total": pnl_total,
        "pnl_total_pct": pnl_total_pct,
        "long_size": long_size,
        "short_size": short_size,
        "escape_active": escape_active,
        "news_block": news_block,
    }
    _log_event(logger, "TICK_SUMMARY", extra)


# ----------------------------------------------------------------------
# Threshold 변경 로그
# ----------------------------------------------------------------------

def log_threshold_update(
    logger,
    *,
    prev: Dict[str, Any],
    new: Dict[str, Any],
    reason: str,
) -> None:
    """
    ESCAPE_THRESHOLD_UPDATE 이벤트.

    prev/new:
      {"ESCAPE_ON_PNL_PCT": 0.04, "ESCAPE_OFF_PNL_PCT": 0.015, ...}
    """
    extra = {
        "prev": prev,
        "new": new,
        "reason": reason,
    }
    _log_event(logger, "ESCAPE_THRESHOLD_UPDATE", extra)

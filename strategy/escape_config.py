from __future__ import annotations

"""
v10.1 Escape / FULL_EXIT / Hedge Threshold Config

- 기본 preset 값은 코드에 상수로 정의.
- config/escape_config.yml 이 존재하면, 해당 값을 읽어서 override.
- 튜닝은 YAML 파일만 수정하면 된다.
"""

import os
import json
from typing import Any, Dict

from utils.logger import logger
from utils.escape_events import log_threshold_update

# ------------------------------------------------------------
# 1) 기본 preset 값 (v10.1 첫 릴리즈 기준)
# ------------------------------------------------------------

FULL_EXIT_PNL_PCT = 0.02               # +2% 이상 전체 계좌 이익 시 FULL_EXIT

ESCAPE_ON_PNL_PCT = 0.04               # -4% 이하 손실이면 ESCAPE (계좌 기준)
ESCAPE_OFF_PNL_PCT = 0.015             # -1.5% 이상 회복 시 ESCAPE 해제 고려

ESCAPE_ON_SEED_PCT = 1.0               # main_notional >= side_seed_total (seed의 100% 노출)
ESCAPE_OFF_MAX_EXPOSURE_RATIO = 0.5    # ESCAPE 해제 시 메인 노출은 seed의 50% 이하

ESCAPE_ATR_SPIKE_MULT = 2.0            # ATR 현재값이 평소 대비 2배 이상이면 Vol Spike
ESCAPE_OFF_ATR_MULT = 1.3              # ATR이 평소의 1.3배 이하로 진정되면 허용

ESCAPE_COOLDOWN_SEC = 300.0            # ESCAPE ON 후 최소 5분 유지

HEDGE_BE_TOLERANCE_PCT = 0.001         # 헷지 PnL이 -0.1% 이상이면 BE로 간주
HEDGE_ENTRY_MIN_NOTIONAL_USDT = 200.0  # 200 USDT 미만 헷지는 스킵
HEDGE_ENTRY_MIN_THRESHOLD = 1e-6       # 수량 자체가 너무 작으면 무시


# ------------------------------------------------------------
# 2) YAML → override 로직
# ------------------------------------------------------------

def _escape_root_dir() -> str:
    """
    프로젝트 루트 기준 config/escape_config.yml 을 찾기 위해,
    strategy/escape_config.py 위치에서 상위 디렉토리로 올라간다.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # .../strategy/escape_config.py → .../
    return os.path.abspath(os.path.join(here, ".."))


def _load_yaml_config(path: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        logger.info("[escape_config] PyYAML not found, skipping YAML config.")
        return {}

    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("[escape_config] YAML root is not a dict, ignoring file.")
            return {}
        return data
    except Exception as exc:
        logger.error(f"[escape_config] Failed to load YAML: {exc}")
        return {}


def _current_values() -> Dict[str, Any]:
    """
    현재 모듈 내 threshold 값을 dict로 반환.
    Threshold 변경 로그(ESCAPE_THRESHOLD_UPDATE)에 사용.
    """
    return {
        "FULL_EXIT_PNL_PCT": FULL_EXIT_PNL_PCT,
        "ESCAPE_ON_PNL_PCT": ESCAPE_ON_PNL_PCT,
        "ESCAPE_OFF_PNL_PCT": ESCAPE_OFF_PNL_PCT,
        "ESCAPE_ON_SEED_PCT": ESCAPE_ON_SEED_PCT,
        "ESCAPE_OFF_MAX_EXPOSURE_RATIO": ESCAPE_OFF_MAX_EXPOSURE_RATIO,
        "ESCAPE_ATR_SPIKE_MULT": ESCAPE_ATR_SPIKE_MULT,
        "ESCAPE_OFF_ATR_MULT": ESCAPE_OFF_ATR_MULT,
        "ESCAPE_COOLDOWN_SEC": ESCAPE_COOLDOWN_SEC,
        "HEDGE_BE_TOLERANCE_PCT": HEDGE_BE_TOLERANCE_PCT,
        "HEDGE_ENTRY_MIN_NOTIONAL_USDT": HEDGE_ENTRY_MIN_NOTIONAL_USDT,
        "HEDGE_ENTRY_MIN_THRESHOLD": HEDGE_ENTRY_MIN_THRESHOLD,
    }


def _apply_yaml_config(cfg: Dict[str, Any]) -> None:
    """
    YAML에서 읽은 cfg dict 를 기반으로 상수값 override.
    """
    global FULL_EXIT_PNL_PCT
    global ESCAPE_ON_PNL_PCT, ESCAPE_OFF_PNL_PCT
    global ESCAPE_ON_SEED_PCT, ESCAPE_OFF_MAX_EXPOSURE_RATIO
    global ESCAPE_ATR_SPIKE_MULT, ESCAPE_OFF_ATR_MULT
    global ESCAPE_COOLDOWN_SEC
    global HEDGE_BE_TOLERANCE_PCT, HEDGE_ENTRY_MIN_NOTIONAL_USDT
    global HEDGE_ENTRY_MIN_THRESHOLD

    if "full_exit_pnl_pct" in cfg:
        FULL_EXIT_PNL_PCT = float(cfg["full_exit_pnl_pct"])

    esc = cfg.get("escape") or {}
    if isinstance(esc, dict):
        if "on_pnl_pct" in esc:
            ESCAPE_ON_PNL_PCT = float(esc["on_pnl_pct"])
        if "off_pnl_pct" in esc:
            ESCAPE_OFF_PNL_PCT = float(esc["off_pnl_pct"])

        if "on_seed_exposure_ratio" in esc:
            ESCAPE_ON_SEED_PCT = float(esc["on_seed_exposure_ratio"])
        if "off_max_exposure_ratio" in esc:
            ESCAPE_OFF_MAX_EXPOSURE_RATIO = float(esc["off_max_exposure_ratio"])

        if "atr_spike_mult" in esc:
            ESCAPE_ATR_SPIKE_MULT = float(esc["atr_spike_mult"])
        if "atr_off_mult" in esc:
            ESCAPE_OFF_ATR_MULT = float(esc["atr_off_mult"])

        if "cooldown_sec" in esc:
            ESCAPE_COOLDOWN_SEC = float(esc["cooldown_sec"])

    hedge = cfg.get("hedge") or {}
    if isinstance(hedge, dict):
        if "be_tolerance_pct" in hedge:
            HEDGE_BE_TOLERANCE_PCT = float(hedge["be_tolerance_pct"])
        if "min_notional_usdt" in hedge:
            HEDGE_ENTRY_MIN_NOTIONAL_USDT = float(hedge["min_notional_usdt"])
        if "entry_min_threshold" in hedge:
            HEDGE_ENTRY_MIN_THRESHOLD = float(hedge["entry_min_threshold"])


def _load_and_apply() -> None:
    """
    모듈 import 시 자동으로 YAML config를 읽고, threshold 값을 override한다.
    Threshold 변경 사항이 있으면 ESCAPE_THRESHOLD_UPDATE 이벤트를 남긴다.
    """
    root = _escape_root_dir()
    yaml_path = os.path.join(root, "config", "escape_config.yml")

    prev = _current_values()
    cfg = _load_yaml_config(yaml_path)
    if not cfg:
        # YAML 없음 or 로딩 실패 → 기본 preset 유지
        logger.info("[escape_config] Using default preset values.")
        return

    _apply_yaml_config(cfg)
    new = _current_values()

    if prev != new:
        # threshold 변경 이벤트 기록
        try:
            log_threshold_update(
                logger,
                prev=prev,
                new=new,
                reason=f"Loaded from {yaml_path}",
            )
        except Exception as exc:
            logger.warning("[escape_config] log_threshold_update failed: %s", exc)
    else:
        logger.info("[escape_config] YAML loaded but no value changes detected.")


# 모듈 import 시 한 번만 적용
_load_and_apply()

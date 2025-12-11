from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from config import STATE_FILE_PATH
from utils.logger import logger
from strategy.state_model import BotState, LineState


class StateManager:
    """
    WaveBot v10.1 BotState 영속/관리 레이어.

    역할:
    - BotState 를 STATE_FILE_PATH(JSON) 에 저장/로드
    - Wave 종료 → 새 Wave 시작 시 BotState 초기화/갱신 (wave reset)
    - LineMemory FSM (FREE/OPEN/LOCKED_LOSS) 업데이트 엔트리 포인트 제공
    """

    def __init__(self, state_file_path: str = STATE_FILE_PATH) -> None:
        self.state_file_path = state_file_path
        self.state: BotState = self._load_state()

    # ==================================================================
    # 내부 유틸: 기본 BotState / 직렬화 & 역직렬화
    # ==================================================================

    def _default_bot_state(self) -> BotState:
        """
        파일이 없거나 손상된 경우 사용할 초기 BotState.
        """
        return BotState(
            mode="STARTUP",
            wave_id=0,
            p_center=0.0,
            p_gap=0.0,
            atr_value=0.0,
            long_seed_total_effective=0.0,
            short_seed_total_effective=0.0,
            unit_seed_long=0.0,
            unit_seed_short=0.0,
            k_long=0,
            k_short=0,
            startup_done=False,
            long_size=0.0,
            short_size=0.0,
            long_pnl=0.0,
            short_pnl=0.0,
            long_pos_nonzero=False,
            short_pos_nonzero=False,
            long_pnl_sign=0,
            short_pnl_sign=0,
            long_steps_filled=0,
            short_steps_filled=0,
            news_block=False,
            cb_block=False,
            line_memory_long={},
            line_memory_short={},
        )

    def _serialize_line_memory(self, mem: Dict[int, LineState]) -> Dict[str, str]:
        """
        line_memory_* : Dict[int, LineState] -> Dict[str, str] (JSON 저장용)
        - key: grid_index (int) -> str
        - value: LineState.value ("FREE" / "OPEN" / "LOCKED_LOSS")
        """
        return {str(idx): state.value for idx, state in mem.items()}

    def _deserialize_line_memory(self, raw: Any) -> Dict[int, LineState]:
        """
        JSON에서 읽어온 line_memory_* 를 Dict[int, LineState] 로 변환.
        - 잘못된 키/값은 무시 (FREE 로 간주 → dict 에 넣지 않음)
        """
        result: Dict[int, LineState] = {}
        if not isinstance(raw, dict):
            return result

        for k, v in raw.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            try:
                state = LineState(v)
            except ValueError:
                # 알 수 없는 값은 무시 (FREE 취급)
                continue
            result[idx] = state
        return result

    def _serialize_bot_state(self, state: BotState) -> Dict[str, Any]:
        """
        BotState → JSON 직렬화용 dict (필드 1:1 매핑).
        """
        return {
            "mode": state.mode,
            "wave_id": state.wave_id,
            "p_center": state.p_center,
            "p_gap": state.p_gap,
            "atr_value": state.atr_value,
            "long_seed_total_effective": state.long_seed_total_effective,
            "short_seed_total_effective": state.short_seed_total_effective,
            "unit_seed_long": state.unit_seed_long,
            "unit_seed_short": state.unit_seed_short,
            "k_long": state.k_long,
            "k_short": state.k_short,
            "startup_done": state.startup_done,
            "long_size": state.long_size,
            "short_size": state.short_size,
            "long_pnl": state.long_pnl,
            "short_pnl": state.short_pnl,
            "long_pos_nonzero": state.long_pos_nonzero,
            "short_pos_nonzero": state.short_pos_nonzero,
            "long_pnl_sign": state.long_pnl_sign,
            "short_pnl_sign": state.short_pnl_sign,
            "long_steps_filled": state.long_steps_filled,
            "short_steps_filled": state.short_steps_filled,
            "news_block": state.news_block,
            "cb_block": state.cb_block,
            "line_memory_long": self._serialize_line_memory(state.line_memory_long),
            "line_memory_short": self._serialize_line_memory(state.line_memory_short),
        }

    def _deserialize_bot_state(self, data: Dict[str, Any]) -> BotState:
        """
        JSON dict → BotState (필드 누락/추가 체크 포함).

        - 필수 키 세트가 아니거나, 예상치 못한 키가 있으면 경고 로그를 남기고
          가능한 한 호환 모드로 로드한다.
        """
        required_keys = {
            "mode",
            "wave_id",
            "p_center",
            "p_gap",
            "atr_value",
            "long_seed_total_effective",
            "short_seed_total_effective",
            "unit_seed_long",
            "unit_seed_short",
            "k_long",
            "k_short",
            "startup_done",
            "long_size",
            "short_size",
            "long_pnl",
            "short_pnl",
            "long_pos_nonzero",
            "short_pos_nonzero",
            "long_pnl_sign",
            "short_pnl_sign",
            "long_steps_filled",
            "short_steps_filled",
            "news_block",
            "cb_block",
            "line_memory_long",
            "line_memory_short",
        }

        if not isinstance(data, dict):
            logger.warning("BotState JSON 형식이 dict가 아님 → 기본 상태로 복구")
            return self._default_bot_state()

        keys = set(data.keys())
        missing = required_keys - keys
        extra = keys - required_keys

        if missing or extra:
            logger.warning(
                "BotState JSON 스키마 차이 감지 (missing=%s, extra=%s) → 호환 모드로 로드합니다.",
                missing,
                extra,
            )

        try:
            # 기본값은 항상 최신 스키마의 기본 상태에서 가져온다.
            default = self._default_bot_state()

            mode = str(data.get("mode", default.mode))
            wave_id = int(data.get("wave_id", default.wave_id))
            p_center = float(data.get("p_center", default.p_center))
            p_gap = float(data.get("p_gap", default.p_gap))
            atr_value = float(data.get("atr_value", default.atr_value))

            # v9 → v10 호환: *_effective 없으면 예전 long_seed_total / short_seed_total 사용
            long_seed_total_effective = float(
                data.get(
                    "long_seed_total_effective",
                    data.get("long_seed_total", default.long_seed_total_effective),
                )
            )
            short_seed_total_effective = float(
                data.get(
                    "short_seed_total_effective",
                    data.get("short_seed_total", default.short_seed_total_effective),
                )
            )

            # v9 → v10 호환: unit_seed_long/unit_seed_short 없으면 예전 unit_seed 사용
            unit_seed_long = float(
                data.get(
                    "unit_seed_long",
                    data.get("unit_seed", default.unit_seed_long),
                )
            )
            unit_seed_short = float(
                data.get(
                    "unit_seed_short",
                    data.get("unit_seed", default.unit_seed_short),
                )
            )

            k_long = int(data.get("k_long", default.k_long))
            k_short = int(data.get("k_short", default.k_short))

            startup_done = bool(data.get("startup_done", default.startup_done))

            long_size = float(data.get("long_size", default.long_size))
            short_size = float(data.get("short_size", default.short_size))
            long_pnl = float(data.get("long_pnl", default.long_pnl))
            short_pnl = float(data.get("short_pnl", default.short_pnl))

            long_pos_nonzero = bool(
                data.get("long_pos_nonzero", default.long_pos_nonzero)
            )
            short_pos_nonzero = bool(
                data.get("short_pos_nonzero", default.short_pos_nonzero)
            )

            long_pnl_sign = int(data.get("long_pnl_sign", default.long_pnl_sign))
            short_pnl_sign = int(data.get("short_pnl_sign", default.short_pnl_sign))

            long_steps_filled = int(
                data.get("long_steps_filled", default.long_steps_filled)
            )
            short_steps_filled = int(
                data.get("short_steps_filled", default.short_steps_filled)
            )

            news_block = bool(data.get("news_block", default.news_block))
            cb_block = bool(data.get("cb_block", default.cb_block))

            line_memory_long = self._deserialize_line_memory(
                data.get("line_memory_long", default.line_memory_long)
            )
            line_memory_short = self._deserialize_line_memory(
                data.get("line_memory_short", default.line_memory_short)
            )

            return BotState(
                mode=mode,
                wave_id=wave_id,
                p_center=p_center,
                p_gap=p_gap,
                atr_value=atr_value,
                long_seed_total_effective=long_seed_total_effective,
                short_seed_total_effective=short_seed_total_effective,
                unit_seed_long=unit_seed_long,
                unit_seed_short=unit_seed_short,
                k_long=k_long,
                k_short=k_short,
                startup_done=startup_done,
                long_size=long_size,
                short_size=short_size,
                long_pnl=long_pnl,
                short_pnl=short_pnl,
                long_pos_nonzero=long_pos_nonzero,
                short_pos_nonzero=short_pos_nonzero,
                long_pnl_sign=long_pnl_sign,
                short_pnl_sign=short_pnl_sign,
                long_steps_filled=long_steps_filled,
                short_steps_filled=short_steps_filled,
                news_block=news_block,
                cb_block=cb_block,
                line_memory_long=line_memory_long,
                line_memory_short=line_memory_short,
            )
        except Exception as exc:
            logger.error("BotState 역직렬화 실패 → 기본 상태로 복구: %s", exc)
            return self._default_bot_state()

    # ==================================================================
    # 파일 I/O: load / save (atomic)
    # ==================================================================

    def _load_state(self) -> BotState:
        """
        STATE_FILE_PATH 에서 BotState 로드.
        - 파일이 없거나 손상된 경우 기본 상태 사용.
        """
        path = self.state_file_path
        if not os.path.exists(path):
            logger.info("BotState 파일 없음 → 기본 상태로 시작")
            return self._default_bot_state()

        try:
            with open(path, "r") as f:
                data = json.load(f)
            state = self._deserialize_bot_state(data)
            logger.info(
                "BotState 로드: mode=%s wave_id=%s p_center=%s p_gap=%s",
                state.mode,
                state.wave_id,
                state.p_center,
                state.p_gap,
            )
            return state
        except Exception as e:
            logger.error(f"BotState 파일 로드 실패 → 기본 상태로 복구: {e}")
            return self._default_bot_state()

    def save_state(self) -> None:
        """
        BotState 를 JSON으로 atomic write 방식으로 저장.
        - tmp 파일 작성 후 os.replace 로 교체.
        """
        data = self._serialize_bot_state(self.state)
        path = self.state_file_path
        tmp_path = f"{path}.tmp"

        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            with open(tmp_path, "w") as f:
                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"BotState 저장 실패: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    # ==================================================================
    # 외부 접근용: BotState 조회/수정 helper
    # ==================================================================

    def get_state(self) -> BotState:
        """
        현재 BotState 객체 반환 (참조).
        ※ 직접 필드를 수정한 뒤에는 save_state() 호출 필요.
        """
        return self.state

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """
        (레거시 호환용) BotState 필드 접근: getattr 래퍼.
        """
        return getattr(self.state, key, default)

    def update(self, key: str, value: Any) -> None:
        if hasattr(self, "logger") and self.logger is not None:
            self.logger.info("[StateManager] update: %s = %s", key, value)
        """
        (레거시 호환용) 단일 BotState 필드 업데이트 후 저장.
        - 존재하지 않는 키는 무시하고 경고만 로그.
        """
        if not hasattr(self.state, key):
            logger.warning("BotState에 존재하지 않는 필드 업데이트 시도: %s", key)
            return
        setattr(self.state, key, value)
        self.save_state()

    # ==================================================================
    # Wave reset (Wave 종료 → 새 Wave 시작)
    # ==================================================================

    def reset_wave(
        self,
        *,
        p_center: float,
        p_gap: float,
        atr_value: float,
        long_seed_total_effective: float,
        short_seed_total_effective: float,
        unit_seed_long: float,
        unit_seed_short: float,
        mode: str = "NORMAL",
    ) -> BotState:
        """
        Wave 종료 → 새 Wave 시작 시 호출.

        v10.1 명세:
        - wave_id += 1
        - p_center, p_gap, atr_value 는 grid_logic / wave_fsm / capital 에서 전달된 값 사용
        - long_seed_total_effective / short_seed_total_effective / unit_seed_* 설정
        - k_long, k_short = 0
        - long_steps_filled, short_steps_filled = 0
        - line_memory_long / line_memory_short 는 FREE 초기화
          (dict 비우기 → 어떤 index 도 LOCKED_LOSS/OPEN 이 아님을 보장)
        """
        new_wave_id = self.state.wave_id + 1

        self.state = BotState(
            mode=mode,
            wave_id=new_wave_id,
            p_center=float(p_center),
            p_gap=float(p_gap),
            atr_value=float(atr_value),
            long_seed_total_effective=float(long_seed_total_effective),
            short_seed_total_effective=float(short_seed_total_effective),
            unit_seed_long=float(unit_seed_long),
            unit_seed_short=float(unit_seed_short),
            k_long=0,
            k_short=0,
            startup_done=False,
            long_size=0.0,
            short_size=0.0,
            long_pnl=0.0,
            short_pnl=0.0,
            long_pos_nonzero=False,
            short_pos_nonzero=False,
            long_pnl_sign=0,
            short_pnl_sign=0,
            long_steps_filled=0,
            short_steps_filled=0,
            news_block=False,
            cb_block=False,
            line_memory_long={},
            line_memory_short={},
        )

        self.save_state()
        logger.info(
            "Wave reset 완료: wave_id=%s mode=%s p_center=%.2f p_gap=%.2f atr=%.2f "
            "long_eff=%.2f short_eff=%.2f",
            self.state.wave_id,
            self.state.mode,
            self.state.p_center,
            self.state.p_gap,
            self.state.atr_value,
            self.state.long_seed_total_effective,
            self.state.short_seed_total_effective,
        )
        return self.state

    # ==================================================================
    # LineMemory FSM API
    # ==================================================================

    def _get_line_memory_ref(self, side: str) -> Dict[int, LineState]:
        """
        방향별 line_memory dict 반환.
        - side: "LONG" / "SHORT"
        """
        s = side.upper()
        if s == "LONG":
            return self.state.line_memory_long
        if s == "SHORT":
            return self.state.line_memory_short
        raise ValueError(f"invalid side for line memory: {side!r}")

    def get_line_state(self, side: str, grid_index: int) -> LineState:
        """
        주어진 방향/라인 인덱스의 LineState 조회.
        - dict 에 없는 경우 FREE 로 취급.
        """
        mem = self._get_line_memory_ref(side)
        return mem.get(int(grid_index), LineState.FREE)

    def mark_line_open(self, side: str, grid_index: int) -> LineState:
        """
        엔트리 시점: 해당 grid_index 를 OPEN 상태로 마킹.
        - line_memory write 는 반드시 이 함수를 통해 수행하도록 설계.
        """
        idx = int(grid_index)
        mem = self._get_line_memory_ref(side)
        mem[idx] = LineState.OPEN
        self.save_state()

        logger.debug("Line OPEN: side=%s idx=%s", side.upper(), idx)
        return mem[idx]

    def update_line_state(self, side: str, grid_index: int, outcome: str) -> LineState:
        """
        포지션 종료 outcome 에 따른 LineMemory FSM 업데이트.

        outcome:
        - "PROFIT"    → line_state = FREE  (재진입 허용)
        - "LOSS"      → line_state = LOCKED_LOSS (Wave 끝까지 재진입 금지)
        - "BREAKEVEN" → line_state = FREE  (손익 0, 재진입 허용)

        구현:
        - FREE 로 전환 시 dict 에서 해당 key 제거 (부재 = FREE 로 간주)
        - LOCKED_LOSS 는 명시적으로 dict 에 저장
        """
        idx = int(grid_index)
        mem = self._get_line_memory_ref(side)
        outcome_norm = outcome.upper()

        if outcome_norm in ("PROFIT", "BREAKEVEN"):
            # FREE: dict에서 제거 → 기본값 FREE
            mem.pop(idx, None)
            new_state = LineState.FREE
        elif outcome_norm == "LOSS":
            mem[idx] = LineState.LOCKED_LOSS
            new_state = LineState.LOCKED_LOSS
        else:
            logger.warning(
                "알 수 없는 outcome 으로 LineState 업데이트 시도: side=%s idx=%s outcome=%s",
                side,
                idx,
                outcome,
            )
            # 상태 변경 없이 현재 상태 반환
            new_state = mem.get(idx, LineState.FREE)

        self.save_state()

        logger.debug(
            "LineState 업데이트: side=%s idx=%s outcome=%s → %s",
            side.upper(),
            idx,
            outcome_norm,
            new_state.value,
        )
        return new_state


# 모듈 레벨 싱글톤 (기존 사용 패턴 고려)
state_manager = StateManager()
bot_state_manager = state_manager  # 필요시 별칭 사용

# --- Global singleton accessor ---------------------------------------------

_bot_state_manager: Optional["StateManager"] | None = None


def get_state_manager() -> "StateManager":
    """
    전역적으로 하나만 쓰는 StateManager 싱글턴을 반환한다.

    - 기존 legacy 전역 bot_state_manager 가 있으면 그 인스턴스를 재사용하고,
    - 없으면 새 StateManager() 를 생성해서 _bot_state_manager 에 보관한다.
    """
    global _bot_state_manager

    # 이미 만들어진 싱글턴이 있으면 그대로 사용
    if _bot_state_manager is not None:
        return _bot_state_manager

    # legacy 전역이 있으면 우선 재사용
    legacy = globals().get("bot_state_manager")
    if isinstance(legacy, StateManager):
        _bot_state_manager = legacy
        return _bot_state_manager

    # fallback: 새 인스턴스 생성
    _bot_state_manager = StateManager()
    return _bot_state_manager

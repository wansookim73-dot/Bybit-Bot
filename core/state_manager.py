from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, List

from config import STATE_FILE_PATH
from utils.logger import logger
from strategy.state_model import BotState, LineState


_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class StateManager:
    """
    WaveBot v10.1 BotState 영속/관리 레이어.

    [핵심 변경 - 패치세트2]
    - StateManager를 '진짜 싱글톤'으로 강제하여
      (1) 모듈 import 시 1회 로드
      (2) WaveBot에서 StateManager()를 다시 호출해도 같은 인스턴스를 반환
      → BotState 중복 로드/중복 인스턴스 위험 제거
    """
    _instance: "StateManager | None" = None
    _initialized: bool = False

    def __new__(cls, state_file_path: str = STATE_FILE_PATH) -> "StateManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, state_file_path: str = STATE_FILE_PATH) -> None:
        if self.__class__._initialized:
            return

        self.state_file_path = self._resolve_state_path(state_file_path)
        self.state: BotState = self._load_state()

        self.__class__._initialized = True

    def _resolve_state_path(self, path: str) -> str:
        p = os.path.expanduser(str(path))
        if not os.path.isabs(p):
            p = os.path.join(_PROJECT_ROOT, p)
        return os.path.abspath(p)

    # ==================================================================
    # 기본 BotState / 직렬화 & 역직렬화
    # ==================================================================

    def _default_bot_state(self) -> BotState:
        return BotState(
            mode="NORMAL",
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
            total_balance_snap=0.0,
            total_balance=0.0,
            free_balance=0.0,
            hedge_size=0.0,
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
        out: Dict[str, str] = {}
        if not isinstance(mem, dict):
            return out

        for idx, st in mem.items():
            try:
                k = str(int(idx))
            except (TypeError, ValueError):
                continue

            if isinstance(st, LineState):
                out[k] = st.value
                continue

            try:
                out[k] = LineState(str(st)).value
            except Exception:
                continue

        return out

    def _deserialize_line_memory(self, raw: Any) -> Dict[int, LineState]:
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
            except Exception:
                continue
            result[idx] = state

        return result

    def _serialize_bot_state(self, state: BotState) -> Dict[str, Any]:
        dca_eps = float(getattr(state, "dca_near_eps", 0.18) or 0.18)

        data: Dict[str, Any] = {
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

            "total_balance_snap": float(getattr(state, "total_balance_snap", 0.0) or 0.0),
            "total_balance": float(getattr(state, "total_balance", 0.0) or 0.0),
            "free_balance": float(getattr(state, "free_balance", 0.0) or 0.0),
            "hedge_size": float(getattr(state, "hedge_size", 0.0) or 0.0),

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

            "long_tp_active": bool(getattr(state, "long_tp_active", False)),
            "long_tp_max_index": int(getattr(state, "long_tp_max_index", 0) or 0),
            "short_tp_active": bool(getattr(state, "short_tp_active", False)),
            "short_tp_max_index": int(getattr(state, "short_tp_max_index", 0) or 0),

            "dca_near_eps": dca_eps,
            "dca_touch_eps": dca_eps,
            "dca_cooldown_sec": float(getattr(state, "dca_cooldown_sec", 20.0) or 20.0),
            "dca_repeat_guard": float(getattr(state, "dca_repeat_guard", 0.50) or 0.50),

            "dca_guard_wave_id": int(getattr(state, "dca_guard_wave_id", 0) or 0),
            "dca_used_indices": list(getattr(state, "dca_used_indices", []) or []),
            "dca_last_ts": float(getattr(state, "dca_last_ts", 0.0) or 0.0),
            "dca_last_idx": int(getattr(state, "dca_last_idx", 10**9) or 10**9),
            "dca_last_price": float(getattr(state, "dca_last_price", 0.0) or 0.0),

            "dca_win_low": getattr(state, "dca_win_low", None),
            "dca_win_high": getattr(state, "dca_win_high", None),
            "dca_win_ts": float(getattr(state, "dca_win_ts", 0.0) or 0.0),

            "prev_long_size": float(getattr(state, "prev_long_size", 0.0) or 0.0),
            "prev_short_size": float(getattr(state, "prev_short_size", 0.0) or 0.0),
            "prev_long_tp_active": bool(getattr(state, "prev_long_tp_active", False)),
            "prev_short_tp_active": bool(getattr(state, "prev_short_tp_active", False)),
        }
        return data

    def _deserialize_bot_state(self, data: Dict[str, Any]) -> BotState:
        if not isinstance(data, dict):
            logger.warning("BotState JSON 형식이 dict가 아님 → 기본 상태로 복구")
            return self._default_bot_state()

        required_keys = {
            "mode", "wave_id", "p_center", "p_gap", "atr_value",
            "long_seed_total_effective", "short_seed_total_effective",
            "unit_seed_long", "unit_seed_short",
            "k_long", "k_short",
            "startup_done",
            "total_balance_snap", "total_balance", "free_balance", "hedge_size",
            "long_size", "short_size", "long_pnl", "short_pnl",
            "long_pos_nonzero", "short_pos_nonzero",
            "long_pnl_sign", "short_pnl_sign",
            "long_steps_filled", "short_steps_filled",
            "news_block", "cb_block",
            "line_memory_long", "line_memory_short",
            "long_tp_active", "long_tp_max_index",
            "short_tp_active", "short_tp_max_index",
            "dca_near_eps", "dca_cooldown_sec", "dca_repeat_guard",
            "dca_guard_wave_id", "dca_used_indices",
            "dca_last_ts", "dca_last_idx", "dca_last_price",
            "dca_win_low", "dca_win_high", "dca_win_ts",
            "prev_long_size", "prev_short_size",
            "prev_long_tp_active", "prev_short_tp_active",
        }
        alias_keys = {"dca_touch_eps"}

        keys = set(data.keys())
        missing = required_keys - keys
        extra = keys - required_keys - alias_keys
        if missing or extra:
            logger.warning(
                "BotState JSON 스키마 차이 감지 (missing=%s, extra=%s) → 호환 모드로 로드합니다.",
                missing, extra,
            )

        def _to_float(x: Any, default: float) -> float:
            try:
                return float(x)
            except (TypeError, ValueError):
                return float(default)

        def _to_int(x: Any, default: int) -> int:
            try:
                return int(x)
            except (TypeError, ValueError):
                return int(default)

        def _to_bool(x: Any, default: bool) -> bool:
            if x is None:
                return bool(default)
            return bool(x)

        def _opt_float(x: Any) -> Optional[float]:
            if x is None:
                return None
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        try:
            default = self._default_bot_state()

            mode = str(data.get("mode", default.mode))
            wave_id = _to_int(data.get("wave_id", default.wave_id), default.wave_id)
            p_center = _to_float(data.get("p_center", default.p_center), default.p_center)
            p_gap = _to_float(data.get("p_gap", default.p_gap), default.p_gap)
            atr_value = _to_float(data.get("atr_value", default.atr_value), default.atr_value)

            long_seed_total_effective = _to_float(
                data.get("long_seed_total_effective", data.get("long_seed_total", default.long_seed_total_effective)),
                default.long_seed_total_effective,
            )
            short_seed_total_effective = _to_float(
                data.get("short_seed_total_effective", data.get("short_seed_total", default.short_seed_total_effective)),
                default.short_seed_total_effective,
            )

            unit_seed_long = _to_float(
                data.get("unit_seed_long", data.get("unit_seed", default.unit_seed_long)),
                default.unit_seed_long,
            )
            unit_seed_short = _to_float(
                data.get("unit_seed_short", data.get("unit_seed", default.unit_seed_short)),
                default.unit_seed_short,
            )

            k_long = _to_int(data.get("k_long", default.k_long), default.k_long)
            k_short = _to_int(data.get("k_short", default.k_short), default.k_short)
            startup_done = _to_bool(data.get("startup_done", default.startup_done), default.startup_done)

            total_balance_snap = _to_float(data.get("total_balance_snap", default.total_balance_snap), default.total_balance_snap)
            total_balance = _to_float(data.get("total_balance", default.total_balance), default.total_balance)
            free_balance = _to_float(data.get("free_balance", default.free_balance), default.free_balance)
            hedge_size = _to_float(data.get("hedge_size", default.hedge_size), default.hedge_size)

            long_size = _to_float(data.get("long_size", default.long_size), default.long_size)
            short_size = _to_float(data.get("short_size", default.short_size), default.short_size)
            long_pnl = _to_float(data.get("long_pnl", default.long_pnl), default.long_pnl)
            short_pnl = _to_float(data.get("short_pnl", default.short_pnl), default.short_pnl)

            long_pos_nonzero = _to_bool(data.get("long_pos_nonzero", default.long_pos_nonzero), default.long_pos_nonzero)
            short_pos_nonzero = _to_bool(data.get("short_pos_nonzero", default.short_pos_nonzero), default.short_pos_nonzero)

            long_pnl_sign = _to_int(data.get("long_pnl_sign", default.long_pnl_sign), default.long_pnl_sign)
            short_pnl_sign = _to_int(data.get("short_pnl_sign", default.short_pnl_sign), default.short_pnl_sign)

            long_steps_filled = _to_int(data.get("long_steps_filled", default.long_steps_filled), default.long_steps_filled)
            short_steps_filled = _to_int(data.get("short_steps_filled", default.short_steps_filled), default.short_steps_filled)

            news_block = _to_bool(data.get("news_block", default.news_block), default.news_block)
            cb_block = _to_bool(data.get("cb_block", default.cb_block), default.cb_block)

            line_memory_long = self._deserialize_line_memory(data.get("line_memory_long", default.line_memory_long))
            line_memory_short = self._deserialize_line_memory(data.get("line_memory_short", default.line_memory_short))

            long_tp_active = _to_bool(data.get("long_tp_active", default.long_tp_active), default.long_tp_active)
            long_tp_max_index = _to_int(data.get("long_tp_max_index", default.long_tp_max_index), default.long_tp_max_index)
            short_tp_active = _to_bool(data.get("short_tp_active", default.short_tp_active), default.short_tp_active)
            short_tp_max_index = _to_int(data.get("short_tp_max_index", default.short_tp_max_index), default.short_tp_max_index)

            dca_near_eps = _to_float(
                data.get("dca_near_eps", data.get("dca_touch_eps", default.dca_near_eps)),
                default.dca_near_eps,
            )
            dca_cooldown_sec = _to_float(data.get("dca_cooldown_sec", default.dca_cooldown_sec), default.dca_cooldown_sec)
            dca_repeat_guard = _to_float(data.get("dca_repeat_guard", default.dca_repeat_guard), default.dca_repeat_guard)

            dca_guard_wave_id = _to_int(data.get("dca_guard_wave_id", default.dca_guard_wave_id), default.dca_guard_wave_id)

            raw_used = data.get("dca_used_indices", default.dca_used_indices)
            used_indices: List[int] = []
            if isinstance(raw_used, list):
                for x in raw_used:
                    try:
                        used_indices.append(int(x))
                    except Exception:
                        continue
            else:
                used_indices = list(default.dca_used_indices)

            dca_last_ts = _to_float(data.get("dca_last_ts", default.dca_last_ts), default.dca_last_ts)
            dca_last_idx = _to_int(data.get("dca_last_idx", default.dca_last_idx), default.dca_last_idx)
            dca_last_price = _to_float(data.get("dca_last_price", default.dca_last_price), default.dca_last_price)

            dca_win_low = _opt_float(data.get("dca_win_low", default.dca_win_low))
            dca_win_high = _opt_float(data.get("dca_win_high", default.dca_win_high))
            dca_win_ts = _to_float(data.get("dca_win_ts", default.dca_win_ts), default.dca_win_ts)

            prev_long_size = _to_float(data.get("prev_long_size", default.prev_long_size), default.prev_long_size)
            prev_short_size = _to_float(data.get("prev_short_size", default.prev_short_size), default.prev_short_size)
            prev_long_tp_active = _to_bool(data.get("prev_long_tp_active", default.prev_long_tp_active), default.prev_long_tp_active)
            prev_short_tp_active = _to_bool(data.get("prev_short_tp_active", default.prev_short_tp_active), default.prev_short_tp_active)

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
                total_balance_snap=total_balance_snap,
                total_balance=total_balance,
                free_balance=free_balance,
                hedge_size=hedge_size,
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
                long_tp_active=long_tp_active,
                long_tp_max_index=long_tp_max_index,
                short_tp_active=short_tp_active,
                short_tp_max_index=short_tp_max_index,
                dca_near_eps=dca_near_eps,
                dca_cooldown_sec=dca_cooldown_sec,
                dca_repeat_guard=dca_repeat_guard,
                dca_guard_wave_id=dca_guard_wave_id,
                dca_used_indices=used_indices,
                dca_last_ts=dca_last_ts,
                dca_last_idx=dca_last_idx,
                dca_last_price=dca_last_price,
                dca_win_low=dca_win_low,
                dca_win_high=dca_win_high,
                dca_win_ts=dca_win_ts,
                prev_long_size=prev_long_size,
                prev_short_size=prev_short_size,
                prev_long_tp_active=prev_long_tp_active,
                prev_short_tp_active=prev_short_tp_active,
            )
        except Exception as exc:
            logger.error("BotState 역직렬화 실패 → 기본 상태로 복구: %s", exc)
            return self._default_bot_state()

    # ==================================================================
    # 파일 I/O
    # ==================================================================

    def _load_state(self) -> BotState:
        path = self.state_file_path
        if not os.path.exists(path):
            logger.info("BotState 파일 없음 → 기본 상태로 시작 (path=%s)", path)
            return self._default_bot_state()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = self._deserialize_bot_state(data)
            logger.info(
                "BotState 로드: path=%s mode=%s wave_id=%s p_center=%s p_gap=%s",
                path, state.mode, state.wave_id, state.p_center, state.p_gap,
            )
            return state
        except Exception as e:
            logger.error("BotState 파일 로드 실패 → 기본 상태로 복구: %s", e)
            return self._default_bot_state()

    def save_state(self) -> None:
        data = self._serialize_bot_state(self.state)
        path = self.state_file_path
        tmp_path = f"{path}.tmp"

        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error("BotState 저장 실패: %s", e)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    # ==================================================================
    # 외부 helper
    # ==================================================================

    def get_state(self) -> BotState:
        return self.state

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        return getattr(self.state, key, default)

    def update(self, key: str, value: Any, *, save: bool = True) -> None:
        """
        단일 BotState 필드 업데이트.
        - save=False 를 쓰면 디스크 저장을 지연시킬 수 있다.
        """
        if not hasattr(self.state, key):
            logger.warning("BotState에 존재하지 않는 필드 업데이트 시도: %s", key)
            return
        setattr(self.state, key, value)
        if save:
            self.save_state()

    # ==================================================================
    # Wave reset
    # ==================================================================

    def reset_wave(
        self,
        *,
        p_center: float,
        p_gap: float,
        atr_value: float,
        total_balance_snap: float,
        long_seed_total_effective: float,
        short_seed_total_effective: float,
        unit_seed_long: float,
        unit_seed_short: float,
        mode: str = "NORMAL",
    ) -> BotState:
        new_wave_id = self.state.wave_id + 1

        dca_near_eps = float(getattr(self.state, "dca_near_eps", 0.18) or 0.18)
        dca_cooldown_sec = float(getattr(self.state, "dca_cooldown_sec", 20.0) or 20.0)
        dca_repeat_guard = float(getattr(self.state, "dca_repeat_guard", 0.50) or 0.50)

        tb_snap = float(total_balance_snap or 0.0)
        prev_total = float(getattr(self.state, "total_balance", 0.0) or 0.0)
        prev_free = float(getattr(self.state, "free_balance", 0.0) or 0.0)
        tb = prev_total if prev_total > 0.0 else tb_snap
        fb = prev_free if prev_free > 0.0 else tb

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
            total_balance_snap=tb_snap,
            total_balance=tb,
            free_balance=fb,
            hedge_size=0.0,
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
            long_tp_active=False,
            long_tp_max_index=0,
            short_tp_active=False,
            short_tp_max_index=0,
            dca_near_eps=dca_near_eps,
            dca_cooldown_sec=dca_cooldown_sec,
            dca_repeat_guard=dca_repeat_guard,
            dca_guard_wave_id=new_wave_id,
            dca_used_indices=[],
            dca_last_ts=0.0,
            dca_last_idx=10**9,
            dca_last_price=0.0,
            dca_win_low=None,
            dca_win_high=None,
            dca_win_ts=0.0,
            prev_long_size=0.0,
            prev_short_size=0.0,
            prev_long_tp_active=False,
            prev_short_tp_active=False,
        )

        self.save_state()
        logger.info(
            "Wave reset 완료: wave_id=%s mode=%s p_center=%.2f p_gap=%.2f atr=%.2f total_balance_snap=%.2f",
            self.state.wave_id,
            self.state.mode,
            self.state.p_center,
            self.state.p_gap,
            self.state.atr_value,
            self.state.total_balance_snap,
        )
        return self.state

    # ==================================================================
    # LineMemory FSM API
    # ==================================================================

    def _get_line_memory_ref(self, side: str) -> Dict[int, LineState]:
        s = side.upper()
        if s == "LONG":
            return self.state.line_memory_long
        if s == "SHORT":
            return self.state.line_memory_short
        raise ValueError(f"invalid side for line memory: {side!r}")

    def get_line_state(self, side: str, grid_index: int) -> LineState:
        mem = self._get_line_memory_ref(side)
        return mem.get(int(grid_index), LineState.FREE)

    def mark_line_open(self, side: str, grid_index: int) -> LineState:
        idx = int(grid_index)
        mem = self._get_line_memory_ref(side)
        mem[idx] = LineState.OPEN
        self.save_state()
        logger.debug("Line OPEN: side=%s idx=%s", side.upper(), idx)
        return mem[idx]

    def update_line_state(self, side: str, grid_index: int, outcome: str) -> LineState:
        idx = int(grid_index)
        mem = self._get_line_memory_ref(side)
        outcome_norm = str(outcome).upper()

        if outcome_norm in ("PROFIT", "BREAKEVEN"):
            mem.pop(idx, None)
            new_state = LineState.FREE
        elif outcome_norm == "LOSS":
            mem[idx] = LineState.LOCKED_LOSS
            new_state = LineState.LOCKED_LOSS
        else:
            logger.warning(
                "알 수 없는 outcome 으로 LineState 업데이트 시도: side=%s idx=%s outcome=%s",
                side, idx, outcome,
            )
            new_state = mem.get(idx, LineState.FREE)

        self.save_state()

        logger.debug(
            "LineState 업데이트: side=%s idx=%s outcome=%s → %s",
            side.upper(), idx, outcome_norm, new_state.value,
        )
        return new_state


state_manager = StateManager()


def get_state_manager() -> StateManager:
    return state_manager

"""
escape_feed_adapter.py

- StateManager state + 실시간 ticker 를
  EscapeLogic.evaluate 에서 기대하는 feed 형태로 변환하는 어댑터.
- ESCAPE/FULL_EXIT/HEDGE FSM 설계는 변경하지 않고,
  feed 배선만 단일 모듈로 모아서 재사용하기 위한 용도.
"""

from __future__ import annotations
from typing import Any, Dict


def build_escape_feed(symbol: str, ticker_price: Any, state_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    StateManager 의 state dict + 현재 가격을 기반으로
    EscapeLogic.evaluate(...) 에 넘길 feed dict 를 구성한다.
    """
    state = dict(state_dict) if isinstance(state_dict, dict) else {}

    feed: Dict[str, Any] = {}

    # 공통 메타 정보
    feed["symbol"] = symbol
    if isinstance(ticker_price, (int, float)):
        feed["price"] = float(ticker_price)
    else:
        feed["price"] = float(state.get("price", 0.0) or 0.0)
    feed["wave_id"] = state.get("wave_id")

    # 포지션/PNL 관련
    feed["long_size"] = state.get("long_size", 0.0)
    feed["short_size"] = state.get("short_size", 0.0)
    feed["long_pnl"] = state.get("long_pnl", 0.0)
    feed["short_pnl"] = state.get("short_pnl", 0.0)

    # Seed / 계좌 / 리스크 관련
    feed["total_balance"] = state.get("total_balance", 0.0)
    feed["reserve_balance"] = state.get("reserve_balance", 0.0)
    feed["unit_seed"] = state.get("unit_seed", 0.0)
    feed["long_seed_total"] = state.get("long_seed_total", 0.0)
    feed["short_seed_total"] = state.get("short_seed_total", 0.0)

    # Grid / ATR / 모드 관련
    feed["mode"] = state.get("mode", "NORMAL")
    feed["p_center"] = state.get("p_center", 0.0)
    feed["p_gap"] = state.get("p_gap", 0.0)
    feed["atr_value"] = state.get("atr_value", 0.0)
    feed["line_index"] = state.get("line_index", 0)
    feed["long_steps_filled"] = state.get("long_steps_filled", 0)
    feed["short_steps_filled"] = state.get("short_steps_filled", 0)

    # Escape / Hedge / 뉴스 관련 플래그
    feed["escape_active"] = state.get("escape_active", False)
    feed["escape_reason"] = state.get("escape_reason", "")
    feed["escape_enter_ts"] = state.get("escape_enter_ts")
    feed["hedge_size"] = state.get("hedge_size", 0.0)
    feed["hedge_side"] = state.get("hedge_side", "")
    feed["news_block"] = state.get("news_block", False)
    feed["is_macro_window"] = state.get("is_macro_window", False)

    # 시그널 관련 (1m 움직임, 변동성 등)
    feed["move_1m"] = state.get("move_1m", 0.0)
    feed["vol_1m"] = state.get("vol_1m", 0.0)
    feed["vol_ma20"] = state.get("vol_ma20", 0.0)
    feed["last3_ranges"] = state.get("last3_ranges", [])

    # touched_lines 같은 히스토리도 그대로 보존
    feed["touched_lines"] = state.get("touched_lines", [])

    return feed


class StateProxy:
    """
    feed.state 가 dict 이면서도, feed.state.long_size 처럼도 접근 가능하게 해주는 proxy.
    EscapeLogic 쪽에서 state.xxx 와 state["xxx"] 둘 다 자연스럽게 쓸 수 있게 한다.
    """
    def __init__(self, state: Dict[str, Any]):
        self._state = state

    def __getattr__(self, name: str) -> Any:
        try:
            return self._state[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __getitem__(self, key: str) -> Any:
        return self._state[key]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._state)


class FeedWrapper:
    """
    EscapeLogic 쪽에서:
      - feed.state.xxx
      - feed.xxx
    두 방식 모두 지원하도록 dict 를 proxy 하는 래퍼.
    """
    def __init__(self, feed: Dict[str, Any]):
        self._feed = feed
        self._state_proxy = StateProxy(feed)

    @property
    def state(self) -> StateProxy:
        return self._state_proxy

    def __getattr__(self, name: str) -> Any:
        # 우선 state proxy 에게 위임 (feed.long_size → state.long_size)
        try:
            return getattr(self._state_proxy, name)
        except AttributeError:
            pass
        # 그래도 없으면 raw dict 에서 직접 찾기
        try:
            return self._feed[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._feed)


def build_wrapped_escape_feed(symbol: str, ticker_price: Any, state_dict: Dict[str, Any]) -> FeedWrapper:
    """
    symbol + ticker_price + state_dict 로부터
    FeedWrapper 인스턴스를 바로 만들어 주는 헬퍼.
    """
    feed = build_escape_feed(symbol, ticker_price, state_dict)
    return FeedWrapper(feed)

# --- EscapeLogic 과 직접 연결하기 위한 helper (운영 코드용) ---

from .escape_logic import EscapeLogic


def evaluate_escape_from_state(
    capital,
    symbol: str,
    ticker_price,
    state_dict,
):
    """
    운영 코드(wave_bot / FSM)에서 쓰기 위한 단일 진입점.

    인자:
      - capital: 이미 wave 엔진에서 사용 중인 capital 객체
      - symbol: 예) "BTCUSDT"
      - ticker_price: 현재 가격 (float 또는 숫자)
      - state_dict: StateManager 에서 꺼낸 state dict

    반환:
      - EscapeLogic.evaluate(...) 가 반환하는 EscapeDecision
        (mode_override, orders, full_exit, state_updates 포함)
    """
    # FeedWrapper 까지 포함된 표준 escape feed 생성
    wrapped_feed = build_wrapped_escape_feed(symbol, ticker_price, state_dict)
    logic = EscapeLogic(capital)
    return logic.evaluate(wrapped_feed)

# --- EscapeLogic 과 직접 연결하기 위한 운영용 helper ---

from .escape_logic import EscapeLogic


def evaluate_escape_from_state(
    capital,
    symbol: str,
    ticker_price,
    state_dict,
):
    """
    운영 코드(wave_bot / FSM)에서 쓰기 위한 단일 진입점.

    인자:
      - capital: 이미 wave 엔진에서 사용 중인 capital 객체
      - symbol: 예) "BTCUSDT"
      - ticker_price: 현재 가격 (float 또는 숫자)
      - state_dict: StateManager 에서 꺼낸 state dict

    반환:
      - EscapeLogic.evaluate(...) 가 반환하는 EscapeDecision
        (mode_override, orders, full_exit, state_updates 포함)
    """
    wrapped_feed = build_wrapped_escape_feed(symbol, ticker_price, state_dict)
    logic = EscapeLogic(capital)
    return logic.evaluate(wrapped_feed)

# --- EscapeDecision 결과를 StateManager state 에 반영하기 위한 helper ---

def apply_escape_state_updates(state_dict, decision):
    """
    EscapeDecision.state_updates 를 기존 state dict 에 반영한 새 dict 를 반환한다.

    인자:
      - state_dict: StateManager 에서 꺼낸 기존 state (dict)
      - decision: EscapeLogic.evaluate(...) 가 반환한 EscapeDecision

    반환:
      - state_updates 가 merge 된 새 dict
    """
    base = dict(state_dict) if isinstance(state_dict, dict) else {}
    updates = getattr(decision, "state_updates", None) or {}
    for k, v in updates.items():
        base[k] = v
    return base

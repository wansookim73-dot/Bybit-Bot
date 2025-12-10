"""
escape_runtime_bridge.py

운영 코드(wave_bot / FSM)에서 ESCAPE/FULL_EXIT/HEDGE 를 평가하고
StateManager / OrderManager 와 연결하기 위한 얇은 브리지 모듈.

전략/설계(escape_logic)는 건드리지 않고,
배선과 상태 반영만 담당한다.
"""

from __future__ import annotations
from typing import Any, Dict, Optional

from core import state_manager as state_manager_mod
from core import exchange_api as exchange_api_mod
from strategy.escape_feed_adapter import (
    evaluate_escape_from_state,
    apply_escape_state_updates,
)


def _safe_get_ticker(api: Any, symbol: str) -> Any:
    """
    ExchangeAPI.get_ticker 서명에 맞춰 유연하게 ticker 를 가져오는 helper.
    - def get_ticker(self)            형태
    - def get_ticker(self, symbol)    형태
    둘 다 지원.
    """
    if not hasattr(api, "get_ticker"):
        raise AttributeError("ExchangeAPI 에 get_ticker 가 없습니다.")

    fn = api.get_ticker
    try:
        # bound method 기준으로 signature 확인
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if len(params) == 1:
            return fn()
        else:
            return fn(symbol)
    except TypeError:
        # 서명 분석이 어긋난 경우 마지막 방어선: 인자 없이 한 번 더 시도
        return fn()


def _get_state_dict(sm: Any) -> Dict[str, Any]:
    """
    StateManager 에서 state dict 하나를 최대한 잘 뽑아내는 helper.
    우선순위:
      1) get_state()
      2) .state / ._state
      3) __dict__
    """
    # 1) get_state()
    if hasattr(sm, "get_state"):
        state = sm.get_state()
        if isinstance(state, dict):
            return state
        return {"_raw_state": state}

    # 2) .state / ._state
    for attr in ("state", "_state"):
        if hasattr(sm, attr):
            val = getattr(sm, attr)
            if isinstance(val, dict):
                return val
            return {"_raw_state": val}

    # 3) __dict__
    return dict(getattr(sm, "__dict__", {}))


def _apply_state_to_manager(sm: Any, new_state: Dict[str, Any]) -> None:
    """
    EscapeDecision.state_updates 가 merge 된 state dict 를
    StateManager 에 다시 반영하는 helper.

    우선순위:
      1) set_state(dict)
      2) update_state(dict)
      3) .state 에 통째로 대입
      4) 개별 속성으로 setattr
    """
    if hasattr(sm, "set_state"):
        sm.set_state(new_state)
        return

    if hasattr(sm, "update_state"):
        sm.update_state(new_state)
        return

    if hasattr(sm, "state"):
        sm.state = new_state
        return

    # 최후의 수단: 개별 속성으로 풀어서 세팅
    for k, v in new_state.items():
        setattr(sm, k, v)


def _dispatch_escape_orders(order_manager: Any, decision: Any) -> None:
    """
    EscapeDecision.orders 를 OrderManager 로 전달하는 helper.

    OrderManager 쪽 메서드 이름이 어떤지 아직 확실치 않으므로,
    아래 우선순위로 유연하게 시도하고, 실패하면 조용히 무시한다.

    우선순위:
      1) submit_escape_orders(orders)
      2) submit_orders(orders)
      3) submit_order(order) 반복 호출
      4) place_order(order) 반복 호출
    """
    if order_manager is None or decision is None:
        return

    orders = getattr(decision, "orders", None) or []
    if not orders:
        return

    # 1) 배치 메서드 우선
    if hasattr(order_manager, "submit_escape_orders"):
        order_manager.submit_escape_orders(orders)
        return

    if hasattr(order_manager, "submit_orders"):
        order_manager.submit_orders(orders)
        return

    # 2) 개별 메서드 반복
    if hasattr(order_manager, "submit_order"):
        for o in orders:
            order_manager.submit_order(o)
        return

    if hasattr(order_manager, "place_order"):
        for o in orders:
            order_manager.place_order(o)
        return

    # 그 외에는 아무 것도 하지 않음 (운영 코드에서 직접 핸들링하도록)


def run_escape_cycle(
    capital: Any,
    symbol: str,
    state_mgr: Optional[Any] = None,
    exch: Optional[Any] = None,
    order_mgr: Optional[Any] = None,
    pnl_total: Any = None,
    pnl_total_pct: Any = None,
) -> Any:
    """
    ESCAPE/FULL_EXIT/HEDGE 1회 평가 사이클을 실행하는 운영용 helper.

    인자:
      - capital   : wave 엔진에서 사용하는 실제 capital 객체
      - symbol    : 예) "BTCUSDT"
      - state_mgr : StateManager 인스턴스 (없으면 core.state_manager.StateManager() 생성 시도)
      - exch      : ExchangeAPI 인스턴스 (없으면 core.exchange_api.ExchangeAPI() 생성 시도)
      - order_mgr : OrderManager 인스턴스 (없으면 orders 전달 생략)

    동작:
      1) ticker, state 를 읽어온다.
      2) evaluate_escape_from_state(...) 로 EscapeDecision 을 계산한다.
      3) apply_escape_state_updates(...) 로 state 업데이트를 merge 한다.
      4) StateManager 에 업데이트를 반영한다.
      5) OrderManager 로 Escape/Hedge 주문(orders)을 전달한다. (가능한 경우)
      6) EscapeDecision 을 그대로 반환한다.
    """
    # 0) 인스턴스 준비
    state_mgr = state_mgr or getattr(state_manager_mod, "StateManager")()
    exch = exch or getattr(exchange_api_mod, "ExchangeAPI")()

    # 1) 가격 / state 읽기
    ticker_price = _safe_get_ticker(exch, symbol)
    state_dict = _get_state_dict(state_mgr)

    # '_raw_state' 래핑된 경우 풀기
    if "_raw_state" in state_dict and isinstance(state_dict["_raw_state"], dict):
        base_state = state_dict["_raw_state"]
    elif "state" in state_dict and isinstance(state_dict["state"], dict):
        base_state = state_dict["state"]
    else:
        base_state = state_dict

    # PnL 값이 명시적으로 주어지지 않았다면, state 기반으로 추정
    if pnl_total is None or pnl_total_pct is None:
        try:
            long_pnl = float(base_state.get("long_pnl", 0.0) or 0.0)
            short_pnl = float(base_state.get("short_pnl", 0.0) or 0.0)
            total_pnl = long_pnl + short_pnl
            total_balance = float(base_state.get("total_balance", 0.0) or 0.0)
        except Exception:
            total_pnl = 0.0
            total_balance = 0.0
        if pnl_total is None:
            pnl_total = total_pnl
        if pnl_total_pct is None:
            pnl_total_pct = (total_pnl / total_balance) if total_balance > 0 else 0.0

    # 2) Escape 평가
    decision = evaluate_escape_from_state(
        capital=capital,
        symbol=symbol,
        ticker_price=ticker_price,
        state_dict=base_state,
    )

    # 3) state 업데이트 merge
    new_state = apply_escape_state_updates(base_state, decision)

    # 4) StateManager 에 반영
    _apply_state_to_manager(state_mgr, new_state)

    # 5) 주문 전달
    _dispatch_escape_orders(order_mgr, decision)

    # 6) 결과 반환 (wave_bot / FSM 에서 mode_override 등 참고 가능)
    return decision

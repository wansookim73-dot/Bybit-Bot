import sys
import inspect
from copy import deepcopy
from pathlib import Path
from pprint import pprint
from typing import Any, Dict

# 프로젝트 루트 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import exchange_api, state_manager
from strategy.escape_feed_adapter import (
    evaluate_escape_from_state,
    apply_escape_state_updates,
)


def safe_get_ticker(api, symbol: str):
    """ExchangeAPI.get_ticker 서명에 맞춰 유연하게 ticker 를 가져오는 helper."""
    if not hasattr(api, "get_ticker"):
        return "[NO get_ticker]"

    fn = api.get_ticker
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if len(params) == 1:
            return fn()
        else:
            return fn(symbol)
    except TypeError:
        try:
            return fn()
        except Exception as e:
            return f"[get_ticker ERROR: {e!r}]"
    except Exception as e:
        return f"[get_ticker ERROR: {e!r}]"


def safe_get_state_dict(sm) -> Dict[str, Any]:
    """StateManager 에서 state dict 하나를 최대한 잘 뽑아내는 helper."""
    if hasattr(sm, "get_state"):
        try:
            state = sm.get_state()
            if isinstance(state, dict):
                return state
            return {"_raw_state": state}
        except Exception as e:
            return {"_type": "get_state_error", "error": repr(e)}

    for attr in ("state", "_state"):
        if hasattr(sm, attr):
            try:
                state = getattr(sm, attr)
                if isinstance(state, dict):
                    return state
                return {"_raw_state": state}
            except Exception as e:
                return {"_type": f"{attr}_error", "error": repr(e)}

    try:
        return {"_raw_state": sm.__dict__}
    except Exception as e:
        return {"_type": "__dict___error", "error": repr(e)}


def resolve_instance(module, *class_name_candidates):
    """
    모듈 안에서 class_name 후보를 순서대로 찾아 인스턴스를 생성한다.
    실제 클래스 이름이 다를 수 있으므로, 그냥 실패하면 None 리턴.
    """
    for name in class_name_candidates:
        cls = getattr(module, name, None)
        if cls is not None:
            try:
                return cls()
            except TypeError:
                # __init__ 인자가 필요한 경우에는 여기서 생성 포기 (운영 코드에서 직접 만들면 됨)
                return None
            except Exception:
                return None
    return None


def main():
    print("[escape_live_tick] starting...")
    print(f"[INFO] ROOT added to sys.path: {ROOT}")

    # ExchangeAPI / StateManager 인스턴스 생성
    api_cls = getattr(exchange_api, "ExchangeAPI", None)
    sm_cls = getattr(state_manager, "StateManager", None)

    if api_cls is None or sm_cls is None:
        print("[ERROR] ExchangeAPI / StateManager 클래스를 찾지 못했습니다.")
        print(" - core.exchange_api / core.state_manager 내용을 확인해서,"
              "   이 스크립트 상단의 클래스 이름을 맞춰 주세요.")
        return

    api = api_cls()
    sm = sm_cls()

    symbol = "BTCUSDT"

    # 실제 가격 / state 가져오기
    ticker = safe_get_ticker(api, symbol)
    print("[TICKER_RAW]")
    pprint(ticker)

    state_dict = safe_get_state_dict(sm)
    print("[STATE_DICT_RAW]")
    pprint(state_dict)

    if not isinstance(state_dict, dict):
        print("[ERROR] state_dict 가 dict 가 아닙니다. 종료.")
        return

    # '_raw_state' 래핑된 경우 풀기
    if "_raw_state" in state_dict and isinstance(state_dict["_raw_state"], dict):
        base_state = state_dict["_raw_state"]
    elif "state" in state_dict and isinstance(state_dict["state"], dict):
        base_state = state_dict["state"]
    else:
        base_state = state_dict

    print("\n[BASE_STATE]")
    pprint(base_state)

    # capital 객체는 운영 코드에서 이미 가지고 있을 텐데,
    # 여기서는 아직 실제 클래스를 모르므로, None 으로 흘려보내고
    # 나중에 wave_bot 에서 실제 capital 인스턴스를 넣도록 한다.
    capital = None

    print("\n[INFO] Calling evaluate_escape_from_state(...) with capital=None (테스트 모드)")
    try:
        decision = evaluate_escape_from_state(
            capital=capital,
            symbol=symbol,
            ticker_price=ticker,
            state_dict=base_state,
        )
    except TypeError as e:
        print("[ERROR] evaluate_escape_from_state 호출 실패:", repr(e))
        print(" - EscapeLogic.__init__(capital)이 실제 capital 객체를 필요로 할 수 있습니다.")
        print(" - wave_bot / FSM 에서 실제 capital 인스턴스를 넘겨주도록 연결하면 됩니다.")
        return
    except Exception as e:
        print("[ESCAPE_EVAL_ERROR]")
        print(repr(e))
        return

    print("\n[ESCAPE_DECISION]")
    pprint(decision)

    # state_updates 반영 시뮬레이션
    new_state = apply_escape_state_updates(base_state, decision)
    print("\n[STATE_AFTER_UPDATES]")
    pprint(new_state)


if __name__ == "__main__":
    main()

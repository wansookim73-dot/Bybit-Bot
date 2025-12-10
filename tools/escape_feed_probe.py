import sys
import inspect
from pathlib import Path
from pprint import pprint

# 1) 프로젝트 루트 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import exchange_api, state_manager
from strategy import escape_logic
from strategy.escape_feed_adapter import build_escape_feed, build_wrapped_escape_feed


def safe_get_ticker(api, symbol: str):
    """
    ExchangeAPI.get_ticker 서명에 맞춰 유연하게 ticker 를 가져오는 helper.
    """
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


def safe_get_state_dict(sm):
    """
    StateManager 에서 state dict 하나를 최대한 잘 뽑아내는 helper.
    """
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


def resolve_escape_evaluator():
    """
    strategy.escape_logic 안에서 Escape 평가 함수를 찾아오는 helper.
    1) EscapeLogic(capital) 인스턴스의 evaluate
    2) 모듈 레벨의 evaluate_escape / evaluate
    """
    logic_cls = getattr(escape_logic, "EscapeLogic", None)
    if logic_cls is not None:
        class DummyCapital:
            """
            EscapeLogic.__init__(capital)을 만족시키기 위한 최소 더미.
            필요한 속성이 있으면 에러 로그를 보고 점진적으로 추가 예정.
            """
            pass

        try:
            inst = logic_cls(DummyCapital())
            eval_fn = getattr(inst, "evaluate", None)
            if callable(eval_fn):
                return eval_fn, "EscapeLogic.evaluate(DummyCapital)"
        except TypeError as e:
            print(f"[WARN] EscapeLogic(DummyCapital) 생성 실패: {e!r}")
        except Exception as e:
            print(f"[WARN] EscapeLogic(DummyCapital) 예외: {e!r}")

    for name in ("evaluate_escape", "evaluate"):
        fn = getattr(escape_logic, name, None)
        if callable(fn):
            return fn, f"escape_logic.{name}"

    return None, None


def main():
    print("[escape_feed_probe] starting...")
    print(f"[INFO] ROOT added to sys.path: {ROOT}")

    api_cls = getattr(exchange_api, "ExchangeAPI", None)
    sm_cls = getattr(state_manager, "StateManager", None)

    if api_cls is None or sm_cls is None:
        print("[ERROR] ExchangeAPI / StateManager 클래스를 찾지 못했습니다.")
        return

    api = api_cls()
    sm = sm_cls()

    symbol = "BTCUSDT"

    ticker = safe_get_ticker(api, symbol)
    print("[TICKER_RAW]")
    pprint(ticker)

    state_dict = safe_get_state_dict(sm)
    print("[STATE_DICT_RAW]")
    pprint(state_dict)

    if not isinstance(state_dict, dict):
        print("[ERROR] state_dict 가 dict 가 아닙니다. escape_feed 구성 불가.")
        return

    if "_raw_state" in state_dict and isinstance(state_dict["_raw_state"], dict):
        base_state = state_dict["_raw_state"]
    elif "state" in state_dict and isinstance(state_dict["state"], dict):
        base_state = state_dict["state"]
    else:
        base_state = state_dict

    # 어댑터 모듈 재사용
    escape_feed = build_escape_feed(symbol, ticker, base_state)
    print("\n[ESCAPE_FEED_BUILT]")
    pprint(escape_feed)

    eval_fn, eval_name = resolve_escape_evaluator()
    if eval_fn is None:
        print("\n[WARN] EscapeLogic evaluator 를 찾지 못했습니다.")
        print(" - strategy/escape_logic.py 안의 클래스/함수 이름을 확인하세요.")
        return

    print(f"\n[INFO] Using escape evaluator: {eval_name}")

    wrapped_feed = build_wrapped_escape_feed(symbol, ticker, base_state)

    try:
        result = eval_fn(wrapped_feed)
        print("\n[ESCAPE_EVAL_RESULT]")
        pprint(result)
    except Exception as e:
        print("\n[ESCAPE_EVAL_ERROR]")
        print(repr(e))


if __name__ == "__main__":
    main()

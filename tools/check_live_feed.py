import time
import sys
import inspect
from pathlib import Path
from pprint import pprint

# 프로젝트 루트(/home/ubuntu/mexc_bot)를 sys.path 에 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import exchange_api, state_manager


def safe_get_ticker(api, symbol: str):
    """
    ExchangeAPI.get_ticker 서명을 자동으로 검사해서,
    - def get_ticker(self):           → 인자 없이 호출
    - def get_ticker(self, symbol):   → symbol 넣어서 호출
    둘 다 지원하는 helper.
    """
    if not hasattr(api, "get_ticker"):
        return "[NO get_ticker]"

    fn = api.get_ticker
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        # bound method 기준:
        #   - def get_ticker(self):           → len(params) == 1
        #   - def get_ticker(self, symbol):   → len(params) == 2
        if len(params) == 1:
            return fn()
        else:
            return fn(symbol)
    except TypeError as e:
        # 혹시 서명 분석이 안 맞으면, 마지막 방어선
        try:
            return fn()
        except Exception:
            return f"[get_ticker TypeError: {e!r}]"
    except Exception as e:
        return f"[get_ticker ERROR: {e!r}]"


def safe_get_position_info(sm):
    """
    StateManager 에서 포지션/상태 정보를 최대한 유연하게 꺼내는 helper.
    우선순위:
      1) get_position(...) 같은 메서드가 있으면 그걸 사용
      2) get_state() 메서드가 있으면 그걸 사용
      3) .state / ._state 필드를 사용
      4) 그래도 없으면 __dict__ 전체를 그대로 보여 줌 (디버그 용도)
    """
    # 1) get_position(심볼) 메서드가 실제로 있는 경우
    if hasattr(sm, "get_position"):
        try:
            # 심볼을 안 받는 형태일 수도 있으니 서명 체크
            fn = sm.get_position
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            if len(params) == 1:
                # def get_position(self): 인 경우
                return fn()
            else:
                # def get_position(self, symbol): 인 경우 (일단 BTCUSDT 사용)
                return fn("BTCUSDT")
        except Exception as e:
            return {"_type": "get_position_error", "error": repr(e)}

    # 2) get_state() 메서드
    if hasattr(sm, "get_state"):
        try:
            state = sm.get_state()
            return {"_type": "get_state", "state": state}
        except Exception as e:
            return {"_type": "get_state_error", "error": repr(e)}

    # 3) .state / ._state 필드
    for attr in ("state", "_state"):
        if hasattr(sm, attr):
            try:
                val = getattr(sm, attr)
                return {"_type": attr, "state": val}
            except Exception as e:
                return {"_type": f"{attr}_error", "error": repr(e)}

    # 4) 최후의 수단: __dict__ 전체
    try:
        return {"_type": "__dict__", "state": sm.__dict__}
    except Exception as e:
        return {"_type": "__dict___error", "error": repr(e)}


def main():
    print("[check_live_feed] starting...")
    print(f"[INFO] ROOT added to sys.path: {ROOT}")

    api_cls = getattr(exchange_api, "ExchangeAPI", None)
    sm_cls = getattr(state_manager, "StateManager", None)

    if api_cls is None or sm_cls is None:
        print("[ERROR] ExchangeAPI / StateManager 클래스를 찾지 못했습니다.")
        print(" - core.exchange_api 안의 클래스/함수 이름을 확인해서 이 스크립트 상단을 수정하세요.")
        print(" - core.state_manager 안의 클래스/함수 이름도 같이 확인 필요.")
        return

    api = api_cls()
    sm = sm_cls()

    symbol = "BTCUSDT"

    for i in range(5):
        print(f"\n[loop {i+1}] ---------------------------")

        # 가격 정보
        ticker = safe_get_ticker(api, symbol)
        print("[TICKER]")
        pprint(ticker)

        # 포지션/상태 정보
        pos_info = safe_get_position_info(sm)
        print("[POSITION_INFO]")
        pprint(pos_info)

        time.sleep(1.0)

    print("\n[check_live_feed] done.")


if __name__ == "__main__":
    main()

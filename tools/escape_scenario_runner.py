import sys
import inspect
from copy import deepcopy
from pathlib import Path
from pprint import pprint

# 프로젝트 루트(/home/ubuntu/mexc_bot)를 sys.path 에 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import exchange_api, state_manager
from strategy.escape_logic import EscapeLogic
from strategy.escape_feed_adapter import build_wrapped_escape_feed


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


def safe_get_state_dict(sm):
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


class DummyCapital:
    """
    EscapeLogic.__init__(capital)을 만족시키기 위한 최소 더미.
    실제 capital 객체를 쓰지 않고도 ESCAPE FSM 동작만 보고 싶은 테스트용.
    필요한 필드는 나중에 에러 메시지를 보고 점진적으로 추가하면 됨.
    """
    symbol = "BTCUSDT"

    def __init__(self):
        # 대략적인 기본 값들 (안 쓰이면 상관 없음)
        self.total_equity = 1000.0
        self.side_seed_long = 250.0
        self.side_seed_short = 250.0
        self.reserve = 500.0


def run_scenarios():
    print("[escape_scenario_runner] starting...")
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

    base_state = safe_get_state_dict(sm)
    print("[BASE_STATE_RAW]")
    pprint(base_state)

    if not isinstance(base_state, dict):
        print("[ERROR] base_state 가 dict 가 아닙니다. 테스트 중단.")
        return

    if "_raw_state" in base_state and isinstance(base_state["_raw_state"], dict):
        base_state = base_state["_raw_state"]
    elif "state" in base_state and isinstance(base_state["state"], dict):
        base_state = base_state["state"]

    # ===== 시나리오 정의 =====
    scenarios = [
        ("BASE_IDLE", {}),
        # 롱 포지션 + 큰 손실 (ESCAPE_ON 후보)
        ("LONG_BIG_LOSS", {
            "long_size": 0.05,
            "long_pnl": -80.0,   # USDT 기준 크게 손실
        }),
        # 숏 포지션 + 큰 손실
        ("SHORT_BIG_LOSS", {
            "short_size": 0.05,
            "short_pnl": -80.0,
        }),
        # 뉴스 필터 ON (news_block)
        ("NEWS_BLOCK", {
            "news_block": True,
        }),
        # 헷지 활성 + escape_active 케이스
        ("HEDGE_ACTIVE", {
            "escape_active": True,
            "escape_reason": "synthetic_hedge_test",
            "hedge_size": 0.05,
            "hedge_side": "Sell",
        }),
    ]

    cap = DummyCapital()
    logic = EscapeLogic(cap)

    for name, patch in scenarios:
        state = deepcopy(base_state)
        state.update(patch)

        print("\n" + "=" * 80)
        print(f"[SCENARIO] {name}")
        print("[STATE_PATCH]")
        pprint(patch)

        wrapped_feed = build_wrapped_escape_feed(symbol, ticker, state)

        try:
            decision = logic.evaluate(wrapped_feed)
            print("[ESCAPE_DECISION]")
            pprint(decision)
        except Exception as e:
            print("[ESCAPE_DECISION_ERROR]")
            print(repr(e))


if __name__ == "__main__":
    run_scenarios()

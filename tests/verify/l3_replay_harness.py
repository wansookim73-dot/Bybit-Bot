from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from strategy.feed_types import OrderInfo, StrategyFeed
from strategy.grid_logic import GridDecision, GridLogic, GridOrderSpec
from strategy.state_model import BotState
from core.order_manager import OrderManager

from tests.verify.l3_fill_engine import FillEvent, L3FillEngine


def _sig_filtered_kwargs(cls_or_fn: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    sig = inspect.signature(cls_or_fn)
    allowed = set(sig.parameters.keys())
    return {k: v for k, v in kwargs.items() if k in allowed}


def _make_strategy_feed(
    *,
    price: float,
    atr_4h_42: float,
    state: BotState,
    open_orders: List[OrderInfo],
    # optional fields (may or may not exist)
    trend_strength: str = "RANGE",
    trend_bias: str = "NONE",
    trend_valid: bool = True,
    trend_fresh: bool = True,
    trend_reason: str = "L3",
) -> StrategyFeed:
    kwargs: Dict[str, Any] = dict(
        price=float(price),
        atr_4h_42=float(atr_4h_42),
        state=state,
        open_orders=list(open_orders),
        pnl_total=0.0,
        pnl_total_pct=0.0,
        trend_strength=trend_strength,
        trend_bias=trend_bias,
        trend_valid=bool(trend_valid),
        trend_fresh=bool(trend_fresh),
        trend_reason=str(trend_reason),
    )
    kwargs = _sig_filtered_kwargs(StrategyFeed, kwargs)
    return StrategyFeed(**kwargs)  # type: ignore[arg-type]


def _make_gridorderspec(d: Dict[str, Any]) -> GridOrderSpec:
    # GridOrderSpec 버전 차이(필드 변형)에 대비해 signature-adaptive
    kwargs: Dict[str, Any] = dict(
        side=str(d.get("side", "")),
        price=float(d.get("price", 0.0) or 0.0),
        qty=float(d.get("qty", 0.0) or 0.0),
        grid_index=int(d.get("grid_index", 0) or 0),
        wave_id=int(d.get("wave_id", 0) or 0),
        mode=str(d.get("mode", "A")),
        reduce_only=bool(d.get("reduce_only", False)),
        position_idx=(int(d["position_idx"]) if d.get("position_idx") is not None else None),
        step_cost=int(d.get("step_cost", 2)),  # 있으면만 들어가게 필터링됨
    )
    kwargs = _sig_filtered_kwargs(GridOrderSpec, kwargs)
    return GridOrderSpec(**kwargs)  # type: ignore[arg-type]


def _make_griddecision_override(d: Dict[str, Any]) -> GridDecision:
    mode = str(d.get("mode", "NORMAL"))
    entries = [_make_gridorderspec(x) for x in (d.get("entries", []) or [])]
    replaces = [_make_gridorderspec(x) for x in (d.get("replaces", []) or [])]
    cancels = list(d.get("cancels", []) or [])
    state_updates = dict(d.get("state_updates", {}) or {})

    # GridDecision도 signature-adaptive
    kwargs: Dict[str, Any] = dict(
        mode=mode,
        grid_entries=entries,
        grid_replaces=replaces,
        grid_cancels=cancels,
        state_updates=state_updates,
    )
    kwargs = _sig_filtered_kwargs(GridDecision, kwargs)
    return GridDecision(**kwargs)  # type: ignore[arg-type]


class L3StubExchange:
    """
    OrderManager가 호출할 최소 API만 제공하는 Stub Exchange.
    - 주문/취소 호출을 기록
    - open_orders를 내부 truth로 유지
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._open_orders: List[Dict[str, Any]] = []
        self._oid_seq: int = 0

    def _new_oid(self) -> str:
        self._oid_seq += 1
        return f"l3_oid_{self._oid_seq}"

    def seed_open_orders(self, orders: List[Dict[str, Any]]) -> None:
        self._open_orders = []
        for o in orders or []:
            self._open_orders.append(
                dict(
                    order_id=str(o.get("order_id", "")),
                    side=str(o.get("side", "BUY")).upper(),
                    price=float(o.get("price", 0.0) or 0.0),
                    qty=float(o.get("qty", 0.0) or 0.0),
                    reduce_only=bool(o.get("reduce_only", False)),
                    position_idx=(int(o["position_idx"]) if o.get("position_idx") in (1, 2) else None),
                    tag=o.get("tag"),
                )
            )

        # oid seq를 충돌 방지(가장 큰 숫자 다음)
        max_n = 0
        for o in self._open_orders:
            oid = str(o.get("order_id", ""))
            if oid.startswith("l3_oid_"):
                try:
                    n = int(oid.split("_")[-1])
                    max_n = max(max_n, n)
                except Exception:
                    pass
        self._oid_seq = max(self._oid_seq, max_n)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return list(self._open_orders)

    def cancel_order(self, order_id: str) -> None:
        oid = str(order_id)
        self.calls.append({"kind": "CANCEL", "order_id": oid})
        self._open_orders = [o for o in self._open_orders if str(o.get("order_id")) != oid]

    def place_limit_order(self, side: int, price: float, qty: float, **kwargs: Any) -> str:
        oid = self._new_oid()
        self.calls.append(dict(kind="LIMIT", oid=oid, side=int(side), price=float(price), qty=float(qty), kwargs=dict(kwargs)))

        side_str = "BUY" if side in (1, 2) else "SELL"
        position_idx = kwargs.get("position_idx", kwargs.get("positionIdx", None))
        reduce_only = bool(kwargs.get("reduce_only", kwargs.get("reduceOnly", False)))

        # L3 verify: 일부 호출에서는 position_idx가 kwargs로 안 올 수 있다.
        # 실전 Bybit hedge 기본 규칙에 맞게 기록 단계에서 안전 기본값을 채운다.
        if position_idx is None:
            position_idx = 1 if side_str == "BUY" else 2

        self._open_orders.append(
            dict(
                order_id=oid,
                side=side_str,
                price=float(price),
                qty=float(qty),
                reduce_only=reduce_only,
                position_idx=(int(position_idx) if position_idx in (1, 2) else None),
                tag=kwargs.get("tag") or kwargs.get("orderLinkId") or kwargs.get("client_order_id"),
            )
        )
        return oid

    def place_tp_limit_order(
        self,
        side: int,
        price: float,
        qty: float,
        *,
        position_idx: int,
        reduce_only: bool = True,
        tag: Optional[str] = None,
    ) -> str:
        oid = self._new_oid()
        self.calls.append(
            dict(
                kind="TP_LIMIT",
                oid=oid,
                side=int(side),
                price=float(price),
                qty=float(qty),
                position_idx=int(position_idx),
                reduce_only=bool(reduce_only),
                tag=tag,
            )
        )

        side_str = "BUY" if side in (1, 2) else "SELL"
        self._open_orders.append(
            dict(
                order_id=oid,
                side=side_str,
                price=float(price),
                qty=float(qty),
                reduce_only=True,
                position_idx=int(position_idx),
                tag=tag,
            )
        )
        return oid

    def place_market_order(self, side: int, qty: float, *, price_for_calc: Optional[float] = None, tag: Optional[str] = None) -> str:
        oid = self._new_oid()
        self.calls.append(dict(kind="MARKET", oid=oid, side=int(side), qty=float(qty), price_for_calc=price_for_calc, tag=tag))
        return oid

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        return {"dealVol": 0.0}

    def get_positions(self) -> Dict[str, Dict[str, float]]:
        return {"LONG": {"qty": 0.0, "avg_price": 0.0}, "SHORT": {"qty": 0.0, "avg_price": 0.0}}


@dataclass
class L3RunOutput:
    calls: List[Dict[str, Any]]
    stub_calls: List[Dict[str, Any]]
    final_state: BotState
    final_positions: Dict[str, Dict[str, float]]
    final_open_orders: List[Dict[str, Any]]


class L3ReplayHarness:
    """
    L3-v1 (수동 조립 + 결정 override 지원):
    - tick에 decision_override가 있으면 GridLogic 대신 override를 사용
    - OrderManager.apply_decision 실행 결과(StubExchange calls)로 불변조건 검증
    """

    def __init__(self) -> None:
        self.fill_engine = L3FillEngine()

    @staticmethod
    def _to_orderinfo_list(open_orders_dicts: List[Dict[str, Any]]) -> List[OrderInfo]:
        out: List[OrderInfo] = []
        for o in open_orders_dicts or []:
            out.append(
                OrderInfo(
                    order_id=str(o.get("order_id", "")),
                    side=str(o.get("side", "BUY")),
                    price=float(o.get("price", 0.0) or 0.0),
                    qty=float(o.get("qty", 0.0) or 0.0),
                    filled_qty=0.0,
                    reduce_only=bool(o.get("reduce_only", False)),
                    order_type="Limit",
                    time_in_force="PostOnly",
                    tag=str(o.get("tag")) if o.get("tag") is not None else None,
                    created_ts=1700000000.0,
                )
            )
        return out

    @staticmethod
    def format_tail(calls: List[Dict[str, Any]], n: int = 10) -> str:
        tail = calls[-n:] if len(calls) > n else calls
        out = []
        for c in tail:
            out.append(f"tick={c.get('tick')} price={c.get('price')} mode={c.get('mode')} entries={c.get('entries_cnt')} cancels={c.get('cancels_cnt')}")
        return "\n".join(out)

    def run(self, spec: Dict[str, Any]) -> L3RunOutput:
        state: BotState = spec["initial_state"]
        positions = {
            "LONG": {"qty": float(spec.get("initial_positions", {}).get("LONG", 0.0))},
            "SHORT": {"qty": float(spec.get("initial_positions", {}).get("SHORT", 0.0))},
        }

        stub = L3StubExchange()
        stub.seed_open_orders(list(spec.get("initial_open_orders", []) or []))

        om = OrderManager(exchange_instance=stub)
        grid = GridLogic()

        calls: List[Dict[str, Any]] = []

        # open_orders truth: stub.get_open_orders()
        for tick in spec["ticks"]:
            price = float(tick["price"])
            open_orders_dicts = stub.get_open_orders()

            feed = _make_strategy_feed(
                price=price,
                atr_4h_42=float(spec.get("atr_4h_42", 300.0)),
                state=state,
                open_orders=self._to_orderinfo_list(open_orders_dicts),
                trend_strength=str(tick.get("trend_strength", "RANGE")),
            )

            if tick.get("decision_override") is not None:
                dec = _make_griddecision_override(tick["decision_override"])
            else:
                dec = grid.process(feed)

            calls.append(
                dict(
                    tick=tick["t"],
                    price=price,
                    mode=getattr(dec, "mode", ""),
                    entries_cnt=len(getattr(dec, "grid_entries", []) or []),
                    cancels_cnt=len(getattr(dec, "grid_cancels", []) or []),
                    state_updates_keys=sorted(list((getattr(dec, "state_updates", {}) or {}).keys())),
                )
            )

            om.apply_decision(dec, feed, time.time())

            # Fill 적용 (tick.fills)
            fills = [FillEvent(**x) for x in (tick.get("fills", []) or [])]
            # fill engine은 stub open_orders를 직접 mutate
            open_orders_after = stub.get_open_orders()
            self.fill_engine.apply_fills(open_orders=open_orders_after, positions=positions, fills=fills)

            # qty<=0인 오더 제거
            stub.seed_open_orders([o for o in open_orders_after if float(o.get("qty", 0.0) or 0.0) > 1e-12])

            # state_updates 반영
            for k, v in (getattr(dec, "state_updates", {}) or {}).items():
                try:
                    setattr(state, k, v)
                except Exception:
                    pass

        return L3RunOutput(
            calls=calls,
            stub_calls=list(stub.calls),
            final_state=state,
            final_positions=positions,
            final_open_orders=stub.get_open_orders(),
        )

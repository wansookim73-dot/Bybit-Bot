from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set


_ALLOWED_SOURCES: Set[str] = {
    "grid",
    "tp",
    "sl",
    "hedge",
    "escape",
    "recenter",
    "manual",
    "external",
    "unknown",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v)
    except Exception:
        return ""


def _as_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    try:
        s = str(v).strip().lower()
    except Exception:
        return None
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return None


def _pick_first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d.get(k)
    return None


@dataclass(frozen=True)
class IntentRecord:
    order_id: str
    order_link_id: str
    source: str
    authoritative: bool
    updated_ms: int


class OrderIntentRegistry:
    """
    orderId / orderLinkId -> source(grid/tp/sl/hedge/escape/recenter/manual/external/unknown)
    - 체결 로그의 'source' 보강을 위해 사용.
    - 체결 판단은 절대 하지 않는다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_order_id: Dict[str, IntentRecord] = {}
        self._by_link_id: Dict[str, IntentRecord] = {}

    @staticmethod
    def normalize_source(src: str) -> str:
        s = _as_str(src).strip().lower()
        return s if s in _ALLOWED_SOURCES else "unknown"

    def register(
        self,
        *,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
        source: str,
        authoritative: bool = True,
    ) -> None:
        """
        authoritative=True:
          - OrderManager(봇 발행)에서 "정답"으로 등록할 때 사용
        authoritative=False:
          - order stream 기반 heuristic(infer)로 추정 등록할 때 사용
        """
        src = self.normalize_source(source)
        oid = _as_str(order_id).strip()
        lid = _as_str(order_link_id).strip()

        if not oid and not lid:
            return

        rec = IntentRecord(
            order_id=oid,
            order_link_id=lid,
            source=src,
            authoritative=bool(authoritative),
            updated_ms=_now_ms(),
        )

        with self._lock:
            if oid:
                prev = self._by_order_id.get(oid)
                self._by_order_id[oid] = self._merge(prev, rec)
            if lid:
                prev2 = self._by_link_id.get(lid)
                self._by_link_id[lid] = self._merge(prev2, rec)

            if oid and lid:
                a = self._by_order_id.get(oid)
                b = self._by_link_id.get(lid)
                if a and b:
                    merged = self._merge(a, b)
                    self._by_order_id[oid] = merged
                    self._by_link_id[lid] = merged

    @staticmethod
    def _tier(src: str) -> int:
        # unknown(0) < external/manual(1) < bot-sources(2)
        if src == "unknown":
            return 0
        if src in ("external", "manual"):
            return 1
        return 2

    @classmethod
    def _merge(cls, old: Optional[IntentRecord], new: IntentRecord) -> IntentRecord:
        if old is None:
            return new

        old_t = cls._tier(old.source)
        new_t = cls._tier(new.source)

        # ✅ authoritative 우선
        if new.authoritative and (not old.authoritative):
            pick_src = new.source
            pick_auth = True
        elif old.authoritative and (not new.authoritative):
            pick_src = old.source
            pick_auth = True
        else:
            # 둘 다 authoritative 또는 둘 다 non-authoritative: tier 우선, 동률이면 old 유지(안정성)
            if new_t > old_t:
                pick_src = new.source
                pick_auth = new.authoritative
            else:
                pick_src = old.source
                pick_auth = old.authoritative

        return IntentRecord(
            order_id=new.order_id or old.order_id,
            order_link_id=new.order_link_id or old.order_link_id,
            source=pick_src,
            authoritative=pick_auth,
            updated_ms=max(int(old.updated_ms), int(new.updated_ms)),
        )

    def ingest_order_message(self, msg: Any) -> int:
        if not isinstance(msg, dict):
            return 0
        data = msg.get("data")
        if data is None:
            return 0

        if isinstance(data, dict):
            recs = [data]
        elif isinstance(data, list):
            recs = [x for x in data if isinstance(x, dict)]
        else:
            return 0

        n = 0
        for rec in recs:
            if self.ingest_order_record(rec):
                n += 1
        return n

    def ingest_order_record(self, rec: Dict[str, Any]) -> bool:
        if not isinstance(rec, dict):
            return False

        info = rec.get("info")
        if isinstance(info, dict):
            merged = dict(info)
            merged.update(rec)
            rec = merged

        order_id = _as_str(_pick_first(rec, "orderId", "order_id", "id")).strip()
        order_link_id = _as_str(
            _pick_first(rec, "orderLinkId", "orderLinkID", "order_link_id", "clientOrderId", "tag")
        ).strip()
        reduce_only = _as_bool(_pick_first(rec, "reduceOnly", "reduce_only", "isReduceOnly", "is_reduce_only"))
        tag = _as_str(_pick_first(rec, "tag", "clientOrderId", "orderLinkId")).strip() or order_link_id

        src = self.infer_source(tag, reduce_only=reduce_only)
        if src is None:
            src = "external" if (not tag) else "unknown"

        # ✅ heuristic 등록은 authoritative=False
        self.register(order_id=order_id or None, order_link_id=order_link_id or None, source=src, authoritative=False)
        return bool(order_id or order_link_id)

    def lookup(self, *, order_id: Optional[str] = None, order_link_id: Optional[str] = None) -> Optional[str]:
        oid = _as_str(order_id).strip()
        lid = _as_str(order_link_id).strip()

        with self._lock:
            if oid:
                rec = self._by_order_id.get(oid)
                if rec:
                    return rec.source
            if lid:
                rec2 = self._by_link_id.get(lid)
                if rec2:
                    return rec2.source
        return None

    def wait_for_source(
        self,
        order_id: Optional[str],
        order_link_id: Optional[str],
        *,
        total_wait_ms: int = 120,
        step_ms: int = 40,
    ) -> Optional[str]:
        total_wait_ms = max(0, int(total_wait_ms))
        step_ms = max(1, int(step_ms))
        deadline = _now_ms() + total_wait_ms

        s = self.lookup(order_id=order_id, order_link_id=order_link_id)
        if s is not None:
            return s

        while _now_ms() < deadline:
            time.sleep(step_ms / 1000.0)
            s = self.lookup(order_id=order_id, order_link_id=order_link_id)
            if s is not None:
                return s

        return None

    @staticmethod
    def infer_source(tag: Optional[str], *, reduce_only: Optional[bool] = None) -> Optional[str]:
        """
        ⚠️ 순서 중요:
        - HEDGE가 ESCAPE 문자열을 포함할 수 있으므로 HEDGE를 ESCAPE보다 먼저 판정
        """
        t = _as_str(tag).strip()
        if not t:
            return None
        u = t.upper()

        if "RECENTER" in u or "RESET_WAVE" in u or "RE-CENTER" in u:
            return "recenter"
        if "MANUAL" in u:
            return "manual"
        if "HEDGE" in u:
            return "hedge"
        if "ESCAPE" in u or "FULL_EXIT" in u:
            return "escape"

        if "STOPLOSS" in u or "STOP_LOSS" in u or "_SL_" in u or u.startswith("SL_") or u.endswith("_SL"):
            return "sl"
        if "_TP_" in u or u.startswith("TP_") or u.endswith("_TP"):
            return "tp"

        if "_GRID_" in u:
            if reduce_only is True:
                return "tp"
            if reduce_only is False:
                return "grid"
            return "unknown"

        if u.startswith("STARTUP") or u.startswith("DCA") or u.startswith("REENTRY"):
            if reduce_only is True:
                return "tp"
            if reduce_only is False:
                return "grid"
            return "unknown"

        if u.startswith("W") and "_GRID" in u:
            if reduce_only is True:
                return "tp"
            if reduce_only is False:
                return "grid"
            return "unknown"

        return None


# ✅ 글로벌 싱글톤 (OrderManager / WebSocketService / ExecutionJournal 공유)
order_intent_registry = OrderIntentRegistry()

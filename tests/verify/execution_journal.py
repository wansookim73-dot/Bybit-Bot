# tests/verify/execution_journal.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class JournalEvent:
    ts: int
    kind: str  # "ORDER" | "CANCEL" | "FILL"
    payload: Dict[str, Any]


class ExecutionJournal:
    def __init__(self) -> None:
        self._events: List[JournalEvent] = []

    def add(self, ts: int, kind: str, payload: Dict[str, Any]) -> None:
        self._events.append(JournalEvent(ts=int(ts), kind=str(kind), payload=dict(payload)))

    def tail(self, n: int = 10) -> List[Dict[str, Any]]:
        n = max(0, int(n))
        out: List[Dict[str, Any]] = []
        for ev in self._events[-n:]:
            out.append({"ts": ev.ts, "kind": ev.kind, "payload": ev.payload})
        return out

    def all(self) -> List[Dict[str, Any]]:
        return self.tail(10_000_000)

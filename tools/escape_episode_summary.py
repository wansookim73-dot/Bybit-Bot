from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class EscapeEpisode:
    start_ts: float
    end_ts: Optional[float]
    reason: str
    pnl_start: float
    pnl_end: Optional[float]
    pnl_min: float
    atr_ratio_start: float
    atr_ratio_end: Optional[float]
    exposure_start: float
    exposure_end: Optional[float]
    duration_sec: Optional[float]


def parse_events(path: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 각 줄에 JSON이 섞여 있을 수 있으니, { 로 시작하는 부분만 파싱 시도
            idx = line.find("{")
            if idx == -1:
                continue
            payload = line[idx:]
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if "event" not in obj:
                continue
            events.append(obj)
    return events


def build_escape_episodes(events: List[Dict[str, Any]]) -> List[EscapeEpisode]:
    episodes: List[EscapeEpisode] = []
    current: Optional[EscapeEpisode] = None

    for ev in events:
        ev_type = ev.get("event")
        ts = float(ev.get("ts", 0.0) or 0.0)
        extra = ev.get("extra") or {}

        if ev_type == "ESCAPE_TRIGGER":
            # 이전 에피소드가 닫히지 않았으면 일단 버리고 새로 시작
            reason = str(extra.get("reason", "UNKNOWN"))
            pnl = float(extra.get("pnl_total_pct", 0.0) or 0.0)
            atr_ratio = float(extra.get("atr_ratio", 0.0) or 0.0)
            exposure = float(extra.get("exposure_ratio", 0.0) or 0.0)
            current = EscapeEpisode(
                start_ts=ts,
                end_ts=None,
                reason=reason,
                pnl_start=pnl,
                pnl_end=None,
                pnl_min=pnl,
                atr_ratio_start=atr_ratio,
                atr_ratio_end=None,
                exposure_start=exposure,
                exposure_end=None,
                duration_sec=None,
            )

        elif ev_type == "ESCAPE_CLEAR" and current is not None:
            pnl = float(extra.get("pnl_total_pct", 0.0) or 0.0)
            atr_ratio = float(extra.get("atr_ratio", 0.0) or 0.0)
            exposure = float(extra.get("exposure_ratio", 0.0) or 0.0)
            duration = float(extra.get("duration_sec", 0.0) or 0.0)

            current.end_ts = ts
            current.pnl_end = pnl
            # pnl_min 은 ESCAPE 구간 동안 TickSummary로 더 보강할 수 있지만,
            # 일단 CLEAR 시점 기준으로만 둔다.
            current.atr_ratio_end = atr_ratio
            current.exposure_end = exposure
            current.duration_sec = duration

            episodes.append(current)
            current = None

        # (선택) ESCAPE 유지 중 최저 PnL 등을 반영하려면
        # TICK_SUMMARY 이벤트도 함께 분석해서 current.pnl_min 갱신 가능.

    return episodes


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python tools/escape_episode_summary.py <log_file>")
        return 1

    log_path = sys.argv[1]
    events = parse_events(log_path)
    if not events:
        print("no events found in log file")
        return 0

    episodes = build_escape_episodes(events)
    if not episodes:
        print("no ESCAPE episodes found")
        return 0

    print(f"ESCAPE episodes: {len(episodes)}")
    print("index\treason\tpnl_start\tpnl_end\tpnl_min\tdur_sec\tatr_start\tatr_end\texp_start\texp_end")
    for idx, ep in enumerate(episodes, start=1):
        print(
            f"{idx}\t{ep.reason}\t"
            f"{ep.pnl_start:.4f}\t{(ep.pnl_end or 0.0):.4f}\t"
            f"{ep.pnl_min:.4f}\t{(ep.duration_sec or 0.0):.1f}\t"
            f"{ep.atr_ratio_start:.2f}\t{(ep.atr_ratio_end or 0.0):.2f}\t"
            f"{ep.exposure_start:.2f}\t{(ep.exposure_end or 0.0):.2f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

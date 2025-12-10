import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def parse_escape_events(log_path: Path) -> List[Dict[str, Any]]:
    """
    로그 파일에서 'escape_events:' 가 포함된 라인을 찾아
    JSON payload 를 파싱해서 리스트로 반환한다.
    """
    events: List[Dict[str, Any]] = []

    if not log_path.exists():
        print(f"[WARN] log file not found: {log_path}")
        return events

    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "escape_events:" not in line:
                continue
            try:
                # 'escape_events:' 이후 부분을 잘라서 JSON 으로 파싱
                payload = line.split("escape_events:", 1)[1].strip()
                data = json.loads(payload)
                events.append(data)
            except Exception as e:
                # 파싱 실패는 그냥 스킵 (로그 포맷이 살짝 다른 경우 대비)
                print(f"[WARN] failed to parse line: {e!r}")
                continue
    return events


def summarize_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    escape_events 리스트를 event 타입별로 집계하고,
    pnl_total_pct / atr_ratio / exposure_ratio / duration_sec 등을 요약한다.
    """
    summary: Dict[str, Any] = {}
    by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for ev in events:
        ev_type = ev.get("event", "UNKNOWN")
        by_type[ev_type].append(ev)

    def _safe_float(x, default=0.0):
        try:
            return float(x)
        except Exception:
            return default

    for ev_type, evs in by_type.items():
        count = len(evs)
        pnl_list = []
        atr_list = []
        exp_list = []
        dur_list = []

        for ev in evs:
            extra = ev.get("extra", {}) or {}
            pnl_list.append(_safe_float(extra.get("pnl_total_pct")))
            atr_list.append(_safe_float(extra.get("atr_ratio")))
            exp_list.append(_safe_float(extra.get("exposure_ratio")))
            dur_list.append(_safe_float(extra.get("duration_sec")))

        def _stats(xs: List[float]):
            xs = [x for x in xs if x is not None]
            if not xs:
                return {"min": None, "max": None, "avg": None}
            return {
                "min": min(xs),
                "max": max(xs),
                "avg": sum(xs) / len(xs),
            }

        summary[ev_type] = {
            "count": count,
            "pnl_total_pct": _stats(pnl_list),
            "atr_ratio": _stats(atr_list),
            "exposure_ratio": _stats(exp_list),
            "duration_sec": _stats(dur_list),
        }

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Escape/Hedge 이벤트 리포트 생성기 (escape_events 로그 기반)."
    )
    parser.add_argument(
        "--log",
        type=str,
        default="bot_output.log",
        help="분석할 로그 파일 경로 (기본: bot_output.log)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="요약 뿐 아니라 raw 이벤트도 함께 출력",
    )

    args = parser.parse_args()

    log_path = Path(args.log)
    print(f"[INFO] Loading escape_events from: {log_path}")

    events = parse_escape_events(log_path)
    if not events:
        print("[INFO] No escape_events found in log.")
        return

    print(f"[INFO] Parsed {len(events)} escape_events")

    summary = summarize_events(events)

    print("\n========== ESCAPE EVENT SUMMARY ==========")
    for ev_type, info in summary.items():
        print(f"\n[EVENT] {ev_type}")
        print(f"  count           : {info['count']}")
        pnl = info["pnl_total_pct"]
        atr = info["atr_ratio"]
        exp = info["exposure_ratio"]
        dur = info["duration_sec"]

        print(f"  pnl_total_pct   : min={pnl['min']}  max={pnl['max']}  avg={pnl['avg']}")
        print(f"  atr_ratio       : min={atr['min']}  max={atr['max']}  avg={atr['avg']}")
        print(f"  exposure_ratio  : min={exp['min']}  max={exp['max']}  avg={exp['avg']}")
        print(f"  duration_sec    : min={dur['min']}  max={dur['max']}  avg={dur['avg']}")

    if args.raw:
        print("\n========== RAW EVENTS ==========")
        for ev in events:
            print(ev)


if __name__ == "__main__":
    main()

import argparse
import sys
from pathlib import Path
from pprint import pprint

# 프로젝트 루트(/home/ubuntu/mexc_bot)를 sys.path 에 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.escape_event_report import parse_escape_events, summarize_events


def main():
    parser = argparse.ArgumentParser(
        description="Escape/Hedge 풀 리포트 (요약 + 최근 이벤트 타임라인)"
    )
    parser.add_argument(
        "--log",
        type=str,
        default="bot_output.log",
        help="분석할 로그 파일 경로 (기본: bot_output.log)",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=30,
        help="타임라인에 표시할 최근 이벤트 개수 (기본: 30)",
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

    print("\n========== ESCAPE EVENT SUMMARY (by event type) ==========")
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

    # 타임라인 (최근 N개)
    tail_n = max(args.tail, 1)
    tail_events = events[-tail_n:]

    print(f"\n========== ESCAPE EVENT TIMELINE (last {len(tail_events)} events) ==========")
    for ev in tail_events:
        ts = ev.get("ts")
        ev_type = ev.get("event", "UNKNOWN")
        extra = ev.get("extra", {}) or {}
        pnl = extra.get("pnl_total_pct")
        atr = extra.get("atr_ratio")
        exp = extra.get("exposure_ratio")
        dur = extra.get("duration_sec")

        print(f"\n[ts={ts}] event={ev_type}")
        print(f"  pnl_total_pct  = {pnl}")
        print(f"  atr_ratio      = {atr}")
        print(f"  exposure_ratio = {exp}")
        print(f"  duration_sec   = {dur}")
        # pprint(extra)  # 필요하면 주석 해제

if __name__ == "__main__":
    main()

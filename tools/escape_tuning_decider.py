import argparse
import sys
from pathlib import Path

# 프로젝트 루트(/home/ubuntu/mexc_bot)를 sys.path 에 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.escape_event_report import parse_escape_events, summarize_events


def main():
    parser = argparse.ArgumentParser(
        description="Escape/FULL_EXIT/Hedge 튜닝 보조 리포트 (자동 변경 X, 힌트만 출력)"
    )
    parser.add_argument(
        "--log",
        type=str,
        default="bot_output.log",
        help="분석할 로그 파일 경로 (기본: bot_output.log)",
    )
    args = parser.parse_args()

    log_path = Path(args.log)
    print(f"[INFO] Loading escape_events from: {log_path}")

    events = parse_escape_events(log_path)
    if not events:
        print("[INFO] No escape_events found in log.")
        print(" - ESCAPE/FULL_EXIT/HEDGE가 아직 한 번도 발생하지 않았을 수 있습니다.")
        return

    print(f"[INFO] Parsed {len(events)} escape_events")

    summary = summarize_events(events)

    print("\n========== ESCAPE TUNING ASSISTANT ==========")

    # 1) ESCAPE_ON / ESCAPE_CLEAR / FULL_EXIT / HEDGE_* 타입이 있는지 확인
    for key in ("ESCAPE_ON", "ESCAPE_CLEAR", "FULL_EXIT", "HEDGE_ENTER", "HEDGE_EXIT"):
        if key in summary:
            info = summary[key]
            print(f"\n[EVENT TYPE] {key}")
            print(f"  count = {info['count']}")
            pnl = info["pnl_total_pct"]
            exp = info["exposure_ratio"]
            dur = info["duration_sec"]

            print(f"  pnl_total_pct avg = {pnl['avg']}, min = {pnl['min']}, max = {pnl['max']}")
            print(f"  exposure_ratio avg = {exp['avg']}, min = {exp['min']}, max = {exp['max']}")
            print(f"  duration_sec   avg = {dur['avg']}, min = {dur['min']}, max = {dur['max']}")

            # 아주 단순한 '힌트' 로직 (실제 파라미터 변경은 하지 않음)
            if key == "ESCAPE_ON":
                if pnl["avg"] is not None and pnl["avg"] > 0:
                    print("  -> 평균적으로 ESCAPE_ON 시점의 PnL이 플러스입니다.")
                    print("     ESCAPE_ON 조건이 너무 이른 것일 수 있습니다. (조금 완화 검토)")
                elif pnl["avg"] is not None and pnl["avg"] < 0:
                    print("  -> ESCAPE_ON 시점의 평균 PnL이 마이너스입니다.")
                    print("     손실 방어용 ESCAPE_ON 이 제대로 동작하는 것으로 보입니다.")
            if key == "FULL_EXIT":
                if pnl["avg"] is not None and pnl["avg"] < 0:
                    print("  -> FULL_EXIT 시점에 평균적으로 손실이 크다면,")
                    print("     FULL_EXIT 트리거를 조금 더 이른 구간으로 옮기는 것도 검토해볼 수 있습니다.")
                elif pnl["avg"] is not None and pnl["avg"] > 0:
                    print("  -> FULL_EXIT 시점이 너무 늦을 수 있습니다.")
                    print("     이익을 더 오래 가져가되, trailing 성격을 강화하는 방향도 고려해볼 수 있습니다.")
            if key.startswith("HEDGE_"):
                if exp["avg"] is not None and exp["avg"] > 1.0:
                    print("  -> 헷지 시점의 노출(exposure_ratio)이 1.0 이상입니다.")
                    print("     HEDGE_NOTIONAL / 헷지 비율을 다소 줄이는 것도 검토 가능.")
                elif exp["avg"] is not None and exp["avg"] < 0.3:
                    print("  -> 헷지가 아주 작은 노출에서도 자주 발생하는 편입니다.")
                    print("     HEDGE_NOTIONAL 기준을 조금 올려도 될 수 있습니다.")

    print("\n[NOTE]")
    print("  - 위 내용은 '로그 통계에 기반한 힌트'일 뿐, 실제 파라미터는 직접 수정해야 합니다.")
    print("  - ESCAPE_ON_PNL_PCT / FULL_EXIT_PNL_PCT / HEDGE_NOTIONAL 등은")
    print("    B 세션(전략 설계/튜닝)에서 이 리포트를 참고하여 조정하는 것이 안전합니다.")


if __name__ == "__main__":
    main()

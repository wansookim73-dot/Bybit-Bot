from pathlib import Path

path = Path("strategy/escape_logic.py")
if not path.exists():
    raise SystemExit("[ERR] strategy/escape_logic.py 를 찾지 못했습니다.")

text = path.read_text(encoding="utf-8")

marker = "def choose_hedge_action_v9_1"
idx = text.find(marker)

if idx == -1:
    raise SystemExit("[ERR] choose_hedge_action_v9_1 블록 시작을 찾지 못했습니다. (이미 수동 수정했을 수도 있음)")

# choose_hedge_action_v9_1 부터 파일 끝까지 날리고, 깨끗한 버전으로 다시 추가
head = text[:idx]

facade_block = '''
def choose_hedge_action_v9_1(
    *,
    side_seed: float,
    used_seed: float,
    grid_split_count: int,
    next_loss_line_touched: bool,
    pnl_base: float,
    pnl_hedge: float,
    total_notional: float,
    hedge_notional: float,
    target_combined_pct: float = 0.02,
    breakeven_eps_usdt: float = 0.5,
) -> dict:
    """v9.1 헷지 의사결정 헬퍼.

    반환되는 dict 의 예시:
    {
        "action": "NONE" | "OPEN_HEDGE" | "CLOSE_HEDGE" | "CLOSE_BOTH",
        "reason": "...",
        "remaining_seed": float,
        "trigger_seed": float,
    }
    """
    return hedge_decision_v9_1(
        side_seed=side_seed,
        used_seed=used_seed,
        grid_split_count=grid_split_count,
        next_loss_line_touched=next_loss_line_touched,
        pnl_base=pnl_base,
        pnl_hedge=pnl_hedge,
        total_notional=total_notional,
        hedge_notional=hedge_notional,
        target_combined_pct=target_combined_pct,
        breakeven_eps_usdt=breakeven_eps_usdt,
    )
'''

path.write_text(head + facade_block + "\n", encoding="utf-8")
print("[OK] choose_hedge_action_v9_1 블록을 깨끗한 버전으로 교체 완료.")

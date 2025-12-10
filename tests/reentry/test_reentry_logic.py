from reentry_logic import (
    decide_line_memory_reset,
    reset_seed_after_full_close,
)


def test_line_memory_reset_on_full_close():
    """
    [라인 메모리 리셋 케이스 1]
    포지션이 완전히 0이 되는 순간에는
    수익/손실과 관계없이 라인 메모리가 리셋되어야 한다.
    """
    decision = decide_line_memory_reset(
        pos_size_before=0.01,   # 직전 틱에는 포지션이 있었다가
        pos_size_now=0.0,       # 이번 틱에 완전히 0이 됨 (전량 익절/청산)
        pnl_before=-50.0,
        pnl_now=-10.0,
        had_non_negative_since_last_reset=False,
    )

    assert decision.should_reset is True
    assert decision.new_had_non_negative is False

    # 실제 라인 메모리 집합을 비우는 것은 호출자 책임.
    # 여기서는 간단히 시뮬레이션한다.
    used_lines = {-3, -4, 1}
    if decision.should_reset:
        used_lines.clear()
    assert used_lines == set()


def test_line_memory_reset_on_pnl_back_to_negative_after_profit():
    """
    [라인 메모리 리셋 케이스 2]
    부분 익절 이후 PnL 이 0 이상(수익 구간)을 한 번이라도 지나갔다가,
    다시 손실 구간(<0)으로 내려갈 때 라인 메모리가 리셋되어야 한다.

    시퀀스:
      - 틱 1: PnL -100 -> +50 (손실에서 수익으로 진입, 아직 리셋 X)
      - 틱 2: PnL +50 -> -10 (수익에서 손실로 재진입, 이 시점에 리셋)
    """

    # 1) 첫 번째 틱: 손실(-100) → 수익(+50) 구간으로 진입
    decision1 = decide_line_memory_reset(
        pos_size_before=0.02,
        pos_size_now=0.02,          # 포지션은 여전히 살아 있음
        pnl_before=-100.0,
        pnl_now=50.0,
        had_non_negative_since_last_reset=False,
    )
    # 아직 리셋은 아니고, "한 번이라도 0 이상 구간을 경험했다" 플래그만 True로 전환
    assert decision1.should_reset is False
    assert decision1.new_had_non_negative is True

    # 2) 두 번째 틱: 수익(+50) → 손실(-10) 로 내려가는 순간
    decision2 = decide_line_memory_reset(
        pos_size_before=0.02,
        pos_size_now=0.02,          # 여전히 포지션은 존재
        pnl_before=50.0,
        pnl_now=-10.0,
        had_non_negative_since_last_reset=decision1.new_had_non_negative,
    )
    # 이 순간에 라인 메모리가 리셋되어야 한다.
    assert decision2.should_reset is True
    assert decision2.new_had_non_negative is False


def test_line_memory_not_reset_when_always_loss():
    """
    PnL 이 계속 손실 구간(음수)에만 머무르고,
    한 번도 0 이상으로 올라간 적이 없다면
    라인 메모리가 리셋되면 안 된다.
    """
    decision = decide_line_memory_reset(
        pos_size_before=0.03,
        pos_size_now=0.03,
        pnl_before=-80.0,
        pnl_now=-30.0,
        had_non_negative_since_last_reset=False,
    )

    assert decision.should_reset is False
    assert decision.new_had_non_negative is False


def test_reset_seed_after_full_close_sets_k_and_seed_correctly():
    """
    전량 익절/청산 후 seed 회계가
      - k_dir = 0
      - used_seed_dir = 0
      - remain_seed_dir = allocated_seed_dir
    로 정확히 초기화되는지 확인.
    """
    allocated = 1_300.0
    unit_seed = 100.0  # 실제 계산에는 쓰이지 않지만, 명세상 인자 형태만 맞춰 준다.

    result = reset_seed_after_full_close(
        allocated_seed_dir=allocated,
        unit_seed=unit_seed,
    )

    assert result.new_k == 0
    assert result.new_used_seed == 0.0
    assert result.new_remain_seed == allocated

from dca_logic import decide_dca_entry


def test_dca_long_triggers_on_new_lower_line_with_negative_pnl_and_seed():
    """
    R2.3e / R3.1e / R3.2d

    Long 방향 손실 상태에서,
    위(0) → 아래(-1)로 새 라인 터치 + seed 충분 + 미사용 라인이면
    DCA 1분할이 허용되어야 한다.

    조건:
      - direction="long"
      - pnl < 0
      - current_line_index < prev_line_index (손실 방향으로 이동)
      - remain_seed >= unit_seed
      - 해당 라인이 아직 사용되지 않음
    """
    decision = decide_dca_entry(
        direction="long",
        pnl=-10.0,              # 손실
        prev_line_index=0,
        current_line_index=-1,  # 아래쪽으로 한 칸 (손실 방향)
        used_dca_lines=set(),   # 아직 아무 라인도 사용 안 함
        remain_seed=100.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is True
    assert decision.line_index == -1


def test_dca_long_blocked_when_not_enough_seed():
    """
    R2.3e

    나머지 조건이 모두 맞더라도,
    remain_seed < unit_seed 이면 DCA를 하면 안 된다.
    (last-chunk 규칙: remain_seed_dir ≥ unit_seed_dir 일 때만 추가 진입 허용)
    """
    decision = decide_dca_entry(
        direction="long",
        pnl=-10.0,
        prev_line_index=0,
        current_line_index=-1,
        used_dca_lines=set(),
        remain_seed=5.0,   # unit_seed(10)보다 작음
        unit_seed=10.0,
    )

    assert decision.should_enter is False
    assert decision.line_index is None


def test_dca_long_triggers_when_remain_seed_equals_unit_seed():
    """
    R2.3e (경계값)

    remain_seed == unit_seed 인 경우는
    remain_seed_dir ≥ unit_seed_dir 조건을 만족하므로 DCA 허용되어야 한다.
    """
    decision = decide_dca_entry(
        direction="long",
        pnl=-5.0,
        prev_line_index=0,
        current_line_index=-1,
        used_dca_lines=set(),
        remain_seed=10.0,  # unit_seed 와 정확히 동일
        unit_seed=10.0,
    )

    assert decision.should_enter is True
    assert decision.line_index == -1


def test_dca_long_blocked_when_line_already_used():
    """
    R3.2d (라인 메모리 조건)

    같은 라인에서 이미 한 번 DCA/Start-up/Refill 이 있었으면,
    라인 메모리(LineMemory) 때문에 재진입이 막혀야 한다.

    조건은 모두 만족하지만 used_dca_lines 에 포함된 라인이면 should_enter=False.
    """
    decision = decide_dca_entry(
        direction="long",
        pnl=-10.0,
        prev_line_index=0,
        current_line_index=-1,
        used_dca_lines={-1},  # -1 라인은 이미 사용됨
        remain_seed=100.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is False
    assert decision.line_index is None


def test_dca_long_blocked_when_price_moves_up_not_down():
    """
    R3.1e / R3.2d (손실 방향 라인 터치 조건)

    Long 방향 DCA 는 '아래 라인'으로 이동해야만 가능하다.
    손실 상태(pnl<0)이고 seed 가 충분해도,
    위로 이동(예: -1 → 0) 한 경우에는 DCA 가 발생하면 안 된다.
    """
    decision = decide_dca_entry(
        direction="long",
        pnl=-15.0,              # 손실
        prev_line_index=-1,
        current_line_index=0,   # 위로 이동 (수익 방향)
        used_dca_lines=set(),
        remain_seed=100.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is False
    assert decision.line_index is None


def test_dca_short_triggers_on_new_higher_line_with_negative_pnl_and_seed():
    """
    R2.3e / R3.1e / R3.2d

    Short 방향 손실 상태에서,
    아래(0) → 위(+1)로 새 라인 터치 + seed 충분 + 미사용 라인이면
    DCA 1분할이 허용되어야 한다.

    조건:
      - direction="short"
      - pnl < 0
      - current_line_index > prev_line_index (손실 방향으로 이동)
      - remain_seed >= unit_seed
      - 해당 라인이 아직 사용되지 않음
    """
    decision = decide_dca_entry(
        direction="short",
        pnl=-20.0,              # 손실
        prev_line_index=0,
        current_line_index=1,   # 위쪽으로 한 칸 (손실 방향)
        used_dca_lines=set(),
        remain_seed=50.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is True
    assert decision.line_index == 1


def test_dca_short_blocked_when_price_moves_down_not_up():
    """
    R3.1e

    Short 방향 DCA 는 '위 라인'으로 이동해야만 가능하다.
    손실 상태이고 seed 가 충분해도,
    아래로 이동(예: 1 → 0) 한 경우에는 DCA 가 발생하면 안 된다.
    """
    decision = decide_dca_entry(
        direction="short",
        pnl=-8.0,               # 손실
        prev_line_index=1,
        current_line_index=0,   # 아래로 이동 (수익 방향)
        used_dca_lines=set(),
        remain_seed=50.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is False
    assert decision.line_index is None


def test_dca_not_trigger_when_pnl_positive_even_if_move_in_loss_direction():
    """
    R3.2d (손실 상태 조건)

    라인 이동이 손실 방향이라도,
    PnL 이 이미 플러스이면 DCA 를 하면 안 된다.
    (v10.1: '손실 상태(pnl<0)' 에서만 DCA 허용)
    """
    decision = decide_dca_entry(
        direction="long",
        pnl=5.0,                # 수익 상태
        prev_line_index=0,
        current_line_index=-1,  # 아래로 내려가도 (손실 방향 이동)
        used_dca_lines=set(),
        remain_seed=100.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is False
    assert decision.line_index is None


def test_dca_not_trigger_when_pnl_zero_even_if_move_in_loss_direction():
    """
    R3.2d (pnl<0 엄격 조건)

    PnL 이 정확히 0 인 경우도 '손실 상태'로 간주하지 않으므로,
    손실 방향 라인 터치 + seed 충분 + 미사용 라인이라도
    DCA 가 발생하면 안 된다.
    """
    decision = decide_dca_entry(
        direction="short",
        pnl=0.0,                # 손익 0
        prev_line_index=0,
        current_line_index=1,   # 위로 이동 (short 손실 방향)
        used_dca_lines=set(),
        remain_seed=100.0,
        unit_seed=10.0,
    )

    assert decision.should_enter is False
    assert decision.line_index is None

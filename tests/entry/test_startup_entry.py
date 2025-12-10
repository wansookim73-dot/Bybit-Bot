from wave_init import init_wave
from entry_logic import decide_startup_entry


def test_startup_both_directions_when_flat_and_overlap():
    """
    R3.2a / R3.2b / R3.2c / R7.1d

    Wave 시작 직후, 계정이 완전 flat 이고 현재가가 Overlap Zone(-7~+7) 안에 있을 때:

    - Long/Short 양 방향 모두 Start-up Entry 를 허용해야 한다.
    - 여기서는 P_center 를 기준으로 Line 0 이 Overlap Zone 안에 해당하는 예시를 사용한다.
    """
    total_balance = 1000.0
    current_price = 30000.0
    atr_4h = 200.0  # P_gap = max(200*0.15, 100) = 100

    # Wave Start: seed / P_center / P_gap 등이 초기화된 상태를 만든다.
    state = init_wave(total_balance, current_price, atr_4h)

    decision = decide_startup_entry(
        state=state,
        pos_long_qty=0.0,
        pos_short_qty=0.0,
        current_line_index=0,  # Line 0 = P_center, Overlap Zone 안
    )

    assert decision.enter_long is True
    assert decision.enter_short is True


def test_startup_blocked_when_outside_overlap():
    """
    R3.1e / R3.2a

    Overlap Zone 밖(Line index = 8)이면,
    seed 가 충분하더라도 Start-up Entry 는 발생하면 안 된다.

    - Line 8 은 Short 운용 라인 범위(-7~+12) 안에는 들어가지만,
      Overlap Zone(-7~+7) 밖이므로 Start-up Entry 금지 조건에 해당한다.
    """
    total_balance = 1000.0
    current_price = 30000.0
    atr_4h = 200.0

    state = init_wave(total_balance, current_price, atr_4h)

    # Line 8: Short 운용 라인 범위에는 포함되지만 Overlap Zone(-7~+7) 밖
    decision = decide_startup_entry(
        state=state,
        pos_long_qty=0.0,
        pos_short_qty=0.0,
        current_line_index=8,
    )

    assert decision.enter_long is False
    assert decision.enter_short is False


def test_startup_respects_remaining_seed_per_direction():
    """
    R2.3e (last-chunk 규칙, remain_seed_dir ≥ unit_seed_dir) + 방향 독립성

    remain_seed_dir ≥ unit_seed_dir 일 때만 새 1분할(Start-up)을 허용하는 조건이
    방향별로 독립적으로 적용되는지 확인한다.

    시나리오:
    - Long 방향: k_long = 13 (이미 13분할 사용했다고 가정)
      → last-chunk 규칙 + k_dir 상한에 의해 더 이상 신규 1분할 진입 불가
    - Short 방향: k_short = 0 (seed 여유 충분)
      → Overlap Zone(Line 0) 에서 Start-up Entry 허용
    """
    total_balance = 1000.0
    current_price = 30000.0
    atr_4h = 200.0

    state = init_wave(total_balance, current_price, atr_4h)

    # Long 방향은 이미 13 Step 을 사용했다고 가정 (k_dir 상한 도달)
    state.seed.k_long = 13

    decision = decide_startup_entry(
        state=state,
        pos_long_qty=0.0,
        pos_short_qty=0.0,
        current_line_index=0,  # Overlap Zone 안
    )

    # Long: k_long 이 상한(13)에 도달했으므로 Start-up 불가
    assert decision.enter_long is False
    # Short: remain_seed_short ≥ unit_seed_short 이고 k_short < 13 이므로 Start-up 허용
    assert decision.enter_short is True


def test_startup_blocked_when_k_dir_reaches_max_for_both_sides():
    """
    R2.3f (동시 보유 분할 상한 k_dir ≤ 13)

    방향별 동시 보유 분할 수 k_dir 이 상한(13)에 도달한 경우,
    Overlap Zone 안에서 flat 이더라도 새 Start-up Entry 는 양 방향 모두 막혀야 한다.

    - Long: k_long = 13
    - Short: k_short = 13
    - pos_long_qty = pos_short_qty = 0 (완전 flat)
    - current_line_index = 0 (Overlap Zone 안)
    """
    total_balance = 1000.0
    current_price = 30000.0
    atr_4h = 200.0

    state = init_wave(total_balance, current_price, atr_4h)

    # 양 방향 모두 이미 13개의 분할을 사용했다고 가정
    state.seed.k_long = 13
    state.seed.k_short = 13

    decision = decide_startup_entry(
        state=state,
        pos_long_qty=0.0,
        pos_short_qty=0.0,
        current_line_index=0,  # Overlap Zone 안
    )

    # k_dir 상한에 걸려 양 방향 Start-up 모두 금지
    assert decision.enter_long is False
    assert decision.enter_short is False

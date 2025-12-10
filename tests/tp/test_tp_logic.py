from tp_logic import compute_profit_line_index, decide_tp_step


def test_profit_line_index_long_and_short_basic():
    """
    v10.1 명세의 profit_line_index 공식이
    Long / Short 모두 올바르게 동작하는지 확인.
    """
    p_gap = 100.0
    avg_entry = 30_000.0

    # Long: 현재가가 평단 위로 250달러 → 2.5 → floor 2
    price_long_1 = 30_250.0
    idx_long_1 = compute_profit_line_index(
        direction="long",
        avg_entry=avg_entry,
        current_price=price_long_1,
        p_gap=p_gap,
    )
    assert idx_long_1 == 2

    # Long: 평단 + 3.2 * gap → 3
    price_long_2 = avg_entry + 3.2 * p_gap
    idx_long_2 = compute_profit_line_index(
        direction="long",
        avg_entry=avg_entry,
        current_price=price_long_2,
        p_gap=p_gap,
    )
    assert idx_long_2 == 3

    # Short: 현재가가 평단 아래로 250달러 → 2.5 → floor 2
    price_short_1 = 29_750.0  # 30,000 - 250
    idx_short_1 = compute_profit_line_index(
        direction="short",
        avg_entry=avg_entry,
        current_price=price_short_1,
        p_gap=p_gap,
    )
    assert idx_short_1 == 2

    # Short: 평단 - 3.7 * gap → 3
    price_short_2 = avg_entry - 3.7 * p_gap
    idx_short_2 = compute_profit_line_index(
        direction="short",
        avg_entry=avg_entry,
        current_price=price_short_2,
        p_gap=p_gap,
    )
    assert idx_short_2 == 3


def test_no_tp_before_profit_line_3():
    """
    profit_line_index가 0, 1, 2 구간에서는
    아무리 수익이어도 TP가 발생하면 안 된다.
    """
    allocated = 1_300.0
    unit = 100.0  # allocated_seed_dir / 13을 가정
    k_dir = 5
    used_tp_lines = set()

    for idx in [0, 1, 2]:
        decision = decide_tp_step(
            profit_line_index=idx,
            used_tp_lines=used_tp_lines,
            k_dir=k_dir,
            allocated_seed_dir=allocated,
            unit_seed=unit,
        )
        assert decision.should_tp is False
        assert decision.profit_line_index is None


def test_tp_triggers_at_line3_and_updates_seed_and_k():
    """
    profit_line_index == 3 이 되는 순간부터 TP가 시작되고,
    TP 한 번에 k_dir 감소 및 seed 회복이 정확히 계산되는지 확인.
    """
    allocated = 1_300.0
    unit = 100.0
    k_dir = 8  # 현재 8분할이 열려 있다고 가정
    used_tp_lines = set()  # 아직 어떤 수익 라인에서도 TP를 안 함

    decision = decide_tp_step(
        profit_line_index=3,
        used_tp_lines=used_tp_lines,
        k_dir=k_dir,
        allocated_seed_dir=allocated,
        unit_seed=unit,
    )

    assert decision.should_tp is True
    assert decision.profit_line_index == 3

    # k_dir는 1 감소해야 한다.
    assert decision.new_k == k_dir - 1

    # used_seed = new_k * unit_seed
    expected_used = (k_dir - 1) * unit
    expected_remain = allocated - expected_used

    assert decision.new_used_seed == expected_used
    assert decision.new_remain_seed == expected_remain


def test_tp_only_once_per_profit_line():
    """
    같은 profit_line_index(예: 3번 라인)에서는
    한 번만 TP가 가능해야 한다.
    """
    allocated = 1_300.0
    unit = 100.0
    k_dir = 5

    # 3번 라인에서는 이미 한 번 TP를 했다고 가정
    used_tp_lines = {3}

    decision = decide_tp_step(
        profit_line_index=3,
        used_tp_lines=used_tp_lines,
        k_dir=k_dir,
        allocated_seed_dir=allocated,
        unit_seed=unit,
    )

    assert decision.should_tp is False
    assert decision.profit_line_index is None
    assert decision.new_k is None
    assert decision.new_used_seed is None
    assert decision.new_remain_seed is None


def test_tp_can_fire_again_on_next_higher_profit_line():
    """
    3번 수익 라인에서 TP를 한 뒤에,
    수익이 더 나서 profit_line_index=4가 되면
    4번 라인에서는 다시 TP 1스텝이 가능해야 한다.
    """
    allocated = 1_300.0
    unit = 100.0
    k_dir = 5

    # 3번 라인은 이미 TP에 사용됨
    used_tp_lines = {3}

    decision = decide_tp_step(
        profit_line_index=4,
        used_tp_lines=used_tp_lines,
        k_dir=k_dir,
        allocated_seed_dir=allocated,
        unit_seed=unit,
    )

    assert decision.should_tp is True
    assert decision.profit_line_index == 4
    assert decision.new_k == k_dir - 1

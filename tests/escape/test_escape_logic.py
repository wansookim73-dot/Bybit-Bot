from escape_logic import (
    decide_escape_trigger_line,
    compute_escape_hedge_qty,
    is_wave_action_allowed,
    decide_escape_exit_action,
    ESCAPE_STATUS_NONE,
    ESCAPE_STATUS_PENDING,
    ESCAPE_STATUS_ACTIVE,
    ESCAPE_STATUS_DONE,
    ESCAPE_HEDGE_EXECUTION_MODE,
)


def test_escape_trigger_line_long_set_once():
    """
    [T-ES-01] Escape Trigger Line 설정 – Long 방향

    - PnL < 0 이고 remain_seed ≤ unit_seed 인 상태에서
      처음 라인 L_k 를 터치하면
        → Escape_Trigger_Line = L_{k-1}
    - 한 번 설정된 후에는 더 이상 갱신되지 않아야 한다.
    """
    # 아직 Trigger 가 없는 상태에서 첫 터치
    result1 = decide_escape_trigger_line(
        pos_pnl=-100.0,
        remain_seed_dir=80.0,
        unit_seed=100.0,
        current_trigger_line_index=None,
        touched_line_index=-5,
        direction="long",
    )
    assert result1.should_set_trigger is True
    assert result1.new_trigger_line_index == -6  # k-1

    # 이미 Trigger 가 설정된 뒤에는 아무리 다시 터치해도 갱신되지 않는다.
    result2 = decide_escape_trigger_line(
        pos_pnl=-200.0,
        remain_seed_dir=20.0,
        unit_seed=100.0,
        current_trigger_line_index=result1.new_trigger_line_index,
        touched_line_index=-7,
        direction="long",
    )
    assert result2.should_set_trigger is False
    assert result2.new_trigger_line_index == -6  # 그대로 유지


def test_escape_trigger_line_short_set_once():
    """
    [T-ES-01] Escape Trigger Line 설정 – Short 방향

    - PnL < 0 이고 remain_seed ≤ unit_seed 인 상태에서
      라인 L_k 를 터치하면
        → Escape_Trigger_Line = L_{k+1}
    """
    result1 = decide_escape_trigger_line(
        pos_pnl=-50.0,
        remain_seed_dir=10.0,
        unit_seed=50.0,
        current_trigger_line_index=None,
        touched_line_index=3,
        direction="short",
    )
    assert result1.should_set_trigger is True
    assert result1.new_trigger_line_index == 4  # k+1

    # 두 번째 호출에서는 이미 Trigger 가 있으므로 그대로 유지
    result2 = decide_escape_trigger_line(
        pos_pnl=-80.0,
        remain_seed_dir=5.0,
        unit_seed=50.0,
        current_trigger_line_index=result1.new_trigger_line_index,
        touched_line_index=5,
        direction="short",
    )
    assert result2.should_set_trigger is False
    assert result2.new_trigger_line_index == 4


def test_compute_escape_hedge_qty_respects_2x_cap_and_qmax():
    """
    [T-ES-02] Escape Hedge 수량 계산

    Q_hedge = min( 2 * |Q_main| , Q_max )
    """
    # 케이스 1: Q_max 가 충분히 큰 경우 → 2배수 캡이 그대로 적용
    q_main = 0.01
    q_max = 1.0
    q_hedge = compute_escape_hedge_qty(q_main, q_max)
    assert q_hedge == 0.02  # 2 * 0.01

    # 케이스 2: Q_max 가 더 작을 때 → Q_max 가 상한
    q_main = -0.01  # Short 본체도 절대값 기준으로 동일
    q_max = 0.015
    q_hedge = compute_escape_hedge_qty(q_main, q_max)
    assert q_hedge == 0.015

    # 실행 모드는 항상 Mode B 여야 한다.
    assert ESCAPE_HEDGE_EXECUTION_MODE == "B"


def test_is_wave_action_allowed_blocks_when_escape_active():
    """
    [T-ES-03] ESCAPE_ACTIVE 동안 웨이브 동결

    - ESCAPE_ACTIVE 인 방향 → Start-up / DCA / Refill / TP 금지
    - 그 외 상태(NONE / PENDING / DONE) → 허용
    """
    assert is_wave_action_allowed(ESCAPE_STATUS_ACTIVE) is False

    assert is_wave_action_allowed(ESCAPE_STATUS_NONE) is True
    assert is_wave_action_allowed(ESCAPE_STATUS_PENDING) is True
    assert is_wave_action_allowed(ESCAPE_STATUS_DONE) is True


def test_hedge_be_close_after_positive_then_zero():
    """
    [T-ES-04] 헷지 단독 BE 청산

    시퀀스:
      - 틱 1: 헷지 PnL 이 +로 올라간 상태 (플래그만 True)
      - 틱 2: 헷지 PnL 이 0 으로 복귀, +2% 조건은 아직 미충족
        → action = "HEDGE_BE_CLOSE"
    """
    n_exposure = 10_000.0

    # 틱 1: 헷지 PnL 이 + 영역으로 들어감
    decision1 = decide_escape_exit_action(
        pnl_main=-300.0,
        pnl_hedge=150.0,
        n_exposure=n_exposure,
        hedge_had_positive_before=False,
    )
    # 아직 +2% 에는 못 미친다고 가정 (총합 -150)
    assert decision1.action == "NONE"
    assert decision1.new_hedge_had_positive is True

    # 틱 2: 헷지 PnL 이 0 으로 복귀, 여전히 총합은 +2% 미만
    decision2 = decide_escape_exit_action(
        pnl_main=-100.0,
        pnl_hedge=0.0,
        n_exposure=n_exposure,
        hedge_had_positive_before=decision1.new_hedge_had_positive,
    )
    assert decision2.action == "HEDGE_BE_CLOSE"
    # BE 청산이 발생하면 플래그는 다시 False 로 초기화
    assert decision2.new_hedge_had_positive is False


def test_plus2_percent_exit_has_priority_over_be():
    """
    [T-ES-05] +2% 동시 탈출이 헷지 BE 청산보다 우선

    - PnL_total 이 +2% 임계값을 넘는 순간에는
      헷지 PnL 이 0 근처라도 "FULL_CLOSE_PLUS_2" 가 먼저 발생해야 한다.
    """
    n_exposure = 10_000.0  # Threshold = 200

    # 이전 틱까지 헷지 PnL 이 양수였다고 가정
    decision = decide_escape_exit_action(
        pnl_main=300.0,      # 본체 수익
        pnl_hedge=0.0,       # 헷지는 이제 0 근처
        n_exposure=n_exposure,
        hedge_had_positive_before=True,
    )

    # 총합 PnL = 300 ≥ 200 → +2% 동시 청산이 우선
    assert decision.action == "FULL_CLOSE_PLUS_2"
    # 플래그는 이후 Wave 종료 처리에서 무시되지만, 여기서는 True 유지만 확인
    assert decision.new_hedge_had_positive is True

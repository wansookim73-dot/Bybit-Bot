# Verify L2 Final - 3rd-party Review x3

## Review #1 (Safety / Blast Radius)
- 목표: 운영 봇/실계정에 절대 영향이 없도록 강제.
- 조치:
  - 테스트 코드에서 REQUEST_LIVE=0, LIVE_GATE=NO 강제 세팅 후 core.order_manager를 지연 import.
  - config.DRY_RUN=True 강제.
  - STATE_FILE_PATH / LOG_FILE_PATH를 data/verify_* 로 강제 분리(운영 파일과 충돌 방지).
  - Exchange는 실제 ExchangeAPI가 아니라 L2StubExchange로 완전 대체(네트워크 호출 0).

## Review #2 (Correctness / Invariants)
- 목표: OrderManager→Exchange 호출에서 깨지기 쉬운 불변조건을 잡기.
- 핵심 불변조건:
  - TP(reduce_only=True)는 position_idx(1/2)가 반드시 유효.
  - NORMAL 흐름에서는 MARKET 주문이 발생하지 않아야 함(메이커-only 전제).
  - cancel은 대상 order_id를 정확히 사용.
- 조치:
  - 주문 호출을 캡처/기록하여 price 기반으로 “내가 의도한 주문이 실제로 어떤 플래그로 나갔는지”를 검증.
  - 테스트가 내부 구현 변화(추가 유지보수 주문 등)에 덜 깨지도록,
    ‘정확한 호출 수’ 대신 ‘필수 호출 존재’ 중심으로 검증.

## Review #3 (Operability / Step-by-step)
- 목표: 네가 주말에도 로그 없이 바로 돌릴 수 있는 “원클릭 검증” 제공.
- 조치:
  - /tmp/gen_verify_l2.sh 로 파일 생성 자동화
  - compileall → pytest 단일 파일 실행 → 전체 verify 스크립트 run_verify_all.sh 확장(선택)

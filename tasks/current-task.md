# 현재 작업

## 목표
`graph_node_queue_service.enqueue()`의 enqueue race를 제거해서, 동일 `task_node_id`에 대한 중복 enqueue가 동시 호출에서도 예외가 아니라 idempotent success가 되게 한다.

## 작업 대상
- `app/services/graph_node_queue_service.py`
- 필요 시 매우 제한적으로 관련 테스트 파일

## 배경
현재 enqueue 경로는 existing row 조회 후 insert/update 형태라 TOCTOU race에 약하다.
동시 enqueue 시 duplicate row는 UNIQUE로 막히더라도 예외나 비결정적 동작이 생길 수 있다.
이 문제는 다중 orchestrator / 다중 프로세스 환경에서 queue 안정성을 직접 해친다.

## 요구사항
- enqueue는 동시 호출에서도 idempotent해야 한다.
- duplicate enqueue는 예외가 아니라 정상 처리여야 한다.
- 가능하면 transaction 또는 upsert 패턴으로 구현한다.
- 기존 queue 의미론(이미 queued / claimed / done 상태 처리)을 불필요하게 깨지 않는다.
- 변경 범위는 queue race 해결에 필요한 최소한으로 제한한다.

## 완료 기준
- 동시 enqueue race에 대한 취약한 read-then-insert 패턴이 제거된다.
- 코드상 duplicate enqueue가 정상 케이스로 취급된다.
- 가능한 경우 관련 테스트가 추가/수정된다.
- 변경 이유와 남은 edge case가 명확히 설명된다.

## 리뷰 중점
- transactional safety
- idempotency
- existing status semantics preservation
- SQLite 호환성

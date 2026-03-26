# AGENTS.md

## 프로젝트 목표
win-agent-runtime을 Windows-first, local-first, approval-safe multi-agent runtime으로 개선한다.

## 현재 단계
이 저장소는 "완전한 graph-first orchestrator 재설계"가 아니라, 작은 고효율 티켓을 반복 처리하는 단계다.
처음 3~5회 루프에서는 구조 전체를 갈아엎지 말고, 작은 범위 변경으로 안정성/운영성/예측 가능성을 높이는 데 집중한다.

## 우선순위
1. approval integrity를 약화시키지 말 것
2. startup side effect를 늘리지 말 것
3. queue / worker / graph 상태 드리프트를 줄일 것
4. UI/API 계약을 조용히 깨지 말 것
5. Windows 실행성과 로컬 재현성을 유지할 것

## 하드 규칙
- 변경 범위를 현재 티켓에 직접 필요한 파일로 제한한다.
- 관련 없는 리팩터링 금지.
- 보안 경계, approval binding, constrained execution 경로를 약화시키지 않는다.
- `data/`, `dist/`, `.venv`, `.codex-*`, `.git`, runtime artifact는 수정/의존/커밋 대상으로 삼지 않는다.
- raw repo zip / dev artifact에 기대지 않는다.
- summary 없는 pending run 상태를 깨뜨리지 않는 방향을 선호한다.
- graph-first로 가는 방향성과 충돌하는 임시 해킹을 넣지 않는다.

## 설계 원칙
- admission / planning / review / execution을 점진적으로 graph 중심 상태 모델로 모은다.
- queue 작업은 idempotent하고 race-safe해야 한다.
- worker는 승인된 execution job이 starvation 되지 않게 해야 한다.
- startup에서는 예측 불가능한 inline execution보다 reconcile/queue 우선 모델을 선호한다.
- durable state와 projection state를 혼동하지 않는다.

## 테스트/검증 원칙
- 가능한 경우 최소 단위의 코드 경로 검증을 우선한다.
- 테스트를 새로 못 쓰더라도, 수정한 코드 경로의 실패/성공 시나리오를 명시한다.
- DB schema 변경이 있다면 migration/호환성 영향을 분명히 적는다.
- API/UI 계약에 영향이 있으면 reviewer가 바로 볼 수 있게 요약한다.

## 보고 형식
항상 아래를 포함해라.
- files changed
- why this patch
- validation performed
- remaining risks
- follow-up suggestions (최대 3개)

## 첫 단계 운영 규칙
- Architect는 티켓을 1~3개 하위 작업으로 쪼갠다.
- Builder는 한 번에 하나의 하위 작업만 구현한다.
- Reviewer는 범위 초과, 상태 드리프트, 계약 파손, race/security regressions를 엄격히 본다.
- 첫 실전 루프에서는 무조건 한 티켓만 닫는다.

# MVP 협업 시작 가이드

상태: 문서 기준 확정 · Phase 1 구현 시작 가능
대상: API, Domain/DB, Worker, Codex/Git, Test 담당자

현재 저장소는 설계 문서 단계입니다. Phase 0의 문서 기준은 확정했고, 설정·마이그레이션·자동화 계약 시험은 Phase 1 이후의 구현 할 일입니다. 먼저 Phase 1 기반 코드를 한 변경 묶음으로 만들고, 그다음 기능별로 나눕니다.

## 1. 문서 읽는 순서

1. [MVP 의사결정 대장](decision-register.md): 확정된 구현 값과 변경 절차
2. [OpenAPI 3.1 계약](../openapi/auto-error-handler-mvp.v1.yaml): HTTP 계약
3. [오류 이벤트 JSON Schema](../schemas/external-error-event-v1.schema.json): Producer payload 계약
4. [DB·Worker·Codex 구현 계약](implementation-contracts.md): 모듈 간 경계
5. [MVP 구현 범위](mvp-scope.md): MVP에 포함·제외할 요소와 불변 조건
6. [MVP 실행 계획](mvp-execution-plan.md): Phase별 Todo·PR·테스트 순서
7. [세부 구현 계획](implementation-plan.md): 상세 구현 참고자료
8. [인터랙티브 서비스 구조](../walkthrough-mvp-service-architecture.html): 전체 비즈니스 흐름과 시각 자료

## 2. Source of Truth 우선순위

충돌할 때 다음 순서로 판단합니다.

1. `ACCEPTED` ADR와 [의사결정 대장](decision-register.md)
2. OpenAPI와 JSON Schema
3. Alembic migration과 자동화 contract test
4. 구현 계약과 서비스 아키텍처
5. 웹 가이드와 README 예시

낮은 우선순위 문서가 다르면 구현자가 해석해 고치지 않고 같은 PR에서 Source of Truth와 파생 문서를 함께 수정합니다.

## 3. Phase별 협업 Todo

### Phase 0 — 확정된 문서 기준

[의사결정 대장](decision-register.md)의 P0 선택은 모두 `ACCEPTED` 또는 MVP 이후로 미룬 `DEFERRED`로 정리했습니다. `OPEN` 항목은 없습니다. 구현과 시험이 끝났다는 뜻은 아닙니다.

- DEC-004 내부 ID
- DEC-005 fingerprint
- DEC-006 기준 commit
- DEC-008 실행·크기 제한
- DEC-012 Worker 동시성
- DEC-013 Codex lifecycle spike 방법

요청 데이터 체크섬 방식(`DEC-025`), 첫 이벤트 입력과 정책 고정 사본(`DEC-022/024`), 실행 명령의 통신 차단(`DEC-023`)도 확정했습니다. 이 값의 실제 동작 시험은 [실행 계획](mvp-execution-plan.md)의 Phase 1~6에 배치합니다.

### Phase 1 — 기반 코드 Todo

한 PR에서 다음을 만듭니다.

- Python package와 lockfile
- FastAPI/Worker entrypoint
- typed settings와 `.env.example`
- PostgreSQL Docker Compose
- 첫 Alembic migration
- OpenAPI/JSON Schema validation test
- formatter, lint, type-check, unit-test CI
- fake `CodexGateway`

Phase 1 Todo가 merge되기 전에 여러 명이 각각 패키지 구조를 만들지 않습니다.

### Phase 2 — 공통 fixture Todo

- 결함이 심어진 `fixtures/fixture-api`
- 오류 이벤트 예시
- 고정 fingerprint와 base SHA
- 성공·실패 validation command
- fake Codex 분석·patch fixture

이 fixture가 모든 E2E acceptance의 공통 입력입니다.

## 4. 권장 작업 분할

| Track | 범위 | 첫 산출물 | 선행 조건 |
|---|---|---|---|
| A. Contract/API | route, Pydantic, Problem 응답 | 오류 접수·조회 endpoint | Phase 0/1 |
| B. Domain/DB | 상태 전이, repository, migration | Incident ingest transaction | Phase 0/1 |
| C. Job Worker | claim, lease, retry, sweeper | fake handler 동시성 test | DEC-011/012 |
| D. Analysis | prompt input, output validation | fake→실제 Codex analyze | DEC-006/013, C |
| E. Approval/Patch | 승인 transaction, workspace, diff | 승인 후 patch E2E | DEC-007/008, B/C |
| F. Quality/E2E | fixture, crash injection, contract test | 전체 수직 흐름 | Phase 2 |

같은 파일 충돌을 줄이기 위해 A는 `apps/control_api`, B는 `packages/domain`과 `infrastructure/persistence`, C는 `apps/job_worker`, D/E는 adapter별 directory, F는 `fixtures`와 `tests/e2e`를 우선 소유합니다. 공용 interface 변경은 먼저 작은 contract PR로 합칩니다.

## 5. GitHub Issue 단위

Issue 하나는 1~3일 안에 review 가능한 크기를 권장합니다.

각 Issue에는 다음을 씁니다.

```markdown
## Goal
사용자 또는 시스템 관점에서 완료할 동작

## In scope / Out of scope
이번 Issue가 바꾸는 것과 바꾸지 않는 것

## Contract
관련 DEC, OpenAPI path, DB table, interface

## Dependencies
선행 Issue와 차단된 decision

## Acceptance tests
Given / When / Then 또는 구체적인 pytest 이름

## Operational notes
timeout, retry, log/metric, cleanup 영향
```

### 권장 첫 Issue 목록

1. `PHASE-1-01 Python project and quality checks`
2. `PHASE-1-02 PostgreSQL compose and Alembic baseline`
3. `PHASE-1-03 Contract tests for OpenAPI and ErrorEvent Schema`
4. `PHASE-1-04 Typed service settings and fixture registration`
5. `PHASE-2-01 Error event idempotent ingest transaction`
6. `PHASE-2-02 Fingerprint and Incident grouping`
7. `PHASE-3-01 PostgreSQL Job claim and lease`
8. `PHASE-3-02 Retry sweeper and crash recovery`
9. `PHASE-3-03 FakeCodexGateway analysis flow`
10. `PHASE-4-01 Codex SDK lifecycle and timeout test`
11. `PHASE-4-02 Read-only analysis runner`
12. `PHASE-5-01 Idempotent approval transaction`
13. `PHASE-5-02 Workspace manager and diff validator`
14. `PHASE-5-03 Writable patch runner and validation`
15. `PHASE-6-01 End-to-end fixture and failure injection`

## 6. Definition of Ready

Issue를 시작하려면 다음을 만족합니다.

- 관련 P0 decision이 `ACCEPTED`입니다.
- 입력·출력 interface가 문서 또는 test로 정의되어 있습니다.
- 선행 migration/endpoint가 merge되어 있습니다.
- acceptance test가 관찰 가능한 결과로 작성되어 있습니다.
- secret, filesystem write, command 실행 여부가 표시되어 있습니다.

하나라도 빠지면 코드부터 작성하지 않고 contract 또는 decision PR을 먼저 만듭니다.

## 7. Definition of Done

- acceptance test와 관련 단위·통합 테스트가 통과합니다.
- OpenAPI/Schema/DB 변경이 있으면 migration과 contract test가 함께 있습니다.
- timeout, retry, idempotency, cleanup 경로를 테스트했습니다.
- 새 오류는 catalog code와 redacted detail을 사용합니다.
- metric/log에 event payload, secret, 전체 stack trace를 남기지 않습니다.
- 사용자에게 보이는 동작이 바뀌면 웹 가이드 또는 README도 갱신합니다.
- 승인 전 repository 무변경 불변 조건을 깨지 않습니다.

## 8. PR 규칙

- 한 PR에는 하나의 비즈니스 목적만 둡니다.
- 제목 예: `feat(worker): add leased job claim`.
- DB/API breaking change는 관련 DEC/ADR 링크가 필요합니다.
- 생성된 OpenAPI 문서만 고치지 말고 원본 Schema를 수정합니다.
- Codex 실제 호출 테스트는 일반 CI와 분리하고, 일반 CI는 fake gateway를 사용합니다.
- 리뷰어는 정상 경로보다 중복 요청, process crash, timeout, stale result를 먼저 확인합니다.

## 9. 목표 로컬 개발 인터페이스

Phase 1에서 아래 명령을 실제로 동작하게 만드는 것을 협업 계약으로 둡니다. 구현 전에는 문서상의 목표 명령입니다.

```bash
cp .env.example .env
docker compose up -d postgres
alembic upgrade head
python -m apps.control_api.main
python -m apps.job_worker.main
pytest -q
pytest -q tests/e2e
```

예정 환경변수:

| 변수 | 필수 | 목적 |
|---|---:|---|
| `DATABASE_URL` | Y | PostgreSQL 연결 |
| `SERVICE_CONFIG_PATH` | Y | 등록 서비스 설정 |
| `WORKSPACE_ROOT` | Y | 허용된 worktree root |
| `WORKER_ID` | N | 기본 hostname+pid |
| `WORKER_CONCURRENCY` | N | DEC-012 기본값 |
| `CODEX_MODEL` | Y | 고정 모델 이름 |
| `CODEX_BIN` | N | pinned runtime 대신 특정 binary를 검증할 때만 |
| `USE_FAKE_CODEX` | N | 로컬/CI fake gateway |

OpenAI credential의 종류와 공급 방식은 실제 SDK integration spike에서 확인하고 secret 값을 문서나 `.env.example`에 넣지 않습니다.

## 10. 첫 구현 회의 권장 순서

45분 안에 다음 순서로 구현 준비를 확인합니다.

1. 10분: MVP 성공 시나리오와 제외 범위 재확인
2. 20분: 확정한 결정의 설정·마이그레이션·계약 시험 담당자 지정
3. 10분: Phase 1 기반 PR 담당자와 Track owner 지정
4. 5분: 첫 종단 간 시험 날짜와 검토 규칙 확정

결정을 바꿔야 한다면 회의 결과를 채팅에만 남기지 말고 같은 날 의사결정 대장과 결정 기록, 관련 계약에 반영합니다.

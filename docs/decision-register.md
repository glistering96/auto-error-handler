# MVP 의사결정 대장

상태: Working Draft  
최종 갱신: 2026-09-01

이 문서는 구현자가 임의로 다른 선택을 하지 않도록 현재 결정, 권장안, 미결정 사항을 한곳에서 관리합니다. 상세 설계보다 이 문서의 `ACCEPTED` 결정이 우선합니다.

## 상태 정의

| 상태 | 의미 |
|---|---|
| `ACCEPTED` | MVP 계약으로 확정. 변경하려면 ADR과 관련 문서를 함께 수정 |
| `PROPOSED` | 권장안은 있으나 협업자가 승인해야 함 |
| `OPEN` | 구현 전에 선택이 필요한 차단 항목 |
| `DEFERRED` | MVP에서 구현하지 않기로 확정 |

우선순위 `P0`는 해당 마일스톤 구현 전에 반드시 확정하고, `P1`은 첫 E2E 전에 확정합니다.

## Core Architecture Decisions

아래 여섯 항목은 구현 세부사항보다 상위에 있는 제품·아키텍처 방향이며 모두 `ACCEPTED`입니다.

| ID | 상태 | 큰 결정 | 선택한 방향 |
|---|---|---|---|
| CORE-001 | ACCEPTED | 서비스 책임 | 단순 Codex 실행 API가 아니라 Incident·분석·승인·Patch 상태를 소유하는 오류 분석·수정 플랫폼 |
| CORE-002 | ACCEPTED | Producer 경계 | Producer는 오류·release·재현처럼 관측된 사실만 보내고 workflow 상태는 플랫폼이 소유 |
| CORE-003 | ACCEPTED | 실행 구조 | MVP는 중앙 Worker를 사용하되 Control Plane과 Execution Plane을 interface/process 경계로 분리 |
| CORE-004 | ACCEPTED | 자동화 수준 | 분석은 자동으로 실행하고 repository 쓰기는 특정 분석·SHA에 대한 사람 승인 이후에만 허용 |
| CORE-005 | ACCEPTED | 처리 단위 | 개별 Event가 아니라 같은 원인의 occurrence를 묶은 Incident 중심으로 처리 |
| CORE-006 | ACCEPTED | 산출물과 로드맵 | MVP 결과는 검증된 Diff이며 이후 GitHub App 기반 Draft PR로 확장 |

이 방향을 바꾸면 API, 상태 머신, 데이터 모델, 보안 경계가 함께 바뀌므로 반드시 ADR을 작성합니다. UUID, timeout, lease 같은 아래 항목은 이 Core Decisions를 구현하기 위한 하위 결정입니다.

## 구현 의사결정 요약

| ID | 우선순위 | 상태 | 주제 | 현재안 또는 권장안 | 확정 시점 |
|---|---:|---|---|---|---|
| DEC-001 | P0 | ACCEPTED | 외부 ingress | Producer는 HTTP `POST /v1/error-events` 사용 | 완료 |
| DEC-002 | P0 | DEFERRED | 인증 | 서비스·승인자 인증은 제외하고 개발망에만 노출 | MVP 이후 |
| DEC-003 | P0 | ACCEPTED | 비동기 전달 | PostgreSQL `jobs`가 Source of Truth이자 Queue | 완료 |
| DEC-004 | P0 | OPEN | 내부 ID | 모든 리소스에 UUIDv4 권장. API 예시의 ULID 표기는 확정 후 통일 | M0 시작 전 |
| DEC-005 | P0 | PROPOSED | Incident grouping | Producer fingerprint 우선, 없으면 exception type + application top frame + route | M1 시작 전 |
| DEC-006 | P0 | PROPOSED | 기준 commit | `release.commitSha` 우선, 없으면 접수 시점 default branch HEAD를 snapshot | M2 시작 전 |
| DEC-007 | P0 | OPEN | 승인 대상 고정 | 승인 body에 `analysisRunId`, `expectedBaseCommitSha`를 받고 불일치 시 `409` 권장 | API 구현 전 |
| DEC-008 | P0 | PROPOSED | 실행·크기 제한 | payload 256 KiB, 변경 10파일/400줄, 분석 600초, 패치 900초, 명령 300초 | M0 시작 전 |
| DEC-009 | P0 | ACCEPTED | 검증 명령 | 서비스 설정의 argv allowlist만 `shell=False`로 실행 | 완료 |
| DEC-010 | P0 | ACCEPTED | Patch 안전 경계 | allowed/denied path 검사, binary·symlink·submodule 차단 | 완료 |
| DEC-011 | P0 | OPEN | Job lease와 retry | lease 120초, heartbeat 30초, 인프라 실패만 총 3회 시도 권장 | M1 시작 전 |
| DEC-012 | P0 | OPEN | Worker 동시성 | MVP 기본값 1, 설정으로만 증가 권장 | M1 시작 전 |
| DEC-013 | P0 | PROPOSED | Codex lifecycle | Job마다 `AsyncCodex` context와 새 thread 사용. 실제 child process 수는 integration spike로 검증 | M2 시작 전 |
| DEC-014 | P1 | OPEN | 실패 artifact 보존 | 성공 worktree 즉시 삭제, 실패 worktree 24시간, DB 결과는 MVP 동안 보존 권장 | M3 시작 전 |
| DEC-015 | P1 | OPEN | 목록 pagination | `created_at DESC, id DESC`, opaque cursor, 기본 20·최대 100 권장 | 조회 API 구현 전 |
| DEC-016 | P1 | OPEN | 미생성 결과 조회 | analysis/patch가 아직 없으면 `404 application/problem+json` 권장 | 조회 API 구현 전 |
| DEC-017 | P1 | PROPOSED | 오류 코드 | `AEH-{AREA}-{HTTP}-{SEQ}` 형식과 고정 catalog 사용 | M0 종료 전 |
| DEC-018 | P0 | ACCEPTED | 분석과 패치 분리 | 분석은 read-only, 승인은 별도 transaction, 패치는 새 writable worktree/thread | 완료 |
| DEC-019 | P1 | ACCEPTED | Scale-out 순서 | API → DB HA/pool → Worker lease → sandbox/관측 → 병목 확인 후 Outbox+RabbitMQ | 완료 |
| DEC-020 | P1 | DEFERRED | Git remote 변경 | branch push와 PR 생성은 MVP 결과에 포함하지 않음 | MVP 이후 |

## P0 회의에서 답해야 할 질문

아래 여덟 항목에 답하면 M0~M3를 서로 다른 사람이 병렬로 구현할 수 있습니다.

1. 리소스 ID를 UUIDv4로 통일할 것인가?
2. Producer fingerprint가 없을 때 application top frame을 어떻게 식별하고 경로·line number를 어떻게 정규화할 것인가?
3. `release.commitSha`가 없거나 fetch할 수 없으면 fallback할 것인가, 요청을 실패시킬 것인가?
4. 승인 요청이 어떤 `analysisRunId`와 SHA를 승인하는지 body에 명시할 것인가?
5. 256 KiB, 10파일, 400줄, 600/900/300초 제한을 그대로 사용할 것인가?
6. lease 120초·heartbeat 30초·총 3회 시도 정책을 사용할 것인가?
7. 첫 MVP Worker 동시성을 1로 제한할 것인가?
8. `AsyncCodex` context 하나가 만드는 app-server lifecycle과 timeout 종료 동작을 integration test로 어떤 환경에서 검증할 것인가?

## 결정별 검증 기준

### DEC-005 — fingerprint

- 동일 eventId 재전송은 occurrence를 추가하지 않습니다.
- 서로 다른 eventId와 동일 fingerprint는 열린 Incident 하나에 누적합니다.
- 숫자 ID, UUID, line number처럼 매번 바뀌는 값은 fallback fingerprint에서 제거합니다.
- 서로 다른 exception type 또는 application top frame은 기본적으로 합치지 않습니다.

### DEC-006/007 — 기준 SHA와 승인

- `analysis_runs.base_commit_sha`, `approvals.base_commit_sha`, `patch_runs.base_commit_sha`가 동일해야 합니다.
- 승인 요청이 가리킨 분석이 최신 active 분석이 아니면 `409`입니다.
- Patch worktree는 approval에 저장된 SHA에서만 생성합니다.
- default branch가 이후 이동해도 이미 승인된 Patch 기준 SHA는 바뀌지 않습니다.

### DEC-011 — retry

- DB 연결 단절, Codex app-server 비정상 종료, 일시적 network 오류만 retry 후보입니다.
- Schema 오류, 정책 위반, 테스트 실패, 존재하지 않는 commit은 자동 retry하지 않습니다.
- retry는 같은 Job과 같은 run ID를 사용하며 새 analysis/patch run을 만들지 않습니다.
- 최대 시도 횟수를 넘으면 Job과 해당 run을 함께 terminal failure로 만듭니다.

## 결정 절차

1. 담당자가 이 문서의 권장안을 검토합니다.
2. 단순 선택은 이 표를 수정하는 PR에서 `ACCEPTED`로 바꿉니다.
3. API·데이터 모델·보안 경계가 바뀌는 결정은 [`docs/decisions/`](decisions/README.md)에 ADR을 추가합니다.
4. 결정 PR에는 영향받는 OpenAPI, JSON Schema, migration, 테스트 문서를 함께 수정합니다.
5. 이미 `ACCEPTED`인 결정과 충돌하는 구현 PR은 merge하지 않습니다.

## 현재 문서 불일치 해소 규칙

- OpenAPI 예시와 DB ID 형식은 DEC-004 확정 후 한 번에 통일합니다.
- 승인 endpoint의 request body는 DEC-007 확정 후 OpenAPI에 추가합니다.
- 모든 크기·timeout 값은 DEC-008의 확정값을 서비스 설정, 문서, 테스트 fixture에서 공유합니다.
- 결정 전 코드는 상수를 복제하지 않고 하나의 typed settings object를 사용합니다.

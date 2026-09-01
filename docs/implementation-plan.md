# Auto Error Handler MVP 세부 구현 계획

상태: Draft  
작성일: 2026-09-01  
관련 문서:

- [서비스 아키텍처](service-architecture.md)
- [HTTP 오류 이벤트 계약 v1](contracts/external-error-event-v1.md)
- [인터랙티브 전체 흐름](../walkthrough-auto-error-handler.html)

## 1. 구현 목표

첫 구현은 아래 단일 수직 흐름만 완성합니다.

```text
fixture 서비스 등록
  → HTTP 오류 이벤트 수신
  → Incident 및 Analysis Job 저장
  → Codex 읽기 전용 분석
  → 분석 결과 API 조회
  → 인증 없는 승인 API
  → 격리 worktree에서 Codex 패치
  → diff 정책 검사와 테스트
  → diff 및 검증 결과 API 조회
```

핵심 완료 조건은 다음입니다.

- 승인 전에는 Git diff가 없습니다.
- 승인 후에는 허용된 경로만 변경됩니다.
- 테스트에 실패하면 `PATCH_READY`가 되지 않습니다.
- 같은 오류 이벤트를 재전송해도 분석과 패치가 중복 생성되지 않습니다.

## 2. 기술 기준

| 영역 | MVP 선택 |
|---|---|
| Runtime | Python 3.12 |
| API | FastAPI + Pydantic |
| Database | PostgreSQL 16 |
| DB access | SQLAlchemy 2.x + Alembic + psycopg |
| 비동기 작업 | PostgreSQL Job Queue |
| Codex | `openai-codex` Python SDK |
| Git workspace | `git worktree` |
| Test | pytest |
| 로컬 실행 | Docker Compose |

패키지 버전은 lockfile로 고정합니다. Codex SDK 호출은 `CodexGateway` adapter 안에 캡슐화합니다.

## 3. 저장소 구조

```text
apps/
  control_api/
    main.py
    routes/
  job_worker/
    main.py
    handlers/
packages/
  domain/
    incidents.py
    jobs.py
    approvals.py
  application/
    ingest_error.py
    analyze_incident.py
    approve_patch.py
    create_patch.py
  infrastructure/
    persistence/
    codex/
    git/
    validation/
  contracts/
    external_error_event.py
fixtures/
  fixture-api/
schemas/
docs/
tests/
  unit/
  integration/
  e2e/
```

API와 Worker는 같은 패키지를 사용하지만 별도 프로세스로 실행합니다.

## 4. 환경 설정

MVP 서비스는 설정 파일로 등록합니다.

```yaml
services:
  fixture-api:
    repositoryPath: /workspace/fixtures/fixture-api
    defaultBranch: main
    runbookPaths: [README.md]
    allowedPaths: [src/**, tests/**]
    deniedPaths: [.github/**, infra/**]
    maxChangedFiles: 10
    maxDiffLines: 400
    analysisTimeoutSeconds: 600
    patchTimeoutSeconds: 900
    validationCommands:
      - id: unit
        argv: [pytest, -q]
        timeoutSeconds: 300
```

애플리케이션 시작 시 설정을 검증하고 `services` 테이블에 upsert합니다. `repositoryPath`는 허용된 fixture root 아래의 정규화된 경로만 허용합니다.

## 5. HTTP API

### 오류 수신

`POST /v1/error-events`

요청은 [HTTP 오류 이벤트 계약](contracts/external-error-event-v1.md)을 따릅니다.

처리:

1. `Idempotency-Key == body.eventId` 검사
2. schema와 body byte limit 검사
3. `serviceKey`로 활성 서비스 조회
4. payload checksum 계산
5. 기존 eventId가 있으면 checksum 비교 후 기존 응답 반환
6. fingerprint 계산
7. 오류 문구와 `reproduction` context를 정규화해 저장
8. 열린 Incident 조회 또는 생성
9. occurrence 저장
10. 새 Incident라면 Analysis Job 저장
11. commit 후 `202 Accepted`

### 사건 조회

- `GET /v1/incidents`
- `GET /v1/incidents/{incidentId}`
- `GET /v1/incidents/{incidentId}/analysis`
- `GET /v1/incidents/{incidentId}/patch`

### 승인

`POST /v1/incidents/{incidentId}/approve`

MVP에서는 인증하지 않습니다. Endpoint는 로컬 개발망에만 노출합니다.

처리 transaction:

1. Incident를 `FOR UPDATE`로 조회
2. 상태가 `AWAITING_APPROVAL`인지 확인
3. 최신 analysis run과 base SHA 확인
4. approval insert
5. Incident를 `PATCHING`으로 전환
6. deterministic key로 Patch Job insert
7. commit

중복 승인은 기존 approval과 patch run을 반환합니다.

## 6. 데이터베이스

### `services`

- `id`
- `key UNIQUE`
- `repository_path`
- `default_branch`
- `policy JSONB`
- `active`

### `error_events`

- `id`
- `service_id`
- `event_id`
- `payload_checksum`
- `normalized_payload JSONB`
- `received_at`

제약: `UNIQUE(service_id, event_id)`

### `incidents`

- `id`
- `service_id`
- `fingerprint`
- `state`
- `version`
- `base_commit_sha`
- `created_at`, `updated_at`

MVP 열린 상태: `RECEIVED`, `ANALYZING`, `AWAITING_APPROVAL`, `PATCHING`, `VALIDATING`.

### `occurrences`

- `id`
- `incident_id`
- `error_event_id UNIQUE`
- `occurred_at`

### `analysis_runs`

- `id`
- `incident_id`
- `status`
- `base_commit_sha`
- `codex_thread_id`
- `result JSONB`
- `error`
- `started_at`, `finished_at`

### `approvals`

- `id`
- `incident_id UNIQUE`
- `analysis_run_id`
- `base_commit_sha`
- `created_at`

### `patch_runs`

- `id`
- `incident_id UNIQUE`
- `approval_id UNIQUE`
- `status`
- `worktree_path`
- `diff_text`
- `validation_result JSONB`
- `error`
- `started_at`, `finished_at`

### `jobs`

- `id`
- `type`: `ANALYZE` 또는 `PATCH`
- `deduplication_key UNIQUE`
- `payload JSONB`
- `status`: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`
- `attempt`
- `available_at`
- `locked_by`, `locked_at`
- `last_error`

## 7. PostgreSQL Job Worker

Worker는 짧은 transaction에서 Job을 claim합니다.

```sql
WITH candidate AS (
  SELECT id
  FROM jobs
  WHERE status = 'PENDING'
    AND available_at <= now()
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs
SET status = 'RUNNING',
    locked_by = :worker_id,
    locked_at = now(),
    attempt = attempt + 1
WHERE id IN (SELECT id FROM candidate)
RETURNING *;
```

Codex 작업 중 DB transaction을 유지하지 않습니다. MVP retry는 프로세스 재시작 시 오래된 `RUNNING` Job을 `PENDING`으로 돌리는 단순 sweeper와 최대 2회 시도로 제한합니다.

deduplication key:

```text
ANALYZE:<incident_id>
PATCH:<approval_id>
```

## 8. 기준 commit과 Workspace Manager

기준 SHA 우선순위:

1. 오류 이벤트의 `release.commitSha`
2. 없으면 최초 Incident 생성 시 local repository의 default branch HEAD

Workspace Manager는 SHA가 저장소에 존재하는지 검증합니다.

분석 worktree:

```text
<temp-root>/analysis/<analysis-run-id>
```

패치 worktree:

```text
<temp-root>/patch/<patch-run-id>
```

각 run 종료 후 worktree를 제거합니다. diff와 검증 로그는 MVP에서는 PostgreSQL에 크기 제한을 두고 저장합니다.

## 9. Codex Adapter

```python
class CodexGateway(Protocol):
    def analyze(self, input: AnalyzeIncidentInput) -> AnalysisResult: ...
    def patch(self, input: CreatePatchInput) -> PatchResult: ...
```

### 분석

```python
with Codex() as codex:
    thread = codex.thread_start(sandbox=Sandbox.read_only)
    result = thread.run(prompt)
```

분석 결과:

```json
{
  "summary": "...",
  "rootCause": "...",
  "confidence": "high",
  "evidence": [
    {"path": "src/users.py", "explanation": "..."}
  ],
  "proposedChanges": [
    {"path": "src/users.py", "purpose": "null guard 추가"}
  ],
  "validationPlan": ["pytest -q"],
  "reproductionAssessment": {
    "reproducible": true,
    "reason": "null userId 입력과 fixture 상태로 재현 가능",
    "suggestedRegressionTests": ["존재하지 않는 사용자 조회 시 404 반환"]
  },
  "risk": "low",
  "missingInformation": []
}
```

`confidence=low`, 근거 없음, 변경 경로가 정책 밖인 결과는 `ANALYSIS_FAILED`로 처리합니다.

오류 이벤트의 `reproduction.description`, `preconditions`, `steps`, `sanitizedInputs`, `expectedBehavior`, `actualBehavior`를 분석 prompt에 포함합니다. 이 값은 untrusted data이며 shell command나 test command로 실행하지 않습니다. 재현 실행이 필요하면 Codex는 저장소 안에 임시 또는 정식 테스트를 작성하고, 플랫폼은 서비스 설정에 등록된 validation command만 실행합니다.

### 패치

패치에서는 분석 thread를 재개하지 않습니다. 다음 입력으로 새 thread를 만듭니다.

- 검증된 분석 결과
- 기준 commit SHA
- allowed/denied path
- 파일 수와 diff line 제한
- validation command
- 테스트 삭제·완화 금지

`Sandbox.workspace_write`를 사용하되, 최종 보안 판단은 실제 Git diff와 명령 결과로 수행합니다.

## 10. Diff Validator

검사 순서:

1. 변경 파일이 worktree 내부인지 확인
2. symlink와 submodule 변경 금지
3. allowed/denied path 확인
4. 변경 파일 수 확인
5. 추가·삭제 line 총합 확인
6. binary와 대형 파일 차단
7. `.github`, 배포, secret 경로 차단
8. `git diff --check`

정책 실패 시 자동 수정 loop 없이 `PATCH_FAILED`로 종료합니다.

## 11. Validation Runner

설정에 저장된 argv만 실행합니다.

```python
subprocess.run(
    command.argv,
    cwd=worktree,
    shell=False,
    timeout=command.timeout_seconds,
    capture_output=True,
    text=True,
)
```

종료 코드, 실행 시간, 제한된 stdout/stderr를 저장합니다. 하나라도 실패하면 `PATCH_FAILED`입니다.

## 12. 구현 마일스톤

### M0 — 기반과 계약

- Python workspace, FastAPI, pytest
- Docker Compose PostgreSQL
- Pydantic 오류 이벤트 모델과 JSON Schema 계약 테스트
- 공통 설정과 구조화 logging

완료 조건: API와 Worker health check 및 CI 통과.

### M1 — Incident와 DB Job

- Alembic migration
- 오류 수신 API
- event 멱등성과 fingerprint
- Incident grouping
- PostgreSQL Job claim
- fake Analysis Worker

완료 조건: 동일 eventId는 한 번만 저장되고 서로 다른 동일 fingerprint 이벤트는 한 Incident에 묶임.

### M2 — Codex 분석

- fixture 저장소
- Workspace Manager
- `CodexGateway` fake와 실제 Python SDK adapter
- read-only 분석 prompt와 result validation
- 분석 결과 조회 API

완료 조건: fixture 오류의 원인 파일과 근거가 저장되고 분석 중 diff가 비어 있음.

### M3 — 승인과 패치

- 승인 API
- Patch Job
- writable patch worktree
- 새 Codex patch thread
- Diff Validator
- Validation Runner
- diff 조회 API

완료 조건: 승인 전 무변경, 승인 후 허용 경로 변경, 테스트 통과 결과만 `PATCH_READY`.

### M4 — 실패와 E2E

- stale Job sweeper와 최대 2회 retry
- timeout과 workspace cleanup
- duplicate 승인 테스트
- 전체 E2E와 crash injection

완료 조건: 프로세스 재시작과 중복 요청에도 분석 및 patch run이 하나만 존재함.

## 13. 테스트 전략

### 단위 테스트

- eventId 멱등성
- reproduction context schema와 크기 제한
- fingerprint 정규화
- Incident 상태 전이
- Job deduplication key
- path allowlist/denylist
- diff 크기 제한

### 통합 테스트

- 오류 수신 transaction과 Analysis Job 원자성
- `FOR UPDATE SKIP LOCKED` 동시 claim
- 중복 승인 transaction
- worktree 생성과 삭제
- 분석 worktree 무변경
- validation 실패 시 `PATCH_FAILED`

### E2E

결함이 심어진 fixture 저장소로 다음을 자동화합니다.

1. 동일 eventId를 3회 전송합니다.
2. Incident 1개, occurrence 1개, analysis run 1개를 확인합니다.
3. 서로 다른 eventId와 동일 fingerprint를 2회 전송합니다.
4. 기존 Incident에 occurrence만 2개 추가되는지 확인합니다.
5. 분석 결과의 원인과 근거를 확인합니다.
6. 승인 전 diff가 비어 있는지 확인합니다.
7. 승인합니다.
8. 허용 경로만 변경되었는지 확인합니다.
9. 테스트 통과와 `PATCH_READY`를 확인합니다.
10. 승인을 다시 호출해 patch run이 추가되지 않는지 확인합니다.

일반 CI에서는 fake CodexGateway를 사용하고 실제 Codex E2E는 수동 또는 별도 CI Job으로 실행합니다.

## 14. MVP Definition of Done

- fixture 서비스가 설정으로 등록됩니다.
- 유효한 HTTP 오류 이벤트가 `202`로 접수됩니다.
- 같은 eventId의 재시도는 중복 저장되지 않습니다.
- Incident와 Analysis Job이 같은 transaction에서 생성됩니다.
- Codex 분석은 read-only workspace에서 실행됩니다.
- 분석 결과에 원인, 근거, 변경 제안, 검증 계획이 포함됩니다.
- 재현 정보가 제공되면 분석 결과에 재현 가능성 판단과 회귀 테스트 제안이 포함됩니다.
- 승인 전에는 writable patch workspace가 생성되지 않습니다.
- 승인 후에는 별도 patch worktree와 새 Codex thread를 사용합니다.
- 정책 밖 변경과 검증 실패는 `PATCH_FAILED`입니다.
- 성공한 patch의 diff와 검증 결과를 API로 조회할 수 있습니다.
- 중복 승인과 Worker 재시작이 중복 patch run을 만들지 않습니다.

## 15. MVP 이후

MVP 완료 뒤 우선순위:

1. GitHub App과 Draft Pull Request
2. 서비스 인증과 승인자 인증
3. Slack 알림
4. 내부 RabbitMQ
5. 멀티테넌시와 운영 인프라

구현되지 않은 기능을 미리 위한 provisioning API나 복잡한 추상화는 MVP에 추가하지 않습니다. 다만 서비스 식별과 Job Queue는 adapter 경계로 두어 인증과 RabbitMQ를 나중에 교체할 수 있게 합니다.

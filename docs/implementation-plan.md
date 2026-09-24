# Auto Error Handler MVP 세부 구현 계획

상태: 세부 구현 참고자료 · 확정 결정 반영
작성일: 2026-09-01  
결정 반영: 2026-09-24
문서 역할: 세부 계약과 구현 참고자료
실행 순서: [MVP 실행 계획](mvp-execution-plan.md)
범위 분리: [MVP 구현 범위](mvp-scope.md)

이 문서의 순서와 예시는 세부 참고자료입니다. 실제 구현 순서는 [MVP 실행 계획](mvp-execution-plan.md)을 따르고, 값과 경계가 다르면 [확정 의사결정](decision-register.md)과 [구현 계약](implementation-contracts.md)을 우선합니다.
관련 문서:

- [서비스 아키텍처](service-architecture.md)
- [협업 시작 가이드](collaboration-guide.md)
- [MVP 의사결정 대장](decision-register.md)
- [DB·작업자·Codex 구현 계약](implementation-contracts.md)
- [HTTP 오류 이벤트 계약 v1](contracts/external-error-event-v1.md)
- [인터랙티브 전체 흐름](../walkthrough-mvp-service-architecture.html)

## 1. 구현 목표

첫 구현은 아래 단일 수직 흐름만 완성합니다.

```text
예제 서비스 등록
  → HTTP 오류 이벤트 수신
  → 사건과 분석 작업 저장
  → Codex 읽기 전용 분석
  → 분석 결과 API 조회
  → 인증 없는 승인 API
  → 분리된 작업 트리에서 Codex 패치
  → 변경 정책 검사와 테스트
  → 변경 내역 및 검증 결과 API 조회
```

핵심 완료 조건은 다음입니다.

- 승인 전에는 Git diff가 없습니다.
- 승인 후에는 허용된 경로만 변경됩니다.
- 테스트에 실패하면 `PATCH_READY`가 되지 않습니다.
- 같은 오류 이벤트를 재전송해도 분석과 패치가 중복 생성되지 않습니다.

## 2. 기술 기준

| 영역 | MVP 선택 |
|---|---|
| 실행 환경 | Python 3.12 |
| API | FastAPI + Pydantic |
| 데이터베이스 | PostgreSQL 16 |
| DB 접근 | SQLAlchemy 2.x + Alembic + psycopg |
| 비동기 작업 | PostgreSQL 작업 대기열 |
| Codex | `openai-codex` Python SDK |
| Git 작업 공간 | `git worktree` |
| 테스트 | pytest |
| 로컬 실행 | Docker Compose |

패키지 버전은 잠금 파일로 고정합니다. Codex SDK 호출은 `CodexGateway` 어댑터 안에서만 수행합니다.

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

API와 작업자는 같은 패키지를 사용하지만 별도 프로세스로 실행합니다.

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
    analysisTimeoutSeconds: 86400
    patchTimeoutSeconds: 900
    validationCommands:
      - id: unit
        argv: [pytest, -q]
        timeoutSeconds: 300
```

애플리케이션을 시작할 때 설정을 검증하고 `services` 테이블에 등록하거나 갱신합니다. `repositoryPath`에는 허용된 예제 저장소 최상위 경로 아래의 정규화된 경로만 사용할 수 있습니다.

## 5. HTTP API

### 오류 수신

`POST /v1/error-events`

요청은 [HTTP 오류 이벤트 계약](contracts/external-error-event-v1.md)을 따릅니다.

처리:

1. `Idempotency-Key == body.eventId`인지 검사
2. 스키마와 요청 본문 용량 제한 검사
3. `serviceKey`로 활성 서비스 조회
4. [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html)로 요청 JSON을 정규화하고 UTF-8 바이트의 SHA-256 체크섬 계산. 중복 객체 키는 `400`, 정규화 불가 수치는 `422` 반환
5. 기존 `eventId`가 있으면 체크섬 비교 후 기존 사건 ID와 현재 상태를 `duplicate=true`로 반환. 이때 Git은 다시 조회하지 않음
6. 필수 `fingerprint`를 양끝 공백 제거와 NFC 방식으로 정규화하고, 비어 있으면 `422` 반환
7. 새 이벤트의 `release.commitSha` 또는 현재 기본 브랜치 `HEAD`를 Git에서 전체 커밋 ID로 확정. 잘못된 SHA는 `422`, 일시적인 저장소 오류는 `503` 반환
8. 오류 문구와 `reproduction` 문맥을 정규화해 저장
9. 같은 서비스·지문·전체 SHA의 처리 중인 사건을 조회하거나 새로 생성
10. 발생 기록을 저장하고, 새 사건이면 접수 당시 저장소 경로·첫 이벤트 ID·기준 SHA를 고정하고 분석 작업 저장
11. 커밋 후 `202 Accepted` 반환

### 사건 조회

- `GET /v1/incidents`
- `GET /v1/incidents/{incidentId}`
- `GET /v1/incidents/{incidentId}/analysis`
- `GET /v1/incidents/{incidentId}/patch`

### 승인

`POST /v1/incidents/{incidentId}/approve`

MVP에서는 인증하지 않습니다. 이 엔드포인트는 로컬 개발망에만 노출합니다.

승인 요청은 본문을 받지 않습니다. 서버가 [DEC-007](decision-register.md)에 따라 Incident의 유일한 성공 분석과 기준 SHA를 조회해 승인 기록에 고정합니다.

처리 트랜잭션:

1. 사건을 `FOR UPDATE`로 조회
2. 상태가 `AWAITING_APPROVAL`인지 확인
3. 성공한 분석 실행이 정확히 하나인지 확인
4. 분석 실행 ID와 기준 SHA를 승인·패치 실행 기록에 복사
5. 승인·패치 실행 기록 추가
6. 사건을 `PATCHING`으로 전환
7. 결정적 키로 패치 작업 추가
8. 커밋

같은 `Idempotency-Key`로 승인을 다시 요청하면 기존 승인·패치 실행 기록을 반환합니다. 이미 승인한 사건을 다른 키로 다시 승인하면 `409`를 반환합니다.

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
- `repository_path_snapshot NOT NULL`
- `base_commit_sha NOT NULL`
- `first_error_event_id NOT NULL`
- `created_at`, `updated_at`

MVP 상태: `RECEIVED`, `ANALYZING`, `AWAITING_APPROVAL`, `ANALYSIS_FAILED`, `PATCHING`, `VALIDATING`, `PATCH_READY`, `PATCH_FAILED`.

처리 중인 사건의 부분 고유 인덱스는 `(service_id, fingerprint, base_commit_sha)`에 적용합니다. 추가 발생은 건수와 `updated_at`만 갱신하며, 상태 전이용 `version`을 올리지 않습니다.

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
- `input_error_event_id NOT NULL`
- `policy_snapshot JSONB NOT NULL`
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
- `locked_by`, `locked_at`, `claim_token`
- `last_error`

## 7. PostgreSQL 작업자

작업자는 짧은 트랜잭션 안에서 작업 하나를 선점합니다.

```sql
WITH candidate AS (
  SELECT id
  FROM jobs
  WHERE status = 'PENDING'
    AND available_at <= now()
  ORDER BY available_at, created_at, id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs
SET status = 'RUNNING',
    locked_by = :worker_id,
    locked_at = now(),
    claim_token = :new_claim_token,
    attempt = attempt + 1
WHERE id IN (SELECT id FROM candidate)
RETURNING *;
```

Codex 작업 중에는 DB 트랜잭션을 유지하지 않습니다. [DEC-011](decision-register.md)에 따라 임대 시간은 120초, 생존 신호 간격은 30초이며 일시적인 기반 시설 오류만 총 3회까지 시도합니다. 작업을 선점할 때마다 새 UUID `claim_token`을 발급하고 생존 신호를 보내거나 결과를 저장할 때 이 값을 확인합니다. 분석 하나의 최대 실행 시간은 24시간이며, 이 시간을 넘으면 자동으로 다시 시도하지 않고 최종 실패로 처리합니다.

deduplication key:

```text
ANALYZE:<incident_id>
PATCH:<approval_id>
```

## 8. 기준 커밋과 작업 공간 관리자

기준 SHA 우선순위:

1. 오류 이벤트의 `release.commitSha`
2. 없으면 새 이벤트를 접수할 때 로컬 저장소 기본 브랜치의 `HEAD`

두 경우 모두 접수 트랜잭션 전에 전체 커밋 ID로 확정합니다. 중복 이벤트는 기존 사건 ID와 현재 상태를 반환하고 Git을 다시 조회하지 않습니다. 작업 공간 관리자는 분석·패치 전에 고정한 SHA가 접수 당시 저장소에 남아 있는지 재확인할 뿐 새 SHA를 선택하지 않습니다.

분석 작업 트리:

```text
<temp-root>/analysis/<analysis-run-id>
```

패치 작업 트리:

```text
<temp-root>/patch/<patch-run-id>
```

성공·실패 여부와 관계없이 실행이 끝나면 작업 트리를 제거합니다. MVP 개발 기간에는 변경 내역과 검증 로그의 크기를 제한해 PostgreSQL에 저장합니다.

## 9. Codex 어댑터

```python
class CodexGateway(Protocol):
    def analyze(self, input: AnalyzeIncidentInput) -> AnalysisResult: ...
    def patch(self, input: CreatePatchInput) -> PatchResult: ...
```

### 분석

```python
# SDK 버전과 하위 프로세스 종료 동작은 잠금 파일과 실제 통합 시험으로 고정합니다.
result = await gateway.analyze(AnalysisRequest(
    workspace=read_only_workspace,
    base_commit_sha=base_commit_sha,
    incident_context=normalized_context,
))
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
    "reason": "null userId 입력과 예제 상태로 재현 가능",
    "suggestedRegressionTests": ["존재하지 않는 사용자 조회 시 404 반환"]
  },
  "risk": "low",
  "missingInformation": []
}
```

`confidence=low`, 근거 없음, 변경 경로가 정책 밖인 결과는 `ANALYSIS_FAILED`로 처리합니다.

사건의 첫 이벤트에 담긴 `reproduction.description`, `preconditions`, `steps`, `sanitizedInputs`, `expectedBehavior`, `actualBehavior`와 `reproductionData`를 분석 프롬프트에 포함합니다. 실제 재현에 필요한 `reproductionData`에는 요청 입력, 예제 데이터 초기값, 기능 플래그를 담을 수 있습니다. 이 값은 신뢰할 수 없는 데이터이므로 셸 명령이나 테스트 명령으로 직접 실행하지 않습니다. 재현 실행이 필요하면 Codex가 저장소 안에 임시 또는 정식 테스트를 작성하고, 플랫폼은 서비스 설정에 등록된 검증 명령만 실행합니다.

첫 분석 선점 때 첫 이벤트 ID와 사건에 저장된 접수 당시 저장소 경로·경로 정책·제한·검증 명령·모델·결과 형식·통신 정책을 `analysis_runs.policy_snapshot`에 고정하고 재시도에서도 재사용합니다. Codex의 모델 서비스 연결은 허용하지만 모델이 실행하는 명령과 검증 명령의 외부 접속은 차단합니다. 이 경계를 실제 실행 환경에서 확인하지 못하면 실제 모델 경로를 준비 완료로 표시하지 않습니다.

### 패치

패치에서는 분석 스레드를 재개하지 않습니다. 다음 입력으로 새 스레드를 만듭니다.

- 검증된 분석 결과
- 기준 커밋 SHA와 승인된 분석의 정책 고정 사본
- 허용·차단 경로
- 파일 수와 변경 줄 수 제한
- 검증 명령
- 테스트 삭제·완화 금지

`Sandbox.workspace_write`를 사용하되, 최종 보안 판단은 실제 Git 변경 내역과 명령 실행 결과를 기준으로 내립니다.

## 10. 변경 내역 검증기

검사 순서:

1. 변경 파일이 작업 트리 내부에 있는지 확인
2. 심볼릭 링크와 하위 모듈 변경 금지
3. 허용·차단 경로 확인
4. 변경 파일 수 확인
5. 추가·삭제한 줄의 총합 확인
6. 바이너리와 대형 파일 차단
7. `.github`, 배포, 비밀값 경로 차단
8. `git diff --check`

정책 검사를 통과하지 못하면 자동 수정 반복 없이 `PATCH_FAILED`로 종료합니다.

## 11. 검증 명령 실행기

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

종료 코드, 실행 시간, 길이를 제한한 표준 출력·표준 오류를 저장합니다. 하나라도 실패하면 `PATCH_FAILED`로 처리합니다.

## 12. 구현 마일스톤

### Phase 1 — 기반과 계약

- Python 작업 공간, FastAPI, pytest
- Docker Compose PostgreSQL
- Pydantic 오류 이벤트 모델과 JSON Schema 계약 테스트
- 공통 설정과 구조화된 로그

완료 조건: API와 작업자 상태 점검 및 CI 통과.

### Phase 2 — 사건과 DB 작업

- Alembic 마이그레이션
- 오류 수신 API
- 이벤트 멱등성과 지문
- 사건 묶기
- PostgreSQL 작업 선점
- 가짜 분석 작업자

완료 조건: 같은 `eventId`는 한 번만 저장되고, 서비스·지문·전체 SHA가 같을 때만 하나의 사건에 묶임.

### Phase 3 — 작업자 임대와 가짜 분석

- 선점마다 새 `claim_token`을 발급하고 이전 작업자의 완료 처리를 거부
- 사건의 첫 이벤트 ID와 정책 고정 사본을 첫 분석 선점 때 저장하고 재시도에서 재사용
- 가짜 분석으로 상태 전이와 실패·복구 계약을 검증

완료 조건: 작업자 장애 후에도 같은 입력과 정책으로 실행을 재개하고 분석 실행이 중복되지 않음.

### Phase 4 — Codex 분석

- 예제 저장소
- 작업 공간 관리자
- 가짜 `CodexGateway`와 실제 Python SDK 어댑터
- 읽기 전용 분석 프롬프트와 결과 검증
- 분석 결과 조회 API

완료 조건: 예제 오류의 원인 파일과 근거가 저장되고 분석 중 변경 내역이 비어 있음.

### Phase 5 — 승인과 패치

- 승인 API
- 패치 작업
- 쓰기 가능한 패치 작업 트리
- 새 Codex 패치 스레드
- 변경 내역 검증기
- 검증 명령 실행기
- 변경 내역 조회 API

완료 조건: 승인 전 무변경, 승인 후 허용 경로 변경, 테스트 통과 결과만 `PATCH_READY`.

### Phase 6 — 실패와 E2E

- 만료된 작업 회수기와 총 3회 시도하는 재시도 정책
- 시간 초과와 작업 공간 정리
- 중복 승인 테스트
- 전체 종단 간 테스트와 비정상 종료 상황 주입

완료 조건: 프로세스를 재시작하거나 요청이 중복되어도 분석·패치 실행 기록이 하나만 존재함.

## 13. 테스트 전략

### 단위 테스트

- `eventId` 멱등성
- 재현 정보 스키마와 크기 제한
- fingerprint 정규화
- 사건 상태 전이
- 작업 중복 제거 키
- 경로 허용·차단 목록
- 변경량 제한

### 통합 테스트

- 오류 접수 트랜잭션과 분석 작업의 원자성
- `FOR UPDATE SKIP LOCKED` 동시 작업 선점
- 중복 승인 트랜잭션
- 작업 트리 생성과 삭제
- 분석 작업 트리 무변경
- 검증 실패 시 `PATCH_FAILED` 전환

### E2E

결함이 심어진 예제 저장소로 다음 과정을 자동화합니다.

1. 동일 eventId를 3회 전송합니다.
2. 사건 1개, 발생 기록 1개, 분석 실행 기록 1개를 확인합니다.
3. 서로 다른 `eventId`에 같은 지문을 넣어 두 번 전송합니다.
4. 같은 전체 SHA라면 기존 사건에 발생 기록만 2개 추가되고, 다른 SHA라면 별도 사건이 생기는지 확인합니다.
5. 분석 결과의 원인과 근거를 확인합니다.
6. 승인 전 변경 내역이 비어 있는지 확인합니다.
7. 승인합니다.
8. 허용된 경로만 변경되었는지 확인합니다.
9. 테스트 통과와 `PATCH_READY`를 확인합니다.
10. 승인을 다시 호출해 패치 실행 기록이 추가되지 않는지 확인합니다.

일반 CI에서는 가짜 `CodexGateway`를 사용하고 실제 Codex 종단 간 테스트는 수동 또는 별도 CI 작업으로 실행합니다.

## 14. MVP 완료 조건

- 예제 서비스가 설정으로 등록됩니다.
- 유효한 HTTP 오류 이벤트가 `202`로 접수됩니다.
- 같은 eventId의 재시도는 중복 저장되지 않습니다.
- 사건과 분석 작업이 같은 트랜잭션에서 생성됩니다.
- Codex 분석은 읽기 전용 작업 공간에서 실행됩니다.
- 분석 결과에 원인, 근거, 변경 제안, 검증 계획이 포함됩니다.
- 재현 정보가 제공되면 분석 결과에 재현 가능성 판단과 회귀 테스트 제안이 포함됩니다.
- 승인 전에는 쓰기 가능한 패치 작업 공간이 생성되지 않습니다.
- 승인 후에는 별도 패치 작업 트리와 새 Codex 스레드를 사용합니다.
- 정책 밖 변경과 검증 실패는 `PATCH_FAILED`입니다.
- 성공한 패치의 변경 내역과 검증 결과를 API로 조회할 수 있습니다.
- 중복 승인과 작업자 재시작이 중복 패치 실행 기록을 만들지 않습니다.

## 15. 구현 전 의사결정

ID 형식, 지문, 기준 SHA, 승인 대상, 실행·크기 제한, 작업 임대·재시도, 작업자 동시성, Codex 수명 주기, 정책 고정 사본과 통신 경계는 [MVP 의사결정 대장](decision-register.md)과 [결정 기록](decisions/README.md)에 확정했습니다. Phase 1 구현은 시작할 수 있습니다. 설정·마이그레이션·계약 시험과 실제 SDK 통합 검증은 각 구현 단계에서 완료해야 합니다. 값이나 경계를 바꾸려면 결정 대장과 API·DB 계약을 함께 수정합니다.

## 16. 수평 확장 준비와 전환 조건

MVP부터 다음 경계를 지킵니다.

- API는 로컬 작업 흐름 상태를 갖지 않는 무상태 프로세스로 둡니다.
- 작업 선점에는 `FOR UPDATE SKIP LOCKED`와 임대 정보(`locked_by`, `locked_at`, `claim_token`)를 사용합니다.
- 작업 완료 처리는 `claim_token`이 일치할 때만 허용하고 만료된 임대를 회수합니다.
- 작업마다 고유한 작업 트리와 격리된 Codex 실행 컨텍스트를 사용하고 호스트별 동시 실행 수를 제한합니다.
- 저장소 미러 캐시와 쓰기 가능한 작업 트리를 분리합니다.
- 대기열 길이와 대기 시간, 선점 지연 시간, 실행 시간, 실패·재시도, 토큰 비용, 디스크 사용량을 측정합니다.
- 서비스별 요청 빈도·동시 실행 수와 전체 대기 작업 수를 제한해 과부하를 제어합니다.

확장은 API 복제본, PostgreSQL 고가용성·연결 풀, 작업자 복제본·임대, 실행 샌드박스·관측 순서로 진행합니다. PostgreSQL 폴링이나 쓰기 부하가 실제 병목이 되면 트랜잭셔널 아웃박스와 RabbitMQ를 추가합니다. 이때 재시도 대기열, 배달 실패 대기열(DLQ), 반복 실패 메시지 격리, 재처리 도구도 함께 설계합니다.

여러 호스트에서 패치를 실행하려면 컨테이너 또는 가상 머신 샌드박스, 결과 산출물 저장소, 중앙 로그가 필요합니다. 멀티테넌시가 필요해지면 서비스 인증, 테넌트 격리, 역할 기반 접근 제어(RBAC), 비밀값 관리자, 감사 로그를 추가합니다.

## 17. MVP 이후

MVP 완료 뒤 우선순위:

1. GitHub App과 Draft Pull Request
2. 서비스 인증과 승인자 인증
3. Slack 알림
4. 내부 RabbitMQ
5. 멀티테넌시와 운영 인프라

아직 구현하지 않을 기능을 위한 프로비저닝 API나 복잡한 추상화는 MVP에 미리 추가하지 않습니다. 다만 서비스 식별과 작업 대기열은 어댑터 경계로 두어 인증 방식과 RabbitMQ를 나중에 교체할 수 있게 합니다.

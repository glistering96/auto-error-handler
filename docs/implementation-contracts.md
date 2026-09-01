# DB·Worker·Codex 구현 계약

상태: Proposed  
관련 결정: [MVP 의사결정 대장](decision-register.md)

이 문서는 API, Persistence, Worker, Codex 담당자가 서로 다른 가정을 하지 않도록 모듈 사이의 경계를 정의합니다. `OPEN` 결정이 확정되기 전의 수치는 권장 기본값이며, 확정 후 migration과 테스트의 기준이 됩니다.

## 1. 공통 불변 조건

1. PostgreSQL이 Incident, run, Job 상태의 유일한 Source of Truth입니다.
2. API process와 Worker process의 메모리에 복구가 필요한 상태를 저장하지 않습니다.
3. 오류 접수 transaction은 `error_event + incident/occurrence + ANALYZE job`을 원자적으로 저장합니다.
4. 승인 transaction은 `approval + patch_run + PATCH job + incident state`를 원자적으로 저장합니다.
5. Codex 실행과 validation은 DB transaction 밖에서 수행합니다.
6. Job 완료 transaction은 Job, run, Incident를 함께 terminal 상태로 전환합니다.
7. 승인 전에는 writable worktree와 PATCH Job을 만들지 않습니다.

## 2. 식별자와 시간

- DEC-004가 확정되기 전 DB 초안은 모든 내부 ID를 PostgreSQL `uuid`로 가정합니다.
- 외부 `eventId`는 Producer가 만든 UUID입니다.
- 모든 시간은 `timestamptz`로 저장하고 애플리케이션에서는 UTC로 처리합니다.
- API는 RFC 3339 UTC 문자열을 반환합니다.
- 순서는 timestamp 하나에 의존하지 않고 `(created_at, id)`를 함께 사용합니다.

## 3. 제안 DB Schema v1

공통 규칙:

- 모든 테이블에 `created_at timestamptz NOT NULL DEFAULT now()`를 둡니다.
- 갱신되는 테이블은 `updated_at timestamptz NOT NULL DEFAULT now()`를 둡니다.
- enum은 첫 migration에서는 PostgreSQL enum 또는 CHECK 중 하나로 통일합니다. 문자열 자유 입력은 허용하지 않습니다.
- 원본 secret을 저장하지 않으며 log/output은 저장 전에 크기 제한과 redaction을 적용합니다.

### `services`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 내부 ID |
| `key` | varchar(100) | N | Producer의 `serviceKey` |
| `repository_path` | text | N | 허용 root 아래 정규화된 로컬 경로 |
| `default_branch` | varchar(255) | N | fallback branch |
| `policy` | jsonb | N | typed service policy의 직렬화 |
| `active` | boolean | N | 기본값 true |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

제약: `UNIQUE(key)`.

### `error_events`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 내부 ID |
| `service_id` | uuid FK | N | `services.id`, delete restrict |
| `event_id` | uuid | N | Producer idempotency ID |
| `payload_checksum` | char(64) | N | canonical JSON SHA-256 |
| `normalized_payload` | jsonb | N | Schema 검증·redaction 후 payload |
| `received_at` | timestamptz | N | 서버 접수 시각 |
| `created_at` | timestamptz | N | 생성 시각 |

제약: `UNIQUE(service_id, event_id)`.

### `incidents`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | Incident ID |
| `service_id` | uuid FK | N | `services.id`, delete restrict |
| `fingerprint` | varchar(200) | N | DEC-005 결과 |
| `state` | incident_state | N | 상태 머신 값 |
| `version` | integer | N | 기본값 1, optimistic lock |
| `base_commit_sha` | char(40) | Y | 분석 기준이 정해진 후 저장 |
| `occurrence_count` | integer | N | 기본값 1, 1 이상 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

인덱스:

```sql
CREATE INDEX ix_incidents_grouping
ON incidents (service_id, fingerprint, state);

CREATE INDEX ix_incidents_list
ON incidents (created_at DESC, id DESC);
```

같은 fingerprint의 terminal Incident를 다시 열지 여부는 MVP 이후로 미룹니다. MVP는 열린 상태만 grouping 대상으로 사용합니다.

### `occurrences`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | occurrence ID |
| `incident_id` | uuid FK | N | `incidents.id`, delete restrict |
| `error_event_id` | uuid FK | N | `error_events.id`, delete restrict |
| `occurred_at` | timestamptz | N | Producer 오류 발생 시각 |
| `created_at` | timestamptz | N | 생성 시각 |

제약: `UNIQUE(error_event_id)`.

### `analysis_runs`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | run ID |
| `incident_id` | uuid FK | N | Incident |
| `status` | run_status | N | PENDING/RUNNING/SUCCEEDED/FAILED |
| `base_commit_sha` | char(40) | N | 실제 분석 SHA |
| `codex_thread_id` | text | Y | SDK thread ID |
| `result` | jsonb | Y | OpenAPI `AnalysisResult` |
| `error_code` | varchar(64) | Y | 고정 error catalog 값 |
| `error_detail` | text | Y | redacted·truncated detail |
| `started_at` | timestamptz | Y | 시작 시각 |
| `finished_at` | timestamptz | Y | 종료 시각 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

MVP는 Incident당 analysis run 하나를 사용합니다. 재분석을 추가할 때 active partial unique index를 도입합니다.

### `approvals`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | approval ID |
| `incident_id` | uuid FK | N | Incident |
| `analysis_run_id` | uuid FK | N | 승인한 분석 |
| `idempotency_key` | uuid | N | 승인 요청 멱등성 key |
| `base_commit_sha` | char(40) | N | 승인한 기준 SHA |
| `created_at` | timestamptz | N | 승인 시각 |

제약: `UNIQUE(incident_id)`, `UNIQUE(idempotency_key)`. 승인자 identity는 인증 도입 전까지 저장하지 않습니다.

### `patch_runs`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | patch run ID |
| `incident_id` | uuid FK | N | Incident |
| `approval_id` | uuid FK | N | Approval |
| `status` | patch_status | N | PENDING/RUNNING/VALIDATING/PATCH_READY/PATCH_FAILED |
| `base_commit_sha` | char(40) | N | Approval에서 복사 |
| `worktree_path` | text | Y | root 상대 경로 또는 내부 경로 |
| `changed_files` | jsonb | N | 기본값 빈 배열 |
| `diff_text` | text | Y | 정책 검사한 diff |
| `validation_result` | jsonb | N | 기본값 빈 배열 |
| `error_code` | varchar(64) | Y | 고정 error catalog 값 |
| `error_detail` | text | Y | redacted·truncated detail |
| `started_at` | timestamptz | Y | 시작 시각 |
| `finished_at` | timestamptz | Y | 종료 시각 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

제약: `UNIQUE(incident_id)`, `UNIQUE(approval_id)`.

### `jobs`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | Job ID |
| `type` | job_type | N | ANALYZE/PATCH |
| `deduplication_key` | varchar(200) | N | 결정적 unique key |
| `payload` | jsonb | N | ID만 포함하는 versioned payload |
| `status` | job_status | N | PENDING/RUNNING/SUCCEEDED/FAILED |
| `attempt` | integer | N | claim할 때 증가, 기본값 0 |
| `max_attempts` | integer | N | 권장 3 |
| `available_at` | timestamptz | N | claim 가능한 시각 |
| `locked_by` | varchar(200) | Y | Worker instance ID |
| `locked_at` | timestamptz | Y | lease 시작/heartbeat 시각 |
| `last_error_code` | varchar(64) | Y | 마지막 실패 분류 |
| `last_error_detail` | text | Y | redacted·truncated detail |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

필수 인덱스:

```sql
CREATE UNIQUE INDEX uq_jobs_deduplication_key
ON jobs (deduplication_key);

CREATE INDEX ix_jobs_claim
ON jobs (status, available_at, created_at)
WHERE status = 'PENDING';

CREATE INDEX ix_jobs_stale
ON jobs (locked_at)
WHERE status = 'RUNNING';
```

Job payload 예시:

```json
{ "version": 1, "incidentId": "...", "analysisRunId": "..." }
```

Repository 경로, prompt, 오류 원문을 Job payload에 복제하지 않고 ID로 조회합니다.

## 4. Worker 실행 계약

### Claim

- 한 coroutine은 한 번에 Job 하나를 claim합니다.
- claim transaction은 Job을 `RUNNING`으로 변경하고 `attempt`, `locked_by`, `locked_at`을 기록한 뒤 즉시 commit합니다.
- Codex와 Git 작업은 claim transaction이 끝난 뒤 시작합니다.
- 여러 Worker는 `FOR UPDATE SKIP LOCKED`로 같은 Job을 동시에 가져가지 않습니다.

### Lease와 heartbeat

- DEC-011 권장값은 lease 120초, heartbeat 30초입니다.
- heartbeat는 `id + locked_by + status=RUNNING` 조건으로 `locked_at`만 갱신합니다.
- 다른 Worker가 소유권을 바꾼 경우 실행 결과를 finalize하지 않고 폐기합니다.
- Sweeper는 lease가 지난 Job을 retry 가능한 PENDING 또는 terminal FAILED로 전환합니다.

### Retry

```text
retryable infrastructure failure
  → attempt < max_attempts: PENDING + available_at(backoff)
  → attempt >= max_attempts: FAILED

domain/policy/validation failure
  → 즉시 FAILED
```

권장 backoff는 10초, 60초입니다. 재시도는 기존 run을 재사용하며 새 approval·patch run을 만들지 않습니다.

### Finalize

성공/실패 transaction은 다음 조건을 확인합니다.

1. Job이 여전히 `RUNNING`이고 `locked_by`가 현재 Worker입니다.
2. run이 예상한 non-terminal 상태입니다.
3. Incident `version`이 handler가 읽은 값과 같습니다.
4. Job, run, Incident 상태를 함께 갱신합니다.
5. 갱신 row가 예상과 다르면 결과를 덮어쓰지 않고 conflict로 기록합니다.

### Graceful shutdown

- 새 claim을 중단합니다.
- 실행 중 handler에 취소 신호를 보냅니다.
- 제한 시간 안에 종료되지 않으면 child process를 종료합니다.
- 완료 transaction이 commit되지 않은 Job은 lease 만료 후 Sweeper가 복구합니다.

## 5. CodexGateway 계약

공식 OpenAI 문서에 따르면 Python `openai-codex` SDK는 로컬 Codex app-server를 JSON-RPC로 제어하고, 배포 SDK에는 pinned Codex CLI runtime dependency가 포함됩니다. `AsyncCodex` context, thread, `Sandbox.read_only`와 `Sandbox.workspace_write`를 사용할 수 있습니다. 실제 child process lifecycle과 강제 종료 동작은 SDK 버전을 고정한 integration spike로 검증합니다.

공식 문서: <https://developers.openai.com/codex/codex-sdk>

```python
class CodexGateway(Protocol):
    async def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...
    async def create_patch(self, request: PatchRequest) -> PatchResult: ...
```

### `AnalysisRequest`

- incident ID와 analysis run ID
- 절대 경로가 아닌 Workspace Manager가 검증한 worktree handle
- base commit SHA
- 정규화된 오류, stack trace, release, reproduction context
- runbook path 목록
- timeout과 structured output schema version

### `AnalysisResult`

OpenAPI의 `AnalysisResult`를 그대로 사용합니다. SDK 응답은 저장 전에 Pydantic으로 검증하고, 실패하면 `AEH-CODEX-502-002`로 분류합니다.

### `PatchRequest`

- approval ID와 patch run ID
- 새 writable worktree handle
- approval에 고정된 base commit SHA
- 검증된 `AnalysisResult`
- allowed/denied path와 diff 제한
- validation command는 prompt에 실행 요청으로 넣지 않고 별도 Validation Runner가 수행

### 실행 격리

| 단계 | Thread | Sandbox | 쓰기 |
|---|---|---|---|
| Analysis | 새 thread | `read_only` | 금지 |
| Patch | Analysis와 다른 새 thread | `workspace_write` | 해당 worktree만 |

- `full_access`는 사용하지 않습니다.
- Patch Runner에 Git push credential, production secret을 주입하지 않습니다.
- timeout이면 SDK context와 child process 종료를 시도하고 worktree를 정리합니다.
- Codex final response만 신뢰하지 않고 실제 Git diff를 다시 읽어 검증합니다.

## 6. Workspace Manager 계약

```python
class WorkspaceManager(Protocol):
    async def create_read_only(self, service_id: UUID, sha: str) -> Workspace: ...
    async def create_writable(self, patch_run_id: UUID, sha: str) -> Workspace: ...
    async def remove(self, workspace: Workspace) -> None: ...
```

- 요청 SHA가 repository에 실제 존재하는지 확인합니다.
- worktree 경로는 애플리케이션이 생성하며 event 입력을 경로에 사용하지 않습니다.
- 같은 repository의 mirror/update 작업에는 repository 단위 lock을 사용합니다.
- symlink·submodule 검사는 Codex 실행 전과 diff 검증 시 모두 수행합니다.
- cleanup 실패는 원래 Job 결과를 덮어쓰지 않고 별도 metric/log로 남깁니다.

## 7. 오류 코드 최소 Catalog

| Code | HTTP/Job | 의미 | Retry |
|---|---|---|---:|
| `AEH-EVENT-400-001` | 400 | header/body 형식 오류 | N |
| `AEH-EVENT-404-001` | 404 | 등록되지 않은 serviceKey | N |
| `AEH-EVENT-409-001` | 409 | 같은 eventId, 다른 payload | N |
| `AEH-EVENT-422-001` | 422 | JSON Schema 실패 | N |
| `AEH-APPROVAL-409-001` | 409 | 승인 불가능 상태 | N |
| `AEH-APPROVAL-409-002` | 409 | 분석 run 또는 SHA 불일치 | N |
| `AEH-GIT-422-001` | Job | commit을 찾을 수 없음 | N |
| `AEH-CODEX-502-001` | Job | app-server/transport 일시 실패 | Y |
| `AEH-CODEX-502-002` | Job | 구조화 응답 검증 실패 | N |
| `AEH-POLICY-422-001` | Job | 허용 경로·diff 제한 위반 | N |
| `AEH-VALIDATION-422-001` | Job | 등록된 검증 명령 실패 | N |
| `AEH-JOB-504-001` | Job | 실행 timeout | 정책에 따라 1회 |
| `AEH-INTERNAL-500-001` | 500/Job | 분류되지 않은 내부 오류 | 기본 N |

API 오류 body는 OpenAPI `Problem` Schema를 따르며 stack trace와 secret은 반환하지 않습니다.

## 8. 구현 검증 테스트

- 두 Worker가 동시에 claim해도 한 Job만 실행됩니다.
- Worker가 Codex 실행 중 죽으면 lease 만료 뒤 같은 Job이 복구됩니다.
- 이전 Worker가 늦게 결과를 반환해도 새 lease 소유자의 결과를 덮어쓰지 못합니다.
- 승인된 SHA와 Patch worktree SHA가 항상 같습니다.
- Analysis 종료 후 Git diff가 비어 있습니다.
- Patch 결과가 allowed path 밖을 변경하면 validation command를 실행하지 않고 실패합니다.
- Codex 응답 Schema 실패, timeout, validation 실패가 서로 다른 error code로 저장됩니다.

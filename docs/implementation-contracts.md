# DB·작업자·Codex 구현 계약

상태: 최초 구현 계약 기록. 현재 DB별 동시성 변경은 [ADR-0003](decisions/0003-sqlite-local-postgresql-scaleout-and-review.md)이 우선
관련 결정: [MVP 의사결정 대장](decision-register.md)

이 문서는 API, 영속성, 작업자, Codex 담당자가 서로 다른 가정을 하지 않도록 모듈 사이의 경계를 정의합니다. 결정 대장의 `ACCEPTED` 값이 우선하며, 스키마 절은 최초 설계의 상세 기록입니다.

## 1. 공통 불변 조건

1. 로컬 기본 SQLite 또는 다중 호스트용 PostgreSQL 중 선택한 DB를 사건·실행·작업 상태의 단일 기준 정보로 사용합니다.
2. API와 작업자 프로세스의 메모리에는 복구해야 하는 상태를 저장하지 않습니다.
3. 오류 접수 트랜잭션은 이벤트, 사건·발생 기록, `ANALYZE` 작업을 원자적으로 저장합니다.
4. 승인 트랜잭션은 승인, 패치 실행, `PATCH` 작업, 사건 상태를 원자적으로 저장합니다.
5. Codex 실행과 검증은 DB 트랜잭션 밖에서 수행합니다.
6. 작업 완료 트랜잭션은 작업·실행·사건을 함께 종료 상태로 전환합니다.
7. 승인 전에는 쓰기 가능한 작업 트리와 `PATCH` 작업을 만들지 않습니다.

## 2. 식별자와 시간

- 모든 내부 ID는 UUIDv4이며 PostgreSQL에서는 `uuid`, SQLite에서는 동일 값을 문자형 UUID로 저장합니다.
- 외부 `eventId`는 오류 이벤트를 보내는 서비스가 만든 UUID입니다.
- 모든 시간은 애플리케이션에서 UTC로 처리합니다. PostgreSQL은 `timestamptz`, SQLite는 UTC 시각으로 직렬화한 날짜·시간 값을 저장합니다.
- API는 RFC 3339 UTC 문자열을 반환합니다.
- 정렬 순서는 시각 값 하나에만 의존하지 않고 `(created_at, id)`를 함께 사용합니다.

## 3. 제안 DB 스키마 v1

공통 규칙:

- 모든 테이블에 UTC `created_at`과 DB의 현재 시각 기본값을 둡니다.
- 갱신되는 테이블에 UTC `updated_at`을 둡니다.
- 상태 값은 두 DB에서 동일한 `CHECK` 제약으로 제한합니다. 임의의 문자열 입력은 허용하지 않습니다.
- 원본 비밀값을 저장하지 않습니다. 로그와 출력은 저장하기 전에 용량을 제한하고 민감 정보를 제거합니다.

### `services`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 내부 ID |
| `key` | varchar(100) | N | Producer의 `serviceKey` |
| `repository_path` | text | N | 허용된 최상위 경로 아래의 정규화된 로컬 경로 |
| `default_branch` | varchar(255) | N | 커밋 SHA가 없을 때 사용할 기본 브랜치 |
| `policy` | jsonb | N | 형식화된 서비스 정책의 직렬화 결과 |
| `active` | boolean | N | 기본값 true |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

제약: `UNIQUE(key)`.

`repository_path` 갱신은 해당 서비스에 처리 중인 사건이 없을 때만 허용합니다. 서비스 행을 잠그고 활성 사건 존재 여부를 같은 트랜잭션에서 검사합니다. 새 사건을 만드는 접수도 같은 서비스 행을 잠가 경로 변경과 직렬화합니다. 다른 정책 필드의 갱신은 허용하며 이미 시작한 분석에는 정책 고정 사본을 적용합니다.

### `error_events`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 내부 ID |
| `service_id` | uuid FK | N | `services.id`, delete restrict |
| `event_id` | uuid | N | 이벤트 발신자가 만든 멱등성 ID |
| `payload_checksum` | char(64) | N | RFC 8785로 정규화한 요청 JSON의 UTF-8 바이트에 대한 SHA-256 체크섬 |
| `normalized_payload` | jsonb | N | 스키마를 검증하고 민감 정보를 제거한 요청 데이터 |
| `received_at` | timestamptz | N | 서버 접수 시각 |
| `created_at` | timestamptz | N | 생성 시각 |

제약: `UNIQUE(service_id, event_id)`.

### `incidents`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 사건 ID |
| `service_id` | uuid FK | N | `services.id`, delete restrict |
| `fingerprint` | varchar(200) | N | Producer가 보낸 지문을 공백 제거·NFC 정규화한 값 |
| `state` | incident_state | N | 상태 머신 값 |
| `version` | integer | N | 기본값 1, 낙관적 잠금에 사용 |
| `repository_path_snapshot` | text | N | SHA를 검증한 접수 당시 정규화된 저장소 경로 |
| `base_commit_sha` | varchar(64) | N | 접수 과정에서 검증한 전체 커밋 객체 ID |
| `first_error_event_id` | uuid FK | N | 사건을 처음 만든 `error_events.id` |
| `occurrence_count` | integer | N | 기본값 1, 1 이상 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

인덱스:

```sql
CREATE UNIQUE INDEX uq_incidents_open_grouping
ON incidents (service_id, fingerprint, base_commit_sha)
WHERE state IN ('RECEIVED', 'ANALYZING', 'AWAITING_APPROVAL', 'PATCHING', 'VALIDATING');

CREATE INDEX ix_incidents_list
ON incidents (created_at DESC, id DESC);
```

Git 조회와 SHA 검증은 DB 트랜잭션 밖에서 끝냅니다. 신규 사건의 접수 트랜잭션에는 SHA를 검증한 정규화된 저장소 경로와 확정한 SHA를 이벤트·발생 기록·`ANALYZE` 작업과 함께 저장합니다. 부분 고유 인덱스 위반이 발생하면 해당 트랜잭션을 정리한 뒤 같은 서비스·지문·SHA의 처리 중인 사건을 다시 조회하고 발생 기록만 추가합니다. 애플리케이션에서 먼저 조회하는 것만으로 사건 묶기의 유일성을 보장하지 않습니다.

같은 `(service_id, event_id)`의 기존 이벤트가 있으면 요청 데이터 체크섬을 먼저 비교합니다. 같으면 Git을 다시 조회하지 않고 발생 기록으로 연결된 기존 사건 ID와 현재 상태를 `duplicate=true`로 반환하며, 다르면 `409`를 반환합니다. 새 이벤트의 제공된 SHA가 커밋을 가리키지 않으면 `422`, 저장소 일시 장애는 `503`이고 어느 경우에도 이벤트 일부를 저장하지 않습니다.

요청 본문은 256 KiB를 JSON 파싱 전에 검사합니다. 객체 키 중복은 `400`으로 거부합니다. 체크섬은 [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html)로 정규화한 요청 JSON에서 계산하며, 정규화할 수 없는 수치는 `422`로 거부합니다. 민감 정보를 제거하기 전에 체크섬을 계산하되 원본 요청과 정규화 바이트는 영구 저장하지 않습니다. 배열 순서와 문자열 값은 비교에서 보존하고, 객체 키 순서와 공백 차이는 무시합니다.

같은 지문의 종료된 사건을 다시 열지는 MVP 이후에 결정합니다. MVP에서는 처리 중인 상태만 사건 묶기 대상으로 사용합니다.

### `occurrences`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | occurrence ID |
| `incident_id` | uuid FK | N | `incidents.id`, delete restrict |
| `error_event_id` | uuid FK | N | `error_events.id`, delete restrict |
| `occurred_at` | timestamptz | N | 발신 서비스에서 오류가 발생한 시각 |
| `created_at` | timestamptz | N | 생성 시각 |

제약: `UNIQUE(error_event_id)`.

### `analysis_runs`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | run ID |
| `incident_id` | uuid FK | N | 사건 |
| `status` | run_status | N | PENDING/RUNNING/SUCCEEDED/FAILED |
| `base_commit_sha` | varchar(64) | N | 저장소에서 검증한 전체 객체 ID |
| `input_error_event_id` | uuid FK | N | 최초 사건을 만든 이벤트. 재시도해도 유지 |
| `policy_snapshot` | jsonb | N | 첫 분석 선점 때 검증해 저장한 실행 정책 고정 사본 |
| `codex_thread_id` | text | Y | SDK 스레드 ID |
| `result` | jsonb | Y | OpenAPI `AnalysisResult` |
| `error_code` | varchar(64) | Y | 고정된 오류 코드 목록의 값 |
| `error_detail` | text | Y | 민감 정보와 초과 내용을 제거한 상세 내용 |
| `started_at` | timestamptz | Y | 시작 시각 |
| `finished_at` | timestamptz | Y | 종료 시각 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

MVP에서는 사건마다 분석 실행 기록 하나만 사용합니다. 첫 선점 때 `incidents.first_error_event_id`를 `input_error_event_id`에, `incidents.repository_path_snapshot`을 정책 고정 사본의 저장소 경로에 복사하고 `policy_snapshot`을 함께 저장합니다. 재시도에서는 이 값을 다시 만들지 않습니다. 고정 사본에는 정규화한 저장소 경로, 실행 지침 경로, 허용·차단 경로, 변경량·시간 제한, 검증 명령의 인자 배열·제한 시간, 모델, 결과 형식 버전, 외부 통신 규칙이 들어갑니다. 분석 조회 API의 `inputEventId`는 이 기록이 가리키는 외부 `eventId`이며, `policySummary`에는 내부 절대 경로와 비밀값을 뺀 고정 정책 요약을 반환합니다. 검증 명령은 `id`와 제한 시간만 노출하고 원본 인자 배열은 노출하지 않습니다. 재분석 기능을 추가할 때 활성 실행을 위한 부분 고유 인덱스를 도입합니다.

### `approvals`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | approval ID |
| `incident_id` | uuid FK | N | 사건 |
| `analysis_run_id` | uuid FK | N | 승인한 분석 |
| `idempotency_key` | uuid | N | 승인 요청 멱등성 키 |
| `base_commit_sha` | varchar(64) | N | 저장소에서 검증한 전체 객체 ID |
| `created_at` | timestamptz | N | 승인 시각 |

제약: `UNIQUE(incident_id)`, `UNIQUE(idempotency_key)`. 인증을 도입하기 전까지는 승인자 식별 정보를 저장하지 않습니다.

승인 처리 규칙:

- 승인 API는 요청 본문을 받지 않고 `incidentId` 경로 변수와 `Idempotency-Key` 헤더만 사용합니다.
- 같은 트랜잭션에서 사건을 `FOR UPDATE`로 잠그고 상태가 `AWAITING_APPROVAL`인지 확인합니다.
- 해당 사건에 `SUCCEEDED` 상태의 분석 실행이 정확히 하나일 때만 승인합니다.
- 서버가 분석 실행 ID와 `base_commit_sha`를 승인·패치 실행 기록에 복사합니다.
- 승인·패치·검증은 승인한 분석의 `policy_snapshot`을 사용합니다. 현재 `services.policy`는 다시 읽지 않습니다.
- 같은 `Idempotency-Key`로 다시 요청하면 기존 결과를 반환합니다. 이미 승인한 사건을 다른 키로 다시 승인하거나 같은 키를 다른 사건에 사용하면 `409`를 반환합니다.

### `patch_runs`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 패치 실행 ID |
| `incident_id` | uuid FK | N | 사건 |
| `approval_id` | uuid FK | N | Approval |
| `status` | patch_status | N | PENDING/RUNNING/VALIDATING/PATCH_READY/PATCH_FAILED |
| `base_commit_sha` | varchar(64) | N | 승인 기록에서 복사한 전체 객체 ID |
| `worktree_path` | text | Y | 최상위 경로 기준 상대 경로 또는 내부 경로 |
| `changed_files` | jsonb | N | 기본값 빈 배열 |
| `diff_text` | text | Y | 정책 검사를 마친 변경 내역 |
| `validation_result` | jsonb | N | 기본값 빈 배열 |
| `error_code` | varchar(64) | Y | 고정된 오류 코드 목록의 값 |
| `error_detail` | text | Y | 민감 정보와 초과 내용을 제거한 상세 내용 |
| `started_at` | timestamptz | Y | 시작 시각 |
| `finished_at` | timestamptz | Y | 종료 시각 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

제약: `UNIQUE(incident_id)`, `UNIQUE(approval_id)`.

### `jobs`

| Column | Type | Null | 설명 |
|---|---|---:|---|
| `id` | uuid PK | N | 작업 ID |
| `type` | job_type | N | ANALYZE/PATCH |
| `deduplication_key` | varchar(200) | N | 결정적 고유 키 |
| `payload` | jsonb | N | ID만 포함하는 버전이 지정된 요청 데이터 |
| `status` | job_status | N | PENDING/RUNNING/SUCCEEDED/FAILED |
| `attempt` | integer | N | 선점할 때 증가, 기본값 0 |
| `max_attempts` | integer | N | 총 3회 시도 |
| `available_at` | timestamptz | N | 선점할 수 있는 시각 |
| `locked_by` | varchar(200) | Y | 작업자 인스턴스 ID |
| `locked_at` | timestamptz | Y | 임대 시작 또는 마지막 생존 신호 시각 |
| `claim_token` | uuid | Y | 선점할 때마다 새로 발급하는 실행 소유권 |
| `last_error_code` | varchar(64) | Y | 마지막 실패 분류 |
| `last_error_detail` | text | Y | 민감 정보와 초과 내용을 제거한 상세 내용 |
| `created_at` | timestamptz | N | 생성 시각 |
| `updated_at` | timestamptz | N | 갱신 시각 |

필수 인덱스:

```sql
CREATE UNIQUE INDEX uq_jobs_deduplication_key
ON jobs (deduplication_key);

CREATE INDEX ix_jobs_claim
ON jobs (status, available_at, created_at, id)
WHERE status = 'PENDING';

CREATE INDEX ix_jobs_stale
ON jobs (locked_at)
WHERE status = 'RUNNING';
```

작업 요청 데이터 예시:

```json
{ "version": 1, "incidentId": "...", "analysisRunId": "..." }
```

저장소 경로, 프롬프트, 오류 원문은 작업 요청 데이터에 복제하지 않고 ID로 조회합니다.

## 4. 작업자 실행 계약

### 작업 선점

- 코루틴 하나는 한 번에 작업 하나만 선점합니다.
- 선점 트랜잭션은 작업을 `RUNNING`으로 바꾸고 `attempt`, `locked_by`, `locked_at`, 새 UUID `claim_token`을 기록한 뒤 즉시 커밋합니다.
- Codex와 Git 작업은 선점 트랜잭션이 끝난 뒤 시작합니다.
- PostgreSQL의 여러 작업자는 `FOR UPDATE SKIP LOCKED`, SQLite 작업자는 `BEGIN IMMEDIATE` 쓰기 트랜잭션을 사용해 같은 작업을 동시에 가져가지 않습니다.
- 선점 순서는 `(available_at, created_at, id)`로 고정하고 부분 인덱스를 사용합니다. `SKIP LOCKED`로 건너뛴 오래된 작업은 임대 회수기가 다시 가져와야 합니다.
- 새 `ANALYZE` 실행을 처음 선점할 때 서비스 정책 고정 사본과 사건의 첫 이벤트 ID를 분석 실행 기록에 함께 저장합니다. 이후 재시도에서는 둘 다 유지합니다.
- 기본 동시 실행 수는 1입니다. `PENDING`과 `RUNNING`을 합친 미완료 작업은 최대 100개이며, 새 사건이나 승인이 작업을 추가하기 전에 상한을 확인합니다. PostgreSQL은 고정 자문 잠금, SQLite는 즉시 시작하는 쓰기 트랜잭션으로 이 검사를 직렬화합니다. 상한에 도달하면 새 작업을 저장하지 않고 `503`을 반환합니다. 중복 이벤트는 기존 사건 ID와 현재 상태를 반환합니다.

### 임대와 생존 신호

- DEC-011에 따라 임대 시간은 120초입니다. 생존 신호는 30초마다 DB 시간을 기준으로 `locked_at`을 갱신합니다.
- 생존 신호는 `id + claim_token + status=RUNNING` 조건이 모두 맞을 때만 갱신합니다.
- DB 연결이 일시적으로 끊기면 임대 만료 시각 전까지 짧은 간격으로 다시 시도합니다. 갱신된 행이 없거나 만료 전까지 갱신하지 못하면 현재 Codex 실행을 취소하고 결과를 폐기합니다.
- 회수기는 마지막 생존 신호 이후 120초가 지난 작업만 회수합니다. 회수할 때 이전 `claim_token`을 지우고, 시도 횟수가 남아 있으면 `PENDING`, 모두 사용했으면 `FAILED`로 전환합니다.

### 재시도

```text
재시도 가능한 기반 시설 오류
  → attempt < max_attempts: PENDING + available_at(backoff)
  → attempt >= max_attempts: FAILED

도메인·정책·검증 실패
  → 즉시 FAILED

분석 최대 실행 시간 24시간 초과
  → 즉시 FAILED, 자동 재시도 없음
```

재시도 대기 시간은 10초, 60초입니다. 일시적인 기반 시설 오류만 총 3회까지 시도합니다. 재시도할 때 기존 실행 기록을 재사용하고 새 승인·패치 실행 기록은 만들지 않습니다. 다만 `claim_token`, Codex 스레드, 작업 트리는 새로 만듭니다.

분석 하나의 최대 실행 시간은 24시간입니다. 생존 신호가 계속 성공하더라도 이 시간을 넘으면 Codex 실행을 취소하고 작업·분석 실행·사건을 최종 실패로 전환합니다.

### 완료 처리

성공·실패 트랜잭션은 다음 조건을 확인합니다.

1. 작업이 여전히 `RUNNING`이고 `claim_token`이 현재 실행의 토큰과 같습니다.
2. 실행 기록이 예상한 미종료 상태입니다.
3. 사건의 `version`이 처리기가 읽은 값과 같습니다.
4. 작업·실행·사건 상태를 함께 갱신합니다.
5. 갱신된 행 수가 예상과 다르면 결과를 덮어쓰지 않고 충돌로 기록합니다.

### 정상 종료

- 새 작업 선점을 중단합니다.
- 실행 중인 처리기에 취소 신호를 보냅니다.
- 제한 시간 안에 종료되지 않으면 하위 프로세스를 종료합니다.
- 완료 트랜잭션이 커밋되지 않은 작업은 임대 만료 후 회수기가 복구합니다. 이전 토큰을 가진 실행은 결과를 저장할 수 없습니다.

## 5. CodexGateway 계약

공식 OpenAI 문서에 따르면 Python `openai-codex` SDK는 로컬 Codex 앱 서버를 JSON-RPC로 제어하며, 배포용 SDK에는 버전이 고정된 Codex CLI 실행 환경 의존성이 포함됩니다. `AsyncCodex` 컨텍스트와 스레드, `Sandbox.read_only`, `Sandbox.workspace_write`를 사용할 수 있습니다. 실제 하위 프로세스의 수명 주기와 강제 종료 동작은 SDK 버전을 고정한 뒤 통합 검증으로 확인합니다.

공식 문서: <https://developers.openai.com/codex/codex-sdk>

```python
class CodexGateway(Protocol):
    async def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...
    async def create_patch(self, request: PatchRequest) -> PatchResult: ...
```

### `AnalysisRequest`

- 사건 ID와 분석 실행 ID
- 절대 경로가 아닌, 작업 공간 관리자가 검증한 작업 트리 참조값
- 기준 커밋 SHA
- 정규화된 오류, 스택 추적, 릴리스, 재현 정보
- 재현에 필요한 `reproductionData`의 요청 입력, 예제 데이터 초기값, 기능 플래그
- 첫 사건 접수에 사용한 `input_error_event_id`와 정책 고정 사본
- 실행 지침 경로 목록
- 시간 제한과 구조화된 출력 스키마 버전

### `AnalysisResult`

OpenAPI의 `AnalysisResult`를 그대로 사용합니다. SDK 응답은 저장 전에 Pydantic으로 검증하고, 실패하면 `AEH-CODEX-502-002`로 분류합니다.

### `PatchRequest`

- 승인 ID와 패치 실행 ID
- 새 쓰기 가능 작업 트리 참조값
- 승인 기록에 고정된 기준 커밋 SHA
- 검증된 `AnalysisResult`
- 허용·차단 경로와 변경량 제한
- 승인된 분석 실행의 `policy_snapshot`; 현재 `services.policy`를 다시 읽지 않음
- 검증 명령은 프롬프트에 실행 요청으로 넣지 않고 별도 검증 명령 실행기가 수행

### 실행 격리

| 단계 | 스레드 | 샌드박스 | 쓰기 |
|---|---|---|---|
| 분석 | 새 스레드 | `read_only` | 금지 |
| 패치 | 분석과 다른 새 스레드 | `workspace_write` | 해당 작업 트리만 |

- `full_access`는 사용하지 않습니다.
- 패치 실행기에 Git 전송 인증 정보와 운영 비밀값을 주입하지 않습니다.
- Codex 앱 서버가 모델 서비스에 연결하는 통신은 허용하고, Codex가 실행하는 명령의 외부 접속은 명시적으로 차단합니다. 별도 검증 명령도 네트워크가 차단된 실행 환경에서 수행합니다.
- 시간이 초과되면 SDK 컨텍스트와 하위 프로세스 종료를 시도하고 작업 트리를 정리합니다.
- Codex 최종 응답만 신뢰하지 않고 실제 Git 변경 내역을 다시 읽어 검증합니다.

## 6. 작업 공간 관리자 계약

```python
class WorkspaceManager(Protocol):
    async def create_read_only(self, service_id: UUID, sha: str) -> Workspace: ...
    async def create_writable(self, patch_run_id: UUID, sha: str) -> Workspace: ...
    async def remove(self, workspace: Workspace) -> None: ...
```

- 요청한 SHA가 저장소에 실제로 존재하는지 확인합니다.
- 작업 트리 경로는 애플리케이션이 생성하며 이벤트 입력값을 경로에 사용하지 않습니다.
- 같은 저장소의 미러·갱신 작업에는 저장소 단위 잠금을 사용합니다.
- 심볼릭 링크·하위 모듈 검사는 Codex 실행 전과 변경 내역 검증 시 모두 수행합니다.
- 정리 실패는 원래 작업 결과를 덮어쓰지 않고 별도 지표와 로그로 남깁니다.

## 7. 최소 오류 코드 목록

| 코드 | HTTP/작업 | 의미 | 재시도 |
|---|---|---|---:|
| `AEH-EVENT-400-001` | 400 | 헤더·요청 본문 형식 오류 | N |
| `AEH-EVENT-413-001` | 413 | 요청 본문 용량 제한 초과 | N |
| `AEH-EVENT-404-001` | 404 | 등록되지 않은 serviceKey | N |
| `AEH-EVENT-409-001` | 409 | 같은 `eventId`에 다른 요청 데이터 | N |
| `AEH-EVENT-422-001` | 422 | JSON Schema 실패 또는 JSON 정규화 불가 | N |
| `AEH-APPROVAL-409-001` | 409 | 승인 불가능 상태 | N |
| `AEH-APPROVAL-409-002` | 409 | 분석 실행 또는 SHA 불일치 | N |
| `AEH-GIT-422-001` | 422/작업 | 커밋을 찾을 수 없음 | N |
| `AEH-GIT-503-001` | 503 | 접수 중 저장소 일시 장애 | Y |
| `AEH-JOB-503-001` | 503 | 미완료 작업 상한 도달 | Y |
| `AEH-CODEX-502-001` | 작업 | 앱 서버 또는 통신의 일시적 실패 | Y |
| `AEH-CODEX-502-002` | 작업 | 구조화된 응답 검증 실패 | N |
| `AEH-POLICY-422-001` | 작업 | 허용 경로·변경량 제한 위반 | N |
| `AEH-VALIDATION-422-001` | 작업 | 등록된 검증 명령 실패 | N |
| `AEH-JOB-504-001` | 작업 | 분석 최대 실행 시간 24시간 초과 | N |
| `AEH-INTERNAL-500-001` | 500/작업 | 분류되지 않은 내부 오류 | 기본 N |

API 오류 응답 본문은 OpenAPI의 `Problem` 스키마를 따르며 스택 추적과 비밀값은 반환하지 않습니다.

### 입력과 산출물 상한

- 요청 원문은 JSON 파싱 전에 256 KiB로 제한합니다. `reproductionData`의 깊이는 자체를 0으로 셌을 때 최대 8, 전체 JSON 값은 1,000개, 개별 문자열은 4,096자입니다. 깊이·값 수 초과는 `422`입니다.
- `AnalysisResult`는 직렬화한 UTF-8 기준 최대 128 KiB, 저장할 Git 변경 내역은 최대 512 KiB입니다. 성공 후보가 이 상한을 넘으면 성공으로 저장하지 않습니다.
- 검증 명령별 표준 출력과 표준 오류는 각각 최대 32 KiB, 저장할 오류 상세 내용은 최대 2 KiB입니다. 민감 정보를 먼저 제거하고 제한된 길이만 저장합니다.
- 변경 파일은 최대 10개, 추가·삭제한 줄의 합은 최대 400줄입니다. 패치는 최대 900초, 검증 명령은 각각 최대 300초 실행합니다. 서비스 정책은 전체 상한보다 엄격할 수 있습니다.

## 8. 구현 검증 테스트

- 두 작업자가 동시에 선점을 시도해도 작업 하나만 실행됩니다.
- Codex 실행 중 작업자가 비정상 종료되면 임대 만료 뒤 같은 작업이 복구됩니다.
- 이전 `claim_token`으로 보낸 생존 신호와 완료 처리는 DB 행을 갱신하지 못합니다.
- 30초마다 생존 신호를 정상적으로 보내면 120초보다 오래 걸리는 분석도 다시 실행되지 않습니다.
- 분석이 24시간을 넘으면 재시도 없이 최종 실패하고 Codex 하위 프로세스가 종료됩니다.
- 승인된 SHA와 패치 작업 트리의 SHA는 항상 같습니다.
- 같은 지문이라도 전체 SHA가 다르면 별도 사건이 생기고, 새 발생 기록은 `version`을 올리지 않습니다.
- 서비스 정책을 분석 후 바꿔도 승인·패치가 승인된 분석의 정책 고정 사본을 사용합니다.
- Codex 모델 호출은 가능하지만 모델 실행 명령과 검증 명령의 외부 접속은 실패합니다.
- 분석이 끝난 뒤 Git 변경 내역이 비어 있습니다.
- 패치 결과가 허용 경로 밖을 변경하면 검증 명령을 실행하지 않고 실패합니다.
- Codex 응답 스키마 실패, 시간 초과, 검증 실패는 서로 다른 오류 코드로 저장됩니다.

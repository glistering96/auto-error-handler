# MVP 의사결정 대장

상태: MVP 주요 결정 확정
최종 갱신: 2026-09-24

이 문서는 MVP 구현 결정을 한곳에서 관리합니다. 상세 설계보다 이 문서의 `ACCEPTED` 결정이 우선하며, 아직 코드로 구현됐다는 뜻은 아닙니다.

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
| CORE-002 | ACCEPTED | Producer 경계 | Producer는 오류·release·재현 설명과 재현에 필요한 비식별 입력·fixture 데이터만 보내고 workflow 상태는 플랫폼이 소유 |
| CORE-003 | ACCEPTED | 실행 구조 | MVP는 중앙 Worker를 사용하되 Control Plane과 Execution Plane을 interface/process 경계로 분리 |
| CORE-004 | ACCEPTED | 자동화 수준 | 분석은 자동으로 실행하고 repository 쓰기는 특정 분석·SHA에 대한 사람 승인 이후에만 허용 |
| CORE-005 | ACCEPTED | 처리 단위 | 개별 Event가 아니라 같은 원인의 occurrence를 묶은 Incident 중심으로 처리 |
| CORE-006 | ACCEPTED | 산출물과 로드맵 | MVP 결과는 검증된 Diff이며 이후 GitHub App 기반 Draft PR로 확장 |

이 방향을 바꾸면 API, 상태 머신, 데이터 모델, 보안 경계가 함께 바뀌므로 반드시 ADR을 작성합니다. UUID, timeout, lease 같은 아래 항목은 이 Core Decisions를 구현하기 위한 하위 결정입니다.

## 구현 의사결정 요약

| ID | 우선순위 | 상태 | 주제 | 확정한 값 | 확정 시점 |
|---|---:|---|---|---|---|
| DEC-001 | P0 | ACCEPTED | 외부 ingress | Producer는 HTTP `POST /v1/error-events` 사용 | 완료 |
| DEC-002 | P0 | DEFERRED | 인증 | 서비스·승인자 인증은 제외하고 개발망에만 노출 | MVP 이후 |
| DEC-003 | P0 | ACCEPTED | 비동기 전달 | PostgreSQL `jobs`가 Source of Truth이자 Queue | 완료 |
| DEC-004 | P0 | ACCEPTED | 내부 ID | 모든 내부 리소스 ID는 UUIDv4. 외부 `eventId`와 승인 멱등성 키는 유효한 UUID | 완료 |
| DEC-005 | P0 | ACCEPTED | 사건 묶기 | Producer `fingerprint` 필수. `(service_id, fingerprint, base_commit_sha)`가 같은 처리 중 사건만 묶음 | 완료 |
| DEC-006 | P0 | ACCEPTED | 기준 저장소·커밋 | 새 사건 접수 때 저장소 경로를 고정하고, 새 이벤트의 제공된 SHA 또는 당시 기본 브랜치 `HEAD`를 전체 커밋 SHA로 검증·고정 | 완료 |
| DEC-007 | P0 | ACCEPTED | 승인 대상 고정 | 요청 본문 없이 서버가 해당 Incident의 유일한 성공 분석과 기준 SHA를 승인 transaction에서 고정 | 완료 |
| DEC-008 | P0 | ACCEPTED | 실행·크기 제한 | 요청 256 KiB, 변경 10파일/400줄, 패치 900초, 명령당 300초. 결과·재현 입력 제한은 아래 기준 적용 | 완료 |
| DEC-009 | P0 | ACCEPTED | 검증 명령 | 서비스 설정의 argv allowlist만 `shell=False`로 실행 | 완료 |
| DEC-010 | P0 | ACCEPTED | Patch 안전 경계 | allowed/denied path 검사, binary·symlink·submodule 차단 | 완료 |
| DEC-011 | P0 | ACCEPTED | Job lease와 재시도 | 임대 120초, 유지 신호 30초, claim마다 UUID token 발급, 일시적 기반 시설 오류만 총 3회 시도 | 완료 |
| DEC-012 | P0 | ACCEPTED | 작업자 동시성 | 로컬 MVP 기본 동시 실행 1, 설정으로만 변경. 미완료 작업 100개 상한 | 완료 |
| DEC-013 | P0 | ACCEPTED | Codex 수명 주기 | 작업 시도마다 `AsyncCodex` 컨텍스트와 새 스레드. 시험한 안정 버전을 잠금 파일에 고정 | 완료 |
| DEC-014 | P1 | ACCEPTED | 실패 작업 공간 | 성공·실패 작업 트리를 실행 종료 때 정리. DB 결과는 MVP 개발 기간 동안 보존 | 완료 |
| DEC-015 | P1 | ACCEPTED | 목록 페이지 나누기 | `(created_at DESC, id DESC)` 순서의 불투명 커서, 기본 20·최대 100 | 완료 |
| DEC-016 | P1 | ACCEPTED | 미생성 결과 조회 | 분석·패치 실행 기록이 없으면 `404 application/problem+json` | 완료 |
| DEC-017 | P1 | ACCEPTED | 오류 코드 | `AEH-{AREA}-{HTTP}-{SEQ}` 형식과 고정 목록 사용 | 완료 |
| DEC-018 | P0 | ACCEPTED | 분석과 패치 분리 | 분석은 read-only, 승인은 별도 transaction, 패치는 새 writable worktree/thread | 완료 |
| DEC-019 | P1 | ACCEPTED | Scale-out 순서 | API → DB HA/pool → Worker lease → sandbox/관측 → 병목 확인 후 Outbox+RabbitMQ | 완료 |
| DEC-020 | P1 | DEFERRED | Git remote 변경 | branch push와 PR 생성은 MVP 결과에 포함하지 않음 | MVP 이후 |
| DEC-021 | P0 | ACCEPTED | 분석 최대 실행 시간 | 유지 신호가 정상이어도 분석 하나는 최대 24시간만 실행하며, 시간 초과는 재시도 없이 최종 실패 | 완료 |
| DEC-022 | P0 | ACCEPTED | 분석 정책 고정 | 첫 분석 선점 때 `analysis_runs.policy_snapshot` 저장, 승인·패치는 이 고정 사본만 사용 | 완료 |
| DEC-023 | P0 | ACCEPTED | 외부 통신 경계 | Codex 모델 연결은 허용, 모델 실행 명령과 검증 명령의 외부 접속은 차단 | 완료 |
| DEC-024 | P1 | ACCEPTED | 분석 입력과 사건 버전 | 첫 이벤트 하나를 분석 입력으로 고정, 추가 발생 기록은 `version`을 올리지 않음 | 완료 |
| DEC-025 | P0 | ACCEPTED | 이벤트 체크섬 | RFC 8785로 정규화한 요청 JSON의 UTF-8 바이트에 SHA-256 적용. 중복 키는 `400`, 정규화 불가 수치는 `422` | 완료 |

`DEC-004/005/006/024/025`의 근거와 호환성 변경은 [ADR-0001](decisions/0001-event-identity-and-base-sha.md), `DEC-013/022/023`의 실행 경계는 [ADR-0002](decisions/0002-frozen-policy-and-execution-network.md)에 기록합니다.

## 확정된 구현 값

- 요청 원문은 최대 256 KiB다. JSON 파싱 전에 검사하며 초과하면 `413`이다.
- 이벤트 요청 데이터의 체크섬은 [RFC 8785 JSON 정규화 방식](https://www.rfc-editor.org/rfc/rfc8785.html)으로 직렬화한 UTF-8 바이트의 SHA-256이다. 객체 키 순서와 공백만 다르면 같은 요청으로 취급한다. 중복 객체 키는 `400`, 정규화할 수 없는 수치는 `422`로 거부한다. 원본 요청이나 정규화 바이트는 체크섬 계산만을 위해 사용하고 영구 저장하지 않는다.
- `reproductionData`는 해당 값 자체를 깊이 0으로 셌을 때 최대 깊이 8, 전체 JSON 값 1,000개, 개별 문자열 4,096자다. 초과하면 `422`다.
- 변경 파일은 최대 10개, 추가·삭제한 줄의 합은 최대 400줄이다. 패치 실행은 최대 900초, 등록된 검증 명령은 각각 최대 300초다.
- 저장할 분석 결과는 직렬화한 UTF-8 기준 최대 128 KiB, 변경 내역은 최대 512 KiB, 명령별 표준 출력과 표준 오류는 각각 최대 32 KiB, 오류 상세 내용은 최대 2 KiB다. 초과한 성공 후보는 성공으로 저장하지 않고 제한 위반으로 처리하며, 로그·오류 설명은 저장 전에 민감 정보를 제거하고 제한 길이로 줄인다.
- 첫 MVP의 작업자 동시 실행은 기본 1이다. `PENDING`과 `RUNNING`을 합친 미완료 작업이 100개면 새 사건을 만드는 요청은 이벤트를 저장하지 않고 `503`을 반환한다. 중복 이벤트 재요청은 이 제한과 무관하게 기존 사건 ID와 현재 상태를 돌려준다. 상한 검사는 접수 트랜잭션에서 고정된 PostgreSQL 자문 잠금을 잡은 뒤 미완료 작업 수를 세어 동시 접수에도 100개를 넘지 않게 한다.
- 서비스별 정책은 위 전체 제한보다 엄격해질 수 있지만 완화할 수는 없다. 분석이 시작할 때 실제 적용할 값을 정책 고정 사본에 저장한다.

## 결정별 검증 기준

### DEC-005 — 지문과 사건 묶기

- 동일 eventId 재전송은 occurrence를 추가하지 않습니다.
- `fingerprint`가 없거나 앞뒤 공백을 제거한 결과가 비어 있으면 `422`입니다. 입력은 유니코드 NFC로 정규화하고 대소문자는 보존합니다.
- 서로 다른 eventId라도 같은 서비스·지문·전체 SHA이면 처리 중인 사건 하나에 누적합니다.
- 같은 지문이어도 SHA가 다르면 다른 사건을 만듭니다. 스택 추적 기반 자동 지문 생성은 MVP에서 하지 않습니다.

### DEC-025 — 요청 데이터 체크섬

- 같은 JSON에서 객체 키 순서와 공백만 바꾼 재요청은 동일 체크섬이며 기존 사건을 반환합니다. 배열 순서나 문자열 값을 바꾸면 `409`입니다.
- 중복 객체 키가 있는 JSON은 파싱 단계에서 `400`, RFC 8785로 정규화할 수 없는 수치는 `422`입니다. 체크섬은 민감 정보 제거 전의 요청 데이터로 계산하고, 저장되는 `normalized_payload`에는 민감 정보를 제거합니다.

### DEC-006/007 — 기준 SHA와 승인

- 새 이벤트는 접수 과정에서 Git 조회를 DB 트랜잭션 밖에서 마친 뒤 전체 커밋 SHA를 사건에 저장합니다. `release.commitSha`가 없을 때만 당시 기본 브랜치 `HEAD`를 사용합니다.
- 새 사건에는 SHA를 검증한 정규화된 저장소 경로도 함께 저장합니다. 분석·패치는 사건의 저장소 경로와 SHA를 사용합니다.
- 처리 중인 사건이 있으면 해당 서비스의 `repository_path` 변경을 거부합니다. 정책 변경은 허용하고 새 분석에만 적용합니다.
- 존재하지 않는 커밋은 `422`, 저장소를 일시적으로 읽지 못한 경우는 `503`입니다. 다른 SHA로 자동 대체하지 않습니다.
- 같은 `eventId`와 같은 요청 데이터의 재전송은 SHA를 다시 조회하지 않고 기존 사건 ID와 현재 상태를 `duplicate=true`로 반환합니다.
- `analysis_runs.base_commit_sha`, `approvals.base_commit_sha`, `patch_runs.base_commit_sha`가 동일해야 합니다.
- 승인 요청은 본문을 받지 않습니다. 서버는 `FOR UPDATE`로 Incident를 잠근 뒤 상태가 `AWAITING_APPROVAL`이고 성공한 분석이 정확히 하나인지 확인합니다.
- 서버는 해당 분석 ID와 기준 SHA를 approval과 patch run에 복사합니다.
- 같은 `Idempotency-Key`를 다시 보내면 기존 승인 결과를 반환하고, 다른 키로 이미 승인된 Incident를 다시 승인하면 `409`입니다.
- Patch worktree는 approval에 저장된 SHA에서만 생성합니다.
- default branch가 이후 이동해도 이미 승인된 Patch 기준 SHA는 바뀌지 않습니다.

### DEC-022/023/024 — 분석 입력과 실행 조건

- 첫 분석 선점 때 `analysis_runs.input_error_event_id`와 `policy_snapshot`을 저장합니다. 재시도해도 둘을 바꾸지 않습니다.
- 추가 발생 기록은 `occurrence_count`와 `updated_at`만 갱신하며 `version`을 올리거나 진행 중인 분석을 다시 시작하지 않습니다.
- 승인·패치·검증은 승인된 분석의 정책 고정 사본을 사용하고 현재 `services.policy`를 다시 읽지 않습니다.
- Codex 앱 서버의 모델 연결과 Codex 명령의 외부 접속을 분리합니다. 검증 명령도 별도 실행 환경에서 외부 접속을 막습니다.

### DEC-011 — retry

- Job을 claim할 때마다 새로운 UUID `claim_token`을 발급합니다.
- heartbeat와 finalize는 `job_id + claim_token + RUNNING` 조건이 모두 맞을 때만 수행합니다.
- 임대 시간은 120초이고 heartbeat는 30초마다 임대 만료 시각을 연장합니다.
- heartbeat가 일시적으로 실패하면 임대 만료 전까지 짧게 다시 시도합니다. 토큰이 달라졌거나 만료 전까지 갱신하지 못하면 현재 Codex 실행을 취소하고 결과를 버립니다.
- DB 연결 단절, Codex app-server 비정상 종료, 일시적 network 오류만 retry 후보입니다.
- Schema 오류, 정책 위반, 테스트 실패, 존재하지 않는 commit은 자동 retry하지 않습니다.
- retry는 같은 Job과 같은 run ID를 사용하되 새 `claim_token`, Codex thread, worktree를 사용합니다.
- 최대 시도 횟수를 넘으면 Job과 해당 run을 함께 terminal failure로 만듭니다.

### DEC-021 — 분석 최대 실행 시간

- 분석 하나의 최대 실행 시간은 24시간입니다.
- heartbeat가 정상이어도 24시간이 지나면 Codex 실행을 취소하고 Job, analysis run, Incident를 최종 실패로 전환합니다.
- 24시간 제한에 도달한 분석은 기반 시설의 일시적 오류로 보지 않으며 자동 retry하지 않습니다.

## 결정 절차

1. 담당자가 이 문서의 권장안을 검토합니다.
2. 단순 선택은 이 표를 수정하는 PR에서 `ACCEPTED`로 바꿉니다.
3. API·데이터 모델·보안 경계가 바뀌는 결정은 [`docs/decisions/`](decisions/README.md)에 ADR을 추가합니다.
4. 결정 PR에는 영향받는 OpenAPI, JSON Schema, migration, 테스트 문서를 함께 수정합니다.
5. 이미 `ACCEPTED`인 결정과 충돌하는 구현 PR은 merge하지 않습니다.

## 문서 일치 규칙

- OpenAPI 예시와 DB ID 형식은 UUID로 통일합니다.
- 승인 endpoint에는 요청 본문을 추가하지 않습니다. 승인 대상은 DEC-007에 따라 서버가 DB에서 조회해 고정합니다.
- 모든 크기·시간 제한은 DEC-008의 확정값을 서비스 설정, 문서, 테스트 데이터에서 공유합니다.
- 코드는 상수를 복제하지 않고 하나의 형식화된 설정 객체를 사용합니다.

# 리뷰 웹·저장소 이식성·동시성 개선 계획

기준: 2026-09-25 `main`에 푸시한 MVP(`e45ce7f`). 기존 오류 접수·승인 계약과 읽기 전용 분석 경계는 유지한다.

## 확인한 문제

- DB 모델의 `JSONB`, PostgreSQL `UUID`, `pg_advisory_xact_lock`, `SKIP LOCKED`가 SQLite 실행을 막는다. SQLite는 WAL에서도 한 시점에 쓰기 작업 하나만 진행한다.
- 같은 `eventId`의 동시 접수는 사전 조회 뒤 중복 삽입을 시도할 수 있다. 사건 묶기는 서비스 행을 잠가 서로 다른 사건까지 직렬화한다.
- 여러 API 인스턴스가 시작할 때마다 `sync_services`를 실행하면 설정 쓰기가 경합한다. 작업자 ID 기본값도 모든 프로세스에서 같다.
- 접수·분석·패치 내역은 HTTP API로만 조회할 수 있어 한 화면에서 사건 흐름을 검토하기 어렵다.

## Phase 1 — DB와 마이그레이션

- [x] SQLite 파일을 로컬 기본값으로 설정하고 WAL·외래 키·대기 시간을 설정한다.
- [x] UUID·JSON·UTC 시간·부분 고유 인덱스를 두 DB에서 동일한 계약으로 사용한다.
- [x] 새 SQLite DB와 기존 PostgreSQL DB에서 Alembic 및 전체 흐름을 검증한다.

## Phase 2 — 동시 접수·작업 처리

- [x] SQLite 쓰기 트랜잭션은 `BEGIN IMMEDIATE`로 시작하고 PostgreSQL은 행 단위 잠금을 유지한다.
- [x] 이벤트·사건 고유 제약 충돌은 전체 트랜잭션을 재시도해 원자성과 멱등성을 보존한다.
- [x] PostgreSQL 서비스 조회는 공유 잠금을 써 서로 다른 사건의 접수를 병렬로 허용한다.
- [x] 작업 선점·회수·승인·생존 신호를 두 DB에서 시험하고 API 시작 시 설정 쓰기를 제거한다.
- [x] 작업자 인스턴스 ID를 고유하게 만들고 SQLite는 단일 호스트 파일 DB, PostgreSQL은 다중 인스턴스 운영 경계를 문서화한다.

## Phase 3 — 리뷰 웹서비스

- [x] 별도 웹 진입점을 만들고 사건 목록·필터·페이지 이동을 제공한다.
- [x] 사건 상세에 첫 오류 입력, 발생 횟수, 분석 근거, 승인 상태, 패치 diff와 검증 결과를 보여준다.
- [x] 화면에 표시하는 이벤트·모델 결과를 안전하게 이스케이프하고 브라우저에서 확인한다.

## Phase 4 — 구조·검증·배포

- [x] 사용자가 지정한 코드 컨벤션 범위에 맞춰 계층과 명명 규칙을 정리한다.
- [x] SQLite 기본 실행, PostgreSQL 다중 작업자, 동시 접수, 리뷰 화면의 자동·수동 검증을 통과시킨다.
- [x] README와 실행 예시를 갱신하고 변경분을 `main`에 푸시한다.

검증 기록: SQLite·PostgreSQL 자동 시험 35개, Ruff, mypy가 통과했다. SQLite 실제 Codex 흐름은 분석 성공, `PATCH_READY`, 등록 검증 통과를 확인했다. 새 PostgreSQL DB의 Alembic 생성 결과에서 `UUID`와 `JSONB` 열을 확인했다. 리뷰 웹은 브라우저에서 실제 Codex 사건의 `PATCH_READY`, 근거, diff, 검증 `PASSED`를 확인했다.

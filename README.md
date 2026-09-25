# Auto Error Handler

오류 이벤트를 HTTP API로 받아 등록된 저장소를 Codex로 분석하고, 사용자가 승인한 경우에만 격리된 worktree에서 패치를 생성·검증하는 MVP입니다.

현재는 **로컬에서 실행 가능한 MVP**입니다. SQLite 기본 저장소와 PostgreSQL 확장 경로, Codex 분석, 명시적 승인, 격리된 패치·검증, 읽기 전용 리뷰 웹을 구현했습니다. 개발망 전용이며 서비스·승인자 인증은 없습니다.

아래 단일 흐름을 실제 Codex SDK와 예제 저장소로 검증했습니다.

```text
HTTP 오류 이벤트 수신
  → Incident 저장
  → Codex 읽기 전용 분석
  → 분석 결과 조회
  → 사용자 승인
  → 격리 worktree 패치
  → 등록된 테스트 실행
  → diff 조회
```

## MVP 원칙

- 외부 서비스는 RabbitMQ가 아니라 HTTP API로 오류를 전송합니다.
- 오류 문구와 함께 재현 절차와 실제 재현에 필요한 비식별 request input·fixture 데이터를 선택적으로 전송할 수 있습니다.
- 서비스는 요청 body의 `serviceKey`로 식별합니다. 서비스 인증은 MVP 이후에 추가합니다.
- Incident와 Job 상태는 선택한 DB 한 곳에 저장합니다. 로컬 기본값은 SQLite이며 여러 호스트로 확장할 때 PostgreSQL을 사용합니다.
- 별도 MQ 없이 DB의 `jobs` 테이블로 비동기 작업을 처리합니다.
- 분석 단계는 저장소를 변경할 수 없습니다.
- 패치 작업은 명시적인 승인 이후에만 시작합니다.
- 패치는 기준 commit에서 만든 별도 worktree에서만 수행합니다.
- MVP 결과물은 검증 결과와 Git diff입니다. GitHub branch·Pull Request 생성은 후속 단계입니다.

## 로컬 실행

Python 3.12, `uv`, `bwrap`, Git이 필요합니다. PostgreSQL을 사용할 때만 Docker Compose가 필요합니다. 로컬 로그인 방식에는 Codex CLI도 설치해야 합니다. [공식 Codex Python SDK](https://learn.chatgpt.com/docs/codex-sdk)는 잠금 파일에 고정된 CLI 실행 환경을 사용합니다.

```bash
cp .env.example .env
uv sync --frozen
uv run python -m aeh.cli init-fixture
uv run alembic upgrade head
uv run python -m aeh.cli sync-services
```

각각 별도 터미널에서 Control API, 작업자, 리뷰 웹을 실행합니다. 세 프로세스의 `DATABASE_URL`은 같아야 합니다.

```bash
uv run python -m apps.control_api.main
uv run python -m apps.job_worker.main
uv run python -m apps.review_web.main
```

리뷰 웹은 `http://127.0.0.1:8001/review/incidents`에서 사건 목록과 접수 이벤트, 분석 근거, 승인, 패치 diff, 검증 결과를 읽기 전용으로 보여줍니다. `GET /health/live`는 프로세스 상태, `GET /health/ready`는 DB·설정·Codex 실행 도구와 인증 상태를 확인합니다. 준비 상태 점검에서 모델을 호출하지는 않습니다. 두 웹 프로세스는 루프백 주소에만 바인딩합니다.

### PostgreSQL로 확장

SQLite는 WAL로 읽기와 쓰기를 함께 처리하지만 쓰기는 한 번에 하나이며 파일을 같은 호스트에 둬야 합니다. 여러 API·작업자를 여러 호스트에서 실행하려면 PostgreSQL을 사용합니다. 모든 인스턴스에 같은 `DATABASE_URL`과 서비스 설정을 제공하고, `REPOSITORY_ROOT` 아래에 같은 기준 커밋을 가진 저장소를 준비합니다. 저장소 루트의 절대 경로는 인스턴스마다 달라도 됩니다.

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql+psycopg://aeh:aeh@localhost:5432/aeh
uv run alembic upgrade head
uv run python -m aeh.cli sync-services
```

서비스 설정 등록은 운영 명령 한 곳에서 실행합니다. API 인스턴스가 시작할 때 설정을 다시 쓰지 않습니다.

### Codex 인증 두 가지

- **로컬 로그인:** `.env`의 `CODEX_AUTH_MODE=local`을 사용합니다. 한 번 `codex login`을 마치고 `codex login status`로 확인하면, 작업자가 저장된 CLI 인증을 격리된 임시 Codex 실행 홈에 복사해 사용합니다.
- **API 키:** `.env`의 `CODEX_AUTH_MODE=api-key`로 바꾸고 작업자 프로세스의 환경에 `OPENAI_API_KEY`를 공급합니다. 작업자는 이를 Codex 호출에 필요한 `CODEX_API_KEY`로 전달합니다. 키 값은 저장소의 설정 파일, 로그, 검증 명령에 기록하지 않습니다. 로컬에 사용 가능한 API 키가 없어 이 경로는 설정 시험까지만 확인했습니다.

두 방식은 [Codex 공식 인증 문서](https://learn.chatgpt.com/docs/auth)의 로컬 로그인·API 키 흐름을 따릅니다. 일반 자동 시험에서 모델 호출을 생략하려면 `.env`에 `USE_FAKE_CODEX=true`를 설정합니다.

### 예제 사건 처리

```bash
curl -i -X POST http://127.0.0.1:8000/v1/error-events \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: bf2e277e-7640-4bb2-947a-a4f639f1ec44' \
  --data-binary @fixtures/event.json
```

응답의 `incidentId`로 `GET /v1/incidents/{incidentId}/analysis`를 조회하고 `AWAITING_APPROVAL`을 확인한 뒤 승인합니다. 승인 요청에는 본문을 넣지 않습니다.

```bash
curl -i -X POST http://127.0.0.1:8000/v1/incidents/INCIDENT_ID/approve \
  -H 'Idempotency-Key: NEW_APPROVAL_UUID'
curl http://127.0.0.1:8000/v1/incidents/INCIDENT_ID/patch
```

분석과 패치의 임시 Git 작업 트리는 실행 종료 시 삭제됩니다. 기준 저장소는 변경하지 않고, 검증된 diff와 결과를 DB에서 조회합니다.

### 검사

```bash
uv run ruff check aeh apps tests scripts migrations
uv run mypy aeh apps
uv run pytest -q
TEST_POSTGRES_URL=postgresql+psycopg://aeh:aeh@localhost:5432/aeh uv run pytest -q
uv run python scripts/probe_codex_flow.py
```

PostgreSQL 시험에는 실행 중인 PostgreSQL이 필요합니다. 마지막 명령은 실제 모델을 호출하므로 로컬 로그인 또는 API 키가 필요합니다. 일반 `pytest`는 가짜 게이트웨이를 사용합니다. 실행 전 결정과 범위는 [기본 MVP 계획](docs/mvp-build-plan-2026-09-24.md)과 [리뷰·DB 확장 계획](docs/review-web-sqlite-scaleout-plan-2026-09-25.md)에 기록했습니다.

## 문서

- [MVP 서비스 구조·구현 계획 인터랙티브 웹 문서](walkthrough-mvp-service-architecture.html)
- [기여 및 PR 규칙](CONTRIBUTING.md)
- [서비스 아키텍처](docs/service-architecture.md)
- [MVP 구현 범위](docs/mvp-scope.md)
- [MVP 실행 계획](docs/mvp-execution-plan.md)
- [세부 구현 계획](docs/implementation-plan.md)
- [협업 시작 가이드](docs/collaboration-guide.md)
- [MVP 의사결정 대장](docs/decision-register.md)
- [SQLite·PostgreSQL·리뷰 웹 결정](docs/decisions/0003-sqlite-local-postgresql-scaleout-and-review.md)
- [MVP 구현 계획 검토 기록](docs/mvp-implementation-review-2026-09-24.tmp.md)
- [DB·Worker·Codex 구현 계약](docs/implementation-contracts.md)
- [HTTP 오류 이벤트 계약 v1](docs/contracts/external-error-event-v1.md)
- [오류 이벤트 JSON Schema](schemas/external-error-event-v1.schema.json)
- [OpenAPI 3.1 MVP 계약](openapi/auto-error-handler-mvp.v1.yaml)

## MVP에서 제외하는 항목

- 서비스 인증 토큰과 OIDC/RBAC
- 외부 RabbitMQ와 서비스별 MQ credential
- 멀티테넌시와 조직별 격리
- Slack, Teams, Discord 알림과 승인
- GitHub App, branch push, Draft Pull Request
- Kubernetes 배포, Secret Manager, S3, OpenTelemetry 대시보드
- 자동 merge와 production 배포

이 기능들은 핵심 수직 흐름이 검증된 이후 필요한 순서대로 추가합니다.

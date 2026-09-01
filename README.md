# Auto Error Handler

오류 이벤트를 HTTP API로 받아 등록된 저장소를 Codex로 분석하고, 사용자가 승인한 경우에만 격리된 worktree에서 패치를 생성·검증하는 MVP입니다.

현재 목표는 운영 기능을 넓게 구현하는 것이 아니라 아래 단일 흐름이 실제로 동작함을 증명하는 것입니다.

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
- 오류 문구와 함께 재현 절차, 비식별 입력, 기대·실제 결과를 선택적으로 전송할 수 있습니다.
- 서비스는 요청 body의 `serviceKey`로 식별합니다. 서비스 인증은 MVP 이후에 추가합니다.
- Incident와 Job 상태의 진실 공급원은 PostgreSQL입니다.
- 별도 MQ 없이 PostgreSQL Job Queue로 비동기 작업을 처리합니다.
- 분석 단계는 저장소를 변경할 수 없습니다.
- 패치 작업은 명시적인 승인 이후에만 시작합니다.
- 패치는 기준 commit에서 만든 별도 worktree에서만 수행합니다.
- MVP 결과물은 검증 결과와 Git diff입니다. GitHub branch·Pull Request 생성은 후속 단계입니다.

## 문서

- [통합 웹 문서: 비즈니스 로직·API 계약·Job Queue](auto-error-handler-mvp-guide.html)
- [기여 및 PR 규칙](CONTRIBUTING.md)
- [인터랙티브 서비스 흐름 Walkthrough](walkthrough-auto-error-handler.html)
- [서비스 아키텍처](docs/service-architecture.md)
- [세부 구현 계획](docs/implementation-plan.md)
- [협업 시작 가이드](docs/collaboration-guide.md)
- [MVP 의사결정 대장](docs/decision-register.md)
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

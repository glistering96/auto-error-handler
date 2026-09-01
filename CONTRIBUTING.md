# Contributing

Auto Error Handler MVP는 계약과 안전 경계를 먼저 합의한 뒤 구현합니다.

## 시작 전

1. [협업 시작 가이드](docs/collaboration-guide.md)를 읽습니다.
2. 작업과 관련된 [의사결정](docs/decision-register.md)이 `ACCEPTED`인지 확인합니다.
3. API/DB/Job/Codex 경계는 [구현 계약](docs/implementation-contracts.md)을 확인합니다.
4. 미결정 P0가 있으면 구현 PR보다 decision/ADR PR을 먼저 만듭니다.

## Branch와 Commit

- branch: `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`
- commit 예: `feat(worker): add leased job claim`
- 한 PR은 하나의 비즈니스 목적만 다룹니다.

## Pull Request

- 관련 DEC/ADR과 Issue를 연결합니다.
- API 변경은 OpenAPI와 contract test를 함께 수정합니다.
- DB 변경은 Alembic migration과 rollback/forward 검증을 포함합니다.
- retry, timeout, idempotency, cleanup 영향을 명시합니다.
- Codex 실제 호출은 별도 integration test로 두고 기본 CI에서는 fake gateway를 사용합니다.

## Review 우선순위

1. 승인 전 무변경과 credential 경계
2. 중복 요청과 transaction 원자성
3. Worker crash, stale lease, 늦게 도착한 결과
4. path/diff/command 정책
5. 정상 경로와 코드 품질

상세 Definition of Ready/Done은 [협업 시작 가이드](docs/collaboration-guide.md)를 따릅니다.

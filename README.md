# Auto Error Handler

Codex SDK를 이용해 등록된 서비스의 장애 원인을 분석하고, 메신저로 결과와 승인 요청을 전달한 뒤, 사용자 승인이 확인된 경우에만 격리된 작업공간에서 패치를 생성·검증하는 서비스입니다.

현재 단계는 구현 전 설계 단계입니다.

## 문서

- [인터랙티브 서비스 흐름 Walkthrough](walkthrough-auto-error-handler.html)
- [서비스 아키텍처](docs/service-architecture.md)
- [세부 구현 계획](docs/implementation-plan.md)
- [외부 오류 이벤트 MQ 메시지 계약 v1](docs/contracts/external-error-event-v1.md)

## 기본 원칙

- 원인 분석은 읽기 전용으로 실행합니다.
- 패치 작업은 명시적인 사용자 승인 이후에만 시작합니다.
- 패치는 운영 서버에 직접 배포하지 않고, 격리된 브랜치와 Pull Request로 전달합니다.
- 모든 이벤트, 승인, Codex 실행, 파일 변경 및 알림 결과를 감사 로그로 남깁니다.

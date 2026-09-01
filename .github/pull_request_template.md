## What

이 PR이 완성하는 하나의 목적을 적습니다.

## Contract

- Related Issue:
- Related DEC/ADR:
- OpenAPI/Schema/Migration 영향:

## Verification

- [ ] 단위 테스트
- [ ] 통합 또는 E2E 테스트
- [ ] 중복 요청/idempotency
- [ ] timeout/retry/crash recovery
- [ ] workspace cleanup

## Safety

- [ ] 승인 전 repository를 변경하지 않음
- [ ] allowed/denied path와 diff 제한을 지킴
- [ ] event의 reproduction 문자열을 실행하지 않음
- [ ] secret·payload·전체 stack trace를 log에 남기지 않음
- [ ] validation command는 등록된 argv와 `shell=False`만 사용

## Documentation

- [ ] 사용자 동작 변경 시 OpenAPI/Schema와 웹 가이드를 갱신함
- [ ] 새 장기 결정이면 decision register 또는 ADR을 갱신함

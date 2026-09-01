# Architecture Decision Records

장기간 유지되거나 여러 모듈에 영향을 주는 결정은 ADR로 기록합니다.

## 파일 규칙

```text
NNNN-short-kebab-title.md
```

예: `0001-resource-id-format.md`

번호는 재사용하지 않습니다. 결정이 바뀌면 기존 ADR을 삭제하지 않고 새 ADR에서 `Supersedes`를 표시합니다.

## 상태

- `Proposed`: 검토 중
- `Accepted`: 구현 계약
- `Deprecated`: 더 이상 사용하지 않음
- `Superseded`: 다른 ADR로 대체됨

## ADR이 필요한 변경

- API 호환성을 깨는 변경
- DB 식별자, 상태 머신, transaction 경계 변경
- Job Queue 또는 retry 의미 변경
- Codex sandbox와 credential 경계 변경
- 외부 MQ, 객체 스토리지, GitHub App 같은 새 인프라 도입

단순한 구현 세부사항, 리팩터링, 오탈자는 ADR 없이 PR 설명으로 충분합니다.

새 문서는 [`0000-template.md`](0000-template.md)를 복사해 작성합니다.

# HTTP 오류 이벤트 계약 v1

상태: Draft  
계약 버전: 1.0  
전송 방식: HTTP JSON  
JSON Schema: [`schemas/external-error-event-v1.schema.json`](../../schemas/external-error-event-v1.schema.json)

## 1. 목적

등록된 서비스가 Auto Error Handler MVP에 오류 발생 사실과 분석에 필요한 최소 정보를 전달하기 위한 HTTP 계약입니다.

MVP에서는 서비스 인증을 적용하지 않습니다. `serviceKey`는 요청 body에서 받아 사전 등록된 서비스 설정을 조회하는 데 사용합니다. 이 API는 개발 환경에만 노출합니다.

## 2. Endpoint

```http
POST /v1/error-events
Content-Type: application/json
Idempotency-Key: bf2e277e-7640-4bb2-947a-a4f639f1ec44
```

`Idempotency-Key`는 body의 `eventId`와 같아야 합니다.

## 3. 요청 예시

```json
{
  "specVersion": "1.0",
  "eventId": "bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "serviceKey": "fixture-api",
  "environment": "development",
  "occurredAt": "2026-09-01T12:34:56.789Z",
  "severity": "error",
  "title": "User lookup failed",
  "message": "Cannot read properties of null",
  "fingerprint": "user-null:get-profile:v1",
  "exception": {
    "type": "TypeError",
    "message": "Cannot read properties of null",
    "stackTrace": "TypeError: Cannot read properties of null\n    at getProfile (...)"
  },
  "release": {
    "version": "fixture-api@1.0.3",
    "commitSha": "a1b2c3d4e5f678901234567890abcdef12345678"
  },
  "request": {
    "method": "GET",
    "route": "/v1/users/:userId",
    "statusCode": 500
  },
  "reproduction": {
    "description": "존재하지 않는 사용자 ID로 프로필 API를 호출하면 재현됩니다.",
    "preconditions": ["fixture 사용자 데이터가 비어 있음"],
    "steps": [
      "userId가 null인 입력을 준비한다",
      "GET /v1/users/:userId를 호출한다"
    ],
    "sanitizedInputs": {
      "userId": null,
      "includeProfile": true
    },
    "expectedBehavior": "404 응답을 반환한다",
    "actualBehavior": "TypeError와 500 응답이 발생한다",
    "frequency": "always"
  },
  "tags": {
    "component": "user-service"
  }
}
```

## 4. 필드

| 필드 | 필수 | 설명 |
|---|---:|---|
| `specVersion` | Y | 정확히 `1.0` |
| `eventId` | Y | UUID. 동일 이벤트 재시도 시 같은 값 사용 |
| `serviceKey` | Y | 사전 등록된 서비스 key |
| `environment` | Y | `production`, `staging`, `development`, `test` |
| `occurredAt` | Y | RFC 3339 오류 발생 시각 |
| `severity` | Y | `warning`, `error`, `critical` |
| `title` | Y | 사람이 식별할 수 있는 오류 제목 |
| `message` | Y | secret과 개인정보를 제거한 오류 메시지 |
| `fingerprint` | N | 동일 원인을 묶는 안정적인 값 |
| `exception` | N | 예외 type, message, stack trace |
| `release` | N | 배포 version과 Git commit SHA |
| `request` | N | HTTP method, route template, status code |
| `reproduction` | N | 재현 설명, 전제조건, 절차, 비식별 입력, 기대/실제 결과 |
| `tags` | N | 분석에 필요한 제한된 문자열 metadata |

### `reproduction`

| 필드 | 설명 |
|---|---|
| `description` | 재현 방법을 한 문단으로 요약 |
| `preconditions` | 재현 전에 필요한 상태나 설정 |
| `steps` | 사람이 따라 할 수 있는 순서화된 절차 |
| `sanitizedInputs` | secret과 개인정보를 제거한 scalar 입력값 |
| `expectedBehavior` | 정상적으로 기대한 결과 |
| `actualBehavior` | 실제 오류 결과 |
| `frequency` | `always`, `intermittent`, `once`, `unknown` |

`reproduction`은 Codex가 원인을 찾고 회귀 테스트를 설계할 때 사용하는 힌트입니다. 이 필드에 shell command, SQL, 원본 HTTP body를 넣지 않으며 Auto Error Handler는 Producer가 보낸 문자열을 명령으로 실행하지 않습니다.

## 5. 응답

### 최초 수신

```http
HTTP/1.1 202 Accepted
```

```json
{
  "eventId": "bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "incidentId": "01K...",
  "status": "RECEIVED",
  "duplicate": false
}
```

### 중복 수신

동일한 `serviceKey + eventId`가 이미 저장되어 있으면 새 occurrence와 분석 Job을 만들지 않고 기존 결과를 반환합니다.

```json
{
  "eventId": "bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "incidentId": "01K...",
  "status": "ANALYZING",
  "duplicate": true
}
```

## 6. 오류 응답

| 상태 | 조건 |
|---:|---|
| `400` | JSON syntax, header, body 크기 오류 |
| `404` | 등록되지 않은 `serviceKey` |
| `409` | 같은 eventId로 다른 payload를 전송 |
| `422` | JSON Schema 검증 실패 |
| `500` | DB transaction 실패 |

오류 형식:

```json
{
  "type": "urn:aeh:error:event-schema-invalid",
  "title": "Error event validation failed",
  "status": 422,
  "code": "AEH-EVENT-422-001",
  "detail": "Request does not match external-error-event-v1 schema",
  "instance": "/v1/error-events/bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "traceId": "..."
}
```

## 7. 멱등성과 재시도

- Producer는 성공 응답을 받지 못하면 같은 `eventId`와 같은 payload로 재시도할 수 있습니다.
- API는 DB commit이 완료된 뒤에만 `202`를 반환합니다.
- `(service_id, event_id)` unique constraint가 중복 저장을 차단합니다.
- 같은 eventId의 payload가 달라지면 `409`를 반환합니다.
- MVP에서는 Producer용 durable outbox를 강제하지 않습니다.

## 8. Incident grouping

fingerprint 우선순위:

1. 요청의 명시적 `fingerprint`
2. 정규화된 exception type과 application stack frame
3. route template, status code, 정규화된 message

서로 다른 eventId가 같은 fingerprint를 가지면 occurrence는 각각 저장하지만 열린 Incident는 하나를 사용합니다. 분석 Job은 Incident 최초 생성 시 한 번만 만듭니다.

## 9. 보안 제한

MVP 요청에 다음 값을 포함하지 않습니다.

- Authorization과 Cookie
- access token, session ID, API key
- request/response body
- 실행 가능한 shell command, SQL, script
- 사용자 이메일, 전화번호, 이름
- 원본 URL query string과 client IP

Consumer는 body를 저장하기 전에 크기 제한과 알려진 secret pattern을 검사합니다. 인증이 추가되기 전까지 Endpoint는 외부 네트워크에 공개하지 않습니다.

## 10. 후속 호환성

서비스 인증을 추가할 때 body schema는 유지합니다. 인증 계층만 다음처럼 교체합니다.

```text
MVP: body.serviceKey → 서비스 조회
향후: Authorization token → 서비스 조회 → body.serviceKey 일치 검사
```

RabbitMQ 전송이 필요해지면 동일 JSON body를 별도의 adapter가 이 HTTP 계약으로 변환하도록 하고, Incident 도메인 로직은 변경하지 않습니다.

# HTTP 오류 이벤트 계약 v1

상태: MVP 계약 확정 · 구현 시험 대기
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
  "reproductionData": {
    "request": {
      "pathParams": {
        "userId": "user-fixture-404"
      },
      "query": {
        "includeProfile": true
      },
      "headers": {
        "accept": "application/json",
        "x-fixture-mode": "empty-user"
      },
      "body": null
    },
    "fixtures": [
      {
        "name": "user",
        "data": {
          "id": "user-fixture-404",
          "exists": false
        }
      }
    ],
    "featureFlags": {
      "profileV2": true
    }
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
| `fingerprint` | Y | Producer가 만든 안정적인 사건 지문. 앞뒤 공백을 제거하고 유니코드 NFC로 정규화함 |
| `exception` | N | 예외 type, message, stack trace |
| `release` | N | 배포 version과 Git commit SHA |
| `request` | N | HTTP method, route template, status code |
| `reproduction` | N | 재현 설명, 전제조건, 절차, 비식별 입력, 기대/실제 결과 |
| `reproductionData` | N | 재현 실행에 필요한 비식별 request input, fixture seed, feature flag |
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

`reproduction`은 재현 방법을 설명하고, `reproductionData`는 실제 재현에 사용할 입력 데이터입니다. `reproductionData.request`에는 path parameter, query, 허용된 테스트용 header, sanitized body를 보낼 수 있고 `fixtures`에는 재현에 필요한 seed 데이터를 보낼 수 있습니다. Producer가 보내는 데이터는 분석과 fixture/test 설계의 입력으로만 사용하며, 플랫폼이 임의의 shell command·SQL·script로 실행하지 않습니다.

`reproductionData`는 선택 필드이지만, 재현에 필요한 값이 있는 오류라면 Producer가 이 필드에 데이터를 넣어야 합니다. 데이터는 JSON 값만 허용합니다. 요청 원문은 최대 256 KiB이며 JSON 파싱 전에 검사합니다. `reproductionData` 자체를 깊이 0으로 셌을 때 중첩 깊이는 최대 8, 전체 JSON 값은 최대 1,000개, 개별 문자열은 최대 4,096자입니다. 깊이와 전체 값 수는 JSON Schema 검증과 별도로 검사합니다. `Authorization`, `Cookie`, 토큰, 운영 DB 덤프, 원본 개인정보는 허용하지 않습니다.

## 5. 응답

### 최초 수신

```http
HTTP/1.1 202 Accepted
```

```json
{
  "eventId": "bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "incidentId": "5812dd8d-3bb4-4d66-a243-228dba0c09be",
  "status": "RECEIVED",
  "duplicate": false
}
```

### 중복 수신

동일한 `serviceKey + eventId`가 이미 저장되어 있으면 새 발생 기록과 분석 작업을 만들지 않습니다. 기존 사건 ID와 현재 상태를 조회해 `duplicate=true`로 반환하므로, 최초 응답의 상태와 달라질 수 있습니다.

```json
{
  "eventId": "bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "incidentId": "5812dd8d-3bb4-4d66-a243-228dba0c09be",
  "status": "ANALYZING",
  "duplicate": true
}
```

## 6. 오류 응답

| 상태 | 조건 |
|---:|---|
| `400` | JSON 문법·객체 키 중복 또는 헤더 형식 오류 |
| `413` | 요청 body가 MVP byte limit을 초과 |
| `404` | 등록되지 않은 `serviceKey` |
| `409` | 같은 eventId로 다른 payload를 전송 |
| `422` | JSON Schema 검증 실패, 정규화할 수 없는 JSON 수치, 비어 있는 지문, 재현 데이터 제한 초과 또는 존재하지 않는 커밋 |
| `503` | 저장소를 일시적으로 읽을 수 없거나 미완료 작업이 100개에 도달 |
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
- 비교용 체크섬은 [RFC 8785의 JSON 정규화 방식](https://www.rfc-editor.org/rfc/rfc8785.html)으로 만든 UTF-8 바이트의 SHA-256입니다. 객체 키 순서와 공백만 달라진 요청은 같게 보고, 배열 순서와 문자열 값은 그대로 비교합니다. 중복 객체 키는 `400`, 정규화할 수 없는 수치는 `422`로 거부합니다.
- MVP에서는 Producer용 durable outbox를 강제하지 않습니다.

## 8. 사건 묶기와 기준 SHA

`fingerprint`는 필수입니다. 앞뒤 공백을 제거하고 유니코드 NFC로 정규화한 결과가 비어 있으면 `422`를 반환합니다. 대소문자는 보존합니다. 스택 추적에서 지문을 자동 생성하지 않습니다.

새 `eventId`를 받으면 제공된 `release.commitSha`를 저장소에서 전체 커밋 SHA로 확인합니다. 없을 때만 접수 과정에서 읽은 기본 브랜치 `HEAD`를 기준으로 사용합니다. 존재하지 않는 커밋은 `422`로 거부하며 다른 SHA로 대체하지 않습니다. 저장소를 일시적으로 읽을 수 없으면 `503`을 반환합니다.

서로 다른 `eventId`가 같은 서비스·정규화한 지문·전체 SHA를 가지면 발생 기록은 각각 저장하지만 처리 중인 사건은 하나를 사용합니다. SHA가 다르면 지문이 같아도 새 사건을 만듭니다. 사건에는 접수 당시 저장소 경로와 전체 SHA를 함께 고정합니다. 분석 작업은 사건 최초 생성 시 한 번만 만듭니다. 같은 `eventId`와 같은 요청 데이터를 다시 보내면 저장소를 다시 조회하지 않고 기존 사건 ID와 현재 상태를 반환합니다.

## 9. 보안 제한

MVP 요청에 다음 값을 포함하지 않습니다.

- Authorization과 Cookie
- access token, session ID, API key
- 원본 request/response body
- 실행 가능한 shell command, SQL, script
- 사용자 이메일, 전화번호, 이름
- 원본 URL query string과 client IP

재현이 필요한 경우에는 원본 body 대신 `reproductionData.request.body`에 비식별화한 최소 입력만 넣고, DB 상태는 `reproductionData.fixtures`로 필요한 최소 seed만 보냅니다. 허용 header는 `content-type`, `accept`, `x-fixture-*`, `x-test-*`뿐입니다.

Consumer는 body를 저장하기 전에 크기 제한과 알려진 secret pattern을 검사합니다. 인증이 추가되기 전까지 Endpoint는 외부 네트워크에 공개하지 않습니다.

## 10. 후속 호환성

서비스 인증을 추가할 때 body schema는 유지합니다. 인증 계층만 다음처럼 교체합니다.

```text
MVP: body.serviceKey → 서비스 조회
향후: Authorization token → 서비스 조회 → body.serviceKey 일치 검사
```

RabbitMQ 전송이 필요해지면 동일 JSON body를 별도의 adapter가 이 HTTP 계약으로 변환하도록 하고, Incident 도메인 로직은 변경하지 않습니다.

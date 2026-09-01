# 외부 오류 이벤트 MQ 메시지 계약 v1

상태: Draft  
계약 버전: 1.0  
전송 방식: AMQP 0-9-1 over TLS (`amqps`)  
기본 Broker: RabbitMQ 4.x  
JSON Schema: [`schemas/external-error-event-v1.schema.json`](../../schemas/external-error-event-v1.schema.json)

## 1. 목적

등록된 외부 서비스가 Auto Error Handler에 오류 발생 사실과 진단에 필요한 최소 정보를 안정적으로 전달하기 위한 생산자 계약이다. 이 문서는 다음을 고정한다.

- RabbitMQ exchange와 routing key
- AMQP message properties와 headers
- JSON body의 필수·선택 필드
- 생산자 재시도와 중복 처리
- consumer acknowledgement, retry, DLQ 처리
- 버전 호환성과 보안 제한

이 계약은 외부 서비스에서 Auto Error Handler로 들어오는 방향만 다룬다. 분석 완료, 승인 요청, 패치 완료 같은 반대 방향 이벤트는 별도 outbound 계약으로 정의한다.

## 2. 전달 보장

전체 전달 의미는 `at-least-once`다. 네트워크 단절이나 publisher confirm timeout이 발생하면 동일 메시지가 두 번 이상 전달될 수 있다. exactly-once를 가정하지 않는다.

```text
외부 서비스
  -> persistent publish + mandatory + publisher confirm
  -> durable topic exchange
  -> quorum ingest queue
  -> MQ Ingest Consumer
  -> PostgreSQL transaction + inbox deduplication
  -> manual ack
```

핵심 규칙:

1. 생산자는 confirm을 받기 전까지 전송 성공으로 간주하지 않는다.
2. 재전송할 때 새로운 ID를 만들지 않고 동일한 `message_id`를 사용한다.
3. Consumer는 schema 검증과 DB transaction commit 이후에만 ack한다.
4. Consumer는 `(service_id, message_id)` unique constraint로 중복을 제거한다.
5. broker confirm은 메시지가 broker에 수락되었다는 뜻이며 사건 분석 완료를 의미하지 않는다.

RabbitMQ quorum queue에서 안전한 발행을 위해 publisher confirm이 필요하고 consumer 측에서는 manual acknowledgement가 권장된다. [RabbitMQ 공식 quorum queue 문서](https://www.rabbitmq.com/docs/4.2/quorum-queues)와 [publisher confirms 문서](https://www.rabbitmq.com/docs/confirms)를 기준으로 한다.

## 3. Broker 연결 정보

서비스 등록이 완료되면 Control API가 다음 정보를 반환한다.

| 항목 | 예시 | 설명 |
|---|---|---|
| `protocol` | `amqps` | TLS 없는 `amqp`는 운영에서 금지 |
| `host` | `mq.aeh.example.com` | Broker endpoint |
| `port` | `5671` | AMQPS 기본 포트 |
| `virtual_host` | `aeh.acme` | 조직별 격리 경계 |
| `exchange` | `aeh.external.error.v1` | 서버가 미리 만든 durable topic exchange |
| `routing_key` | `error.payments-api.production.error` | 서비스별 허용 prefix |
| `username` | 발급 값 | publish-only 사용자 |
| `password` | 최초 1회 표시 | Secret Manager에 저장할 값 |
| `ca_certificate` | 공개 인증서 | private CA를 사용하는 경우 제공 |
| `schema_url` | 버전 고정 URL | 생산자 build-time 검증용 schema |

생산자는 exchange, queue, binding, DLX를 생성하거나 변경하지 않는다. Broker topology는 Auto Error Handler 운영자가 관리한다.

## 4. RabbitMQ topology

| 종류 | 이름 | 설정 |
|---|---|---|
| Virtual host | `aeh.<organization_key>` | 조직별 분리 |
| External exchange | `aeh.external.error.v1` | `topic`, durable |
| Ingest queue | `aeh.ingest.error.v1` | durable quorum queue |
| Binding | `error.#` | ingest queue 연결 |
| Retry exchanges | `aeh.internal.retry.10s.v1`, `60s`, `300s` | 내부 Consumer 전용 |
| Dead-letter exchange | `aeh.external.error.dlx.v1` | durable topic exchange |
| Dead-letter queue | `aeh.ingest.error.dlq.v1` | durable quorum queue |

DLX 설정은 queue 선언 코드의 고정 `x-arguments`가 아니라 RabbitMQ policy로 관리한다. RabbitMQ 공식 문서도 운영 중 변경 가능한 policy 방식을 권장한다. [RabbitMQ DLX 문서](https://www.rabbitmq.com/docs/dlx)

### Routing key

형식:

```text
error.<service_key>.<environment>.<severity>
```

예시:

```text
error.payments-api.production.critical
error.payments-api.staging.error
error.catalog-worker.production.warning
```

제약:

- `service_key`: `^[a-z][a-z0-9-]{1,62}$`
- `environment`: `production`, `staging`, `development`, `test` 중 하나
- `severity`: `warning`, `error`, `critical` 중 하나
- routing key의 서비스, 환경, 심각도는 JSON body 값과 같아야 한다.
- credential은 등록된 `error.<service_key>.*.*` prefix에만 write할 수 있다.

## 5. AMQP message properties

| Property | 필수 | 값 또는 규칙 |
|---|---:|---|
| `content_type` | Y | `application/json` |
| `content_encoding` | Y | `utf-8` |
| `delivery_mode` | Y | `2` (persistent) |
| `message_id` | Y | UUID v4, body의 `id`와 동일 |
| `type` | Y | `com.auto-error-handler.error.occurred.v1` |
| `timestamp` | Y | 발생 시각의 Unix timestamp |
| `app_id` | Y | 등록된 `service_key` |
| `correlation_id` | N | trace ID 또는 request ID |
| `expiration` | N | 생산자가 지정하지 않음. Broker policy가 관리 |
| `reply_to` | N | 사용하지 않음 |

### Headers

| Header | 필수 | 값 또는 규칙 |
|---|---:|---|
| `x-aeh-schema-version` | Y | `1.0` |
| `x-aeh-service-id` | Y | 등록 시 발급한 service UUID |
| `traceparent` | N | W3C Trace Context 형식 |
| `tracestate` | N | W3C Trace Context 형식 |

`Authorization`, cookie, API token, 사용자 개인정보를 header에 넣지 않는다.

## 6. JSON body

### 전체 예시

```json
{
  "specVersion": "1.0",
  "id": "bf2e277e-7640-4bb2-947a-a4f639f1ec44",
  "type": "com.auto-error-handler.error.occurred.v1",
  "source": "service://payments-api",
  "subject": "payment.authorize",
  "time": "2026-09-01T12:34:56.789Z",
  "organizationId": "e080e7e0-1f4a-419b-a146-07c66415b71c",
  "service": {
    "id": "491f935d-0791-4ae3-9a08-641461a7d1bd",
    "key": "payments-api",
    "environment": "production"
  },
  "data": {
    "severity": "error",
    "title": "Payment authorization failed",
    "message": "PaymentMethod was null",
    "fingerprint": "payment-method-null:authorize:v2",
    "exception": {
      "type": "TypeError",
      "message": "Cannot read properties of null",
      "stackTrace": "TypeError: Cannot read properties of null\n    at validateCard (...)",
      "causes": []
    },
    "release": {
      "version": "payments-api@2026.09.01.3",
      "commitSha": "a1b2c3d4e5f678901234567890abcdef12345678",
      "deployedAt": "2026-09-01T12:20:00.000Z"
    },
    "runtime": {
      "name": "node",
      "version": "20.18.0",
      "region": "ap-northeast-2",
      "instanceId": "payments-api-7d9f6c9b6f-x2k4p"
    },
    "traceContext": {
      "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
      "spanId": "00f067aa0ba902b7",
      "requestId": "req_01J8XYZ"
    },
    "request": {
      "method": "POST",
      "route": "/v1/payments/:paymentId/authorize",
      "statusCode": 500
    },
    "tags": {
      "component": "payment-validator",
      "deployment": "blue"
    },
    "logReferences": [
      {
        "provider": "sentry",
        "locator": "project-42:event-a92d",
        "expiresAt": "2026-09-08T12:34:56.789Z"
      }
    ],
    "extensions": {}
  }
}
```

### Envelope 필드

| 필드 | 타입 | 필수 | 제약 |
|---|---|---:|---|
| `specVersion` | string | Y | 정확히 `1.0` |
| `id` | UUID string | Y | AMQP `message_id`와 동일, 재전송 시 불변 |
| `type` | string | Y | 정확히 `com.auto-error-handler.error.occurred.v1` |
| `source` | URI string | Y | `service://<service_key>` 권장 |
| `subject` | string | Y | 오류가 발생한 operation/component, 최대 200자 |
| `time` | RFC 3339 string | Y | 실제 오류 발생 시각, UTC 권장 |
| `organizationId` | UUID string | Y | credential 소속 조직과 일치 |
| `service` | object | Y | 등록 서비스 식별자와 환경 |
| `data` | object | Y | 오류 상세 |

### `service`

| 필드 | 타입 | 필수 | 제약 |
|---|---|---:|---|
| `id` | UUID string | Y | 등록 시 발급 값 |
| `key` | string | Y | routing key 및 AMQP `app_id`와 동일 |
| `environment` | enum | Y | `production`, `staging`, `development`, `test` |

### `data`

| 필드 | 타입 | 필수 | 제약 |
|---|---|---:|---|
| `severity` | enum | Y | `warning`, `error`, `critical` |
| `title` | string | Y | 1~200자, 사람이 식별할 수 있는 오류명 |
| `message` | string | Y | 1~8,192자, secret과 개인정보 제거 |
| `fingerprint` | string | N | 동일 원인을 묶는 안정적인 값, 최대 200자 |
| `exception` | object | N | 예외 타입, 메시지, stack trace, cause |
| `release` | object | N | 버전, Git commit SHA, 배포 시각 |
| `runtime` | object | N | 런타임, 리전, 익명화된 instance ID |
| `traceContext` | object | N | trace/span/request 식별자 |
| `request` | object | N | HTTP method, route template, status code만 허용 |
| `tags` | object | N | 최대 50개, key 64자, value 256자 |
| `logReferences` | array | N | 등록된 로그 공급자의 opaque locator, 최대 20개 |
| `extensions` | object | N | 사전 합의된 확장만 사용, 최대 20개 scalar 값 |

### 예외 정보

- `exception.stackTrace`는 최대 65,536자다.
- cause는 최대 5개이며 각 cause에는 `type`과 `message`만 넣는다.
- minified source, source map, heap dump, core dump는 메시지에 넣지 않는다.
- 큰 artifact는 등록된 객체 스토리지에 저장하고 `logReferences`의 opaque locator로 참조한다.

### HTTP 요청 정보

허용 필드는 `method`, route template, `statusCode`뿐이다. 다음 값은 보내지 않는다.

- 원본 URL query string
- request/response body
- Authorization, Cookie를 포함한 원본 headers
- client IP
- 사용자 이메일, 전화번호, 이름
- access token, session ID, API key

## 7. 크기와 형식 제한

| 항목 | 제한 |
|---|---:|
| AMQP body 전체 | 262,144 bytes |
| `message` | 8,192 characters |
| `stackTrace` | 65,536 characters |
| `tags` | 50 entries |
| `logReferences` | 20 entries |
| `exception.causes` | 5 entries |
| 메시지 clock skew | 미래 방향 최대 5분 |

제한을 넘는 로그는 잘라서 보내지 말고 외부 로그 저장소에 기록한 뒤 locator만 제공한다. Consumer는 body를 역직렬화하기 전에 byte 크기를 검사한다.

## 8. 생산자 처리 규칙

### 발행 순서

1. 등록된 AMQPS endpoint에 TLS로 연결한다.
2. publisher confirm mode를 활성화한다.
3. 서버가 제공한 exchange가 존재하는지 passive check한다.
4. JSON Schema로 body를 검증한다.
5. persistent, mandatory message로 발행한다.
6. confirm 또는 return을 비동기로 처리한다.
7. confirm timeout, nack, connection loss이면 같은 `message_id`로 재시도한다.

### 재시도

권장 backoff는 `1s, 2s, 5s, 10s, 30s`, 이후 최대 5분 간격이다. 생산자는 최소 24시간 동안 로컬 durable outbox를 유지한다.

- confirm 수신: broker 접수 성공, 로컬 outbox 제거 가능
- `basic.return`: routing 실패, 재시도 중지 후 운영 경고
- `basic.nack`: 같은 ID로 재시도
- confirm timeout/connection loss: 결과를 알 수 없으므로 같은 ID로 재시도
- 인증·권한 실패: 재시도 중지, credential 확인

오류 발생 경로에서 broker publish를 동기적으로 기다려 사용자 요청을 지연시키지 않는다. 애플리케이션은 오류 이벤트를 로컬 outbox에 기록하고 별도 publisher가 전송하는 방식을 권장한다.

## 9. Consumer, retry, DLQ 규칙

### 성공

1. AMQP property와 header를 검증한다.
2. body byte 제한과 JSON syntax를 검증한다.
3. v1 JSON Schema를 검증한다.
4. credential, routing key, body의 조직·서비스·환경 일치를 확인한다.
5. PostgreSQL transaction 안에서 inbox deduplication, occurrence, incident, outbox를 기록한다.
6. transaction commit 이후 `basic.ack`한다.

이미 `(service_id, message_id)`가 존재하면 occurrence를 다시 만들지 않고 중복 수신 메타데이터만 갱신한 후 ack한다.

### 일시 실패

DB 연결 실패, lock timeout, 일시적인 내부 의존성 실패는 retry 대상이다.

| 시도 | 지연 |
|---:|---:|
| 1 | 10초 |
| 2 | 60초 |
| 3 | 300초 |

Consumer는 retry queue로의 재발행이 publisher confirm된 뒤 원본 메시지를 ack한다. 재시도 횟수는 broker의 `x-death`와 내부 inbox attempt를 함께 기록한다. 재시도를 소진하면 DLQ로 보낸다.

### 영구 실패

다음 오류는 즉시 reject하고 DLQ로 보낸다.

- JSON syntax 또는 schema 오류
- body 크기 제한 초과
- credential과 organization/service 불일치
- 허용되지 않은 routing key
- 지원하지 않는 `type` 또는 schema major version
- timestamp가 허용 범위를 크게 벗어남

DLQ 항목은 원문, 실패 코드, 실패 위치, 최초·마지막 수신 시각, redelivery 횟수를 보존한다. DLQ replay는 운영자만 실행할 수 있으며, 원문을 수정해야 하면 새로운 `message_id`를 발급한다.

## 10. 오류 코드

| 코드 | 분류 | 의미 |
|---|---|---|
| `AEH-MQ-001` | permanent | content type 또는 encoding 오류 |
| `AEH-MQ-002` | permanent | JSON parsing 실패 |
| `AEH-MQ-003` | permanent | JSON Schema 검증 실패 |
| `AEH-MQ-004` | permanent | message ID 불일치 또는 형식 오류 |
| `AEH-MQ-005` | permanent | 조직·서비스·routing key 불일치 |
| `AEH-MQ-006` | permanent | payload 크기 제한 초과 |
| `AEH-MQ-007` | permanent | 지원하지 않는 계약 버전 |
| `AEH-MQ-101` | transient | PostgreSQL 연결 또는 transaction 실패 |
| `AEH-MQ-102` | transient | 내부 Outbox 기록 실패 |
| `AEH-MQ-103` | transient | Consumer 처리 timeout |

## 11. 순서와 중복

- 서로 다른 메시지 사이의 전역 순서는 보장하지 않는다.
- 같은 서비스의 메시지도 Consumer 병렬성 때문에 처리 완료 순서가 달라질 수 있다.
- 오류 분석은 `time`이 아니라 DB의 incident/occurrence 관계를 기준으로 한다.
- 동일 오류가 여러 instance에서 발생하면 각각 고유 `message_id`를 사용하고 동일 `fingerprint`를 사용한다.
- 네트워크 재시도는 동일 `message_id`를 사용한다.
- 이미 ack된 메시지의 내용을 수정해 재발행하려면 새로운 `message_id`를 사용한다.

## 12. 버전 정책

버전은 envelope의 `specVersion`, event `type`, exchange 이름, schema 파일에 함께 나타난다.

- 선택 필드 추가: v1 안에서 허용하되 Consumer schema 배포 후 Producer가 사용한다.
- enum 값 추가: Consumer가 모르는 값을 거절하므로 사전 배포가 필요하다.
- 필드 삭제·의미 변경·타입 변경: `v2` event type과 exchange를 만든다.
- v1과 v2는 migration 기간 동안 동시에 소비한다.
- 임의 확장은 `data.extensions`에만 넣는다.

생산자는 자신이 빌드될 때 사용한 schema checksum을 기록하고, 계약 테스트에서 서버 제공 schema와 호환성을 검사한다.

## 13. 인증과 보안

- 운영 연결은 TLS 1.2 이상을 사용하는 AMQPS만 허용한다.
- 조직별 virtual host와 서비스별 credential을 사용한다.
- credential은 지정 exchange와 routing prefix에 대한 `write`만 허용한다.
- 생산자는 queue consume, topology configure, 다른 서비스 routing key publish 권한을 갖지 않는다.
- credential은 Secret Manager에 저장하고 주기적으로 rotation한다.
- Broker는 connection rate, publish rate, 최대 channel 수, 최대 message size를 제한한다.
- Consumer는 body의 URL이나 파일 경로를 직접 fetch하지 않는다.
- `logReferences.locator`는 등록된 provider adapter가 해석하는 opaque ID다.
- payload는 저장 전과 Codex 전달 전 두 번 secret/PII redaction 검사를 거친다.

## 14. 관측성

생산자 지표:

- publish attempt/confirm/nack/return/timeout 수
- confirm latency
- durable outbox backlog와 oldest age
- broker reconnect 수

Consumer 지표:

- queue depth와 oldest message age
- ingest 처리량과 처리 시간
- schema rejection, duplicate, retry, DLQ 수
- ack 이전 DB transaction 실패율
- 서비스·환경·severity별 사건 수

로그에는 credential, 전체 message body, stack trace를 남기지 않는다. `message_id`, `service_id`, routing key, schema version, 결과 코드만 구조화 로그로 남긴다.

## 15. 생산자 완료 조건

외부 서비스 연동은 다음 계약 테스트를 모두 통과해야 완료로 본다.

1. 유효한 v1 메시지가 publisher confirm을 받고 사건으로 한 번 저장된다.
2. 같은 `message_id`를 3회 발행해도 occurrence가 한 번만 생성된다.
3. routing key와 body service가 다르면 DLQ로 이동한다.
4. 알 수 없는 필드, 잘못된 enum, 제한 초과 payload가 거절된다.
5. confirm timeout 후 같은 ID로 재전송해 데이터가 유실되지 않는다.
6. Consumer가 DB commit 전에 종료되면 redelivery 후 정상 처리된다.
7. Consumer가 DB commit 직후 ack 전에 종료돼도 중복 occurrence가 생성되지 않는다.
8. secret pattern이 포함된 메시지는 정책에 따라 마스킹 또는 거절된다.
9. 권한 없는 서비스 routing key로 publish할 수 없다.
10. DLQ 메시지를 운영자가 원인 확인 후 안전하게 replay할 수 있다.

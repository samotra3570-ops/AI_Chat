# GPT·Claude 서버 배포

## 재사용 연결 코드

앱 재설치나 다른 기기 복구 때 같은 코드를 사용할 수 있도록 `ACM_CONNECTION_CODE_SHA256`을 지원한다. 원문 코드는 운영자가 비공개로 보관하고 앱의 기존 연결 코드 칸에 입력한다. 활성 코드는 소유자 하나의 기존 예산과 원장을 공유하며 연결마다 별도 기기 세션을 발급한다. 기존 10분 일회용 코드도 사용할 수 있다. 상세 설정·교체·검증은 [CONNECTION.md](CONNECTION.md)를 따른다. 아래 일회용 코드 CLI 설명은 일회용 방식을 선택한 경우에 적용한다.

## 0.3.9 Billing

인증된 읽기 전용 OpenAI 비용 조회와 공식 Billing/Usage 링크를 추가했다. 자세한 범위·관리자 키·프로젝트 필터·시간 제한은 [BILLING.md](BILLING.md)를 따른다. 관리자 키가 없으면 GPT 연결을 그대로 유지하고 비용 조회만 `not_configured`로 표시한다. 현재 실제 크레딧 잔액의 자동 조회는 지원하지 않는다. 기존 모델·예산·원장·생성 키는 변경하지 않는다.

## 0.3.7 모델·예산 설정

운영 서버는 `https://acm-gateway-production.up.railway.app`이다. 저장소 루트의 Dockerfile과 railway.toml을 사용하며 `/data/acm`의 원장·키를 재사용한다. 아래 초기 배포 설명은 역사적 준비 단계의 기록이다.

앱의 **AI / API → 모델·예산 설정** 또는 **비용과 요청 기록 → 모델·예산 설정**에서 기본 모델과 요청당·하루·월 USD 한도를 저장한다. 기본 모델은 기기의 암호화된 연결 정보에 저장되며, 예산은 인증된 `GET/POST /v1/settings`를 통해 서버 SQLite `owner_settings`에 저장된다. 단일 소유자가 CLI로 발급한 코드로 연결한 모든 기기는 같은 예산을 공유한다. 연결 코드를 다른 사용자에게 배포하는 다중 계정 서비스로 사용하지 않는다.

예산 수정은 원장과 같은 SQLite 쓰기 트랜잭션으로 직렬화된다. `expected_revision`이 다르면 409로 거절해 다른 기기의 변경을 덮어쓰지 않는다. 기존 사용액·불확실한 요청의 예약 비용·가격 스냅샷은 수정하지 않는다. 환경 변수의 초기 예산이 재적용되어도 앱에서 저장한 예산은 유지된다. 0인 한도가 있으면 생성을 차단한다. UTC 기준 일·월 누적에 적용하며, 이 서버 이외의 API 사용과 Railway 호스팅 요금은 포함하지 않는다.

`config.verified-2026-10-09.json`은 공식 문서에서 확인한 GPT-4.1 nano, GPT-4.1 mini, GPT-5.4 mini의 날짜 고정 모델 ID와 Standard 가격이다. 모델을 자동 선택하지 않고 기본 예산은 0으로 둔다. 모델 목록에서 예약 상한과 가격 갱신 상태를 제공한다. 앱은 가격·API 키·서버 허용 모델을 변경할 수 없다. 가격 확인 7일 초과 차단 규칙은 유지한다. 공급자 계정의 모델 접근 권한·결제 상태와 실제 유료 응답은 별도 확인 대상이다.

공식 가격·입출력 지원 근거(2026-10-09 확인):

- https://developers.openai.com/api/docs/models/gpt-4.1-nano
- https://developers.openai.com/api/docs/models/gpt-4.1-mini
- https://developers.openai.com/api/docs/models/gpt-5.4-mini


이 패키지는 단일 소유자·단일 서버의 SQLite 원장을 유지한다. Railway 계정 연결과 전용 프로젝트·서비스 초안 생성은 확인했다. 공개 서버는 아직 배포하지 않았다. GitHub 소유자/저장소명은 사용자가 지정해야 하며 초안에는 소스·볼륨·실행 설정이 아직 반영되지 않았다. 제공된 Dockerfile의 컨테이너 빌드는 이번 환경에서 미실행이며, Python 실행 진입점과 상태 보존은 테스트로 검증한다.

## 배포 설정

Railway에서 이 소스의 `backend`를 서비스 루트(`/backend`)로 설정하고 config file path는 저장소 루트 기준 `/backend/railway.toml`을 사용한다. 전용 영구 볼륨을 `/data`에 연결하고 복제본·worker는 1개만 사용한다. 원장과 암호화 키는 `/data/acm`에 저장한다. 시작 시 기존 원장·키를 재사용하며, 원장만 남고 키가 사라지면 새 키를 생성하지 않고 중단한다. Docker 시작 과정은 볼륨을 준비한 뒤 UID 10001로 실행한다.

| 변수 | 설정 값 |
|---|---|
| `OPENAI_API_KEY` | OpenAI의 전용 비밀 변수에 등록할 API 키 |
| `ANTHROPIC_API_KEY` | Claude의 전용 비밀 변수에 등록할 API 키 |
| `ACM_CONFIG_JSON` | 아래 구조의 허용 모델·공식 확인 가격·예산 설정 |
| `ACM_PUBLIC_ORIGIN` | 배포 후 발급받은 `https://도메인` 원점 주소 |
| `ACM_TRUSTED_PROXY_IPS` | 실제 앞단 HTTPS 프록시의 확인된 IP/CIDR 목록. 쉼표로 구분. `*`는 거절 |

키는 서버의 보호 파일로 옮기고 Python 프로세스의 원래 키 환경 변수에서 제거한다. API 키 파일 경로를 직접 관리하는 기존 환경은 `ACM_OPENAI_KEY_FILE`, `ACM_ANTHROPIC_KEY_FILE`을 사용할 수 있다. 기존 `ACM_PROVIDER_KEY_FILE`은 OpenAI의 호환 경로로 유지한다. 배포 플랫폼의 비밀 변수 자체는 유지되어야 재배포 시 공급자를 활성화할 수 있다.

`ACM_CONFIG_JSON`은 기본적으로 모델이 없고 예산이 0이다. 모델 ID·가격·예산을 임의로 활성화하지 않는다. 운영자가 선택한 모델의 최신 공식 가격과 확인 시각을 설정한다. 가격 단위는 백만 토큰당 USD 문자열, 예산 단위는 마이크로달러(USD × 1,000,000) 정수다. 서버가 가격 확인 후 7일을 넘기면 생성을 차단한다.

```json
{
  "models": {},
  "budgets": {
    "day_micro_usd": 0,
    "month_micro_usd": 0,
    "request_micro_usd": 0
  }
}
```

각 허용 모델 항목에는 `provider` (`openai` 또는 `anthropic`), `input_price`, `cached_input_price`, `output_price`, `max_input_tokens`, `max_output_tokens`, `price_verified_at`(UTC epoch), `vision`이 필요하다. `provider`가 없는 기존 모델은 OpenAI로 해석한다. 키가 없는 공급자의 모델은 목록에서 제외하고 생성 전 차단한다. 두 공급자는 같은 원장의 일·월·건별 한도를 공유한다.

Claude는 JSON structured output을 지원하는 모델만 설정한다. 프롬프트 캐시 쓰기와 도구 호출을 요청하지 않는다. 캐시 읽기 토큰을 전체 입력에 합산해 정산하고, 예상하지 않은 캐시 쓰기 비용은 추정으로 정산하지 않고 예약을 유지한다. 이미지 모델의 가격·토큰 상한을 실제 공급자와 검증하기 전 `vision=false`를 유지한다.

## HTTPS와 연결 코드

Railway의 도메인 기능으로 주소를 발급하더라도, 이 앱은 임의 웹사이트나 OpenAI·Claude API 원점에 직접 연결하지 않는다. 서버 앞단의 실제 TLS와 프록시 주소 신뢰 설정을 확인한다. 신뢰되지 않은 `X-Forwarded-Proto`를 HTTPS 증거로 사용하지 않는다. `/health`는 내부 HTTP 상태 검사에만 예외적으로 허용하고, 모든 `/v1` 동작은 HTTPS가 필요하다. 신뢰할 프록시의 주소를 확인할 수 없다면 인증서 검사를 끄거나 `*`로 대체하지 말고 배포 단계에서 해결한다.

서버의 전용 운영 터미널에서 실행한다:

```bash
python -m gateway.deploy pair
```

출력의 `server_url`과 `pairing_code`를 앱에 입력한다. 코드는 10분·일회용이다. 코드 발급은 인증되지 않은 HTTP 경로에 노출하지 않는다. 키·코드·대화는 로그나 소스에 저장하지 않는다. 앱에서 GPT 또는 Claude를 고른 뒤 해당 서비스의 기본 모델을 선택한다. 방별 모델을 명시한 캐릭터는 기존 모델 설정을 따른다.

자체 서버에서 사용할 Docker 빌드 명령:

```bash
docker build -t acm-gateway backend
```

## 배포 후 확인

서버 주소·일회용 코드·모델 목록 → 사용자 승인 없는 견적 → 승인 응답 및 분할/줄바꿈 → 결과 조회 → 재연결/재시작 → 실제 공급자 usage와 비용 → 예산 차단을 확인한다. 유료 응답은 사용자가 정한 모델·예산·키가 제공된 뒤에만 실행한다. 결과가 불확실하면 GET 조회만 하고, 예약 해제와 재호출을 임의로 하지 않는다.

공식 근거:

- https://developers.openai.com/api/docs/quickstart
- https://platform.claude.com/docs/en/api/messages/create
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- https://docs.railway.com/volumes
- https://docs.railway.com/networking/public-networking

이번 결과의 실제 완료/미실행 범위는 `docs/PROVIDER_SELECTION_RESULT.json`을 따른다.

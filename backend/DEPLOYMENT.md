# GPT·Claude 서버 배포

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

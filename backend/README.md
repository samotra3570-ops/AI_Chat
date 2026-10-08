# 0.3.0 Gateway 재구성본

기록상 delta를 재구성한 단일 소유자·단일 호스트 서버입니다. 원본 0.3.0의 코드 동일성은 보장하지 않습니다. 앱 API 키 입력은 없으며 운영자가 서버에만 공급자 키를 둡니다. 기본 config의 모델 목록은 비어 있고 예산은 0이므로 유료 요청을 실행할 수 없습니다. 이 작업에서 실제 공급자 호출이나 배포는 하지 않았습니다.

Python 환경을 만들고 requirements.lock을 설치한 뒤 `PYTHONPATH=backend python -m pytest backend/tests -q`로 모의 검증합니다. 실제 실행은 `PYTHONPATH=backend uvicorn gateway.server:create_server --factory`이며 HTTPS 인증서 또는 신뢰할 프록시를 별도 구성해야 합니다. HTTPS URL scheme 검사 때문에 HTTP 직접 접근은 거절합니다. 프록시 forwarded 헤더는 신뢰할 주소만 허용하고 public 임의 헤더를 신뢰하지 않습니다. access log를 끄고 요청 body·pair code·bearer token·공급자 키를 기록하지 않습니다.

필수 환경 경로: ACM_CONFIG_FILE, ACM_SECRET_FILE(독립 random32bytes, owner read only), ACM_LEDGER_FILE. 선택 ACM_PROVIDER_KEY_FILE(owner read only). 이 파일들은 저장소나 소스 ZIP에 넣지 않습니다. 운영 DB/WAL/SHM과 비밀 파일을 보호하고 SQLite 파일을 여러 호스트가 동시에 공유하지 않습니다.

config 모델별 설정은 input_price/cached_input_price/output_price(USD per million tokens decimal string), max_input_tokens/max_output_tokens(양의 정수), price_verified_at(UTC epoch seconds), vision(boolean)입니다. 가격은 운영자가 공식 공급자 가격을 확인하여 설정하며 7일이 지나면 요청을 차단합니다. 모델별 이미지 비용 상한은 실 API 확인 전 vision을 켜지 않습니다. budgets의 day_micro_usd/month_micro_usd/request_micro_usd는 양의 정수입니다. 허용 모델·가격·예산을 임의 기본값으로 활성화하지 않습니다.

일회용 pairing code는 `PYTHONPATH=backend python -m gateway.admin --database PATH --secret-file PATH --config-file PATH pair`로 발급합니다. access 10분/refresh7일, refresh rotation 및 재사용 탐지 revocation을 적용합니다. 비용 단위는 micro-USD입니다. `/v1/quote`는 비용만 계산하며 `/v1/generate`는 approved_micro_usd가 없거나 증가한 비용이 확인값을 넘으면 거절합니다. 예약과 일/월/건별 Hard Cap은 동일 SQLite 트랜잭션에서 처리합니다.

결과가 불확실한 요청은 예약 금액을 계속 보류합니다. 동일 request_id 재호출은 공급자 POST를 반복하지 않고 기존 결과를 반환합니다. 앱 재시도는 GET 조회만 수행합니다. 공급자 usage/receipt/invoice를 확인하지 않고 hold를 해제하거나 금액을 0으로 바꾸지 않습니다. 운영자가 검증된 근거를 확보한 경우에만 GatewayCore.settle의 reconciliation_reference로 정산하며 이 근거는 서버 로컬 운영 작업입니다. 원장 초기화로 예산을 우회하지 않습니다. 결과 암호화 키를 잃으면 읽지 못하므로 안전하게 보존해야 합니다.

실제 과금·token upper bound·vision 모델 가격·타임아웃 청구·reverse proxy HTTPS·rotation·backup recovery는 생산 투입 전 별도 검증 게이트입니다. 과거 44개가 아니라 이번 backend test 실제 결과를 사용합니다.

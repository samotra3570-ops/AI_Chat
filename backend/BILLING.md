# OpenAI Billing 연결 — 0.3.9 / D-045

앱의 비용과 요청 기록 화면에서 앱 예산/원장과 OpenAI Billing을 구분한다. 실제 충전 크레딧 잔액은 숫자를 추정하지 않고 고정 공식 Billing 링크를 Android 브라우저로 연다. 로그인 정보는 앱/서버에서 수집하지 않는다. 브라우저의 계정·조직은 사용자가 확인한다.

## 공식 사용 비용

인증된 `GET /v1/billing/openai`는 OpenAI Costs API를 읽기 전용으로 조회한다. 일반 생성 키는 이 조회에 사용하지 않는다. 관리자 키가 없으면 `not_configured`를 반환하고 비용/잔액 0을 만들어 반환하지 않는다. `balance`는 항상 null이며 `balance_status`는 `official_page_only`다. 기존 생성·승인·예약·정산·예산 저장은 변경하지 않는다.

소유자가 별도로 허용한 관리자 키는 보호된 서버 변수 `OPENAI_ADMIN_KEY` 또는 `ACM_OPENAI_ADMIN_KEY_FILE`에 둔다. 앱 입력과 공개 저장소/전달 ZIP에는 넣지 않는다. Railway 실행 진입점은 키가 제공된 경우에만 전용 상태 영역의 0600 파일에 저장한다. 이번 작업에서는 관리자 키를 생성/등록하지 않았다. 연결 및 관리자 권한 부여는 별도 작업이다.

선택 변수 `ACM_OPENAI_BILLING_PROJECT_ID`를 `proj_...`로 지정하면 해당 프로젝트로 필터링한다. 미지정 시 **관리자 키가 속한 조직 전체** 비용이므로 다른 앱 사용도 포함될 수 있다. 실제 GPT 키와 동일 조직/프로젝트인지 소유자가 확인해야 한다. 외부 Billing 링크는 조직을 자동 선택하지 않는다.

UTC 달력 기준 오늘/이번 달, USD, Decimal 합산 후 소수점 6자리 반올림으로 표시한다. 공식 집계 지연이 있을 수 있으며 실시간 차단 한도로 사용하지 않는다. 앱 Hard Cap은 기존 원장에 계속 적용한다. 충전/환급/크레딧 만료/자동 충전 내역을 추정하여 잔액으로 만들지 않는다.

서버는 전체 조회 16초, upstream 조회 15초, 페이지6개/응답2MiB 제한을 적용한다. 일자별 성공 캐시60초, 오류15초; UTC 날짜가 바뀌면 갱신한다. 401/403은 권한 확인 상태, 잘못된 금액/통화/부분 페이지/429/네트워크 실패는 조회 불가로 안전하게 종료한다. URL과 요청 범위는 서버 고정이며 외부 페이지/credential 변경 입력을 받지 않는다. 인증·HTTPS 필수 및 no-store를 유지한다.

앱은 읽기 조회20초, 브라우저 실행8초 제한을 적용하고 늦은 결과를 무시한다. 마지막 비용은 조회 시각/범위/UTC 및 오래된 내역 여부와 함께 표시한다. 브라우저 복귀 시 갱신하며, 실패 시 자동 무한 재시도하지 않는다.

공식 문서(2026-10-09 확인):

- https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs
- https://developers.openai.com/cookbook/examples/completions_usage_api
- https://developers.openai.com/api/docs/guides/admin-apis

직접 계정 로그인 후 충전 잔액을 반환하는 공개 공식 API는 확인되지 않았다. 실제 계정 잔액·로그인·관리자 키 연동과 실기기 브라우저 복귀는 미검증이다. 테스트는 모의 공급자 및 독립 시험 데이터로 실행한다.

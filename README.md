# AI Character Messenger Gateway

GPT(OpenAI)·Claude(Anthropic) 연결 서버입니다. 앱 0.3.6의 서비스 선택과 비용 원장을 지원합니다.

Railway는 저장소 루트의 `Dockerfile`과 `railway.toml`을 사용합니다. 서비스 루트는 `/`로 유지합니다. 실행 소스는 `backend/gateway/`이며, `/data` 영구 볼륨과 복제본 1개를 사용합니다.

API 키는 서버 비밀 설정으로 관리합니다. 기본 모델 목록은 비어 있고 예산은 0입니다. 공개 HTTPS·프록시·운영 설정 검증 후 유료 모델 호출을 활성화합니다. 운영자 연결 코드 발급과 상세 설정은 `backend/DEPLOYMENT.md`를 따릅니다. `backend/Dockerfile`은 backend를 루트로 사용하는 별도 환경용으로 유지합니다.

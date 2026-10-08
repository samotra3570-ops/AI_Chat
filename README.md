# AI Character Messenger Gateway

GPT(OpenAI)·Claude(Anthropic) 연결 서버입니다. 앱 0.3.6의 서비스 선택과 비용 원장을 지원합니다.

서버 소스는 `backend/`에 있습니다. Railway 서비스 루트는 `/backend`, 설정 파일은 `/backend/railway.toml`입니다. 영구 볼륨은 `/data`에 연결하고 복제본은 1개만 사용합니다.

API 키는 서버 비밀 설정으로 관리합니다. 기본 모델 목록은 비어 있고 예산은 0입니다. 운영 설정과 공개 HTTPS 검증 전 유료 모델 호출을 활성화하지 않습니다. 자세한 내용은 `backend/DEPLOYMENT.md`를 따릅니다.

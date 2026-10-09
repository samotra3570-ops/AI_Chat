"""전용 영구 저장 영역에 설정·키·원장을 보존하는 서버 실행 진입점."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


def configure_runtime():
    # 기존 원장과 암호화 키를 보존하고 전용 디렉터리의 접근 권한을 제한한다.
    os.umask(0o077)
    state = Path(os.environ.get('ACM_STATE_DIR', '/data/acm'))
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    secret, ledger, config = (state / name for name in ('secret.bin', 'ledger.sqlite', 'config.json'))
    if not secret.exists():
        if ledger.exists():
            raise ValueError('Existing ledger requires its original secret')
        with secret.open('xb') as target:
            target.write(secrets.token_bytes(32))
    raw = os.environ.pop('ACM_CONFIG_JSON', None)
    if raw is not None:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('models'), dict) or not isinstance(value.get('budgets'), dict):
            raise ValueError('Invalid gateway configuration')
        temporary = state / 'config.json.new'
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(config)
    elif not config.exists():
        config.write_text(json.dumps({'models': {}, 'budgets': {
            'day_micro_usd': 0, 'month_micro_usd': 0, 'request_micro_usd': 0}}))
    os.environ.update(ACM_CONFIG_FILE=str(config), ACM_SECRET_FILE=str(secret), ACM_LEDGER_FILE=str(ledger))
    for provider, variable in [('OPENAI', 'OPENAI_API_KEY'), ('ANTHROPIC', 'ANTHROPIC_API_KEY'),
                               ('OPENAI_ADMIN', 'OPENAI_ADMIN_KEY')]:
        key = os.environ.pop(variable, None)
        if key is not None:
            if not key.strip():
                raise ValueError('Empty provider credential')
            target = state / (provider.lower() + '.credential')
            target.write_text(key.strip())
            target.chmod(0o600)
            os.environ['ACM_' + provider + '_KEY_FILE'] = str(target)
    # 컨테이너는 저장 영역 초기화 후 전용 UID로 권한을 낮춰 실행한다.
    run_uid = os.environ.get('ACM_RUN_UID')
    if run_uid and hasattr(os, 'getuid') and os.getuid() == 0:
        uid = int(run_uid)
        if uid <= 0:
            raise ValueError('A non-root runtime UID is required')
        for target in [state, *state.iterdir()]:
            if target.is_symlink():
                raise ValueError('Symlinks are not supported in gateway state')
            os.chown(target, uid, uid)
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return state


def public_origin():
    raw = os.environ.get('ACM_PUBLIC_ORIGIN', '').strip()
    url = urlsplit(raw)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('Configure ACM_PUBLIC_ORIGIN with the issued HTTPS origin')
    return raw.rstrip('/')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('serve', 'pair'))
    args = parser.parse_args()
    configure_runtime()
    if args.operation == 'pair':
        from .core import GatewayCore
        origin = public_origin()
        core = GatewayCore(os.environ['ACM_LEDGER_FILE'], Path(os.environ['ACM_SECRET_FILE']).read_bytes(),
                           json.loads(Path(os.environ['ACM_CONFIG_FILE']).read_text()))
        # 연결 코드는 운영자의 명시적 CLI 작업에서만 표시한다. HTTP로 발급하지 않는다.
        print(json.dumps({'server_url': origin, 'pairing_code': core.pairing_code(), 'expires_in_seconds': 600}))
        return
    trusted = os.environ.get('ACM_TRUSTED_PROXY_IPS', '127.0.0.1')
    if '*' in trusted or not trusted.strip():
        raise ValueError('Configure explicit trusted proxy IPs/CIDRs; wildcard is not allowed')
    import uvicorn
    uvicorn.run('gateway.server:create_server', factory=True,
                host=os.environ.get('ACM_BIND_HOST', '127.0.0.1'),
                port=int(os.environ.get('PORT', '8000')), workers=1,
                proxy_headers=True, forwarded_allow_ips=trusted,
                access_log=False, timeout_keep_alive=5, timeout_graceful_shutdown=10)


if __name__ == '__main__':
    main()

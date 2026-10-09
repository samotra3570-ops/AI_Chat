"""Explicit environment-only deployment factory. No paid request at startup."""
import json, os
from pathlib import Path
from .core import GatewayCore
from .api import create_app
from .provider import OpenAIResponsesProvider, AnthropicMessagesProvider, ProviderRouter
from .billing import OpenAIBilling

def create_server():
    config=json.loads(Path(os.environ['ACM_CONFIG_FILE']).read_text())
    secret=Path(os.environ['ACM_SECRET_FILE']).read_bytes()
    core=GatewayCore(os.environ['ACM_LEDGER_FILE'],secret,config)
    providers = {}
    for name, adapter, variable in [
        ('openai', OpenAIResponsesProvider, 'ACM_OPENAI_KEY_FILE'),
        ('anthropic', AnthropicMessagesProvider, 'ACM_ANTHROPIC_KEY_FILE')]:
        keyfile = os.environ.get(variable)
        if name == 'openai' and not keyfile:
            keyfile = os.environ.get('ACM_PROVIDER_KEY_FILE')
        if keyfile:
            providers[name] = adapter(Path(keyfile).read_text().strip())
    models = config.get('models', {})
    if not isinstance(models, dict) or any(not isinstance(cfg, dict) or
            cfg.get('provider', 'openai') not in ('openai', 'anthropic')
            for cfg in models.values()):
        raise ValueError('Invalid model provider configuration')
    provider = ProviderRouter(models, providers) if providers else None
    admin_file = os.environ.get('ACM_OPENAI_ADMIN_KEY_FILE')
    admin_key = Path(admin_file).read_text().strip() if admin_file else os.environ.get('OPENAI_ADMIN_KEY')
    billing = OpenAIBilling(admin_key, os.environ.get('ACM_OPENAI_BILLING_PROJECT_ID'))
    return create_app(core, provider, billing)

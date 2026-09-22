from app.arca.service import _sanitize_provider_payload
from app.core.config import get_settings


def test_api_token_is_required_by_default(monkeypatch):
    monkeypatch.delenv('REQUIRE_API_TOKEN', raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().require_api_token is True
    finally:
        get_settings.cache_clear()


def test_provider_payload_is_recursively_redacted():
    payload = {
        'result': 'approved',
        'nested': {
            'access_token': 'secret-token',
            'private_key_pem': 'private-key',
            'message': 'Authorization: Bearer exposed',
        },
    }

    sanitized = _sanitize_provider_payload(payload)

    assert sanitized['result'] == 'approved'
    assert sanitized['nested']['access_token'] == '[REDACTED]'
    assert sanitized['nested']['private_key_pem'] == '[REDACTED]'
    assert 'exposed' not in sanitized['nested']['message']

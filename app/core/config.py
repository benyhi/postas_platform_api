import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]


def load_env_file(path: str | Path = BASE_DIR / ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return float(raw_value)


def _int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return int(raw_value)


def _csv_env(name: str) -> tuple[str, ...]:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return ()
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str
    api_prefix: str
    database_url: str
    postas_service_token: str | None
    service_token_header_name: str
    service_source_header_name: str
    ai_provider: str
    fallback_ai_provider: str | None
    max_ai_attempts: int
    google_api_key: str | None
    google_model: str
    accepted_confidence_threshold: float
    minimum_confidence_threshold: float
    input_token_cost_per_million: float
    output_token_cost_per_million: float
    postas_ai_api_token: str | None
    require_api_token: bool
    token_header_name: str
    source_header_name: str
    allowed_request_sources: tuple[str, ...]
    image_download_timeout_seconds: float
    max_image_bytes: int
    internal_require_tls: bool
    arca_credential_master_keys: str | None
    arca_credential_active_key_id: str | None
    arca_production_calls_enabled: bool
    arca_timeout_seconds: float
    arca_worker_poll_seconds: float
    arca_worker_batch_size: int
    arca_consumer_final_identification_threshold: float
    mercado_pago_client_id: str | None
    mercado_pago_client_secret: str | None
    mercado_pago_redirect_uri: str | None
    mercado_pago_api_base_url: str
    mercado_pago_auth_base_url: str
    mercado_pago_platform_id: str | None
    mercado_pago_application_id: str | None
    mercado_pago_integration_id: str | None
    mercado_pago_connect_timeout_seconds: float
    mercado_pago_read_timeout_seconds: float
    mercado_pago_write_timeout_seconds: float
    mercado_pago_oauth_state_ttl_seconds: int
    mercado_pago_refresh_skew_seconds: int
    mercado_pago_credential_master_keys: str | None
    mercado_pago_credential_active_key_id: str | None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_env_file()
    fallback_provider = os.getenv("FALLBACK_AI_PROVIDER")
    fallback_provider = fallback_provider.strip() if fallback_provider else None
    api_token = os.getenv("POSTAS_AI_API_TOKEN") or os.getenv("API_SHARED_TOKEN")
    api_token = api_token.strip() if api_token else None
    service_token = os.getenv("POSTAS_SERVICE_TOKEN")
    service_token = service_token.strip() if service_token else None
    master_keys = os.getenv("ARCA_CREDENTIAL_MASTER_KEYS")
    master_keys = master_keys.strip() if master_keys else None
    active_key_id = os.getenv("ARCA_CREDENTIAL_ACTIVE_KEY_ID")
    active_key_id = active_key_id.strip() if active_key_id else None
    mp_master_keys = os.getenv("MERCADO_PAGO_CREDENTIAL_MASTER_KEYS")
    mp_master_keys = mp_master_keys.strip() if mp_master_keys else None
    mp_active_key_id = os.getenv("MERCADO_PAGO_CREDENTIAL_ACTIVE_KEY_ID")
    mp_active_key_id = mp_active_key_id.strip() if mp_active_key_id else None

    return Settings(
        app_name=os.getenv("APP_NAME", "Postas Platform API"),
        api_prefix=os.getenv("API_PREFIX", "/api/v1"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./postas_platform.db"),
        postas_service_token=service_token,
        service_token_header_name=os.getenv("POSTAS_SERVICE_TOKEN_HEADER", "X-Postas-Service-Token"),
        service_source_header_name=os.getenv("POSTAS_SERVICE_SOURCE_HEADER", "X-Postas-Source"),
        ai_provider=os.getenv("AI_PROVIDER", "google_genai").strip(),
        fallback_ai_provider=fallback_provider or None,
        max_ai_attempts=max(1, _int_env("MAX_AI_ATTEMPTS", 1)),
        google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("API_KEY"),
        google_model=os.getenv("GOOGLE_MODEL", "gemini-2.5-flash-lite"),
        accepted_confidence_threshold=_float_env("ACCEPTED_CONFIDENCE_THRESHOLD", 0.85),
        minimum_confidence_threshold=_float_env("MINIMUM_CONFIDENCE_THRESHOLD", 0.60),
        input_token_cost_per_million=_float_env("INPUT_TOKEN_COST_PER_MILLION", 0.0),
        output_token_cost_per_million=_float_env("OUTPUT_TOKEN_COST_PER_MILLION", 0.0),
        postas_ai_api_token=api_token,
        require_api_token=_bool_env("REQUIRE_API_TOKEN", False),
        token_header_name=os.getenv("POSTAS_AI_TOKEN_HEADER", "X-Postas-AI-Token"),
        source_header_name=os.getenv("POSTAS_AI_SOURCE_HEADER", "X-Postas-Source"),
        allowed_request_sources=_csv_env("ALLOWED_REQUEST_SOURCES"),
        image_download_timeout_seconds=_float_env("IMAGE_DOWNLOAD_TIMEOUT_SECONDS", 15.0),
        max_image_bytes=max(1, _int_env("MAX_IMAGE_BYTES", 10 * 1024 * 1024)),
        internal_require_tls=_bool_env("POSTAS_INTERNAL_REQUIRE_TLS", True),
        arca_credential_master_keys=master_keys,
        arca_credential_active_key_id=active_key_id,
        arca_production_calls_enabled=_bool_env("ARCA_PRODUCTION_CALLS_ENABLED", False),
        arca_timeout_seconds=_float_env("ARCA_TIMEOUT_SECONDS", 30.0),
        arca_worker_poll_seconds=_float_env("ARCA_WORKER_POLL_SECONDS", 5.0),
        arca_worker_batch_size=max(1, _int_env("ARCA_WORKER_BATCH_SIZE", 20)),
        arca_consumer_final_identification_threshold=_float_env(
            "ARCA_CONSUMER_FINAL_IDENTIFICATION_THRESHOLD", 10_000_000.0
        ),
        mercado_pago_client_id=os.getenv("MERCADO_PAGO_CLIENT_ID") or None,
        mercado_pago_client_secret=os.getenv("MERCADO_PAGO_CLIENT_SECRET") or None,
        mercado_pago_redirect_uri=os.getenv("MERCADO_PAGO_REDIRECT_URI") or None,
        mercado_pago_api_base_url=os.getenv(
            "MERCADO_PAGO_API_BASE_URL", "https://api.mercadopago.com"
        ).rstrip("/"),
        mercado_pago_auth_base_url=os.getenv(
            "MERCADO_PAGO_AUTH_BASE_URL", "https://auth.mercadopago.com"
        ).rstrip("/"),
        mercado_pago_platform_id=os.getenv("MERCADO_PAGO_PLATFORM_ID") or None,
        mercado_pago_application_id=os.getenv("MERCADO_PAGO_APPLICATION_ID") or None,
        mercado_pago_integration_id=(
            os.getenv("MERCADO_PAGO_INTEGRATOR_ID")
            or os.getenv("MERCADO_PAGO_INTEGRATION_ID")
            or None
        ),
        mercado_pago_connect_timeout_seconds=_float_env("MERCADO_PAGO_CONNECT_TIMEOUT_SECONDS", 5.0),
        mercado_pago_read_timeout_seconds=_float_env("MERCADO_PAGO_READ_TIMEOUT_SECONDS", 20.0),
        mercado_pago_write_timeout_seconds=_float_env("MERCADO_PAGO_WRITE_TIMEOUT_SECONDS", 20.0),
        mercado_pago_oauth_state_ttl_seconds=max(
            60, _int_env("MERCADO_PAGO_OAUTH_STATE_TTL_SECONDS", 600)
        ),
        mercado_pago_refresh_skew_seconds=max(
            0, _int_env("MERCADO_PAGO_REFRESH_SKEW_SECONDS", 300)
        ),
        mercado_pago_credential_master_keys=mp_master_keys,
        mercado_pago_credential_active_key_id=mp_active_key_id,
    )

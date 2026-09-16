from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.mercado_pago.client import MercadoPagoClient, MercadoPagoClientError, normalize_order
from app.mercado_pago.crypto import MercadoPagoCredentialCipher
from app.mercado_pago.models import MercadoPagoConnection, MercadoPagoOAuthState
from app.mercado_pago.schemas import MercadoPagoOrderCreateRequest, OAuthTokenData
from app.mercado_pago.service import (
    MercadoPagoDomainError,
    MercadoPagoService,
    build_order_body,
)


class FakeMercadoPagoClient:
    def __init__(self) -> None:
        self.exchange_calls = 0
        self.refresh_calls = 0
        self.last_access_token = None

    def exchange_authorization_code(self, code: str, code_verifier: str) -> OAuthTokenData:
        self.exchange_calls += 1
        assert code == "authorization-code"
        assert len(code_verifier) >= 43
        return OAuthTokenData(
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_in=3600,
            user_id="collector-123",
            scope="offline_access read write",
            live_mode=False,
            application_id="app-123",
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokenData:
        self.refresh_calls += 1
        assert refresh_token == "refresh-secret"
        return OAuthTokenData(
            access_token="refreshed-access-secret",
            refresh_token="rotated-refresh-secret",
            expires_in=7200,
            user_id="collector-123",
            scope="read write",
        )

    def list_terminals(self, access_token: str):
        self.last_access_token = access_token
        return [{"id": "POINT-1", "status": "active", "secret": "must-not-leak"}]

    def list_pos(self, access_token: str):
        self.last_access_token = access_token
        return [{"id": 1, "name": "Caja", "external_id": "POS-1", "access_token": "no"}]


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("MERCADO_PAGO_CREDENTIAL_MASTER_KEYS", json.dumps({"key-1": key}))
    monkeypatch.setenv("MERCADO_PAGO_CREDENTIAL_ACTIVE_KEY_ID", "key-1")
    monkeypatch.setenv("MERCADO_PAGO_CLIENT_ID", "client-123")
    monkeypatch.setenv("MERCADO_PAGO_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("MERCADO_PAGO_REDIRECT_URI", "https://platform.test/api/v1/mercado-pago/oauth/callback")
    monkeypatch.setenv("MERCADO_PAGO_PLATFORM_ID", "platform-123")
    monkeypatch.setenv("MERCADO_PAGO_INTEGRATION_ID", "integrator-123")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with TestingSessionLocal() as db:
        yield db
    Base.metadata.drop_all(engine)
    get_settings.cache_clear()


def authorize(service: MercadoPagoService) -> tuple[str, MercadoPagoConnection]:
    start = service.start_authorization(uuid4())
    state = parse_qs(urlparse(start.authorization_url).query)["state"][0]
    response = service.complete_authorization(state, "authorization-code")
    connection = service.db.scalar(
        select(MercadoPagoConnection).where(
            MercadoPagoConnection.tenant_id == str(response.tenant_id)
        )
    )
    assert connection is not None
    return state, connection


def test_oauth_state_is_hashed_encrypted_single_use_and_tokens_are_not_exposed(
    db_session: Session,
) -> None:
    fake = FakeMercadoPagoClient()
    service = MercadoPagoService(db_session, client=fake)
    tenant_id = uuid4()

    start = service.start_authorization(tenant_id)
    query = parse_qs(urlparse(start.authorization_url).query)
    state = query["state"][0]
    stored_state = db_session.scalar(select(MercadoPagoOAuthState))
    assert stored_state is not None
    assert stored_state.state_hash != state
    assert state not in stored_state.code_verifier_encrypted
    assert query["code_challenge_method"] == ["S256"]

    result = service.complete_authorization(state, "authorization-code")
    serialized = result.model_dump_json()
    connection = db_session.scalar(select(MercadoPagoConnection))
    assert connection is not None
    assert connection.access_token_encrypted != "access-secret"
    assert connection.refresh_token_encrypted != "refresh-secret"
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert result.collector_id == "collector-123"

    with pytest.raises(MercadoPagoDomainError) as reused:
        service.complete_authorization(state, "authorization-code")
    assert reused.value.code == "oauth_state_used"
    assert fake.exchange_calls == 1


def test_expired_oauth_state_is_rejected_without_exchange(db_session: Session) -> None:
    fake = FakeMercadoPagoClient()
    service = MercadoPagoService(db_session, client=fake)
    start = service.start_authorization(uuid4())
    state = parse_qs(urlparse(start.authorization_url).query)["state"][0]
    stored = db_session.scalar(select(MercadoPagoOAuthState))
    assert stored is not None
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    with pytest.raises(MercadoPagoDomainError) as expired:
        service.complete_authorization(state, "authorization-code")
    assert expired.value.code == "oauth_state_expired"
    assert fake.exchange_calls == 0


def test_expired_access_token_is_refreshed_and_rotated(db_session: Session) -> None:
    fake = FakeMercadoPagoClient()
    service = MercadoPagoService(db_session, client=fake)
    _, connection = authorize(service)
    connection.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    terminals = service.list_terminals(UUID(connection.tenant_id))

    assert terminals[0].id == "POINT-1"
    assert fake.refresh_calls == 1
    assert fake.last_access_token == "refreshed-access-secret"
    assert service.cipher.decrypt(
        connection.refresh_token_encrypted or "", connection.credential_key_id or ""
    ) == "rotated-refresh-secret"


def test_tenant_isolation_and_unlink_destroy_secrets(db_session: Session) -> None:
    fake = FakeMercadoPagoClient()
    service = MercadoPagoService(db_session, client=fake)
    _, connection = authorize(service)
    tenant_id = UUID(connection.tenant_id)
    other_tenant = uuid4()

    assert service.get_status(other_tenant).connected is False
    with pytest.raises(MercadoPagoDomainError) as missing:
        service.list_terminals(other_tenant)
    assert missing.value.code == "mercado_pago_not_connected"

    status = service.unlink(tenant_id)
    db_session.refresh(connection)
    assert status.connected is False
    assert status.status == "unlinked"
    assert connection.access_token_encrypted is None
    assert connection.refresh_token_encrypted is None


def test_client_marks_mutable_timeout_uncertain_and_never_retries(db_session: Session) -> None:
    calls = []

    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    http_client = httpx.Client(
        base_url="https://api.mercadopago.test", transport=httpx.MockTransport(timeout)
    )
    client = MercadoPagoClient(get_settings(), http_client=http_client)

    with pytest.raises(MercadoPagoClientError) as error:
        client.create_order("access", {"type": "point"}, "same-key")

    assert error.value.uncertain is True
    assert len(calls) == 1
    assert calls[0].headers["X-Idempotency-Key"] == "same-key"


@pytest.mark.parametrize("operation", ["cancel", "refund"])
def test_cancel_and_refund_timeout_once_with_the_same_idempotency_key(
    db_session: Session, operation: str
) -> None:
    calls = []

    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    client = MercadoPagoClient(
        get_settings(),
        http_client=httpx.Client(
            base_url="https://api.mercadopago.test",
            transport=httpx.MockTransport(timeout),
        ),
    )

    with pytest.raises(MercadoPagoClientError) as error:
        if operation == "cancel":
            client.cancel_order("access", "ORDER-1", "same-key")
        else:
            client.refund_order("access", "ORDER-1", "same-key")

    assert error.value.uncertain is True
    assert len(calls) == 1
    assert calls[0].headers["X-Idempotency-Key"] == "same-key"
    assert calls[0].url.path == f"/v1/orders/ORDER-1/{operation}"


def test_client_maps_remote_error_without_exposing_authorization(db_session: Session) -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"code": "unauthorized", "message": "invalid credential"},
            request=request,
        )

    client = MercadoPagoClient(
        get_settings(),
        http_client=httpx.Client(
            base_url="https://api.mercadopago.test", transport=httpx.MockTransport(unauthorized)
        ),
    )
    with pytest.raises(MercadoPagoClientError) as error:
        client.get_order("access-secret", "ORD-1")
    assert error.value.status_code == 401
    assert error.value.code == "unauthorized"
    assert "access-secret" not in error.value.message


def test_order_payload_uses_current_point_and_dynamic_qr_contract(db_session: Session) -> None:
    settings = get_settings()
    point = build_order_body(
        MercadoPagoOrderCreateRequest(
            type="point", external_reference="sale_1", amount="24.00", terminal_id="POINT-1"
        ),
        settings,
    )
    qr = build_order_body(
        MercadoPagoOrderCreateRequest(
            type="qr", external_reference="sale_2", amount="50.00", external_pos_id="POS-1"
        ),
        settings,
    )

    assert point["config"]["point"]["terminal_id"] == "POINT-1"
    assert point["transactions"]["payments"][0]["amount"] == "24.00"
    assert qr["config"]["qr"] == {"external_pos_id": "POS-1", "mode": "dynamic"}
    assert qr["integration_data"] == {
        "platform_id": "platform-123",
        "integrator_id": "integrator-123",
    }


def test_keyring_is_separate_from_arca_settings(db_session: Session) -> None:
    cipher = MercadoPagoCredentialCipher.from_settings(get_settings())
    encrypted = cipher.encrypt("secret")
    assert encrypted != "secret"
    assert cipher.decrypt(encrypted, "key-1") == "secret"


def test_discovery_supports_current_terminal_and_pos_response_shapes(
    db_session: Session,
) -> None:
    def responses(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/terminals/v1/list":
            return httpx.Response(
                200,
                json={"data": {"terminals": [{"id": "POINT-1"}]}},
                request=request,
            )
        return httpx.Response(
            200,
            json={"paging": {"total": 1}, "data": [{"id": 10, "external_id": "POS-1"}]},
            request=request,
        )

    client = MercadoPagoClient(
        get_settings(),
        http_client=httpx.Client(
            base_url="https://api.mercadopago.test", transport=httpx.MockTransport(responses)
        ),
    )

    assert client.list_terminals("access") == [{"id": "POINT-1"}]
    assert client.list_pos("access") == [{"id": 10, "external_id": "POS-1"}]


def test_canonical_order_reads_currency_from_payment(db_session: Session) -> None:
    order = normalize_order(
        {
            "id": "ORDER-1",
            "type": "point",
            "status": "processed",
            "expiration_date": "2026-09-08T15:00:00Z",
            "transactions": {
                "payments": [
                    {
                        "amount": "42.00",
                        "currency_id": "ARS",
                        "status": "processed",
                        "status_detail": "accredited",
                    }
                ]
            },
        }
    )

    assert order.currency == "ARS"
    assert order.payment_amount == Decimal("42.00")
    assert order.payment_status_detail == "accredited"
    assert order.expires_at == "2026-09-08T15:00:00Z"


def test_cancel_maps_terminal_intervention_to_conflict(db_session: Session) -> None:
    fake = FakeMercadoPagoClient()
    service = MercadoPagoService(db_session, client=fake)
    _, connection = authorize(service)
    fake.cancel_order = Mock(
        side_effect=MercadoPagoClientError(
            "order_at_terminal",
            "The order is already at the terminal",
            status_code=400,
        )
    )

    with pytest.raises(MercadoPagoDomainError) as error:
        service.cancel_order(UUID(connection.tenant_id), "ORDER-1", "same-key")

    assert error.value.status_code == 409
    assert error.value.code == "point_action_required"


def test_oauth_rejects_connection_without_refresh_token_or_collector(
    db_session: Session,
) -> None:
    fake = FakeMercadoPagoClient()
    fake.exchange_authorization_code = Mock(
        return_value=OAuthTokenData(access_token="access", expires_in=3600)
    )
    service = MercadoPagoService(db_session, client=fake)
    start = service.start_authorization(uuid4())
    state = parse_qs(urlparse(start.authorization_url).query)["state"][0]

    with pytest.raises(MercadoPagoDomainError) as error:
        service.complete_authorization(state, "authorization-code")

    assert error.value.code == "invalid_oauth_grant"
    assert db_session.scalar(select(MercadoPagoConnection)) is None


def test_refresh_preserves_connection_identity_when_optional_fields_are_omitted(
    db_session: Session,
) -> None:
    fake = FakeMercadoPagoClient()
    service = MercadoPagoService(db_session, client=fake)
    _, connection = authorize(service)
    connection.collector_id = "collector-preserved"
    connection.scopes = ["offline_access", "read"]
    connection.live_mode = True
    connection.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    fake.refresh_token = Mock(
        return_value=OAuthTokenData(
            access_token="refreshed",
            refresh_token="rotated",
            expires_in=7200,
        )
    )

    service.list_terminals(UUID(connection.tenant_id))
    db_session.refresh(connection)

    assert connection.collector_id == "collector-preserved"
    assert connection.scopes == ["offline_access", "read"]
    assert connection.live_mode is True

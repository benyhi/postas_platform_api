from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.arca.client import ArcaClientError, ArcaClientOptions, ArcaHTTPError, create_arca_client
from app.arca.crypto import (
    CredentialCipher,
    CredentialDecryptionError,
    CredentialValidationError,
    DecryptedCredentials,
    validate_credential_material,
)
from app.arca.models import ArcaFiscalProfile, ArcaInvoice
from app.arca.schemas import (
    FiscalProfileResponse,
    FiscalProfileWrite,
    InvoiceCreateRequest,
    SalesPointDiscoveryRequest,
)
from app.arca.service import ArcaDomainError, FiscalProfileService, InvoiceService
from app.arca.worker import main as worker_main
from app.billing.models import Plan, TenantSubscription
from app.billing.seed import seed_billing_catalog
from app.core.config import get_settings
from app.db.base import Base


CUIT = "20111111112"


def credential_material(cuit=CUIT, *, key=None, not_before=None, not_after=None):
    private_key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Tenant ARCA Test"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(days=1))
        .not_valid_after(not_after or now + timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


class FakeBilling:
    def __init__(self):
        self.last = 10
        self.create_calls = 0
        self.authorized = {}
        self.timeout_after_authorization = False
        self.crash_after_authorization = False

    def getLastVoucher(self, point_of_sale, voucher_type):
        return self.last

    def getSalesPoints(self):
        return {
            "ResultGet": {
                "PtoVenta": [
                    {"Nro": 2, "EmisionTipo": "CAE", "Bloqueado": "N", "FchBaja": None},
                    {"Nro": 3, "EmisionTipo": "CAE", "Bloqueado": "S", "FchBaja": None},
                    {"Nro": 4, "EmisionTipo": "CAE", "Bloqueado": "N", "FchBaja": "20260831"},
                    {"Nro": 1, "EmisionTipo": "CAE", "Bloqueado": "N", "FchBaja": None},
                ]
            }
        }

    def createVoucher(self, payload, return_response=False):
        self.create_calls += 1
        number = payload["CbteDesde"]
        response = {
            "FeDetResp": {
                "FECAEDetResponse": {
                    "Resultado": "A",
                    "CAE": f"700000000000{number}",
                    "CAEFchVto": "20260815",
                }
            }
        }
        self.authorized[number] = response
        self.last = number
        if self.timeout_after_authorization:
            self.timeout_after_authorization = False
            raise socket.timeout("access_token=secret-token")
        if self.crash_after_authorization:
            self.crash_after_authorization = False
            raise SystemExit("simulated process death")
        return response

    def getVoucherInfo(self, number, point_of_sale, voucher_type):
        if number not in self.authorized:
            raise ArcaClientError("(602) Comprobante no encontrado")
        return self.authorized[number]


class FakeClient:
    def __init__(self, billing=None):
        self.ElectronicBilling = billing or FakeBilling()


class InvalidTokenBilling(FakeBilling):
    def getLastVoucher(self, point_of_sale, voucher_type):
        raise ArcaHTTPError(401, "invalid access_token=tenant-access-token")


class TimeoutBeforeAuthorizationBilling(FakeBilling):
    def createVoucher(self, payload, return_response=False):
        if self.create_calls == 0:
            self.create_calls += 1
            raise socket.timeout("access_token=secret-token")
        return super().createVoucher(payload, return_response=return_response)


class AlwaysTimeoutBeforeAuthorizationBilling(FakeBilling):
    def createVoucher(self, payload, return_response=False):
        self.create_calls += 1
        raise socket.timeout("access_token=secret-token")


class TemporarilyUnavailableLookupBilling(FakeBilling):
    def getVoucherInfo(self, number, point_of_sale, voucher_type):
        raise ConnectionError("access_token=secret-token")


@pytest.fixture()
def db(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ARCA_CREDENTIAL_MASTER_KEYS", json.dumps({"key-1": key}))
    monkeypatch.setenv("ARCA_CREDENTIAL_ACTIVE_KEY_ID", "key-1")
    monkeypatch.setenv("ARCA_PRODUCTION_CALLS_ENABLED", "false")
    monkeypatch.setenv("ARCA_CONSUMER_FINAL_IDENTIFICATION_THRESHOLD", "10000000")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        seed_billing_catalog(session)
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()
    get_settings.cache_clear()


def subscribe(db: Session, tenant_id: UUID, plan_code="business"):
    plan = db.scalar(select(Plan).where(Plan.code == plan_code))
    now = datetime.now(timezone.utc)
    db.add(
        TenantSubscription(
            tenant_id=str(tenant_id),
            plan_id=plan.id,
            status="active",
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=30),
        )
    )
    db.commit()


def profile_payload(cert, key):
    return FiscalProfileWrite(
        arca_cuit=CUIT,
        certificate=cert,
        private_key=key,
        access_token="tenant-access-token",
        point_of_sale=1,
        automatic_voucher_type=6,
        concept=1,
        default_vat_rate=Decimal("21.00"),
        default_vat_id=5,
    )


def invoice_payload(external_id="sale-1", total="121.00"):
    return InvoiceCreateRequest.model_validate(
        {
            "external_id": external_id,
            "receiver": {"doc_type": 99, "doc_number": "0", "iva_condition_id": 5},
            "items": [{"description": "Producto", "quantity": "1", "final_unit_price": total}],
        }
    )


def valid_profile(db, tenant_id, environment="development", billing=None):
    cert, key = credential_material()
    fake = FakeClient(billing)
    service = FiscalProfileService(db, client_factory=lambda options: fake)
    profile = service.upsert(tenant_id, environment, profile_payload(cert, key))
    result = service.validate(tenant_id, environment)
    assert result.valid is True
    return profile, fake, service


@pytest.mark.parametrize("plan_code", ["business", "business_ai", "custom", "test", "demo_2", "demo_3"])
def test_arca_entitlement_is_enabled_for_initial_supported_plans(db, plan_code):
    tenant_id = uuid4()
    subscribe(db, tenant_id, plan_code)

    FiscalProfileService(db).require_entitlement(tenant_id)


def test_custom_plan_keeps_arca_entitlement_manually_configurable(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id, "custom")

    FiscalProfileService(db).require_entitlement(tenant_id)
    plan = db.scalar(select(Plan).where(Plan.code == "custom"))
    entitlement = next(item for item in plan.features if item.feature.key == "arca_invoicing")
    entitlement.enabled = False
    db.commit()

    with pytest.raises(ArcaDomainError) as captured:
        FiscalProfileService(db).require_entitlement(tenant_id)

    assert captured.value.status_code == 403
    assert captured.value.code == "feature_not_enabled"


def test_cipher_encrypts_all_secrets_and_rotation_preserves_plaintext(db, monkeypatch):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    cert, key = credential_material()
    profile = FiscalProfileService(db).upsert(tenant_id, "development", profile_payload(cert, key))

    assert cert not in profile.certificate_encrypted
    assert key not in profile.private_key_encrypted
    assert "tenant-access-token" not in profile.access_token_encrypted

    old_key = json.loads(get_settings().arca_credential_master_keys)["key-1"]
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("ARCA_CREDENTIAL_MASTER_KEYS", json.dumps({"key-1": old_key, "key-2": new_key}))
    monkeypatch.setenv("ARCA_CREDENTIAL_ACTIVE_KEY_ID", "key-2")
    get_settings.cache_clear()
    rotated = FiscalProfileService(db).rotate_all_to_active_key()
    db.refresh(profile)

    assert rotated == 1
    assert profile.credential_key_id == "key-2"
    assert FiscalProfileService(db).decrypt(profile).access_token == "tenant-access-token"


def test_retired_key_fails_closed_without_exposing_plaintext():
    old_key = Fernet.generate_key()
    encrypted = CredentialCipher({"old": Fernet(old_key)}, "old").encrypt("tenant-secret")
    active_only = CredentialCipher({"new": Fernet(Fernet.generate_key())}, "new")

    with pytest.raises(CredentialDecryptionError) as captured:
        active_only.decrypt(encrypted, "old")

    assert "tenant-secret" not in str(captured.value)


def test_profile_response_never_serializes_the_three_secrets(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    cert, key = credential_material()
    profile = FiscalProfileService(db).upsert(tenant_id, "development", profile_payload(cert, key))

    payload = FiscalProfileResponse.model_validate(profile).model_dump(mode="json")

    assert "certificate_encrypted" not in payload
    assert "private_key_encrypted" not in payload
    assert "access_token_encrypted" not in payload
    serialized = json.dumps(payload)
    assert cert not in serialized
    assert key not in serialized
    assert "tenant-access-token" not in serialized


def test_certificate_and_private_key_mismatch_is_rejected():
    cert, _ = credential_material()
    _, other_key = credential_material()
    with pytest.raises(CredentialValidationError, match="no corresponden"):
        validate_credential_material(cert, other_key, CUIT)


def test_invalid_and_expired_certificates_are_rejected():
    with pytest.raises(CredentialValidationError, match="PEM valido"):
        validate_credential_material("not-a-certificate", "not-a-key", CUIT)

    now = datetime.now(timezone.utc)
    cert, key = credential_material(
        not_before=now - timedelta(days=10),
        not_after=now - timedelta(days=1),
    )
    with pytest.raises(CredentialValidationError, match="vencido"):
        validate_credential_material(cert, key, CUIT)


def test_certificate_cuit_mismatch_is_rejected():
    cert, key = credential_material("20999999995")

    with pytest.raises(CredentialValidationError, match="CUIT no coincide"):
        validate_credential_material(cert, key, CUIT)


def test_development_and_production_profiles_are_independent(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    development, _, _ = valid_profile(db, tenant_id, "development")
    production, _, _ = valid_profile(db, tenant_id, "production")

    assert development.id != production.id
    assert development.environment == "development"
    assert production.environment == "production"


def test_profile_delete_removes_secrets_and_allows_reconfiguration(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    cert, key = credential_material()
    service = FiscalProfileService(db)
    profile = service.upsert(tenant_id, "development", profile_payload(cert, key))
    encrypted_before = (
        profile.certificate_encrypted,
        profile.private_key_encrypted,
        profile.access_token_encrypted,
    )

    service.delete(tenant_id, "development")
    db.refresh(profile)

    assert service.get(tenant_id, "development") is None
    assert profile.validation_status == "deleted"
    assert profile.certificate_encrypted not in encrypted_before
    assert cert not in profile.certificate_encrypted
    assert key not in profile.private_key_encrypted
    assert "tenant-access-token" not in profile.access_token_encrypted

    restored = service.upsert(tenant_id, "development", profile_payload(cert, key))
    assert restored.id == profile.id
    assert restored.validation_status == "pending"


def test_production_guard_fails_before_any_network_call():
    with pytest.raises(ArcaClientError, match="produccion"):
        create_arca_client(
            ArcaClientOptions(
                cuit=CUIT,
                environment="production",
                credentials=DecryptedCredentials("cert", "key", "token"),
                timeout_seconds=1,
                production_calls_enabled=False,
            )
        )


def test_worker_fails_closed_without_postgresql():
    with pytest.raises(RuntimeError, match="requiere PostgreSQL"):
        worker_main()


def test_invoice_uses_gross_prices_and_is_idempotent(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    profile, fake, profile_service = valid_profile(db, tenant_id)
    service = InvoiceService(db, profile_service=profile_service)

    first = service.enqueue(tenant_id, "development", invoice_payload())
    second = service.enqueue(tenant_id, "development", invoice_payload())
    record = db.get(ArcaInvoice, first.id)
    approved = service.process(record)

    assert first.id == second.id
    assert first.net_amount == Decimal("100.00")
    assert first.iva_amount == Decimal("21.00")
    assert approved.status == "approved"
    assert fake.ElectronicBilling.create_calls == 1


def test_same_external_id_with_changed_payload_is_rejected(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    _, _, profile_service = valid_profile(db, tenant_id)
    service = InvoiceService(db, profile_service=profile_service)
    service.enqueue(tenant_id, "development", invoice_payload("same", "121"))

    with pytest.raises(ArcaDomainError) as captured:
        service.enqueue(tenant_id, "development", invoice_payload("same", "242"))
    assert captured.value.code == "idempotency_conflict"


def test_timeout_adopts_existing_cae_before_reissue(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    billing = FakeBilling()
    billing.timeout_after_authorization = True
    _, fake, profile_service = valid_profile(db, tenant_id, billing=billing)
    service = InvoiceService(db, profile_service=profile_service)
    queued = service.enqueue(tenant_id, "development", invoice_payload("timeout"))
    record = db.get(ArcaInvoice, queued.id)

    first = service.process(record)
    second = service.process(record)

    assert first.status == "retrying"
    assert second.status == "approved"
    assert fake.ElectronicBilling.create_calls == 1
    assert second.error is None
    assert "secret-token" not in (record.last_error_message or "")


def test_process_crash_after_authorization_keeps_durable_number_for_recovery(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    billing = FakeBilling()
    billing.crash_after_authorization = True
    _, fake, profile_service = valid_profile(db, tenant_id, billing=billing)
    service = InvoiceService(db, profile_service=profile_service)
    queued = service.enqueue(tenant_id, "development", invoice_payload("crash"))
    record = db.get(ArcaInvoice, queued.id)

    with pytest.raises(SystemExit, match="simulated process death"):
        service.process(record)

    db.expire_all()
    reserved = db.get(ArcaInvoice, queued.id)
    assert reserved.voucher_number == 11
    recovered = service.process(reserved)
    assert recovered.status == "approved"
    assert fake.ElectronicBilling.create_calls == 1


def test_timeout_reemits_only_after_same_voucher_is_confirmed_missing(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    billing = TimeoutBeforeAuthorizationBilling()
    _, fake, profile_service = valid_profile(db, tenant_id, billing=billing)
    service = InvoiceService(db, profile_service=profile_service)
    queued = service.enqueue(tenant_id, "development", invoice_payload("confirmed-missing"))
    record = db.get(ArcaInvoice, queued.id)

    first = service.process(record)
    second = service.process(record)

    assert first.status == "retrying"
    assert second.status == "approved"
    assert fake.ElectronicBilling.create_calls == 2


def test_reserved_correlative_blocks_a_competing_invoice_until_sequence_advances(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    billing = TimeoutBeforeAuthorizationBilling()
    _, fake, profile_service = valid_profile(db, tenant_id, billing=billing)
    service = InvoiceService(db, profile_service=profile_service)
    first = service.enqueue(tenant_id, "development", invoice_payload("concurrent-first"))
    second = service.enqueue(tenant_id, "development", invoice_payload("concurrent-second"))
    first_record = db.get(ArcaInvoice, first.id)
    second_record = db.get(ArcaInvoice, second.id)

    first_attempt = service.process(first_record)
    competing_attempt = service.process(second_record)

    assert first_attempt.status == "retrying"
    assert first_record.voucher_number == 11
    assert competing_attempt.status == "retrying"
    assert competing_attempt.error.code == "sequence_busy"
    assert second_record.voucher_number is None

    assert service.process(first_record).status == "approved"
    completed_second = service.process(second_record)
    assert completed_second.status == "approved"
    assert completed_second.voucher_number == 12
    assert fake.ElectronicBilling.create_calls == 3


def test_retry_does_not_reissue_when_same_voucher_lookup_is_unavailable(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    billing = TemporarilyUnavailableLookupBilling()
    billing.timeout_after_authorization = True
    _, fake, profile_service = valid_profile(db, tenant_id, billing=billing)
    service = InvoiceService(db, profile_service=profile_service)
    queued = service.enqueue(tenant_id, "development", invoice_payload("lookup-unavailable"))
    record = db.get(ArcaInvoice, queued.id)

    first = service.process(record)
    second = service.process(record)

    assert first.status == "retrying"
    assert second.status == "retrying"
    assert fake.ElectronicBilling.create_calls == 1
    assert second.error.code == "arca_unavailable"


def test_retry_schedule_is_zero_one_ten_thirty_sixty_then_terminal(db):
    fixed_now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    billing = AlwaysTimeoutBeforeAuthorizationBilling()
    _, _, profile_service = valid_profile(db, tenant_id, billing=billing)
    service = InvoiceService(db, profile_service=profile_service, now_provider=lambda: fixed_now)
    queued = service.enqueue(tenant_id, "development", invoice_payload("retry-schedule"))
    record = db.get(ArcaInvoice, queued.id)

    assert queued.next_retry_at == fixed_now
    for attempt, delay in enumerate((1, 10, 30, 60), start=1):
        response = service.process(record)
        assert response.attempt_count == attempt
        assert response.status == "retrying"
        assert response.next_retry_at == fixed_now + timedelta(minutes=delay)

    terminal = service.process(record)
    assert terminal.attempt_count == 5
    assert terminal.status == "failed"
    assert terminal.next_retry_at is None


def test_invalid_token_marks_profile_invalid_and_redacts_secret(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    cert, key = credential_material()
    service = FiscalProfileService(
        db,
        client_factory=lambda options: FakeClient(InvalidTokenBilling()),
    )
    service.upsert(tenant_id, "development", profile_payload(cert, key))

    result = service.validate(tenant_id, "development")

    assert result.valid is False
    assert result.error == "ARCA rechazo las credenciales configuradas."
    assert "tenant-access-token" not in result.error


def test_consumer_final_threshold_keeps_sale_confirmed_but_requires_receiver(db, monkeypatch):
    monkeypatch.setenv("ARCA_CONSUMER_FINAL_IDENTIFICATION_THRESHOLD", "100")
    get_settings.cache_clear()
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    _, _, profile_service = valid_profile(db, tenant_id)
    response = InvoiceService(db, profile_service=profile_service).enqueue(
        tenant_id, "development", invoice_payload("threshold", "121")
    )

    assert response.status == "receiver_identification_required"
    assert response.next_retry_at is None
    assert response.error.code == "receiver_identification_required"


def test_receiver_identification_completes_same_sale_and_invoice_explicitly(db, monkeypatch):
    monkeypatch.setenv("ARCA_CONSUMER_FINAL_IDENTIFICATION_THRESHOLD", "100")
    get_settings.cache_clear()
    tenant_id = uuid4()
    sale_id = uuid4()
    subscribe(db, tenant_id)
    _, fake, profile_service = valid_profile(db, tenant_id)
    service = InvoiceService(db, profile_service=profile_service)
    original = InvoiceCreateRequest.model_validate(
        {
            "external_id": str(sale_id),
            "sale_id": str(sale_id),
            "items": [{"description": "Producto", "quantity": "1", "final_unit_price": "121.00"}],
        }
    )
    pending = service.enqueue(tenant_id, "development", original, automatic=True)
    identified = InvoiceCreateRequest.model_validate(
        {
            **original.model_dump(mode="json"),
            "receiver": {"doc_type": 80, "doc_number": "20333333334", "iva_condition_id": 5},
        }
    )

    resumed = service.enqueue(tenant_id, "development", identified, voucher_number=11)
    approved = service.process(db.get(ArcaInvoice, resumed.id))

    assert resumed.id == pending.id
    assert approved.status == "approved"
    assert fake.ElectronicBilling.create_calls == 1
    assert db.scalars(select(ArcaInvoice).where(ArcaInvoice.sale_id == str(sale_id))).all() == [
        db.get(ArcaInvoice, pending.id)
    ]


def test_receiver_completion_preserves_legacy_immutable_payload(db, monkeypatch):
    monkeypatch.setenv("ARCA_CONSUMER_FINAL_IDENTIFICATION_THRESHOLD", "100")
    get_settings.cache_clear()
    tenant_id = uuid4()
    sale_id = uuid4()
    subscribe(db, tenant_id)
    _, _, profile_service = valid_profile(db, tenant_id)
    service = InvoiceService(db, profile_service=profile_service)
    original = InvoiceCreateRequest.model_validate(
        {
            "external_id": "legacy-manual-reference",
            "sale_id": str(sale_id),
            "invoice_date": "2025-01-02",
            "items": [
                {"description": "Legacy description", "quantity": "1", "final_unit_price": "121.00"}
            ],
        }
    )
    pending = service.enqueue(tenant_id, "development", original)
    derived_retry = InvoiceCreateRequest.model_validate(
        {
            "external_id": "legacy-manual-reference",
            "sale_id": str(sale_id),
            "invoice_date": "2026-09-01",
            "receiver": {"doc_type": 80, "doc_number": "20333333334", "iva_condition_id": 5},
            "items": [
                {"description": "Current product name", "quantity": "2", "final_unit_price": "60.50"}
            ],
        }
    )

    resumed = service.enqueue(
        tenant_id,
        "development",
        derived_retry,
        voucher_number=11,
    )
    record = db.get(ArcaInvoice, pending.id)

    assert resumed.id == pending.id
    assert record.request_payload["invoice_date"] == "2025-01-02"
    assert record.request_payload["items"][0]["description"] == "Legacy description"
    assert record.request_payload["receiver"]["doc_number"] == "20333333334"


def test_automatic_sales_require_products_concept_and_services_require_dates(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    profile, _, profile_service = valid_profile(db, tenant_id)
    profile.concept = 2
    db.commit()
    service = InvoiceService(db, profile_service=profile_service)

    with pytest.raises(ArcaDomainError) as automatic_error:
        service.enqueue(tenant_id, "development", invoice_payload("automatic-service"), automatic=True)
    assert automatic_error.value.code == "automatic_invoicing_requires_products_concept"

    with pytest.raises(ArcaDomainError) as explicit_error:
        service.enqueue(tenant_id, "development", invoice_payload("explicit-service"))
    assert explicit_error.value.code == "service_dates_required"


def test_cross_tenant_lookup_is_not_allowed(db):
    tenant_a, tenant_b = uuid4(), uuid4()
    subscribe(db, tenant_a)
    subscribe(db, tenant_b)
    _, _, profile_service = valid_profile(db, tenant_a)
    service = InvoiceService(db, profile_service=profile_service)
    service.enqueue(tenant_a, "development", invoice_payload("private"))

    with pytest.raises(ArcaDomainError) as captured:
        service.get_by_external_id(tenant_b, "private")
    assert captured.value.status_code == 404


def test_sales_point_discovery_filters_disabled_points_without_persisting_secrets(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    cert, key = credential_material()
    service = FiscalProfileService(db, client_factory=lambda _options: FakeClient())

    result = service.discover_sales_points(
        tenant_id,
        SalesPointDiscoveryRequest(
            arca_environment="development",
            arca_cuit=CUIT,
            certificate=cert,
            private_key=key,
            access_token="temporary-access-token",
        ),
    )

    assert [point.number for point in result.results] == [1, 2]
    assert all(point.blocked is False and point.deactivation_date is None for point in result.results)
    assert db.scalars(select(ArcaFiscalProfile)).all() == []


def test_sales_point_discovery_requires_entitlement_before_credentials_or_network(db):
    tenant_id = uuid4()
    client_options = []
    service = FiscalProfileService(
        db,
        client_factory=lambda options: client_options.append(options),
    )

    with pytest.raises(ArcaDomainError) as captured:
        service.discover_sales_points(
            tenant_id,
            SalesPointDiscoveryRequest(
                arca_environment="development",
                arca_cuit=CUIT,
                certificate="not-a-certificate",
                private_key="not-a-private-key",
                access_token="temporary-access-token",
            ),
        )

    assert captured.value.status_code == 403
    assert client_options == []
    assert db.scalars(select(ArcaFiscalProfile)).all() == []


def test_sales_point_discovery_maps_timeout_without_exposing_token(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    cert, key = credential_material()

    class TimeoutSalesPoints(FakeBilling):
        def getSalesPoints(self):
            raise socket.timeout("access_token=temporary-access-token")

    service = FiscalProfileService(
        db,
        client_factory=lambda _options: FakeClient(TimeoutSalesPoints()),
    )
    with pytest.raises(ArcaDomainError) as captured:
        service.discover_sales_points(
            tenant_id,
            SalesPointDiscoveryRequest(
                arca_environment="development",
                arca_cuit=CUIT,
                certificate=cert,
                private_key=key,
                access_token="temporary-access-token",
            ),
        )

    assert captured.value.code == "arca_timeout"
    assert captured.value.status_code == 504
    assert "temporary-access-token" not in captured.value.message


def test_invoice_list_is_tenant_scoped_ordered_and_paginated(db):
    tenant_a = uuid4()
    tenant_b = uuid4()
    subscribe(db, tenant_a)
    subscribe(db, tenant_b)
    _, _, profiles_a = valid_profile(db, tenant_a)
    _, _, profiles_b = valid_profile(db, tenant_b)
    service_a = InvoiceService(db, profile_service=profiles_a)
    service_b = InvoiceService(db, profile_service=profiles_b)
    first = service_a.enqueue(tenant_a, "development", invoice_payload("a-first"))
    second = service_a.enqueue(tenant_a, "development", invoice_payload("a-second"))
    service_b.enqueue(tenant_b, "development", invoice_payload("b-private"))

    page = service_a.list_invoices(tenant_a, offset=0, limit=1)
    remaining = service_a.list_invoices(tenant_a, offset=1, limit=10)

    assert page.count == 2
    assert [item.id for item in page.results] == [second.id]
    assert [item.id for item in remaining.results] == [first.id]


def test_sale_id_is_unique_and_sale_invoices_require_products_concept(db):
    tenant_id = uuid4()
    subscribe(db, tenant_id)
    profile, _, profiles = valid_profile(db, tenant_id)
    service = InvoiceService(db, profile_service=profiles)
    sale_id = uuid4()
    first_payload = InvoiceCreateRequest.model_validate(
        {**invoice_payload("sale-first").model_dump(mode="json"), "sale_id": str(sale_id)}
    )
    service.enqueue(tenant_id, "development", first_payload)
    duplicate_payload = InvoiceCreateRequest.model_validate(
        {**invoice_payload("sale-second").model_dump(mode="json"), "sale_id": str(sale_id)}
    )

    with pytest.raises(ArcaDomainError) as duplicate:
        service.enqueue(tenant_id, "development", duplicate_payload)
    assert duplicate.value.code == "sale_already_invoiced"

    profile.concept = 2
    db.commit()
    other_sale_payload = InvoiceCreateRequest.model_validate(
        {
            **invoice_payload("service-sale").model_dump(mode="json"),
            "sale_id": str(uuid4()),
            "service_start_date": "2026-09-01",
            "service_end_date": "2026-09-01",
            "payment_due_date": "2026-09-01",
        }
    )
    with pytest.raises(ArcaDomainError) as incompatible:
        service.enqueue(tenant_id, "development", other_sale_payload)
    assert incompatible.value.code == "automatic_invoicing_requires_products_concept"


def test_same_sale_id_is_independent_between_tenants(db):
    tenant_a = uuid4()
    tenant_b = uuid4()
    sale_id = uuid4()
    subscribe(db, tenant_a)
    subscribe(db, tenant_b)
    _, _, profiles_a = valid_profile(db, tenant_a)
    _, _, profiles_b = valid_profile(db, tenant_b)
    payload_a = InvoiceCreateRequest.model_validate(
        {**invoice_payload("tenant-a-sale").model_dump(mode="json"), "sale_id": str(sale_id)}
    )
    payload_b = InvoiceCreateRequest.model_validate(
        {**invoice_payload("tenant-b-sale").model_dump(mode="json"), "sale_id": str(sale_id)}
    )

    invoice_a = InvoiceService(db, profile_service=profiles_a).enqueue(
        tenant_a, "development", payload_a
    )
    invoice_b = InvoiceService(db, profile_service=profiles_b).enqueue(
        tenant_b, "development", payload_b
    )

    assert invoice_a.sale_id == sale_id
    assert invoice_b.sale_id == sale_id
    assert invoice_a.tenant_id == tenant_a
    assert invoice_b.tenant_id == tenant_b

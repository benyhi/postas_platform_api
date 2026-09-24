from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.billing.models import Feature, Payment, Plan, TenantSubscription, UsageCounter, UsageEvent
from app.billing.schemas import EntitlementCheckRequest
from app.billing.seed import seed_billing_catalog
from app.billing.service import BillingService
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from scripts.seed_plan_tenants import seed_plan_tenants
from scripts.seed_preprod_tenant import DEFAULT_TENANT_ID, seed_preprod_tenant


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTAS_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_REQUEST_SOURCES", "postas_api,postas_ai_api")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestingSessionLocal() as db:
        seed_billing_catalog(db)
        yield db

    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    get_settings.cache_clear()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def auth_headers(source: str = "postas_api") -> dict[str, str]:
    return {
        "X-Postas-Source": source,
        "X-Postas-Service-Token": "test-token",
    }


def test_liveness_does_not_depend_on_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_execute(*_args, **_kwargs):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(Session, 'execute', fail_execute)
    response = client.get('/api/v1/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_readiness_checks_database(client: TestClient) -> None:
    response = client.get('/api/v1/ready')

    assert response.status_code == 200
    assert response.json() == {'status': 'ready'}


def test_readiness_fails_closed_when_database_is_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_execute(*_args, **_kwargs):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(Session, 'execute', fail_execute)
    response = client.get('/api/v1/ready')

    assert response.status_code == 503
    assert response.json() == {'detail': 'Service unavailable'}


def create_subscription(
    db: Session,
    tenant_id: UUID,
    plan_code: str,
    status: str = "active",
    current_period_end: datetime | None = None,
) -> TenantSubscription:
    plan = db.scalar(select(Plan).where(Plan.code == plan_code))
    assert plan is not None
    now = datetime.now(timezone.utc)
    subscription = TenantSubscription(
        tenant_id=str(tenant_id),
        plan_id=plan.id,
        status=status,
        current_period_start=now - timedelta(days=1),
        current_period_end=current_period_end or now + timedelta(days=30),
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def test_seed_creates_plans_and_features_without_duplicates(db_session: Session) -> None:
    first_plan_count = db_session.scalar(select(func.count()).select_from(Plan))
    seed_billing_catalog(db_session)
    second_plan_count = db_session.scalar(select(func.count()).select_from(Plan))

    assert first_plan_count == 6
    assert second_plan_count == first_plan_count


@pytest.mark.parametrize(
    ("plan_code", "expected_limit"),
    [
        ("free", 1),
        ("starter", 3),
        ("business", 5),
        ("business_ai", None),
        ("custom", None),
        ("test", 1),
    ],
)
def test_seed_configures_simultaneous_open_cashbox_limits(
    db_session: Session,
    plan_code: str,
    expected_limit: int | None,
) -> None:
    plan = db_session.scalar(select(Plan).where(Plan.code == plan_code))
    assert plan is not None

    plan_features = {plan_feature.feature.key: plan_feature for plan_feature in plan.features}
    cashboxes = plan_features["cashboxes"]

    assert cashboxes.enabled is True
    assert cashboxes.limit_value == expected_limit
    assert cashboxes.reset_period is None
    assert cashboxes.feature.description == "Cantidad maxima de cajas abiertas simultaneamente."


def test_seed_creates_test_plan_with_one_unit_limits(db_session: Session) -> None:
    plan = db_session.scalar(select(Plan).where(Plan.code == "test"))
    assert plan is not None
    assert plan.name == "Test"
    assert plan.is_public is False

    features = db_session.scalars(select(Feature)).all()
    plan_features = {plan_feature.feature.key: plan_feature for plan_feature in plan.features}

    assert set(plan_features) == {feature.key for feature in features}
    for feature in features:
        plan_feature = plan_features[feature.key]
        assert plan_feature.enabled is True
        if feature.type in {"monthly_usage", "resource_limit"}:
            assert plan_feature.limit_value == 1
        else:
            assert plan_feature.limit_value is None
        if feature.type == "monthly_usage":
            assert plan_feature.reset_period == "monthly"


def test_seed_plan_tenants_creates_missing_consecutive_tenants_and_is_idempotent(db_session: Session) -> None:
    existing_tenant = UUID(int=1)
    create_subscription(db_session, existing_tenant, "starter")

    first_result = seed_plan_tenants(db_session)

    subscriptions = db_session.scalars(
        select(TenantSubscription)
        .join(Plan)
        .where(TenantSubscription.status == "active")
        .order_by(TenantSubscription.tenant_id)
    ).all()
    tenant_by_plan = {subscription.plan.code: UUID(subscription.tenant_id).int for subscription in subscriptions}

    assert tenant_by_plan == {
        "starter": 1,
        "free": 2,
        "business": 3,
        "business_ai": 4,
        "custom": 5,
        "test": 6,
    }
    assert first_result.created_subscriptions == 5
    assert first_result.created_payments == 6
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 6

    second_result = seed_plan_tenants(db_session)

    assert second_result.created_subscriptions == 0
    assert second_result.created_payments == 0
    assert db_session.scalar(select(func.count()).select_from(TenantSubscription)) == 6
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 6


def test_seed_preprod_tenant_is_idempotent_and_uses_requested_plan(db_session: Session) -> None:
    tenant_id = UUID('00000000-0000-0000-0000-000000000001')

    first = seed_preprod_tenant(db_session, tenant_id=tenant_id, plan_code='business_ai')
    first_subscription = db_session.scalar(
        select(TenantSubscription).where(TenantSubscription.tenant_id == str(tenant_id))
    )
    first_payment = db_session.scalar(
        select(Payment).where(Payment.tenant_id == str(tenant_id))
    )
    assert first_subscription is not None
    assert first_payment is not None
    first_subscription_id = first_subscription.id
    first_payment_id = first_payment.id
    first_period = (
        first_subscription.current_period_start,
        first_subscription.current_period_end,
        first_payment.paid_at,
        first_payment.period_start,
        first_payment.period_end,
    )

    second = seed_preprod_tenant(db_session, tenant_id=tenant_id, plan_code='business_ai')

    subscription = db_session.scalar(
        select(TenantSubscription).where(TenantSubscription.tenant_id == str(tenant_id))
    )
    assert subscription is not None
    assert subscription.id == first_subscription_id
    assert subscription.tenant_id == str(DEFAULT_TENANT_ID) == str(tenant_id)
    assert subscription.plan.code == 'business_ai'
    assert subscription.status == 'active'
    assert subscription.current_period_end > subscription.current_period_start
    assert subscription.cancel_at_period_end is False
    assert first.subscription_created is True
    assert first.payment_created is True
    assert second.subscription_created is False
    assert second.payment_created is False
    assert db_session.scalar(
        select(func.count()).select_from(TenantSubscription).where(
            TenantSubscription.tenant_id == str(tenant_id)
        )
    ) == 1
    assert db_session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == str(tenant_id))
    ) == 1
    payment = db_session.scalar(select(Payment).where(Payment.tenant_id == str(tenant_id)))
    assert payment is not None
    assert payment.id == first_payment_id
    assert payment.subscription_id == subscription.id
    assert payment.provider == 'manual'
    assert payment.provider_payment_id == f'preprod-{tenant_id.hex}-business_ai'
    assert payment.provider_status == 'approved'
    assert payment.raw_payload == {
        'source': 'scripts/seed_preprod_tenant.py',
        'plan_code': 'business_ai',
    }
    assert (
        subscription.current_period_start,
        subscription.current_period_end,
        payment.paid_at,
        payment.period_start,
        payment.period_end,
    ) == first_period


def test_seed_preprod_tenant_rejects_existing_unmanaged_subscription(
    db_session: Session,
) -> None:
    tenant_id = UUID('00000000-0000-0000-0000-000000000001')
    existing = create_subscription(db_session, tenant_id, 'starter')
    original_period = (existing.current_period_start, existing.current_period_end)

    with pytest.raises(ValueError, match='no administrada por este seed'):
        seed_preprod_tenant(db_session, tenant_id=tenant_id, plan_code='business_ai')

    db_session.refresh(existing)
    assert existing.plan.code == 'starter'
    assert (existing.current_period_start, existing.current_period_end) == original_period
    assert db_session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == str(tenant_id))
    ) == 0


def test_seed_preprod_tenant_does_not_adopt_new_subscription_from_historical_seed_payment(
    db_session: Session,
) -> None:
    tenant_id = UUID('00000000-0000-0000-0000-000000000001')
    seed_preprod_tenant(db_session, tenant_id=tenant_id, plan_code='business_ai')
    historical_subscription = db_session.scalar(
        select(TenantSubscription).where(TenantSubscription.tenant_id == str(tenant_id))
    )
    assert historical_subscription is not None
    historical_subscription.status = 'cancelled'
    db_session.commit()

    current_subscription = create_subscription(db_session, tenant_id, 'starter')
    current_subscription_id = current_subscription.id
    original_period = (
        current_subscription.current_period_start,
        current_subscription.current_period_end,
    )

    with pytest.raises(ValueError, match='no administrada por este seed'):
        seed_preprod_tenant(db_session, tenant_id=tenant_id, plan_code='business_ai')

    db_session.refresh(current_subscription)
    assert current_subscription.id == current_subscription_id
    assert current_subscription.plan.code == 'starter'
    assert (
        current_subscription.current_period_start,
        current_subscription.current_period_end,
    ) == original_period


def test_seed_preprod_tenant_rejects_invalid_period_without_writes(
    db_session: Session,
) -> None:
    tenant_id = UUID('00000000-0000-0000-0000-000000000001')

    with pytest.raises(ValueError, match='period_days debe ser mayor a cero'):
        seed_preprod_tenant(
            db_session,
            tenant_id=tenant_id,
            plan_code='business_ai',
            period_days=0,
        )

    assert db_session.scalar(
        select(func.count()).select_from(TenantSubscription).where(
            TenantSubscription.tenant_id == str(tenant_id)
        )
    ) == 0
    assert db_session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == str(tenant_id))
    ) == 0


def test_seed_preprod_tenant_rejects_unknown_plan_without_tenant_records(
    db_session: Session,
) -> None:
    tenant_id = UUID('00000000-0000-0000-0000-000000000001')

    with pytest.raises(ValueError, match='Plan activo inexistente: missing-plan'):
        seed_preprod_tenant(db_session, tenant_id=tenant_id, plan_code='missing-plan')

    assert db_session.scalar(
        select(func.count()).select_from(TenantSubscription).where(
            TenantSubscription.tenant_id == str(tenant_id)
        )
    ) == 0
    assert db_session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == str(tenant_id))
    ) == 0


def test_business_ai_can_use_document_extraction_when_quota_available(db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai")

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(tenant_id=tenant_id, feature_key="document_extraction", amount=1)
    )

    assert response.allowed is True
    assert response.reason == "allowed"
    assert response.limit == 100
    assert response.used == 0
    assert response.remaining == 100


def test_expired_subscription_denies_with_specific_reason(db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(
        db_session,
        tenant_id,
        "business_ai",
        current_period_end=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(tenant_id=tenant_id, feature_key="document_extraction", amount=1)
    )

    assert response.allowed is False
    assert response.reason == "subscription_expired"
    assert response.subscription_status == "active"
    assert response.upgrade_required is True


@pytest.mark.parametrize(
    ("subscription_status", "expected_reason"),
    [
        ("cancelled", "subscription_cancelled"),
        ("canceled", "subscription_cancelled"),
        ("past_due", "subscription_payment_required"),
        ("unpaid", "subscription_payment_required"),
        ("payment_failed", "subscription_payment_required"),
        ("paused", "subscription_inactive"),
    ],
)
def test_inactive_subscription_statuses_return_specific_reasons(
    db_session: Session,
    subscription_status: str,
    expected_reason: str,
) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai", status=subscription_status)

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(tenant_id=tenant_id, feature_key="document_extraction", amount=1)
    )

    assert response.allowed is False
    assert response.reason == expected_reason
    assert response.subscription_status == subscription_status
    assert response.upgrade_required is True


def test_tenant_status_endpoint_returns_subscription_and_feature_usage(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai")

    response = client.get(f"/internal/v1/tenants/{tenant_id}/status", headers=auth_headers("postas_api"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["subscription"]["plan"] == "business_ai"
    assert payload["features"]["document_extraction"]["enabled"] is True
    assert payload["features"]["document_extraction"]["limit"] == 100
    assert payload["features"]["document_extraction"]["used"] == 0


def test_tenant_status_endpoint_reflects_expired_subscription(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(
        db_session,
        tenant_id,
        "business_ai",
        current_period_end=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    response = client.get(f"/internal/v1/tenants/{tenant_id}/status", headers=auth_headers("postas_api"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "subscription_expired"
    assert payload["subscription"]["status"] == "subscription_expired"
    assert payload["features"]["document_extraction"]["enabled"] is False


def test_business_ai_cannot_use_document_extraction_when_quota_is_exhausted(db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai")
    db_session.add(
        UsageCounter(
            tenant_id=str(tenant_id),
            feature_key="document_extraction",
            period_key=datetime.now(timezone.utc).strftime("%Y-%m"),
            used=100,
            limit_value=100,
        )
    )
    db_session.commit()

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(tenant_id=tenant_id, feature_key="document_extraction", amount=1)
    )

    assert response.allowed is False
    assert response.reason == "quota_exceeded"
    assert response.upgrade_required is True
    assert response.remaining == 0


def test_business_plan_cannot_use_document_extraction(db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business")

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(tenant_id=tenant_id, feature_key="document_extraction", amount=1)
    )

    assert response.allowed is False
    assert response.reason == "feature_not_enabled"


def test_usage_consume_creates_event_and_counter(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai")

    response = client.post(
        "/internal/v1/usage/consume",
        headers=auth_headers("postas_ai_api"),
        json={
            "tenant_id": str(tenant_id),
            "feature_key": "document_extraction",
            "amount": 1,
            "external_id": "doc-1",
            "idempotency_key": "document-extraction:doc-1",
            "occurred_at": "2026-05-27T23:10:00Z",
            "metadata": {"provider": "google_genai"},
        },
    )

    assert response.status_code == 200
    assert response.json()["recorded"] is True
    assert response.json()["used"] == 1
    assert db_session.scalar(select(UsageEvent).where(UsageEvent.idempotency_key == "document-extraction:doc-1")) is not None
    counter = db_session.scalar(select(UsageCounter).where(UsageCounter.tenant_id == str(tenant_id)))
    assert counter is not None
    assert counter.used == 1
    assert counter.limit_value == 100


def test_usage_consume_is_idempotent(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai")
    payload = {
        "tenant_id": str(tenant_id),
        "feature_key": "document_extraction",
        "amount": 1,
        "external_id": "doc-2",
        "idempotency_key": "document-extraction:doc-2",
        "occurred_at": "2026-05-27T23:10:00Z",
    }

    first = client.post("/internal/v1/usage/consume", headers=auth_headers("postas_ai_api"), json=payload)
    second = client.post("/internal/v1/usage/consume", headers=auth_headers("postas_ai_api"), json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["recorded"] is False
    assert second.json()["already_recorded"] is True
    counter = db_session.scalar(select(UsageCounter).where(UsageCounter.tenant_id == str(tenant_id)))
    assert counter is not None
    assert counter.used == 1


def test_check_and_consume_does_not_record_when_feature_is_disabled(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business")

    response = client.post(
        "/internal/v1/usage/check-and-consume",
        headers=auth_headers("postas_api"),
        json={
            "tenant_id": str(tenant_id),
            "feature_key": "document_extraction",
            "amount": 1,
            "external_id": "doc-3",
            "idempotency_key": "document-extraction:doc-3",
        },
    )

    assert response.status_code == 200
    assert response.json()["allowed"] is False
    assert response.json()["reason"] == "feature_not_enabled"
    assert db_session.scalar(select(UsageEvent).where(UsageEvent.idempotency_key == "document-extraction:doc-3")) is None


def test_check_and_consume_uses_occurred_at_period_for_quota(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "business_ai")
    db_session.add(
        UsageCounter(
            tenant_id=str(tenant_id),
            feature_key="document_extraction",
            period_key="2026-04",
            used=100,
            limit_value=100,
        )
    )
    db_session.commit()

    response = client.post(
        "/internal/v1/usage/check-and-consume",
        headers=auth_headers("postas_api"),
        json={
            "tenant_id": str(tenant_id),
            "feature_key": "document_extraction",
            "amount": 1,
            "external_id": "doc-4",
            "idempotency_key": "document-extraction:doc-4",
            "occurred_at": "2026-04-30T23:10:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["allowed"] is False
    assert response.json()["reason"] == "quota_exceeded"
    assert db_session.scalar(select(UsageEvent).where(UsageEvent.idempotency_key == "document-extraction:doc-4")) is None


def test_resource_limit_denies_when_resource_count_exceeds_limit(db_session: Session) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "free")

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(
            tenant_id=tenant_id,
            feature_key="products",
            amount=1,
            resource_count=21,
        )
    )

    assert response.allowed is False
    assert response.reason == "resource_limit_exceeded"
    assert response.limit == 20
    assert response.remaining == 0


@pytest.mark.parametrize(
    ("plan_code", "limit"),
    [
        ("free", 1),
        ("starter", 3),
        ("business", 5),
        ("test", 1),
    ],
)
def test_cashbox_entitlement_allows_limit_and_denies_projected_count_above_it(
    db_session: Session,
    plan_code: str,
    limit: int,
) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, plan_code)
    service = BillingService(db_session)

    allowed = service.check_entitlement(
        EntitlementCheckRequest(
            tenant_id=tenant_id,
            feature_key="cashboxes",
            resource_count=limit,
        )
    )
    denied = service.check_entitlement(
        EntitlementCheckRequest(
            tenant_id=tenant_id,
            feature_key="cashboxes",
            resource_count=limit + 1,
        )
    )

    assert allowed.allowed is True
    assert allowed.limit == limit
    assert allowed.used == limit
    assert allowed.remaining == 0
    assert denied.allowed is False
    assert denied.reason == "resource_limit_exceeded"
    assert denied.limit == limit
    assert denied.used == limit + 1
    assert denied.remaining == 0


@pytest.mark.parametrize("plan_code", ["business_ai", "custom"])
def test_cashbox_entitlement_is_unlimited_for_unlimited_plans(
    db_session: Session,
    plan_code: str,
) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, plan_code)

    response = BillingService(db_session).check_entitlement(
        EntitlementCheckRequest(
            tenant_id=tenant_id,
            feature_key="cashboxes",
            resource_count=10_000,
        )
    )

    assert response.allowed is True
    assert response.limit is None
    assert response.used == 10_000
    assert response.remaining is None


def test_cashbox_entitlement_endpoint_accepts_projected_open_count(
    client: TestClient,
    db_session: Session,
) -> None:
    tenant_id = uuid4()
    create_subscription(db_session, tenant_id, "starter")

    response = client.post(
        "/internal/v1/entitlements/check",
        headers=auth_headers("postas_api"),
        json={
            "tenant_id": str(tenant_id),
            "feature_key": "cashboxes",
            "resource_count": 4,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "allowed": False,
        "reason": "resource_limit_exceeded",
        "message": "Limite de recursos alcanzado para esta funcionalidad.",
        "feature_key": "cashboxes",
        "subscription_status": "active",
        "limit": 3,
        "used": 4,
        "remaining": 0,
        "upgrade_required": True,
    }


def test_internal_security_rejects_requests_without_valid_token(client: TestClient) -> None:
    response = client.post(
        "/internal/v1/entitlements/check",
        headers={"X-Postas-Source": "postas_api"},
        json={
            "tenant_id": str(uuid4()),
            "feature_key": "document_extraction",
            "amount": 1,
        },
    )

    assert response.status_code == 403


def test_internal_security_requires_tls(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTAS_INTERNAL_REQUIRE_TLS", "true")
    get_settings.cache_clear()
    insecure_client = TestClient(app, base_url="http://testserver")
    response = insecure_client.get(
        f"/internal/v1/tenants/{uuid4()}/status",
        headers=auth_headers("postas_api"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "TLS es obligatorio"


def test_internal_security_fails_closed_without_service_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTAS_SERVICE_TOKEN", "")
    get_settings.cache_clear()
    response = client.get(
        f"/internal/v1/tenants/{uuid4()}/status",
        headers={"X-Postas-Source": "postas_api"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "La autenticacion interna no esta configurada"

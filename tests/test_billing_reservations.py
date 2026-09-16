from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.billing.models import BillingReservation, Plan, TenantSubscription, UsageCounter, UsageEvent
from app.billing.seed import seed_billing_catalog
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTAS_SERVICE_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_REQUEST_SOURCES", "postas_api,postas_ai_api")
    monkeypatch.setenv("POSTAS_INTERNAL_REQUIRE_TLS", "true")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

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


def headers(source: str = "postas_api") -> dict[str, str]:
    return {"X-Postas-Source": source, "X-Postas-Service-Token": "test-token"}


def subscription(db: Session, tenant_id: UUID, plan_code: str = "test") -> None:
    plan = db.scalar(select(Plan).where(Plan.code == plan_code))
    assert plan is not None
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


def reserve_payload(tenant_id: UUID, key: str, *, amount: int = 1) -> dict:
    return {
        "tenant_id": str(tenant_id),
        "feature_key": "pos_sales",
        "idempotency_key": key,
        "amount": amount,
        "external_id": "sale-1",
        "metadata": {"sale_id": "sale-1"},
    }


def action_payload(tenant_id: UUID, key: str) -> dict:
    return {
        "tenant_id": str(tenant_id),
        "feature_key": "pos_sales",
        "idempotency_key": key,
    }


def test_active_reservation_counts_against_limit_without_consuming(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id)

    first = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "sale-key-1"),
        headers=headers(),
    )
    second = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "sale-key-2"),
        headers=headers(),
    )

    assert first.status_code == 200
    assert first.json()["status"] == "active"
    assert first.json()["used"] == 0
    assert first.json()["reserved"] == 1
    assert second.status_code == 200
    assert second.json()["allowed"] is False
    assert second.json()["reason"] == "quota_exceeded"
    assert db_session.scalar(select(UsageCounter)) is None
    assert db_session.scalar(select(UsageEvent)) is None


def test_reserve_commit_and_release_are_idempotent(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "free")
    key = "sale-key-commit"
    payload = reserve_payload(tenant_id, key)

    first = client.post(
        "/internal/v1/billing/reservations/reserve", json=payload, headers=headers()
    )
    repeated = client.post(
        "/internal/v1/billing/reservations/reserve", json=payload, headers=headers()
    )
    committed = client.post(
        "/internal/v1/billing/reservations/commit",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )
    committed_again = client.post(
        "/internal/v1/billing/reservations/commit",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )

    assert first.json()["reservation_id"] == repeated.json()["reservation_id"]
    assert repeated.json()["already_applied"] is True
    assert committed.json()["status"] == "committed"
    assert committed.json()["used"] == 1
    assert committed_again.json()["already_applied"] is True
    assert len(db_session.scalars(select(UsageEvent)).all()) == 1
    release_after_commit = client.post(
        "/internal/v1/billing/reservations/release",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )
    assert release_after_commit.status_code == 409
    assert release_after_commit.json()["detail"]["code"] == "reservation_committed"


def test_release_restores_capacity_without_consuming(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id)
    client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "release-1"),
        headers=headers(),
    )

    released = client.post(
        "/internal/v1/billing/reservations/release",
        json=action_payload(tenant_id, "release-1"),
        headers=headers(),
    )
    next_reservation = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "release-2"),
        headers=headers(),
    )

    assert released.json()["status"] == "released"
    assert released.json()["reserved"] == 0
    assert next_reservation.json()["allowed"] is True
    assert db_session.scalar(select(UsageCounter)) is None


def test_reserve_rejects_reused_key_with_different_payload(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "business")
    client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "conflict-key"),
        headers=headers(),
    )
    changed = reserve_payload(tenant_id, "conflict-key")
    changed["external_id"] = "sale-2"

    response = client.post(
        "/internal/v1/billing/reservations/reserve", json=changed, headers=headers()
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_conflict"


def test_reservation_endpoints_only_accept_postas_api(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id)
    response = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "wrong-source"),
        headers=headers("postas_ai_api"),
    )
    assert response.status_code == 403


def test_reservation_period_is_fixed_when_committed(client: TestClient, db_session: Session) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "business")
    key = "fixed-period"
    client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, key),
        headers=headers(),
    )
    reservation = db_session.scalar(select(BillingReservation))
    assert reservation is not None
    reservation.period_key = "2026-08"
    db_session.commit()

    response = client.post(
        "/internal/v1/billing/reservations/commit",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )

    assert response.json()["period_key"] == "2026-08"
    counter = db_session.scalar(select(UsageCounter))
    assert counter is not None
    assert counter.period_key == "2026-08"


def test_reservations_with_same_key_are_isolated_by_tenant(
    client: TestClient, db_session: Session
) -> None:
    first_tenant = uuid4()
    second_tenant = uuid4()
    subscription(db_session, first_tenant, "business")
    subscription(db_session, second_tenant, "business")

    first = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(first_tenant, "shared-key"),
        headers=headers(),
    )
    second = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(second_tenant, "shared-key"),
        headers=headers(),
    )
    wrong_tenant_commit = client.post(
        "/internal/v1/billing/reservations/commit",
        json=action_payload(uuid4(), "shared-key"),
        headers=headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reservation_id"] != second.json()["reservation_id"]
    assert wrong_tenant_commit.status_code == 409
    assert wrong_tenant_commit.json()["detail"]["code"] == "reservation_not_found"
    assert len(db_session.scalars(select(BillingReservation)).all()) == 2


def test_released_reservation_cannot_be_committed(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "business")
    key = "release-before-commit"
    client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, key),
        headers=headers(),
    )
    first_release = client.post(
        "/internal/v1/billing/reservations/release",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )
    repeated_release = client.post(
        "/internal/v1/billing/reservations/release",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )
    commit = client.post(
        "/internal/v1/billing/reservations/commit",
        json=action_payload(tenant_id, key),
        headers=headers(),
    )

    assert first_release.status_code == 200
    assert repeated_release.json()["already_applied"] is True
    assert commit.status_code == 409
    assert commit.json()["detail"]["code"] == "reservation_released"
    assert db_session.scalar(select(UsageEvent)) is None


def test_same_reserve_request_reactivates_a_released_reservation(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "business")
    payload = reserve_payload(tenant_id, "reactivate-key")
    first = client.post(
        "/internal/v1/billing/reservations/reserve", json=payload, headers=headers()
    )
    client.post(
        "/internal/v1/billing/reservations/release",
        json=action_payload(tenant_id, "reactivate-key"),
        headers=headers(),
    )

    reactivated = client.post(
        "/internal/v1/billing/reservations/reserve", json=payload, headers=headers()
    )

    assert reactivated.status_code == 200
    assert reactivated.json()["status"] == "active"
    assert reactivated.json()["reservation_id"] == first.json()["reservation_id"]
    assert reactivated.json()["reserved"] == 1


def test_released_reservation_replay_rechecks_current_capacity(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "test")
    released_payload = reserve_payload(tenant_id, "released-slot")
    client.post(
        "/internal/v1/billing/reservations/reserve",
        json=released_payload,
        headers=headers(),
    )
    client.post(
        "/internal/v1/billing/reservations/release",
        json=action_payload(tenant_id, "released-slot"),
        headers=headers(),
    )
    active = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "current-slot"),
        headers=headers(),
    )

    replay = client.post(
        "/internal/v1/billing/reservations/reserve",
        json=released_payload,
        headers=headers(),
    )

    assert active.json()["allowed"] is True
    assert replay.status_code == 200
    assert replay.json()["allowed"] is False
    assert replay.json()["reason"] == "quota_exceeded"
    released = db_session.scalar(
        select(BillingReservation).where(
            BillingReservation.idempotency_key == "released-slot"
        )
    )
    assert released is not None
    assert released.status == "released"


def test_active_reservation_blocks_traditional_check_and_consume(
    client: TestClient, db_session: Session
) -> None:
    tenant_id = uuid4()
    subscription(db_session, tenant_id, "test")
    client.post(
        "/internal/v1/billing/reservations/reserve",
        json=reserve_payload(tenant_id, "reserved-slot"),
        headers=headers(),
    )

    consumed = client.post(
        "/internal/v1/usage/check-and-consume",
        json={
            "tenant_id": str(tenant_id),
            "feature_key": "pos_sales",
            "amount": 1,
            "external_id": "local-sale",
            "idempotency_key": "local-sale-key",
        },
        headers=headers(),
    )

    assert consumed.status_code == 200
    assert consumed.json()["allowed"] is False
    assert consumed.json()["reason"] == "quota_exceeded"
    assert db_session.scalar(select(UsageEvent)) is None

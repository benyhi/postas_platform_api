from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.billing.models import Payment, Plan, TenantSubscription, UsageCounter, UsageEvent
from app.billing.seed import seed_billing_catalog
from app.db.base import Base
from scripts.seed_demo import run_phase
from app.arca.models import ArcaFiscalProfile
from app.mercado_pago.models import MercadoPagoConnection


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def manifest():
    tenant_id = str(UUID(int=1))
    return {"version": 1, "dataset_id": "production-demo-v1", "seed": 42,
            "fingerprint": "test-fingerprint", "as_of": "2026-10-02T10:00:00+00:00",
            "start_at": "2025-10-02T10:00:00+00:00", "tenants": [
                {"id": tenant_id, "name": "Kiko Store", "plan_code": "business_ai"},
                {"id": str(UUID(int=2)), "name": "FreeMarket", "plan_code": "free"}],
            "usage_events": [{"id": str(UUID(int=100)), "tenant_id": tenant_id,
                "feature_key": "pos_sales", "quantity": 1,
                "occurred_at": "2026-09-30T23:30:00-03:00",
                "external_id": "sale-100", "idempotency_key": "demo:sale:100"}]}


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_dry_run_has_no_catalog_or_subscription_writes(db, manifest):
    run_phase(db, manifest, "subscriptions", dry_run=True)
    assert count(db, Plan) == count(db, TenantSubscription) == count(db, UsageEvent) == 0


def test_subscription_payments_and_usage_are_resumable(db, manifest):
    run_phase(db, manifest, "subscriptions")
    run_phase(db, manifest, "subscriptions")
    assert count(db, TenantSubscription) == 2
    assert count(db, Payment) == 13
    assert all(row.amount > 0 and row.provider == "manual" for row in db.scalars(select(Payment)))
    assert count(db, UsageEvent) == 2  # Ownership markers include free tenants, no fake payments.
    run_phase(db, manifest, "usage")
    counter = db.scalar(select(UsageCounter))
    assert (counter.period_key, counter.used) == ("2026-10", 1)
    counter.used += 4  # Real activity must survive reruns.
    db.add(UsageEvent(tenant_id=counter.tenant_id, feature_key="pos_sales", amount=4,
        source="postas_api", idempotency_key="real-operation", occurred_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        period_key="2026-10"))
    db.commit()
    run_phase(db, manifest, "usage")
    run_phase(db, manifest, "verify")
    assert count(db, UsageEvent) == 4
    assert db.scalar(select(UsageCounter)).used == 5


def test_ownership_conflicts_do_not_modify_catalog(db, manifest):
    seed_billing_catalog(db)
    plan = db.scalar(select(Plan).where(Plan.code == "free"))
    db.add(TenantSubscription(tenant_id=manifest["tenants"][0]["id"], plan_id=plan.id,
        status="active", current_period_start=datetime(2026, 10, 1, tzinfo=timezone.utc),
        current_period_end=datetime(2026, 11, 1, tzinfo=timezone.utc)))
    db.commit()
    with pytest.raises(ValueError, match="refusing adoption"):
        run_phase(db, manifest, "subscriptions")
    assert count(db, Payment) == count(db, UsageEvent) == 0


def test_changed_manifest_is_rejected(db, manifest):
    run_phase(db, manifest, "subscriptions")
    manifest["fingerprint"] = "changed"
    with pytest.raises(ValueError, match="Conflicting dataset"):
        run_phase(db, manifest, "subscriptions")
    assert count(db, Payment) == 13


def test_usage_failure_rolls_back_whole_phase_and_retries(db, manifest):
    run_phase(db, manifest, "subscriptions")
    broken = deepcopy(manifest)
    broken["usage_events"].append({**broken["usage_events"][0], "idempotency_key": "another", "quantity": -1})
    with pytest.raises(ValueError, match="Invalid usage"):
        run_phase(db, broken, "usage")
    assert count(db, UsageCounter) == 0
    assert count(db, UsageEvent) == 2
    run_phase(db, manifest, "usage")
    assert count(db, UsageCounter) == 1


def test_verification_detects_missing_payment_and_changed_event(db, manifest):
    run_phase(db, manifest, "subscriptions")
    run_phase(db, manifest, "usage")
    event = db.scalar(select(UsageEvent).where(UsageEvent.feature_key == "pos_sales"))
    event.amount = 2
    db.commit()
    with pytest.raises(ValueError, match="Conflicting usage"):
        run_phase(db, manifest, "verify")
    event = db.scalar(select(UsageEvent).where(UsageEvent.feature_key == "pos_sales"))
    event.amount = 1
    db.delete(db.scalar(select(Payment)))
    db.commit()
    with pytest.raises(ValueError, match="Missing payment"):
        run_phase(db, manifest, "verify")


def test_free_plan_quota_and_disabled_feature_fail_atomically(db, manifest):
    run_phase(db, manifest, "subscriptions")
    event = manifest["usage_events"][0]
    event.update(tenant_id=manifest["tenants"][1]["id"], quantity=21)
    with pytest.raises(ValueError, match="quota"):
        run_phase(db, manifest, "usage")
    event.update(feature_key="document_extraction", quantity=1)
    with pytest.raises(ValueError, match="Disabled"):
        run_phase(db, manifest, "usage")
    assert count(db, UsageCounter) == 0


def test_catalog_can_participate_in_callers_transaction(db):
    seed_billing_catalog(db, commit=False)
    db.rollback()
    assert count(db, Plan) == 0


@pytest.mark.parametrize("phase", ["preflight", "subscriptions"])
@pytest.mark.parametrize("integration", ["mercado_pago", "arca"])
def test_orphan_integration_records_block_first_adoption(db, manifest, phase, integration):
    tenant_id = manifest["tenants"][0]["id"]
    if integration == "mercado_pago":
        record = MercadoPagoConnection(tenant_id=tenant_id)
    else:
        record = ArcaFiscalProfile(tenant_id=tenant_id, environment="testing", arca_cuit="20000000001",
            certificate_encrypted="test-placeholder", private_key_encrypted="test-placeholder",
            access_token_encrypted="test-placeholder", credential_key_id="test-key",
            certificate_fingerprint="f" * 64, certificate_expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            point_of_sale=1, automatic_voucher_type=11, default_vat_rate=21)
    db.add(record)
    db.commit()
    with pytest.raises(ValueError, match="refusing adoption"):
        run_phase(db, manifest, phase)
    assert count(db, Plan) == count(db, TenantSubscription) == count(db, Payment) == count(db, UsageEvent) == 0
    assert count(db, type(record)) == 1


def test_owned_tenant_can_add_integration_after_seeding(db, manifest):
    run_phase(db, manifest, "subscriptions")
    db.add(MercadoPagoConnection(tenant_id=manifest["tenants"][0]["id"]))
    db.commit()
    run_phase(db, manifest, "subscriptions")
    assert count(db, MercadoPagoConnection) == 1
    assert count(db, TenantSubscription) == 2

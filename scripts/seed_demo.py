"""Resumable, offline billing half of the shared production-demo manifest."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session
from app.billing.models import BillingReservation, Feature, Payment, Plan, PlanFeature, TenantSubscription, UsageCounter, UsageEvent
from app.billing.seed import PLAN_CATALOG, seed_billing_catalog
from app.db.base import Base
# Register integration tables for the tenant ownership check, without calling clients.
from app.arca import models as arca_models
from app.mercado_pago import models as mercado_pago_models

SOURCE = "production-demo-v1"
MARKER = "__seed_manifest__"
CATALOG = {plan["code"]: plan for plan in PLAN_CATALOG}


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        # SQLite returns naive values for timezone-aware columns.
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(value: datetime) -> datetime:
    return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)


def validate_manifest(manifest: dict) -> None:
    if manifest.get("version") != 1 or manifest.get("dataset_id") != SOURCE or not manifest.get("fingerprint"):
        raise ValueError("Invalid demo manifest version, dataset or fingerprint")
    for field in ("start_at", "as_of"):
        if datetime.fromisoformat(manifest[field]).tzinfo is None:
            raise ValueError(f"{field} must include a timezone")
    if utc(manifest["start_at"]) >= utc(manifest["as_of"]):
        raise ValueError("start_at must precede as_of")
    identifiers = set()
    for tenant in manifest["tenants"]:
        identifier = str(UUID(tenant["id"]))
        if identifier != tenant["id"] or identifier in identifiers or tenant["plan_code"] not in CATALOG:
            raise ValueError("Duplicate/invalid tenant UUID or unknown plan")
        identifiers.add(identifier)
    if not identifiers:
        raise ValueError("Manifest must contain tenants")


def preflight(db: Session, manifest: dict) -> dict:
    validate_manifest(manifest)
    inspector = inspect(db.get_bind())
    for model in (Feature, Plan, PlanFeature, TenantSubscription, Payment, UsageEvent, UsageCounter, BillingReservation):
        actual = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        if not set(model.__table__.columns.keys()).issubset(actual):
            raise ValueError(f"Apply migrations before seeding {model.__tablename__}")
    for tenant in manifest["tenants"]:
        tenant_id = tenant["id"]
        events = list(db.scalars(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id, UsageEvent.feature_key == MARKER)))
        subscriptions = list(db.scalars(select(TenantSubscription).where(TenantSubscription.tenant_id == tenant_id).with_for_update()))
        if not events:
            for table in Base.metadata.sorted_tables:
                if "tenant_id" in table.c and db.scalar(
                        select(table.c.tenant_id).where(table.c.tenant_id == tenant_id).limit(1)):
                    raise ValueError(f"Tenant {tenant_id} has unrelated data in {table.name}; refusing adoption")
        else:
            if len(events) != 1 or len(subscriptions) != 1:
                raise ValueError(f"Unexpected subscription/marker history for {tenant_id}")
            event = events[0]
            metadata = event.event_metadata or {}
            subscription = subscriptions[0]
            if (event.source != SOURCE or metadata.get("fingerprint") != manifest["fingerprint"]
                    or metadata.get("plan_code") != tenant["plan_code"]
                    or metadata.get("subscription_id") != subscription.id
                    or subscription.plan.code != tenant["plan_code"]):
                raise ValueError(f"Conflicting dataset or subscription for {tenant_id}")
    return {"tenants": len(manifest["tenants"]), "phase": "preflight"}


def subscriptions(db: Session, manifest: dict, *, verify: bool = False) -> dict:
    if not verify:
        seed_billing_catalog(db, commit=False)
    as_of = utc(manifest["as_of"])
    period = month_start(as_of)
    created = 0
    for tenant in manifest["tenants"]:
        tenant_id = tenant["id"]
        plan = db.scalar(select(Plan).where(Plan.code == tenant["plan_code"]))
        subscription = db.scalar(select(TenantSubscription).where(TenantSubscription.tenant_id == tenant_id))
        if subscription is None:
            if verify:
                raise ValueError(f"Missing subscription for {tenant_id}")
            subscription = TenantSubscription(tenant_id=tenant_id, plan_id=plan.id, status="active",
                current_period_start=period, current_period_end=next_month(period),
                created_at=utc(manifest["start_at"]))
            db.add(subscription)
            db.flush()
            db.add(UsageEvent(tenant_id=tenant_id, feature_key=MARKER, amount=0, source=SOURCE,
                idempotency_key=f"{SOURCE}:manifest", occurred_at=as_of, period_key=period.strftime("%Y-%m"),
                event_metadata={"fingerprint": manifest["fingerprint"], "plan_code": tenant["plan_code"],
                                "subscription_id": subscription.id}))
            created += 1
        if (subscription.status != "active" or
                not utc(subscription.current_period_start) <= as_of < utc(subscription.current_period_end)):
            raise ValueError(f"Subscription no longer covers manifest instant: {tenant_id}")
        if not plan.price_amount:
            continue
        payment_period = month_start(utc(manifest["start_at"]))
        while payment_period <= as_of:
            external_id = f"{SOURCE}:{tenant_id}:{payment_period:%Y-%m}"
            payment = db.scalar(select(Payment).where(Payment.provider_payment_id == external_id))
            if payment is None:
                if verify:
                    raise ValueError(f"Missing payment: {external_id}")
                db.add(Payment(tenant_id=tenant_id, subscription_id=subscription.id, provider="manual",
                    provider_payment_id=external_id, provider_status="approved", amount=plan.price_amount,
                    currency="ARS", paid_at=max(payment_period, utc(manifest["start_at"])), period_start=payment_period,
                    period_end=next_month(payment_period), created_at=max(payment_period, utc(manifest["start_at"])),
                    raw_payload={"source": SOURCE, "fingerprint": manifest["fingerprint"], "simulated": True}))
            elif (payment.tenant_id != tenant_id or payment.subscription_id != subscription.id
                    or (payment.raw_payload or {}).get("fingerprint") != manifest["fingerprint"]
                    or (payment.raw_payload or {}).get("source") != SOURCE
                    or payment.amount != plan.price_amount or payment.currency != "ARS"
                    or payment.provider != "manual" or payment.provider_status != "approved"
                    or utc(payment.period_start) != payment_period or utc(payment.period_end) != next_month(payment_period)):
                raise ValueError(f"Payment ownership conflict: {external_id}")
            payment_period = next_month(payment_period)
    if not verify:
        db.flush()
    return {"phase": "subscriptions", "created": created}


def usage(db: Session, manifest: dict, *, verify: bool = False) -> dict:
    tenants = {tenant["id"]: tenant for tenant in manifest["tenants"]}
    existing = {(event.tenant_id, event.feature_key, event.idempotency_key): event for event in
                db.scalars(select(UsageEvent).where(UsageEvent.tenant_id.in_(tenants)))}
    counters = {(row.tenant_id, row.feature_key, row.period_key): row for row in
                db.scalars(select(UsageCounter).where(UsageCounter.tenant_id.in_(tenants)).with_for_update())}
    totals = defaultdict(int)
    seen = set()
    created = 0
    for item in manifest.get("usage_events", []):
        tenant_id, feature_key = item["tenant_id"], item["feature_key"]
        if tenant_id not in tenants:
            raise ValueError("Usage references a tenant outside this manifest")
        config = CATALOG[tenants[tenant_id]["plan_code"]]["features"].get(feature_key)
        if not config or not config[0] or config[2] != "monthly":
            raise ValueError(f"Disabled/non-monthly feature: {feature_key}")
        occurred = utc(item["occurred_at"])
        amount = item["quantity"]
        if (type(amount) is not int or amount <= 0 or
                not utc(manifest["start_at"]) <= occurred <= utc(manifest["as_of"])):
            raise ValueError("Invalid usage quantity or timestamp")
        period_key = occurred.strftime("%Y-%m")
        key = (tenant_id, feature_key, item["idempotency_key"])
        if key in seen:
            raise ValueError("Duplicate usage idempotency key in manifest")
        seen.add(key)
        counter_key = (tenant_id, feature_key, period_key)
        totals[counter_key] += amount
        if config[1] is not None and totals[counter_key] > config[1]:
            raise ValueError(f"Demo exceeds monthly quota: {counter_key}")
        event = existing.get(key)
        if event is not None:
            if (event.source != SOURCE or event.amount != amount or event.external_id != item["external_id"]
                    or utc(event.occurred_at) != occurred or event.period_key != period_key
                    or (event.event_metadata or {}).get("fingerprint") != manifest["fingerprint"]):
                raise ValueError(f"Conflicting usage event: {key}")
        elif verify:
            raise ValueError(f"Missing usage event: {key}")
        else:
            db.add(UsageEvent(tenant_id=tenant_id, feature_key=feature_key, amount=amount, source=SOURCE,
                external_id=item["external_id"], idempotency_key=item["idempotency_key"], occurred_at=occurred,
                period_key=period_key, created_at=occurred,
                event_metadata={"fingerprint": manifest["fingerprint"], "manifest_event_id": item["id"]}))
            counter = counters.get(counter_key)
            if counter is None:
                counter = UsageCounter(tenant_id=tenant_id, feature_key=feature_key, period_key=period_key,
                                       used=0, limit_value=config[1])
                counters[counter_key] = counter
                db.add(counter)
            counter.used += amount
            created += 1
    for key, total in totals.items():
        if key not in counters or counters[key].used < total:
            raise ValueError(f"Counter missing or lower than dataset usage: {key}")
    if not verify:
        db.flush()
    all_totals = {(tenant_id, feature_key, period_key): total for tenant_id, feature_key, period_key, total in
        db.execute(select(UsageEvent.tenant_id, UsageEvent.feature_key, UsageEvent.period_key, func.sum(UsageEvent.amount))
            .where(UsageEvent.tenant_id.in_(tenants), UsageEvent.feature_key != MARKER)
            .group_by(UsageEvent.tenant_id, UsageEvent.feature_key, UsageEvent.period_key))}
    for key in set(all_totals) | set(counters):
        if key not in counters or counters[key].used != all_totals.get(key, 0):
            raise ValueError(f"Counter does not reconcile with all recorded events: {key}")
    return {"phase": "verify" if verify else "usage", "created": created, "events": len(seen)}


def run_phase(db: Session, manifest: dict, phase: str, *, dry_run: bool = False) -> dict:
    """Own one local transaction. No remote calls or schema creation."""
    try:
        result = preflight(db, manifest)
        if dry_run or phase == "preflight":
            db.rollback()
            return {**result, "dry_run": dry_run}
        if phase == "subscriptions":
            result = subscriptions(db, manifest)
        elif phase in ("usage", "verify"):
            for tenant in manifest["tenants"]:
                if not db.scalar(select(UsageEvent).where(UsageEvent.tenant_id == tenant["id"], UsageEvent.feature_key == MARKER)):
                    raise ValueError("Run subscriptions phase first")
            subscriptions(db, manifest, verify=True)
            result = usage(db, manifest, verify=phase == "verify")
        else:
            raise ValueError(f"Unknown phase: {phase}")
        if phase == "verify":
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def check_migration_heads(db: Session) -> None:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT_DIR / "alembic"))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    actual = set(MigrationContext.configure(db.connection()).get_current_heads())
    if actual != expected:
        raise ValueError("Platform migrations are pending; run alembic upgrade head first")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--phase", choices=("preflight", "subscriptions", "usage", "verify"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        from app.db.session import SessionLocal
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        with SessionLocal() as db:
            check_migration_heads(db)
            result = run_phase(db, manifest, args.phase, dry_run=args.dry_run)
        print(json.dumps(result))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        # Database exceptions may embed bound parameters or a credential-bearing URL.
        print(f"Error: Platform demo seed failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

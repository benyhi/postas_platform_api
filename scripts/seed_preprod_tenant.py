from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.billing.models import Payment, Plan, TenantSubscription
from app.billing.seed import seed_billing_catalog
from app.db.session import SessionLocal


DEFAULT_TENANT_ID = UUID('00000000-0000-0000-0000-000000000001')


@dataclass(frozen=True)
class PreprodTenantSeedResult:
    tenant_id: str
    plan_code: str
    subscription_created: bool
    payment_created: bool


def seed_preprod_tenant(
    db: Session,
    *,
    tenant_id: UUID = DEFAULT_TENANT_ID,
    plan_code: str = 'business_ai',
    period_days: int = 30,
    force_existing: bool = False,
) -> PreprodTenantSeedResult:
    if period_days < 1:
        raise ValueError('period_days debe ser mayor a cero.')

    seed_billing_catalog(db)
    plan = db.scalar(select(Plan).where(Plan.code == plan_code, Plan.is_active.is_(True)))
    if plan is None:
        raise ValueError(f'Plan activo inexistente: {plan_code}')

    tenant_value = str(tenant_id)
    provider_payment_id = f'preprod-{tenant_id.hex}-{plan_code}'
    payment = db.scalar(
        select(Payment).where(
            Payment.provider == 'manual',
            Payment.provider_payment_id == provider_payment_id,
        )
    )
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=period_days)
    subscription = db.scalar(
        select(TenantSubscription)
        .where(
            TenantSubscription.tenant_id == tenant_value,
            TenantSubscription.status.in_({'trialing', 'active'}),
        )
        .order_by(TenantSubscription.created_at.desc(), TenantSubscription.id.desc())
    )
    payment_is_managed = bool(
        payment is not None
        and payment.tenant_id == tenant_value
        and payment.raw_payload
        and payment.raw_payload.get('source') == 'scripts/seed_preprod_tenant.py'
    )
    subscription_is_managed = bool(
        subscription is not None
        and payment_is_managed
        and payment.subscription_id == subscription.id
    )
    if subscription is not None and not subscription_is_managed and not force_existing:
        raise ValueError(
            'El tenant ya tiene una suscripcion activa no administrada por este seed. '
            'Revisa el UUID o usa --force-existing de forma explicita.'
        )
    if payment is not None and not payment_is_managed and not force_existing:
        raise ValueError(
            'El identificador de pago de preproduccion ya existe y no pertenece a este seed.'
        )

    subscription_created = subscription is None
    if subscription is None:
        subscription = TenantSubscription(
            tenant_id=tenant_value,
            plan_id=plan.id,
            status='active',
            current_period_start=now,
            current_period_end=period_end,
            cancel_at_period_end=False,
        )
        db.add(subscription)
        db.flush()
    elif not subscription_is_managed or subscription.plan_id != plan.id:
        subscription.plan_id = plan.id
        subscription.status = 'active'
        subscription.current_period_start = now
        subscription.current_period_end = period_end
        subscription.cancel_at_period_end = False
    else:
        now = subscription.current_period_start
        period_end = subscription.current_period_end

    payment_created = payment is None
    if payment is None:
        payment = Payment(
            tenant_id=tenant_value,
            subscription_id=subscription.id,
            provider='manual',
            provider_payment_id=provider_payment_id,
            provider_status='approved',
            amount=plan.price_amount or 0,
            currency=plan.currency,
            paid_at=now,
            period_start=now,
            period_end=period_end,
            raw_payload={'source': 'scripts/seed_preprod_tenant.py', 'plan_code': plan_code},
        )
        db.add(payment)
    elif not payment_is_managed or payment.subscription_id != subscription.id:
        payment.subscription_id = subscription.id
        payment.provider_status = 'approved'
        payment.amount = plan.price_amount or 0
        payment.currency = plan.currency
        payment.paid_at = now
        payment.period_start = now
        payment.period_end = period_end
        payment.raw_payload = {
            'source': 'scripts/seed_preprod_tenant.py',
            'plan_code': plan_code,
        }

    db.commit()
    return PreprodTenantSeedResult(
        tenant_id=tenant_value,
        plan_code=plan_code,
        subscription_created=subscription_created,
        payment_created=payment_created,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Seed idempotente del tenant de preproduccion.')
    parser.add_argument(
        '--tenant-id',
        default=os.getenv('PREPROD_TENANT_ID', str(DEFAULT_TENANT_ID)),
    )
    parser.add_argument('--plan', default=os.getenv('PREPROD_PLAN_CODE', 'business_ai'))
    parser.add_argument(
        '--period-days',
        type=int,
        default=int(os.getenv('PREPROD_SUBSCRIPTION_DAYS', '30')),
    )
    parser.add_argument(
        '--force-existing',
        action='store_true',
        help='Permite reemplazar una suscripcion activa ajena al seed.',
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        result = seed_preprod_tenant(
            db,
            tenant_id=UUID(args.tenant_id),
            plan_code=args.plan,
            period_days=args.period_days,
            force_existing=args.force_existing,
        )

    print(
        f'Seed aplicado: tenant={result.tenant_id} plan={result.plan_code} '
        f'subscription={"creada" if result.subscription_created else "actualizada"} '
        f'payment={"creado" if result.payment_created else "actualizado"}'
    )


if __name__ == '__main__':
    main()

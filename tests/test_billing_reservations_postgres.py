from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.billing.models import BillingReservation, Plan, TenantSubscription, UsageCounter
from app.billing.schemas import BillingReservationReserveRequest, CheckAndConsumeRequest
from app.billing.seed import seed_billing_catalog
from app.billing.service import BillingService
from app.db.base import Base


DATABASE_URL = os.getenv("MERCADO_PAGO_POSTGRES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason=(
        "Mercado Pago billing concurrency checks require "
        "MERCADO_PAGO_POSTGRES_TEST_DATABASE_URL"
    ),
)


def test_postgresql_serializes_reservations_and_traditional_consumption():
    schema = f"mp_billing_test_{uuid4().hex}"
    admin_engine = create_engine(DATABASE_URL)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        DATABASE_URL,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=6,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        Base.metadata.create_all(engine)
        tenant_id = uuid4()
        with factory() as db:
            seed_billing_catalog(db)
            plan = db.scalar(select(Plan).where(Plan.code == "test"))
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

        barrier = threading.Barrier(2)

        def reserve():
            with factory() as db:
                barrier.wait(timeout=10)
                return BillingService(db).reserve_usage(
                    BillingReservationReserveRequest(
                        tenant_id=tenant_id,
                        idempotency_key="external-sale",
                        external_id="external-sale",
                    ),
                    "postas_api",
                )

        def consume():
            with factory() as db:
                barrier.wait(timeout=10)
                return BillingService(db).check_and_consume_usage(
                    CheckAndConsumeRequest(
                        tenant_id=tenant_id,
                        feature_key="pos_sales",
                        idempotency_key="local-sale",
                        external_id="local-sale",
                    ),
                    "postas_api",
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            reserve_future = executor.submit(reserve)
            consume_future = executor.submit(consume)
            reserve_result = reserve_future.result(timeout=15)
            consume_result = consume_future.result(timeout=15)

        assert int(reserve_result.allowed) + int(consume_result.allowed) == 1
        with factory() as db:
            used = db.scalar(select(func.coalesce(func.sum(UsageCounter.used), 0))) or 0
            reserved = db.scalar(
                select(func.coalesce(func.sum(BillingReservation.amount), 0)).where(
                    BillingReservation.status == "active"
                )
            ) or 0
            assert used + reserved == 1
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Plan(TimestampMixin, Base):
    __tablename__ = "billing_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="ARS", server_default="ARS", nullable=False)
    billing_interval: Mapped[str] = mapped_column(String(20), default="monthly", server_default="monthly", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), nullable=False)

    features: Mapped[list[PlanFeature]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
    )
    subscriptions: Mapped[list[TenantSubscription]] = relationship(back_populates="plan")


class Feature(TimestampMixin, Base):
    __tablename__ = "billing_features"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)

    plans: Mapped[list[PlanFeature]] = relationship(
        back_populates="feature",
        cascade="all, delete-orphan",
    )


class PlanFeature(TimestampMixin, Base):
    __tablename__ = "billing_plan_features"
    __table_args__ = (
        UniqueConstraint("plan_id", "feature_id", name="uq_billing_plan_features_plan_feature"),
        Index("ix_billing_plan_features_plan_id", "plan_id"),
        Index("ix_billing_plan_features_feature_id", "feature_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("billing_plans.id", ondelete="CASCADE"), nullable=False)
    feature_id: Mapped[int] = mapped_column(ForeignKey("billing_features.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    limit_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reset_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hard_limit: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="features")
    feature: Mapped[Feature] = relationship(back_populates="plans")


class TenantSubscription(TimestampMixin, Base):
    __tablename__ = "billing_tenant_subscriptions"
    __table_args__ = (
        Index("ix_billing_tenant_subscriptions_tenant_id", "tenant_id"),
        Index("ix_billing_tenant_subscriptions_plan_id", "plan_id"),
        Index(
            "uq_billing_tenant_active_subscription",
            "tenant_id",
            unique=True,
            sqlite_where=text("status IN ('trialing', 'active')"),
            postgresql_where=text("status IN ('trialing', 'active')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_id: Mapped[int] = mapped_column(ForeignKey("billing_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="subscriptions")
    payments: Mapped[list[Payment]] = relationship(back_populates="subscription")


class Payment(Base):
    __tablename__ = "billing_payments"
    __table_args__ = (Index("ix_billing_payments_tenant_id", "tenant_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("billing_tenant_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="ARS", server_default="ARS", nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    subscription: Mapped[TenantSubscription | None] = relationship(back_populates="payments")


class UsageEvent(Base):
    __tablename__ = "billing_usage_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "feature_key", "idempotency_key", name="uq_billing_usage_events_idempotency"),
        Index("ix_billing_usage_events_tenant_id", "tenant_id"),
        Index("ix_billing_usage_events_feature_key", "feature_key"),
        Index("ix_billing_usage_events_period_key", "period_key"),
        Index("ix_billing_usage_events_tenant_feature_idempotency", "tenant_id", "feature_key", "idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_key: Mapped[str] = mapped_column(String(7), nullable=False)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UsageCounter(Base):
    __tablename__ = "billing_usage_counters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "feature_key", "period_key", name="uq_billing_usage_counters_period"),
        Index("ix_billing_usage_counters_tenant_id", "tenant_id"),
        Index("ix_billing_usage_counters_feature_key", "feature_key"),
        Index("ix_billing_usage_counters_period_key", "period_key"),
        Index("ix_billing_usage_counters_tenant_feature_period", "tenant_id", "feature_key", "period_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(80), nullable=False)
    period_key: Mapped[str] = mapped_column(String(7), nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    limit_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BillingReservation(Base):
    __tablename__ = "billing_reservations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "feature_key",
            "idempotency_key",
            name="uq_billing_reservations_idempotency",
        ),
        Index("ix_billing_reservations_tenant_id", "tenant_id"),
        Index("ix_billing_reservations_period", "tenant_id", "feature_key", "period_key"),
        Index("ix_billing_reservations_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    reservation_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    period_key: Mapped[str] = mapped_column(String(7), nullable=False)
    limit_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

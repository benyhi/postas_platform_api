from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    price_amount: Decimal | None
    currency: str
    billing_interval: str
    is_active: bool
    is_public: bool
    created_at: datetime
    updated_at: datetime


class FeatureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    name: str
    description: str | None = None
    type: str
    created_at: datetime
    updated_at: datetime


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: UUID
    plan_id: int
    status: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    created_at: datetime
    updated_at: datetime


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: UUID
    subscription_id: int | None
    provider: str
    provider_payment_id: str | None
    provider_status: str | None
    amount: Decimal
    currency: str
    paid_at: datetime | None
    period_start: datetime | None
    period_end: datetime | None
    raw_payload: dict[str, Any] | None
    created_at: datetime


class TenantSubscriptionStatus(BaseModel):
    plan: str
    plan_name: str
    status: str
    current_period_start: datetime
    current_period_end: datetime


class TenantFeatureStatus(BaseModel):
    enabled: bool
    limit: int | None = None
    used: int | None = None
    remaining: int | None = None
    reset_period: str | None = None


class TenantStatusResponse(BaseModel):
    tenant_id: UUID
    status: str
    subscription: TenantSubscriptionStatus
    features: dict[str, TenantFeatureStatus]


class EntitlementCheckRequest(BaseModel):
    tenant_id: UUID
    feature_key: str = Field(min_length=1, max_length=80)
    amount: int = Field(default=1, ge=1)
    resource_count: int | None = Field(default=None, ge=0)
    context: dict[str, Any] = Field(default_factory=dict)


class EntitlementCheckResponse(BaseModel):
    allowed: bool
    reason: str
    feature_key: str
    message: str | None = None
    limit: int | None = None
    used: int | None = None
    remaining: int | None = None
    subscription_status: str | None = None
    upgrade_required: bool = False


class UsageConsumeRequest(BaseModel):
    tenant_id: UUID
    feature_key: str = Field(min_length=1, max_length=80)
    amount: int = Field(default=1, ge=1)
    source: str | None = Field(default=None, max_length=80)
    external_id: str | None = Field(default=None, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=160)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UsageConsumeResponse(BaseModel):
    recorded: bool
    already_recorded: bool
    feature_key: str
    period_key: str
    used: int
    limit: int | None = None
    remaining: int | None = None


class CheckAndConsumeRequest(UsageConsumeRequest):
    resource_count: int | None = Field(default=None, ge=0)
    context: dict[str, Any] = Field(default_factory=dict)


class CheckAndConsumeResponse(BaseModel):
    allowed: bool
    reason: str
    feature_key: str
    message: str | None = None
    recorded: bool = False
    already_recorded: bool = False
    period_key: str | None = None
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    subscription_status: str | None = None
    upgrade_required: bool = False


class BillingReservationReserveRequest(BaseModel):
    tenant_id: UUID
    feature_key: Literal["pos_sales"] = "pos_sales"
    idempotency_key: str = Field(min_length=1, max_length=160)
    amount: int = Field(default=1, ge=1)
    external_id: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BillingReservationActionRequest(BaseModel):
    tenant_id: UUID
    feature_key: Literal["pos_sales"] = "pos_sales"
    idempotency_key: str = Field(min_length=1, max_length=160)


class BillingReservationResponse(BaseModel):
    allowed: bool
    status: Literal["active", "committed", "released", "denied"]
    reservation_id: UUID | None = None
    reason: str
    feature_key: str = "pos_sales"
    period_key: str | None = None
    used: int | None = None
    reserved: int | None = None
    limit: int | None = None
    remaining: int | None = None
    already_applied: bool = False

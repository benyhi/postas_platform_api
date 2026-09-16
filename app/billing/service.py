from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing.models import (
    BillingReservation,
    Feature,
    PlanFeature,
    TenantSubscription,
    UsageCounter,
    UsageEvent,
)
from app.billing.schemas import (
    BillingReservationActionRequest,
    BillingReservationReserveRequest,
    BillingReservationResponse,
    CheckAndConsumeRequest,
    CheckAndConsumeResponse,
    EntitlementCheckRequest,
    EntitlementCheckResponse,
    TenantFeatureStatus,
    TenantStatusResponse,
    TenantSubscriptionStatus,
    UsageConsumeRequest,
    UsageConsumeResponse,
)


ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active"}
CANCELLED_SUBSCRIPTION_STATUSES = {"cancelled", "canceled"}
PAYMENT_REQUIRED_SUBSCRIPTION_STATUSES = {"past_due", "unpaid", "payment_failed"}


class BillingReservationConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BillingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_tenant_status(self, tenant_id: UUID) -> TenantStatusResponse | None:
        subscription = self.get_subscription_for_tenant(tenant_id)
        if subscription is None:
            return None

        effective_status = effective_subscription_status(subscription)
        entitlements_active = effective_status in ACTIVE_SUBSCRIPTION_STATUSES
        period_key = current_period_key()
        features: dict[str, TenantFeatureStatus] = {}
        for plan_feature in subscription.plan.features:
            feature = plan_feature.feature
            used = None
            remaining = None
            if feature.type == "monthly_usage":
                counter = self.get_usage_counter(tenant_id, feature.key, period_key)
                used = counter.used if counter else 0
                reserved = (
                    self._active_reserved_amount(tenant_id, feature.key, period_key)
                    if feature.key == "pos_sales"
                    else 0
                )
                remaining = calculate_remaining(plan_feature.limit_value, used + reserved)

            features[feature.key] = TenantFeatureStatus(
                enabled=plan_feature.enabled and entitlements_active,
                limit=plan_feature.limit_value,
                used=used,
                remaining=remaining,
                reset_period=plan_feature.reset_period,
            )

        return TenantStatusResponse(
            tenant_id=tenant_id,
            status=effective_status,
            subscription=TenantSubscriptionStatus(
                plan=subscription.plan.code,
                plan_name=subscription.plan.name,
                status=effective_status,
                current_period_start=subscription.current_period_start,
                current_period_end=subscription.current_period_end,
            ),
            features=features,
        )

    def check_entitlement(self, request: EntitlementCheckRequest) -> EntitlementCheckResponse:
        return self._check_entitlement(request, period_key=current_period_key())

    def _check_entitlement(self, request: EntitlementCheckRequest, period_key: str) -> EntitlementCheckResponse:
        subscription = self.get_subscription_for_tenant(request.tenant_id)
        if subscription is None:
            return self._denied(
                request.feature_key,
                reason="subscription_not_found",
                message="El tenant no tiene una suscripcion registrada.",
                subscription_status=None,
                upgrade_required=True,
            )

        denial_reason = subscription_denial_reason(subscription)
        if denial_reason is not None:
            return self._denied(
                request.feature_key,
                reason=denial_reason,
                message=subscription_denial_message(denial_reason),
                subscription_status=subscription.status,
                upgrade_required=True,
            )

        feature = self.get_feature(request.feature_key)
        if feature is None:
            return self._denied(
                request.feature_key,
                reason="feature_not_found",
                message="La funcionalidad no existe en el catalogo.",
                subscription_status=subscription.status,
            )

        plan_feature = self.get_plan_feature(subscription.plan_id, request.feature_key)
        if plan_feature is None or not plan_feature.enabled:
            return self._denied(
                request.feature_key,
                reason="feature_not_enabled",
                message="La funcionalidad no esta habilitada para el plan actual.",
                subscription_status=subscription.status,
                limit=plan_feature.limit_value if plan_feature else None,
                used=0,
            )

        if feature.type == "boolean":
            return self._allowed(request.feature_key, subscription.status)

        if feature.type == "monthly_usage":
            counter = self.get_usage_counter(request.tenant_id, request.feature_key, period_key)
            used = counter.used if counter else 0
            reserved = (
                self._active_reserved_amount(request.tenant_id, request.feature_key, period_key)
                if request.feature_key == "pos_sales"
                else 0
            )
            limit_value = plan_feature.limit_value
            remaining = calculate_remaining(limit_value, used + reserved)
            if limit_value is not None and used + reserved + request.amount > limit_value:
                return self._denied(
                    request.feature_key,
                    reason="quota_exceeded",
                    message="Limite mensual alcanzado para esta funcionalidad.",
                    subscription_status=subscription.status,
                    limit=limit_value,
                    used=used,
                    remaining=remaining,
                    upgrade_required=True,
                )
            return self._allowed(
                request.feature_key,
                subscription.status,
                limit=limit_value,
                used=used,
                remaining=calculate_remaining(limit_value, used + reserved),
            )

        if feature.type == "resource_limit":
            limit_value = plan_feature.limit_value
            used = request.resource_count
            if limit_value is not None:
                if used is None:
                    return self._denied(
                        request.feature_key,
                        reason="resource_limit_exceeded",
                        message="resource_count es requerido para validar este limite.",
                        subscription_status=subscription.status,
                        limit=limit_value,
                        used=None,
                        remaining=None,
                        upgrade_required=True,
                    )
                if used > limit_value:
                    return self._denied(
                        request.feature_key,
                        reason="resource_limit_exceeded",
                        message="Limite de recursos alcanzado para esta funcionalidad.",
                        subscription_status=subscription.status,
                        limit=limit_value,
                        used=used,
                        remaining=calculate_remaining(limit_value, used),
                        upgrade_required=True,
                    )
            return self._allowed(
                request.feature_key,
                subscription.status,
                limit=limit_value,
                used=used,
                remaining=calculate_remaining(limit_value, used or 0),
            )

        return self._denied(
            request.feature_key,
            reason="feature_not_found",
            message="Tipo de funcionalidad no soportado.",
            subscription_status=subscription.status,
        )

    def consume_usage(self, request: UsageConsumeRequest, request_source: str) -> UsageConsumeResponse:
        existing = self.get_usage_event(request.tenant_id, request.feature_key, request.idempotency_key)
        if existing is not None:
            return self._usage_response_for_existing_event(existing)

        try:
            response = self._record_usage(request, request_source)
            self.db.commit()
            return response
        except IntegrityError:
            self.db.rollback()
            existing = self.get_usage_event(request.tenant_id, request.feature_key, request.idempotency_key)
            if existing is None:
                raise
            return self._usage_response_for_existing_event(existing)

    def check_and_consume_usage(
        self,
        request: CheckAndConsumeRequest,
        request_source: str,
    ) -> CheckAndConsumeResponse:
        self._locked_subscription(request.tenant_id)
        existing = self.get_usage_event(request.tenant_id, request.feature_key, request.idempotency_key)
        if existing is not None:
            usage = self._usage_response_for_existing_event(existing)
            subscription = self.get_subscription_for_tenant(request.tenant_id)
            return CheckAndConsumeResponse(
                allowed=True,
                reason="allowed",
                feature_key=request.feature_key,
                recorded=False,
                already_recorded=True,
                period_key=usage.period_key,
                used=usage.used,
                limit=usage.limit,
                remaining=usage.remaining,
                subscription_status=subscription.status if subscription else None,
            )

        occurred_at = ensure_utc(request.occurred_at or utc_now())
        check = self._check_entitlement(
            EntitlementCheckRequest(
                tenant_id=request.tenant_id,
                feature_key=request.feature_key,
                amount=request.amount,
                resource_count=request.resource_count,
                context=request.context,
            ),
            period_key=period_key_for(occurred_at),
        )
        if not check.allowed:
            return CheckAndConsumeResponse(
                allowed=False,
                reason=check.reason,
                feature_key=check.feature_key,
                message=check.message,
                recorded=False,
                already_recorded=False,
                used=check.used,
                limit=check.limit,
                remaining=check.remaining,
                subscription_status=check.subscription_status,
                upgrade_required=check.upgrade_required,
            )

        try:
            usage = self._record_usage(request, request_source)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.get_usage_event(request.tenant_id, request.feature_key, request.idempotency_key)
            if existing is None:
                raise
            usage = self._usage_response_for_existing_event(existing)
            usage.recorded = False
            usage.already_recorded = True

        return CheckAndConsumeResponse(
            allowed=True,
            reason="allowed",
            feature_key=request.feature_key,
            recorded=usage.recorded,
            already_recorded=usage.already_recorded,
            period_key=usage.period_key,
            used=usage.used,
            limit=usage.limit,
            remaining=usage.remaining,
            subscription_status=check.subscription_status,
        )

    def reserve_usage(
        self,
        request: BillingReservationReserveRequest,
        request_source: str,
    ) -> BillingReservationResponse:
        request_hash = reservation_request_hash(request)
        subscription = self._locked_subscription(request.tenant_id)
        if subscription is None:
            return self._reservation_denied(request.feature_key, "subscription_not_found")

        existing = self.get_reservation(request.tenant_id, request.feature_key, request.idempotency_key)
        if existing is not None:
            self._ensure_reservation_request_matches(existing, request_hash)
            if existing.status != "released":
                return self._reservation_response(existing, already_applied=True)

        denial_reason = subscription_denial_reason(subscription)
        if denial_reason is not None:
            return self._reservation_denied(request.feature_key, denial_reason)

        plan_feature = self.get_plan_feature(subscription.plan_id, request.feature_key)
        if (
            plan_feature is None
            or not plan_feature.enabled
            or plan_feature.feature.type != "monthly_usage"
        ):
            return self._reservation_denied(request.feature_key, "feature_not_enabled")

        period_key = current_period_key()
        counter = self._locked_usage_counter(request.tenant_id, request.feature_key, period_key)
        used = counter.used if counter else 0
        reserved = self._active_reserved_amount(request.tenant_id, request.feature_key, period_key)
        limit_value = plan_feature.limit_value
        if limit_value is not None and used + reserved + request.amount > limit_value:
            return BillingReservationResponse(
                allowed=False,
                status="denied",
                reason="quota_exceeded",
                feature_key=request.feature_key,
                period_key=period_key,
                used=used,
                reserved=reserved,
                limit=limit_value,
                remaining=max(limit_value - used - reserved, 0),
            )

        if existing is not None:
            reservation = existing
            reservation.status = "active"
            reservation.period_key = period_key
            reservation.limit_value = limit_value
            reservation.released_at = None
        else:
            reservation = BillingReservation(
                tenant_id=str(request.tenant_id),
                feature_key=request.feature_key,
                idempotency_key=request.idempotency_key,
                request_hash=request_hash,
                amount=request.amount,
                external_id=request.external_id,
                source=request_source,
                reservation_metadata=request.metadata or None,
                period_key=period_key,
                limit_value=limit_value,
            )
            self.db.add(reservation)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.get_reservation(request.tenant_id, request.feature_key, request.idempotency_key)
            if existing is None:
                raise
            self._ensure_reservation_request_matches(existing, request_hash)
            return self._reservation_response(existing, already_applied=True)
        self.db.refresh(reservation)
        return self._reservation_response(reservation)

    def commit_reservation(self, request: BillingReservationActionRequest) -> BillingReservationResponse:
        self._locked_subscription(request.tenant_id)
        reservation = self._locked_reservation(request)
        if reservation.status == "released":
            raise BillingReservationConflict(
                "reservation_released",
                "La reserva ya fue liberada y no puede confirmarse.",
            )
        if reservation.status == "committed":
            return self._reservation_response(reservation, already_applied=True)

        counter = self._locked_usage_counter(
            request.tenant_id,
            request.feature_key,
            reservation.period_key,
        )
        if counter is None:
            counter = UsageCounter(
                tenant_id=reservation.tenant_id,
                feature_key=reservation.feature_key,
                period_key=reservation.period_key,
                used=0,
                limit_value=reservation.limit_value,
            )
            self.db.add(counter)
            self.db.flush()
        counter.used += reservation.amount
        counter.limit_value = reservation.limit_value
        counter.updated_at = utc_now()
        self.db.add(
            UsageEvent(
                tenant_id=reservation.tenant_id,
                feature_key=reservation.feature_key,
                amount=reservation.amount,
                source=reservation.source,
                external_id=reservation.external_id,
                idempotency_key=f"reservation:{reservation.id}",
                occurred_at=utc_now(),
                period_key=reservation.period_key,
                event_metadata=reservation.reservation_metadata,
            )
        )
        reservation.status = "committed"
        reservation.committed_at = utc_now()
        self.db.commit()
        self.db.refresh(reservation)
        return self._reservation_response(reservation)

    def release_reservation(self, request: BillingReservationActionRequest) -> BillingReservationResponse:
        self._locked_subscription(request.tenant_id)
        reservation = self._locked_reservation(request)
        if reservation.status == "committed":
            raise BillingReservationConflict(
                "reservation_committed",
                "La reserva ya fue confirmada y no puede liberarse.",
            )
        if reservation.status == "released":
            return self._reservation_response(reservation, already_applied=True)
        reservation.status = "released"
        reservation.released_at = utc_now()
        self.db.commit()
        self.db.refresh(reservation)
        return self._reservation_response(reservation)

    def get_subscription_for_tenant(self, tenant_id: UUID) -> TenantSubscription | None:
        return self.db.scalar(
            select(TenantSubscription)
            .where(TenantSubscription.tenant_id == str(tenant_id))
            .order_by(TenantSubscription.created_at.desc(), TenantSubscription.id.desc())
        )

    def get_reservation(
        self,
        tenant_id: UUID,
        feature_key: str,
        idempotency_key: str,
    ) -> BillingReservation | None:
        return self.db.scalar(
            select(BillingReservation).where(
                BillingReservation.tenant_id == str(tenant_id),
                BillingReservation.feature_key == feature_key,
                BillingReservation.idempotency_key == idempotency_key,
            )
        )

    def _locked_subscription(self, tenant_id: UUID) -> TenantSubscription | None:
        return self.db.scalar(
            select(TenantSubscription)
            .where(TenantSubscription.tenant_id == str(tenant_id))
            .order_by(TenantSubscription.created_at.desc(), TenantSubscription.id.desc())
            .with_for_update()
        )

    def _locked_usage_counter(
        self,
        tenant_id: UUID,
        feature_key: str,
        period_key: str,
    ) -> UsageCounter | None:
        return self.db.scalar(
            select(UsageCounter)
            .where(
                UsageCounter.tenant_id == str(tenant_id),
                UsageCounter.feature_key == feature_key,
                UsageCounter.period_key == period_key,
            )
            .with_for_update()
        )

    def _active_reserved_amount(self, tenant_id: UUID, feature_key: str, period_key: str) -> int:
        return int(
            self.db.scalar(
                select(func.coalesce(func.sum(BillingReservation.amount), 0)).where(
                    BillingReservation.tenant_id == str(tenant_id),
                    BillingReservation.feature_key == feature_key,
                    BillingReservation.period_key == period_key,
                    BillingReservation.status == "active",
                )
            )
            or 0
        )

    def _locked_reservation(self, request: BillingReservationActionRequest) -> BillingReservation:
        reservation = self.db.scalar(
            select(BillingReservation)
            .where(
                BillingReservation.tenant_id == str(request.tenant_id),
                BillingReservation.feature_key == request.feature_key,
                BillingReservation.idempotency_key == request.idempotency_key,
            )
            .with_for_update()
        )
        if reservation is None:
            raise BillingReservationConflict("reservation_not_found", "La reserva no existe.")
        return reservation

    def _ensure_reservation_request_matches(self, reservation: BillingReservation, request_hash: str) -> None:
        if reservation.request_hash != request_hash:
            raise BillingReservationConflict(
                "idempotency_conflict",
                "La clave de idempotencia ya fue usada con otro payload.",
            )

    def _reservation_response(
        self,
        reservation: BillingReservation,
        *,
        already_applied: bool = False,
    ) -> BillingReservationResponse:
        counter = self.get_usage_counter(
            UUID(reservation.tenant_id), reservation.feature_key, reservation.period_key
        )
        used = counter.used if counter else 0
        reserved = self._active_reserved_amount(
            UUID(reservation.tenant_id), reservation.feature_key, reservation.period_key
        )
        return BillingReservationResponse(
            allowed=True,
            status=reservation.status,
            reservation_id=UUID(reservation.id),
            reason="allowed",
            feature_key=reservation.feature_key,
            period_key=reservation.period_key,
            used=used,
            reserved=reserved,
            limit=reservation.limit_value,
            remaining=(
                None
                if reservation.limit_value is None
                else max(reservation.limit_value - used - reserved, 0)
            ),
            already_applied=already_applied,
        )

    def _reservation_denied(self, feature_key: str, reason: str) -> BillingReservationResponse:
        return BillingReservationResponse(
            allowed=False,
            status="denied",
            reason=reason,
            feature_key=feature_key,
        )

    def get_plan_feature(self, plan_id: int, feature_key: str) -> PlanFeature | None:
        return self.db.scalar(
            select(PlanFeature)
            .join(Feature)
            .where(PlanFeature.plan_id == plan_id, Feature.key == feature_key)
        )

    def get_feature(self, feature_key: str) -> Feature | None:
        return self.db.scalar(select(Feature).where(Feature.key == feature_key))

    def get_usage_counter(self, tenant_id: UUID, feature_key: str, period_key: str) -> UsageCounter | None:
        return self.db.scalar(
            select(UsageCounter).where(
                UsageCounter.tenant_id == str(tenant_id),
                UsageCounter.feature_key == feature_key,
                UsageCounter.period_key == period_key,
            )
        )

    def get_usage_event(self, tenant_id: UUID, feature_key: str, idempotency_key: str) -> UsageEvent | None:
        return self.db.scalar(
            select(UsageEvent).where(
                UsageEvent.tenant_id == str(tenant_id),
                UsageEvent.feature_key == feature_key,
                UsageEvent.idempotency_key == idempotency_key,
            )
        )

    def _record_usage(self, request: UsageConsumeRequest, request_source: str) -> UsageConsumeResponse:
        occurred_at = ensure_utc(request.occurred_at or utc_now())
        period_key = period_key_for(occurred_at)
        source = request.source or request_source
        limit_value = self._current_monthly_limit(request.tenant_id, request.feature_key)

        counter = self.get_usage_counter(request.tenant_id, request.feature_key, period_key)
        if counter is None:
            counter = UsageCounter(
                tenant_id=str(request.tenant_id),
                feature_key=request.feature_key,
                period_key=period_key,
                used=0,
                limit_value=limit_value,
            )
            self.db.add(counter)
        else:
            counter.limit_value = limit_value

        event = UsageEvent(
            tenant_id=str(request.tenant_id),
            feature_key=request.feature_key,
            amount=request.amount,
            source=source,
            external_id=request.external_id,
            idempotency_key=request.idempotency_key,
            occurred_at=occurred_at,
            period_key=period_key,
            event_metadata=request.metadata or None,
        )
        self.db.add(event)
        counter.used += request.amount
        counter.updated_at = utc_now()
        self.db.flush()

        return UsageConsumeResponse(
            recorded=True,
            already_recorded=False,
            feature_key=request.feature_key,
            period_key=period_key,
            used=counter.used,
            limit=counter.limit_value,
            remaining=calculate_remaining(counter.limit_value, counter.used),
        )

    def _usage_response_for_existing_event(self, event: UsageEvent) -> UsageConsumeResponse:
        counter = self.db.scalar(
            select(UsageCounter).where(
                UsageCounter.tenant_id == event.tenant_id,
                UsageCounter.feature_key == event.feature_key,
                UsageCounter.period_key == event.period_key,
            )
        )
        if counter is None:
            limit_value = self._current_monthly_limit(UUID(event.tenant_id), event.feature_key)
            used = 0
        else:
            limit_value = counter.limit_value
            used = counter.used

        return UsageConsumeResponse(
            recorded=False,
            already_recorded=True,
            feature_key=event.feature_key,
            period_key=event.period_key,
            used=used,
            limit=limit_value,
            remaining=calculate_remaining(limit_value, used),
        )

    def _current_monthly_limit(self, tenant_id: UUID, feature_key: str) -> int | None:
        subscription = self.get_subscription_for_tenant(tenant_id)
        if subscription is None or subscription.status not in ACTIVE_SUBSCRIPTION_STATUSES:
            return None
        plan_feature = self.get_plan_feature(subscription.plan_id, feature_key)
        if plan_feature is None or plan_feature.feature.type != "monthly_usage":
            return None
        return plan_feature.limit_value

    def _allowed(
        self,
        feature_key: str,
        subscription_status: str,
        limit: int | None = None,
        used: int | None = None,
        remaining: int | None = None,
    ) -> EntitlementCheckResponse:
        return EntitlementCheckResponse(
            allowed=True,
            reason="allowed",
            feature_key=feature_key,
            limit=limit,
            used=used,
            remaining=remaining,
            subscription_status=subscription_status,
        )

    def _denied(
        self,
        feature_key: str,
        reason: str,
        message: str,
        subscription_status: str | None,
        limit: int | None = None,
        used: int | None = None,
        remaining: int | None = None,
        upgrade_required: bool = False,
    ) -> EntitlementCheckResponse:
        return EntitlementCheckResponse(
            allowed=False,
            reason=reason,
            message=message,
            feature_key=feature_key,
            limit=limit,
            used=used,
            remaining=remaining,
            subscription_status=subscription_status,
            upgrade_required=upgrade_required,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def period_key_for(value: datetime) -> str:
    value = ensure_utc(value)
    return f"{value.year:04d}-{value.month:02d}"


def current_period_key() -> str:
    return period_key_for(utc_now())


def calculate_remaining(limit_value: int | None, used: int | None) -> int | None:
    if limit_value is None or used is None:
        return None
    return max(limit_value - used, 0)


def reservation_request_hash(request: BillingReservationReserveRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def effective_subscription_status(subscription: TenantSubscription) -> str:
    return subscription_denial_reason(subscription) or subscription.status


def subscription_denial_reason(subscription: TenantSubscription) -> str | None:
    status = subscription.status.lower()
    if ensure_utc(subscription.current_period_end) < utc_now():
        return "subscription_expired"
    if status in CANCELLED_SUBSCRIPTION_STATUSES:
        return "subscription_cancelled"
    if status in PAYMENT_REQUIRED_SUBSCRIPTION_STATUSES:
        return "subscription_payment_required"
    if status not in ACTIVE_SUBSCRIPTION_STATUSES:
        return "subscription_inactive"
    return None


def subscription_denial_message(reason: str) -> str:
    messages = {
        "subscription_expired": "La suscripcion del tenant esta vencida.",
        "subscription_cancelled": "La suscripcion del tenant fue cancelada.",
        "subscription_payment_required": "La suscripcion del tenant requiere regularizar el pago.",
        "subscription_inactive": "La suscripcion del tenant no esta activa.",
    }
    return messages.get(reason, "La suscripcion del tenant no esta activa.")

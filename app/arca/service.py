from __future__ import annotations

import hashlib
import json
import re
import socket
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.arca.client import ArcaClientError, ArcaClientOptions, ArcaHTTPError, create_arca_client
from app.arca.crypto import (
    CredentialCipher,
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialValidationError,
    DecryptedCredentials,
    validate_credential_material,
)
from app.arca.models import ArcaFiscalProfile, ArcaInvoice, ArcaInvoiceItem
from app.arca.schemas import (
    FiscalProfileWrite,
    InvoiceCreateRequest,
    InvoiceError,
    InvoiceListResponse,
    InvoiceResponse,
    LastVoucherResponse,
    ProfileValidationResponse,
    SalesPointDiscoveryRequest,
    SalesPointListResponse,
    SalesPointResponse,
)
from app.billing.schemas import EntitlementCheckRequest
from app.billing.service import BillingService
from app.core.config import Settings, get_settings


MONEY = Decimal("0.01")
RETRY_DELAYS_MINUTES = (0, 1, 10, 30, 60)
MAX_ATTEMPTS = len(RETRY_DELAYS_MINUTES)
ACTIVE_STATUSES = {"pending", "processing", "retrying"}
NON_TERMINAL_STATUSES = ACTIVE_STATUSES | {"receiver_identification_required", "service_dates_required"}
NOT_FOUND_CODES = {"602", "not_found"}


class ArcaDomainError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _sanitize(message: str) -> str:
    sanitized = re.sub(r"Bearer\s+[^\s,;]+", "Bearer [REDACTED]", message, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
        "[REDACTED PEM]",
        sanitized,
        flags=re.DOTALL,
    )
    sanitized = re.sub(r"(?i)(access[_ -]?token\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", sanitized)
    return sanitized.strip()[:1000]


_SENSITIVE_PROVIDER_KEYS = {
    'access_token',
    'authorization',
    'certificate',
    'client_secret',
    'password',
    'private_key',
    'secret',
    'token',
}


def _sanitize_provider_payload(value: Any, *, key: str = '') -> Any:
    normalized_key = key.casefold().replace('-', '_').replace(' ', '_')
    if any(marker in normalized_key for marker in _SENSITIVE_PROVIDER_KEYS):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {
            item_key: _sanitize_provider_payload(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_provider_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_provider_payload(item) for item in value]
    if isinstance(value, str):
        return _sanitize(value)
    return value


class FiscalProfileService:
    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        cipher: CredentialCipher | None = None,
        client_factory: Callable[[ArcaClientOptions], Any] = create_arca_client,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self._cipher = cipher
        self.client_factory = client_factory

    @property
    def cipher(self) -> CredentialCipher:
        if self._cipher is None:
            self._cipher = CredentialCipher.from_settings(self.settings)
        return self._cipher

    def require_entitlement(self, tenant_id: UUID) -> None:
        result = BillingService(self.db).check_entitlement(
            EntitlementCheckRequest(tenant_id=tenant_id, feature_key="arca_invoicing")
        )
        if not result.allowed:
            raise ArcaDomainError(result.reason, result.message or "El plan no habilita facturacion ARCA.", status_code=403)

    def get(self, tenant_id: UUID, environment: str, *, for_update: bool = False) -> ArcaFiscalProfile | None:
        query = select(ArcaFiscalProfile).where(
                ArcaFiscalProfile.tenant_id == str(tenant_id),
                ArcaFiscalProfile.environment == environment,
                ArcaFiscalProfile.validation_status != "deleted",
        )
        if for_update:
            query = query.with_for_update()
        return self.db.scalar(query)

    def _get_any(
        self, tenant_id: UUID, environment: str, *, for_update: bool = False
    ) -> ArcaFiscalProfile | None:
        query = select(ArcaFiscalProfile).where(
                ArcaFiscalProfile.tenant_id == str(tenant_id),
                ArcaFiscalProfile.environment == environment,
        )
        if for_update:
            query = query.with_for_update()
        return self.db.scalar(query)

    def require(
        self,
        tenant_id: UUID,
        environment: str,
        *,
        validated: bool = False,
        for_update: bool = False,
    ) -> ArcaFiscalProfile:
        profile = self.get(tenant_id, environment, for_update=for_update)
        if profile is None:
            raise ArcaDomainError("fiscal_profile_not_found", "No existe un perfil fiscal para el ambiente solicitado.", status_code=404)
        if validated and profile.validation_status != "valid":
            raise ArcaDomainError(
                "fiscal_profile_not_valid",
                "El perfil fiscal debe estar validado antes de emitir.",
                status_code=409,
            )
        return profile

    def upsert(self, tenant_id: UUID, environment: str, payload: FiscalProfileWrite) -> ArcaFiscalProfile:
        self.require_entitlement(tenant_id)
        try:
            metadata = validate_credential_material(payload.certificate, payload.private_key, payload.arca_cuit)
            certificate_encrypted = self.cipher.encrypt(payload.certificate)
            private_key_encrypted = self.cipher.encrypt(payload.private_key)
            access_token_encrypted = self.cipher.encrypt(payload.access_token)
        except CredentialValidationError as exc:
            raise ArcaDomainError("invalid_credentials", str(exc), status_code=422) from exc
        except CredentialConfigurationError as exc:
            raise ArcaDomainError("credential_keyring_unavailable", str(exc), status_code=503) from exc
        profile = self._get_any(tenant_id, environment, for_update=True)
        now = utc_now()
        if profile is None:
            profile = ArcaFiscalProfile(tenant_id=str(tenant_id), environment=environment)
            self.db.add(profile)
        elif self.db.scalar(
            select(ArcaInvoice.id).where(
                ArcaInvoice.fiscal_profile_id == profile.id,
                ArcaInvoice.status.in_(NON_TERMINAL_STATUSES),
            ).limit(1)
        ):
            raise ArcaDomainError(
                "fiscal_profile_has_pending_invoices",
                "No se puede reemplazar el perfil mientras tenga solicitudes fiscales no terminales.",
                status_code=409,
            )
        profile.arca_cuit = payload.arca_cuit
        profile.certificate_encrypted = certificate_encrypted
        profile.private_key_encrypted = private_key_encrypted
        profile.access_token_encrypted = access_token_encrypted
        profile.credential_key_id = self.cipher.active_key_id
        profile.certificate_fingerprint = metadata.fingerprint
        profile.certificate_expires_at = metadata.expires_at
        profile.point_of_sale = payload.point_of_sale
        profile.automatic_voucher_type = payload.automatic_voucher_type
        profile.concept = payload.concept
        profile.default_vat_rate = payload.default_vat_rate
        profile.default_vat_id = payload.default_vat_id
        profile.validation_status = "pending"
        profile.validation_error = None
        profile.validated_at = None
        profile.credentials_rotated_at = now
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def delete(self, tenant_id: UUID, environment: str) -> None:
        profile = self.require(tenant_id, environment, for_update=True)
        active_invoice = self.db.scalar(
            select(ArcaInvoice.id).where(
                ArcaInvoice.fiscal_profile_id == profile.id,
                ArcaInvoice.status.in_(NON_TERMINAL_STATUSES),
            ).limit(1)
        )
        if active_invoice:
            raise ArcaDomainError(
                "fiscal_profile_in_use",
                "El perfil posee facturas pendientes y no puede eliminarse.",
                status_code=409,
            )
        tombstone = f"deleted:{profile.id}:{self.now_iso()}"
        profile.certificate_encrypted = self.cipher.encrypt(tombstone)
        profile.private_key_encrypted = self.cipher.encrypt(tombstone)
        profile.access_token_encrypted = self.cipher.encrypt(tombstone)
        profile.credential_key_id = self.cipher.active_key_id
        profile.validation_status = "deleted"
        profile.validation_error = None
        profile.validated_at = None
        profile.credentials_rotated_at = utc_now()
        self.db.commit()

    @staticmethod
    def now_iso() -> str:
        return utc_now().isoformat()

    def decrypt(self, profile: ArcaFiscalProfile) -> DecryptedCredentials:
        try:
            return DecryptedCredentials(
                certificate=self.cipher.decrypt(profile.certificate_encrypted, profile.credential_key_id),
                private_key=self.cipher.decrypt(profile.private_key_encrypted, profile.credential_key_id),
                access_token=self.cipher.decrypt(profile.access_token_encrypted, profile.credential_key_id),
            )
        except (CredentialConfigurationError, CredentialDecryptionError) as exc:
            raise ArcaDomainError("credential_keyring_unavailable", str(exc), status_code=503) from exc

    def client_for(self, profile: ArcaFiscalProfile):
        return self.client_factory(
            ArcaClientOptions(
                cuit=profile.arca_cuit,
                environment=profile.environment,
                credentials=self.decrypt(profile),
                timeout_seconds=self.settings.arca_timeout_seconds,
                production_calls_enabled=self.settings.arca_production_calls_enabled,
            )
        )

    def validate(self, tenant_id: UUID, environment: str) -> ProfileValidationResponse:
        self.require_entitlement(tenant_id)
        profile = self.require(tenant_id, environment)
        try:
            client = self.client_for(profile)
            client.ElectronicBilling.getLastVoucher(profile.point_of_sale, profile.automatic_voucher_type)
        except ArcaDomainError:
            raise
        except Exception as exc:
            error = _classify_error(exc)
            profile.validation_status = "invalid"
            profile.validation_error = error.message
            profile.validated_at = utc_now()
            self.db.commit()
            return ProfileValidationResponse(valid=False, status="invalid", error=error.message)
        profile.validation_status = "valid"
        profile.validation_error = None
        profile.validated_at = utc_now()
        self.db.commit()
        return ProfileValidationResponse(valid=True, status="valid")

    def discover_sales_points(
        self, tenant_id: UUID, payload: SalesPointDiscoveryRequest
    ) -> SalesPointListResponse:
        self.require_entitlement(tenant_id)
        try:
            validate_credential_material(payload.certificate, payload.private_key, payload.arca_cuit)
            client = self.client_factory(
                ArcaClientOptions(
                    cuit=payload.arca_cuit,
                    environment=payload.arca_environment,
                    credentials=DecryptedCredentials(
                        certificate=payload.certificate,
                        private_key=payload.private_key,
                        access_token=payload.access_token,
                    ),
                    timeout_seconds=self.settings.arca_timeout_seconds,
                    production_calls_enabled=self.settings.arca_production_calls_enabled,
                )
            )
            raw = client.ElectronicBilling.getSalesPoints()
        except CredentialValidationError as exc:
            raise ArcaDomainError("invalid_credentials", str(exc), status_code=422) from exc
        except ArcaDomainError:
            raise
        except Exception as exc:
            if "produccion estan deshabilitadas" in _sanitize(str(exc)).lower():
                raise ArcaDomainError(
                    "arca_production_disabled",
                    "Las llamadas ARCA de produccion estan deshabilitadas en esta instancia.",
                    status_code=503,
                ) from exc
            error = _classify_error(exc)
            status_code = 504 if error.code == "arca_timeout" else 503 if error.retryable else 422
            raise ArcaDomainError(error.code, error.message, status_code=status_code) from exc
        return SalesPointListResponse(results=_normalize_sales_points(raw))

    def rotate_all_to_active_key(self) -> int:
        profiles = list(self.db.scalars(select(ArcaFiscalProfile).with_for_update()).all())
        rotated = 0
        for profile in profiles:
            if profile.credential_key_id == self.cipher.active_key_id:
                continue
            credentials = self.decrypt(profile)
            profile.certificate_encrypted = self.cipher.encrypt(credentials.certificate)
            profile.private_key_encrypted = self.cipher.encrypt(credentials.private_key)
            profile.access_token_encrypted = self.cipher.encrypt(credentials.access_token)
            profile.credential_key_id = self.cipher.active_key_id
            profile.credentials_rotated_at = utc_now()
            rotated += 1
        self.db.commit()
        return rotated


class InvoiceService:
    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        profile_service: FiscalProfileService | None = None,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.profiles = profile_service or FiscalProfileService(db, settings=self.settings)
        self.now = now_provider

    def enqueue(
        self,
        tenant_id: UUID,
        environment: str,
        payload: InvoiceCreateRequest,
        *,
        voucher_number: int | None = None,
        automatic: bool = False,
    ) -> InvoiceResponse:
        self.profiles.require_entitlement(tenant_id)
        profile = self.profiles.require(tenant_id, environment, validated=True, for_update=True)
        request_hash = self._request_hash(environment, payload, voucher_number)
        existing = self._find_external(tenant_id, payload.external_id)
        if existing is not None:
            if existing.status == "receiver_identification_required":
                return self._resume_receiver_identification(
                    existing,
                    payload,
                    voucher_number=voucher_number,
                )
            if existing.request_hash != request_hash:
                raise ArcaDomainError(
                    "idempotency_conflict",
                    "El external_id ya fue utilizado con un contenido diferente.",
                    status_code=409,
                )
            return self.to_response(existing)

        if payload.sale_id is not None and self._find_sale(tenant_id, payload.sale_id) is not None:
            raise ArcaDomainError(
                "sale_already_invoiced",
                "La venta ya posee una solicitud fiscal.",
                status_code=409,
            )

        if (automatic or payload.sale_id is not None) and profile.concept != 1:
            raise ArcaDomainError(
                "automatic_invoicing_requires_products_concept",
                "La facturacion automatica de ventas requiere concepto Productos.",
                status_code=409,
            )
        if profile.concept in {2, 3} and not all(
            (payload.service_start_date, payload.service_end_date, payload.payment_due_date)
        ):
            raise ArcaDomainError(
                "service_dates_required",
                "El concepto Servicios o Mixto requiere fechas de servicio y vencimiento.",
                status_code=422,
            )

        item_amounts, net_total, vat_total, final_total = self._calculate_amounts(
            payload, profile.default_vat_rate, profile.default_vat_id
        )
        receiver_missing = (
            final_total >= Decimal(str(self.settings.arca_consumer_final_identification_threshold))
            and payload.receiver.doc_type == 99
            and payload.receiver.doc_number in {"0", ""}
        )
        status = "receiver_identification_required" if receiver_missing else "pending"
        now = self.now()
        record = ArcaInvoice(
            tenant_id=str(tenant_id),
            environment=environment,
            fiscal_profile_id=profile.id,
            sale_id=str(payload.sale_id) if payload.sale_id else None,
            external_id=payload.external_id,
            actor_id=str(payload.actor_id) if payload.actor_id else None,
            actor_role=payload.actor_role,
            request_hash=request_hash,
            request_payload=payload.model_dump(mode="json"),
            arca_cuit=profile.arca_cuit,
            point_of_sale=profile.point_of_sale,
            voucher_type=profile.automatic_voucher_type,
            voucher_number=voucher_number,
            explicit_number=voucher_number is not None,
            concept=profile.concept,
            doc_type=payload.receiver.doc_type,
            doc_number=payload.receiver.doc_number,
            receiver_iva_condition_id=payload.receiver.iva_condition_id,
            invoice_date=payload.invoice_date,
            service_start_date=payload.service_start_date,
            service_end_date=payload.service_end_date,
            payment_due_date=payload.payment_due_date,
            net_amount=net_total,
            exempt_amount=Decimal("0.00"),
            iva_amount=vat_total,
            total_amount=final_total,
            status=status,
            next_retry_at=None if receiver_missing else now,
            last_error_code="receiver_identification_required" if receiver_missing else None,
            last_error_message=(
                "La operacion requiere identificar al receptor antes de emitir."
                if receiver_missing
                else None
            ),
        )
        for source, amounts in zip(payload.items, item_amounts, strict=True):
            record.items.append(
                ArcaInvoiceItem(
                    description=source.description,
                    quantity=source.quantity,
                    final_unit_price=source.final_unit_price,
                    final_subtotal=amounts["final"],
                    net_amount=amounts["net"],
                    vat_rate=profile.default_vat_rate,
                    vat_id=profile.default_vat_id,
                    vat_amount=amounts["vat"],
                )
            )
        self.db.add(record)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            existing = self._find_external(tenant_id, payload.external_id)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ArcaDomainError(
                        "idempotency_conflict",
                        "El external_id ya fue utilizado con un contenido diferente.",
                        status_code=409,
                    ) from exc
                return self.to_response(existing)
            if payload.sale_id is not None and self._find_sale(tenant_id, payload.sale_id) is not None:
                raise ArcaDomainError(
                    "sale_already_invoiced",
                    "La venta ya posee una solicitud fiscal.",
                    status_code=409,
                ) from exc
            raise ArcaDomainError(
                "fiscal_reference_conflict",
                "La referencia fiscal ya esta reservada por otra solicitud.",
                status_code=409,
            ) from exc
        return self.to_response(record)

    def _resume_receiver_identification(
        self,
        record: ArcaInvoice,
        payload: InvoiceCreateRequest,
        *,
        voucher_number: int | None,
    ) -> InvoiceResponse:
        original = record.request_payload
        candidate = payload.model_dump(mode="json")
        immutable_fields = ("external_id", "sale_id")
        if any(original.get(field) != candidate.get(field) for field in immutable_fields):
            raise ArcaDomainError(
                "idempotency_conflict",
                "Solo pueden completarse los datos del receptor de la solicitud existente.",
                status_code=409,
            )
        if payload.receiver.doc_type == 99 and payload.receiver.doc_number in {"", "0"}:
            return self.to_response(record)
        if voucher_number is None:
            raise ArcaDomainError(
                "explicit_voucher_required",
                "La solicitud debe completarse mediante emision explicita.",
                status_code=409,
            )
        resumed_payload = InvoiceCreateRequest.model_validate(
            {**original, "receiver": payload.receiver.model_dump(mode="json")}
        )
        record.request_hash = self._request_hash(
            record.environment,
            resumed_payload,
            voucher_number,
        )
        record.request_payload = resumed_payload.model_dump(mode="json")
        record.doc_type = payload.receiver.doc_type
        record.doc_number = payload.receiver.doc_number
        record.receiver_iva_condition_id = payload.receiver.iva_condition_id
        record.voucher_number = voucher_number
        record.explicit_number = True
        record.status = "pending"
        record.next_retry_at = self.now()
        record.last_error_code = None
        record.last_error_message = None
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ArcaDomainError(
                "fiscal_reference_conflict",
                "La referencia fiscal ya esta reservada por otra solicitud.",
                status_code=409,
            ) from exc
        return self.to_response(record)

    def get_by_external_id(self, tenant_id: UUID, external_id: str) -> InvoiceResponse:
        record = self._find_external(tenant_id, external_id)
        if record is None:
            raise ArcaDomainError("invoice_not_found", "No se encontro la factura.", status_code=404)
        return self.to_response(record)

    def get_by_sale_id(self, tenant_id: UUID, sale_id: UUID) -> InvoiceResponse:
        record = self.db.scalar(
            select(ArcaInvoice).where(
                ArcaInvoice.tenant_id == str(tenant_id), ArcaInvoice.sale_id == str(sale_id)
            )
        )
        if record is None:
            raise ArcaDomainError("invoice_not_found", "No se encontro la factura de la venta.", status_code=404)
        return self.to_response(record)

    def list_invoices(self, tenant_id: UUID, *, offset: int, limit: int) -> InvoiceListResponse:
        tenant = str(tenant_id)
        count = self.db.scalar(
            select(func.count()).select_from(ArcaInvoice).where(ArcaInvoice.tenant_id == tenant)
        ) or 0
        records = self.db.scalars(
            select(ArcaInvoice)
            .options(selectinload(ArcaInvoice.items))
            .where(ArcaInvoice.tenant_id == tenant)
            .order_by(ArcaInvoice.created_at.desc(), ArcaInvoice.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return InvoiceListResponse(
            count=count,
            results=[self.to_response(record) for record in records],
        )

    def get_by_fiscal_reference(
        self, tenant_id: UUID, environment: str, point_of_sale: int, voucher_type: int, voucher_number: int
    ) -> InvoiceResponse:
        record = self.db.scalar(
            select(ArcaInvoice).where(
                ArcaInvoice.tenant_id == str(tenant_id),
                ArcaInvoice.environment == environment,
                ArcaInvoice.point_of_sale == point_of_sale,
                ArcaInvoice.voucher_type == voucher_type,
                ArcaInvoice.voucher_number == voucher_number,
            )
        )
        if record is None:
            raise ArcaDomainError("invoice_not_found", "No se encontro la referencia fiscal.", status_code=404)
        return self.to_response(record)

    def get_last_voucher(self, tenant_id: UUID, environment: str, voucher_type: int | None = None) -> LastVoucherResponse:
        self.profiles.require_entitlement(tenant_id)
        profile = self.profiles.require(tenant_id, environment, validated=True)
        client = self.profiles.client_for(profile)
        selected_type = voucher_type or profile.automatic_voucher_type
        last = client.ElectronicBilling.getLastVoucher(profile.point_of_sale, selected_type)
        return LastVoucherResponse(
            environment=environment,
            point_of_sale=profile.point_of_sale,
            voucher_type=selected_type,
            last_voucher_number=int(last or 0),
        )

    def process(self, record: ArcaInvoice) -> InvoiceResponse:
        if record.status not in ACTIVE_STATUSES:
            return self.to_response(record)
        profile = self.db.get(ArcaFiscalProfile, record.fiscal_profile_id)
        if profile is None or profile.tenant_id != record.tenant_id or profile.environment != record.environment:
            return self._finish_error(record, InvoiceError(code="fiscal_profile_mismatch", message="El perfil fiscal de la factura no es valido."))
        if profile.validation_status != "valid":
            return self._finish_error(record, InvoiceError(code="fiscal_profile_not_valid", message="El perfil fiscal dejo de estar validado."))

        record.status = "processing"
        record.last_attempt_at = self.now()
        record.attempt_count += 1
        lease_seconds = max(60.0, self.settings.arca_timeout_seconds * 2)
        record.next_retry_at = self.now() + timedelta(seconds=lease_seconds)
        self.db.commit()

        try:
            client = self.profiles.client_for(profile)
            billing = client.ElectronicBilling
        except Exception as exc:
            return self._handle_error(record, _classify_error(exc))

        if record.voucher_number is not None and record.attempt_count > 1:
            try:
                existing = billing.getVoucherInfo(record.voucher_number, record.point_of_sale, record.voucher_type)
            except Exception as exc:
                query_error = _classify_error(exc)
                if query_error.code != "not_found":
                    return self._handle_error(record, query_error)
            else:
                normalized = self._normalize_response(existing)
                if normalized["cae"]:
                    return self._approve(record, existing, normalized)
                return self._finish_error(
                    record,
                    InvoiceError(code="arca_rejected", message="ARCA devolvio un comprobante sin CAE."),
                )

        try:
            self._acquire_sequence_lock(record)
            last_number = int(billing.getLastVoucher(record.point_of_sale, record.voucher_type) or 0)
            expected = last_number + 1
            if record.voucher_number is None:
                reserved = self.db.scalar(
                    select(ArcaInvoice.id).where(
                        ArcaInvoice.id != record.id,
                        ArcaInvoice.environment == record.environment,
                        ArcaInvoice.arca_cuit == record.arca_cuit,
                        ArcaInvoice.point_of_sale == record.point_of_sale,
                        ArcaInvoice.voucher_type == record.voucher_type,
                        ArcaInvoice.voucher_number == expected,
                        ArcaInvoice.status != "rejected",
                    ).limit(1)
                )
                if reserved is not None:
                    return self._handle_error(
                        record,
                        InvoiceError(
                            code="sequence_busy",
                            message="La correlativa esta reservada por otra solicitud fiscal.",
                            retryable=True,
                        ),
                    )
                record.voucher_number = expected
                try:
                    self.db.commit()
                except IntegrityError:
                    self.db.rollback()
                    record = self.db.get(ArcaInvoice, record.id)
                    return self._handle_error(
                        record,
                        InvoiceError(
                            code="sequence_busy",
                            message="La correlativa fue reservada concurrentemente.",
                            retryable=True,
                        ),
                    )
            elif record.voucher_number != expected:
                return self._finish_error(
                    record,
                    InvoiceError(
                        code="correlative_conflict",
                        message=f"La proxima correlativa disponible es {expected}.",
                    ),
                )
            raw = billing.createVoucher(self._arca_payload(record), return_response=True)
            normalized = self._normalize_response(raw)
            if not normalized["cae"]:
                return self._finish_error(
                    record,
                    InvoiceError(code="arca_rejected", message="ARCA no autorizo el comprobante."),
                    raw=raw,
                )
            return self._approve(record, raw, normalized)
        except Exception as exc:
            return self._handle_error(record, _classify_error(exc))

    def _approve(self, record: ArcaInvoice, raw: dict[str, Any], normalized: dict[str, Any]) -> InvoiceResponse:
        record.status = "approved"
        record.cae = normalized["cae"]
        record.cae_expiration_date = normalized["expiration"]
        record.arca_result = normalized["result"]
        record.arca_response = _sanitize_provider_payload(raw)
        record.observations = normalized["observations"]
        record.last_error_code = None
        record.last_error_message = None
        record.next_retry_at = None
        self.db.commit()
        return self.to_response(record)

    def _finish_error(self, record: ArcaInvoice, error: InvoiceError, raw: dict | None = None) -> InvoiceResponse:
        record.status = "rejected" if error.code in {"arca_rejected", "correlative_conflict"} else "failed"
        record.last_error_code = error.code
        record.last_error_message = error.message
        record.arca_response = _sanitize_provider_payload(raw)
        record.next_retry_at = None
        self.db.commit()
        return self.to_response(record)

    def _handle_error(self, record: ArcaInvoice, error: InvoiceError) -> InvoiceResponse:
        record.last_error_code = error.code
        record.last_error_message = error.message
        if error.retryable and record.attempt_count < MAX_ATTEMPTS:
            record.status = "retrying"
            delay = RETRY_DELAYS_MINUTES[record.attempt_count]
            record.next_retry_at = self.now() + timedelta(minutes=delay)
        else:
            record.status = "failed" if error.retryable else "rejected"
            record.next_retry_at = None
        self.db.commit()
        return self.to_response(record)

    def _acquire_sequence_lock(self, record: ArcaInvoice) -> None:
        if self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return
        raw = f"{record.environment}:{record.arca_cuit}:{record.point_of_sale}:{record.voucher_type}"
        unsigned = int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")
        signed = unsigned if unsigned < (1 << 63) else unsigned - (1 << 64)
        self.db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": signed})

    def _arca_payload(self, record: ArcaInvoice) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "CantReg": 1,
            "PtoVta": record.point_of_sale,
            "CbteTipo": record.voucher_type,
            "Concepto": record.concept,
            "DocTipo": record.doc_type,
            "DocNro": int(record.doc_number or "0"),
            "CbteDesde": record.voucher_number,
            "CbteHasta": record.voucher_number,
            "CbteFch": record.invoice_date.strftime("%Y%m%d"),
            "ImpTotal": float(record.total_amount),
            "ImpTotConc": 0.0,
            "ImpNeto": float(record.net_amount),
            "ImpOpEx": float(record.exempt_amount),
            "ImpIVA": float(record.iva_amount),
            "ImpTrib": 0.0,
            "MonId": "PES",
            "MonCotiz": 1.0,
            "CondicionIVAReceptorId": record.receiver_iva_condition_id,
            "Iva": [
                {
                    "Id": record.items[0].vat_id,
                    "BaseImp": float(record.net_amount),
                    "Importe": float(record.iva_amount),
                }
            ],
        }
        if record.concept in {2, 3}:
            payload.update(
                {
                    "FchServDesde": record.service_start_date.strftime("%Y%m%d"),
                    "FchServHasta": record.service_end_date.strftime("%Y%m%d"),
                    "FchVtoPago": record.payment_due_date.strftime("%Y%m%d"),
                }
            )
        return payload

    @staticmethod
    def _calculate_amounts(payload: InvoiceCreateRequest, vat_rate: Decimal, vat_id: int):
        factor = Decimal("1") + vat_rate / Decimal("100")
        rows: list[dict[str, Decimal | int]] = []
        net_total = Decimal("0")
        vat_total = Decimal("0")
        final_total = Decimal("0")
        for item in payload.items:
            final = _quantize(item.quantity * item.final_unit_price)
            net = _quantize(final / factor) if factor else final
            vat = final - net
            rows.append({"final": final, "net": net, "vat": vat, "vat_id": vat_id})
            net_total += net
            vat_total += vat
            final_total += final
        return rows, _quantize(net_total), _quantize(vat_total), _quantize(final_total)

    @staticmethod
    def _request_hash(environment: str, payload: InvoiceCreateRequest, voucher_number: int | None) -> str:
        canonical = json.dumps(
            {
                "environment": environment,
                "voucher_number": voucher_number,
                "payload": payload.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _find_external(self, tenant_id: UUID, external_id: str) -> ArcaInvoice | None:
        return self.db.scalar(
            select(ArcaInvoice)
            .options(selectinload(ArcaInvoice.items))
            .where(ArcaInvoice.tenant_id == str(tenant_id), ArcaInvoice.external_id == external_id)
        )

    def _find_sale(self, tenant_id: UUID, sale_id: UUID) -> ArcaInvoice | None:
        return self.db.scalar(
            select(ArcaInvoice)
            .options(selectinload(ArcaInvoice.items))
            .where(
                ArcaInvoice.tenant_id == str(tenant_id),
                ArcaInvoice.sale_id == str(sale_id),
            )
        )

    @staticmethod
    def _normalize_response(raw: dict[str, Any]) -> dict[str, Any]:
        cae = _find_first(raw, {"CAE", "Cae", "CodAutorizacion"})
        expiration = _find_first(raw, {"CAEFchVto", "FchVto"})
        result = _find_first(raw, {"Resultado", "Result"})
        return {
            "cae": str(cae) if cae not in (None, "") else None,
            "expiration": _parse_arca_date(expiration),
            "result": str(result) if result not in (None, "") else None,
            "observations": _collect_observations(raw),
        }

    @staticmethod
    def to_response(record: ArcaInvoice) -> InvoiceResponse:
        error = None
        if record.last_error_code:
            error = InvoiceError(
                code=record.last_error_code,
                message=record.last_error_message or "La operacion fiscal no pudo completarse.",
                retryable=record.status in {"pending", "processing", "retrying"},
            )
        return InvoiceResponse(
            id=record.id,
            tenant_id=UUID(record.tenant_id),
            environment=record.environment,
            fiscal_profile_id=record.fiscal_profile_id,
            sale_id=UUID(record.sale_id) if record.sale_id else None,
            external_id=record.external_id,
            actor_id=UUID(record.actor_id) if record.actor_id else None,
            actor_role=record.actor_role,
            status=record.status,
            point_of_sale=record.point_of_sale,
            voucher_type=record.voucher_type,
            voucher_number=record.voucher_number,
            net_amount=record.net_amount,
            iva_amount=record.iva_amount,
            total_amount=record.total_amount,
            cae=record.cae,
            cae_expiration_date=record.cae_expiration_date,
            observations=record.observations or [],
            attempt_count=record.attempt_count,
            next_retry_at=record.next_retry_at,
            error=error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def _classify_error(exc: Exception) -> InvoiceError:
    message = _sanitize(str(exc))
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return InvoiceError(
            code="arca_timeout",
            message="ARCA no respondio dentro del tiempo esperado; se verificara el mismo comprobante.",
            retryable=True,
        )
    if isinstance(exc, ArcaHTTPError):
        if exc.status_code in {401, 403}:
            return InvoiceError(code="arca_auth_error", message="ARCA rechazo las credenciales configuradas.")
        if exc.status_code in {408, 429} or exc.status_code >= 500:
            return InvoiceError(code="arca_unavailable", message="ARCA se encuentra temporalmente fuera de servicio.", retryable=True)
    if isinstance(exc, (ConnectionError, OSError)):
        return InvoiceError(code="arca_unavailable", message="No fue posible conectarse con ARCA.", retryable=True)
    match = re.match(r"^\(([^)]+)\)\s*(.*)$", message)
    external_code = match.group(1) if match else None
    external_message = match.group(2) if match else message
    if external_code in NOT_FOUND_CODES or "no encontrado" in external_message.lower():
        return InvoiceError(code="not_found", message="ARCA no encontro el comprobante.")
    if any(word in external_message.lower() for word in ("token", "credencial", "autoriz", "autentic")):
        return InvoiceError(code="arca_auth_error", message="ARCA rechazo las credenciales configuradas.")
    if isinstance(exc, ArcaClientError):
        return InvoiceError(code="arca_rejected", message=external_message[:500] or "ARCA rechazo la solicitud.")
    return InvoiceError(code="arca_unavailable", message="La integracion ARCA devolvio un error inesperado.", retryable=True)


def _find_first(node: Any, keys: set[str]):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                return value
        for value in node.values():
            found = _find_first(value, keys)
            if found is not None:
                return found
    elif isinstance(node, (list, tuple)):
        for value in node:
            found = _find_first(value, keys)
            if found is not None:
                return found
    return None


def _collect_observations(node: Any) -> list[str]:
    messages: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("Msg"), str):
            code = node.get("Code")
            messages.append(f"({code}) {node['Msg']}" if code is not None else node["Msg"])
        for value in node.values():
            if isinstance(value, (dict, list, tuple)):
                messages.extend(_collect_observations(value))
    elif isinstance(node, (list, tuple)):
        for value in node:
            messages.extend(_collect_observations(value))
    return list(dict.fromkeys(message[:500] for message in messages))


def _parse_arca_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def _normalize_sales_points(raw: Any) -> list[SalesPointResponse]:
    candidates: list[dict[str, Any]] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if any(key in node for key in ("Nro", "Numero", "number")):
                candidates.append(node)
            else:
                for value in node.values():
                    collect(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                collect(value)

    collect(raw)
    results: list[SalesPointResponse] = []
    for candidate in candidates:
        number = candidate.get("Nro", candidate.get("Numero", candidate.get("number")))
        emission = candidate.get(
            "EmisionTipo", candidate.get("TipoEmision", candidate.get("emission_type", ""))
        )
        blocked_raw = candidate.get("Bloqueado", candidate.get("blocked", False))
        blocked = blocked_raw is True or str(blocked_raw).strip().upper() in {
            "S",
            "SI",
            "TRUE",
            "1",
        }
        deactivation = _parse_arca_date(
            candidate.get("FchBaja", candidate.get("FechaBaja", candidate.get("deactivation_date")))
        )
        try:
            parsed_number = int(number)
        except (TypeError, ValueError):
            continue
        if parsed_number <= 0 or blocked or deactivation is not None:
            continue
        results.append(
            SalesPointResponse(
                number=parsed_number,
                emission_type=str(emission or ""),
                blocked=False,
                deactivation_date=None,
            )
        )
    return sorted(results, key=lambda point: point.number)

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.mercado_pago.client import MercadoPagoClient, MercadoPagoClientError
from app.mercado_pago.crypto import MercadoPagoCredentialCipher
from app.mercado_pago.models import MercadoPagoConnection, MercadoPagoOAuthState
from app.mercado_pago.schemas import (
    MercadoPagoConnectionResponse,
    MercadoPagoOrderCreateRequest,
    MercadoPagoOrderResponse,
    MercadoPagoPosResponse,
    MercadoPagoTerminalResponse,
    OAuthAuthorizationResponse,
    OAuthTokenData,
)


class MercadoPagoDomainError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class MercadoPagoService:
    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        client: MercadoPagoClient | None = None,
        cipher: MercadoPagoCredentialCipher | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = client or MercadoPagoClient(self.settings)
        self._cipher = cipher

    @property
    def cipher(self) -> MercadoPagoCredentialCipher:
        if self._cipher is None:
            self._cipher = MercadoPagoCredentialCipher.from_settings(self.settings)
        return self._cipher

    def start_authorization(self, tenant_id: UUID) -> OAuthAuthorizationResponse:
        client_id = self._required_setting(self.settings.mercado_pago_client_id, "client_id")
        redirect_uri = self._required_setting(self.settings.mercado_pago_redirect_uri, "redirect_uri")
        now = utc_now()
        for old_state in self.db.scalars(
            select(MercadoPagoOAuthState).where(
                MercadoPagoOAuthState.tenant_id == str(tenant_id),
                MercadoPagoOAuthState.consumed_at.is_(None),
            )
        ):
            old_state.consumed_at = now

        raw_state = secrets.token_urlsafe(48)
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        expires_at = now + timedelta(seconds=self.settings.mercado_pago_oauth_state_ttl_seconds)
        self.db.add(
            MercadoPagoOAuthState(
                tenant_id=str(tenant_id),
                state_hash=hash_secret(raw_state),
                code_verifier_encrypted=self.cipher.encrypt(verifier),
                credential_key_id=self.cipher.active_key_id,
                expires_at=expires_at,
            )
        )
        self.db.commit()
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": raw_state,
            "scope": "offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.settings.mercado_pago_platform_id:
            query["platform_id"] = self.settings.mercado_pago_platform_id
        return OAuthAuthorizationResponse(
            authorization_url=f"{self.settings.mercado_pago_auth_base_url}/authorization?{urlencode(query)}",
            expires_at=expires_at,
        )

    def complete_authorization(self, state: str, code: str) -> MercadoPagoConnectionResponse:
        oauth_state = self.db.scalar(
            select(MercadoPagoOAuthState)
            .where(MercadoPagoOAuthState.state_hash == hash_secret(state))
            .with_for_update()
        )
        if oauth_state is None:
            raise MercadoPagoDomainError("invalid_oauth_state", "El state OAuth es invalido.", 400)
        if oauth_state.consumed_at is not None:
            raise MercadoPagoDomainError("oauth_state_used", "El state OAuth ya fue utilizado.", 409)
        if ensure_utc(oauth_state.expires_at) <= utc_now():
            oauth_state.consumed_at = utc_now()
            self.db.commit()
            raise MercadoPagoDomainError("oauth_state_expired", "El state OAuth expiro.", 400)

        verifier = self.cipher.decrypt(
            oauth_state.code_verifier_encrypted, oauth_state.credential_key_id
        )
        oauth_state.consumed_at = utc_now()
        self.db.commit()
        try:
            token = self.client.exchange_authorization_code(code, verifier)
        except MercadoPagoClientError as exc:
            raise _domain_from_client(exc) from exc
        if not token.refresh_token or token.user_id is None:
            raise MercadoPagoDomainError(
                "invalid_oauth_grant",
                "Mercado Pago no devolvio refresh token y collector para una conexion renovable.",
                502,
            )
        return self._save_connection(UUID(oauth_state.tenant_id), token)

    def get_status(self, tenant_id: UUID) -> MercadoPagoConnectionResponse:
        connection = self._get_connection(tenant_id)
        if connection is None:
            return MercadoPagoConnectionResponse(
                tenant_id=tenant_id, connected=False, status="unlinked"
            )
        return connection_response(connection)

    def unlink(self, tenant_id: UUID) -> MercadoPagoConnectionResponse:
        now = utc_now()
        connection = self._get_connection(tenant_id, for_update=True)
        if connection is not None:
            connection.access_token_encrypted = None
            connection.refresh_token_encrypted = None
            connection.credential_key_id = None
            connection.token_expires_at = None
            connection.status = "unlinked"
            connection.disconnected_at = now
            connection.last_error_code = None
        for oauth_state in self.db.scalars(
            select(MercadoPagoOAuthState).where(
                MercadoPagoOAuthState.tenant_id == str(tenant_id),
                MercadoPagoOAuthState.consumed_at.is_(None),
            )
        ):
            oauth_state.consumed_at = now
        self.db.commit()
        return self.get_status(tenant_id)

    def list_terminals(self, tenant_id: UUID) -> list[MercadoPagoTerminalResponse]:
        token = self._access_token(tenant_id)
        try:
            raw = self.client.list_terminals(token)
        except MercadoPagoClientError as exc:
            raise _domain_from_client(exc) from exc
        results: list[MercadoPagoTerminalResponse] = []
        for item in raw:
            terminal_id = item.get("id") or item.get("terminal_id") or item.get("device_id")
            if terminal_id is None:
                continue
            results.append(
                MercadoPagoTerminalResponse(
                    id=str(terminal_id),
                    status=_string_or_none(item.get("status")),
                    operating_mode=_string_or_none(item.get("operating_mode")),
                    pos_id=item.get("pos_id"),
                    store_id=item.get("store_id"),
                )
            )
        return results

    def list_pos(self, tenant_id: UUID) -> list[MercadoPagoPosResponse]:
        token = self._access_token(tenant_id)
        try:
            raw = self.client.list_pos(token)
        except MercadoPagoClientError as exc:
            raise _domain_from_client(exc) from exc
        results: list[MercadoPagoPosResponse] = []
        for item in raw:
            pos_id = item.get("id") or item.get("pos_id")
            if pos_id is None:
                continue
            results.append(
                MercadoPagoPosResponse(
                    id=pos_id,
                    name=_string_or_none(item.get("name")),
                    external_id=_string_or_none(item.get("external_id")),
                    store_id=item.get("store_id"),
                    external_store_id=_string_or_none(item.get("external_store_id")),
                    fixed_amount=item.get("fixed_amount") if isinstance(item.get("fixed_amount"), bool) else None,
                    category=item.get("category"),
                )
            )
        return results

    def create_order(
        self,
        tenant_id: UUID,
        request: MercadoPagoOrderCreateRequest,
        idempotency_key: str,
    ) -> MercadoPagoOrderResponse:
        access_token = self._access_token(tenant_id)
        body = build_order_body(request, self.settings)
        try:
            response = self.client.create_order(access_token, body, idempotency_key)
            return self._complete_canonical_context(tenant_id, response, currency=request.currency)
        except MercadoPagoClientError as exc:
            if exc.uncertain:
                return uncertain_order(request)
            raise _domain_from_client(exc) from exc

    def get_order(self, tenant_id: UUID, order_id: str) -> MercadoPagoOrderResponse:
        access_token = self._access_token(tenant_id)
        try:
            response = self.client.get_order(access_token, order_id)
            return self._complete_canonical_context(tenant_id, response)
        except MercadoPagoClientError as exc:
            raise _domain_from_client(exc) from exc

    def cancel_order(
        self, tenant_id: UUID, order_id: str, idempotency_key: str
    ) -> MercadoPagoOrderResponse:
        access_token = self._access_token(tenant_id)
        try:
            response = self.client.cancel_order(access_token, order_id, idempotency_key)
            return self._complete_canonical_context(tenant_id, response)
        except MercadoPagoClientError as exc:
            if exc.uncertain:
                return MercadoPagoOrderResponse(id=order_id, status="unknown", uncertain=True)
            if _requires_terminal_intervention(exc):
                raise MercadoPagoDomainError(
                    "point_action_required",
                    "La operacion requiere intervencion en la terminal Point.",
                    409,
                ) from exc
            raise _domain_from_client(exc) from exc

    def refund_order(
        self, tenant_id: UUID, order_id: str, idempotency_key: str
    ) -> MercadoPagoOrderResponse:
        access_token = self._access_token(tenant_id)
        try:
            response = self.client.refund_order(access_token, order_id, idempotency_key)
            return self._complete_canonical_context(tenant_id, response)
        except MercadoPagoClientError as exc:
            if exc.uncertain:
                return MercadoPagoOrderResponse(id=order_id, status="unknown", uncertain=True)
            raise _domain_from_client(exc) from exc

    def _access_token(self, tenant_id: UUID) -> str:
        connection = self._get_connection(tenant_id, for_update=True)
        if (
            connection is None
            or connection.status != "active"
            or not connection.access_token_encrypted
            or not connection.credential_key_id
        ):
            raise MercadoPagoDomainError(
                "mercado_pago_not_connected", "El tenant no tiene Mercado Pago vinculado.", 409
            )
        refresh_at = utc_now() + timedelta(seconds=self.settings.mercado_pago_refresh_skew_seconds)
        if connection.token_expires_at is None or ensure_utc(connection.token_expires_at) > refresh_at:
            return self.cipher.decrypt(
                connection.access_token_encrypted, connection.credential_key_id
            )
        if not connection.refresh_token_encrypted:
            connection.status = "error"
            connection.last_error_code = "refresh_token_missing"
            self.db.commit()
            raise MercadoPagoDomainError(
                "refresh_token_missing", "La conexion debe vincularse nuevamente.", 409
            )
        refresh_token = self.cipher.decrypt(
            connection.refresh_token_encrypted, connection.credential_key_id
        )
        try:
            token = self.client.refresh_token(refresh_token)
        except MercadoPagoClientError as exc:
            connection.last_error_code = exc.code
            self.db.commit()
            raise _domain_from_client(exc) from exc
        self._apply_token(connection, token)
        connection.last_refresh_at = utc_now()
        self.db.commit()
        return self.cipher.decrypt(
            connection.access_token_encrypted or "", connection.credential_key_id or ""
        )

    def _save_connection(
        self, tenant_id: UUID, token: OAuthTokenData
    ) -> MercadoPagoConnectionResponse:
        connection = self._get_connection(tenant_id, for_update=True)
        if connection is None:
            connection = MercadoPagoConnection(tenant_id=str(tenant_id), scopes=[])
            self.db.add(connection)
        self._apply_token(connection, token)
        connection.status = "active"
        connection.disconnected_at = None
        connection.last_error_code = None
        self.db.commit()
        self.db.refresh(connection)
        return connection_response(connection)

    def _apply_token(self, connection: MercadoPagoConnection, token: OAuthTokenData) -> None:
        retained_refresh_token = None
        if (
            not token.refresh_token
            and connection.refresh_token_encrypted
            and connection.credential_key_id
        ):
            retained_refresh_token = self.cipher.decrypt(
                connection.refresh_token_encrypted,
                connection.credential_key_id,
            )
        connection.access_token_encrypted = self.cipher.encrypt(token.access_token)
        refresh_token = token.refresh_token or retained_refresh_token
        if refresh_token:
            connection.refresh_token_encrypted = self.cipher.encrypt(refresh_token)
        connection.credential_key_id = self.cipher.active_key_id
        connection.token_expires_at = utc_now() + timedelta(seconds=token.expires_in)
        if token.user_id is not None:
            connection.collector_id = _string_or_none(token.user_id)
        if token.scope is not None:
            connection.scopes = normalize_scopes(token.scope)
        if "live_mode" in token.model_fields_set:
            connection.live_mode = token.live_mode
        if token.application_id is not None or connection.application_id is None:
            connection.application_id = (
                _string_or_none(token.application_id)
                or self.settings.mercado_pago_application_id
            )
        connection.integration_id = self.settings.mercado_pago_integration_id

    def _get_connection(
        self, tenant_id: UUID, *, for_update: bool = False
    ) -> MercadoPagoConnection | None:
        statement = select(MercadoPagoConnection).where(
            MercadoPagoConnection.tenant_id == str(tenant_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    @staticmethod
    def _required_setting(value: str | None, label: str) -> str:
        if not value:
            raise MercadoPagoDomainError(
                "mercado_pago_not_configured",
                f"Mercado Pago {label} no esta configurado.",
                503,
            )
        return value

    def _complete_canonical_context(
        self,
        tenant_id: UUID,
        response: MercadoPagoOrderResponse,
        *,
        currency: str | None = None,
    ) -> MercadoPagoOrderResponse:
        connection = self._get_connection(tenant_id)
        if connection is None:
            return response
        return response.model_copy(
            update={
                "collector_id": response.collector_id or connection.collector_id,
                "live_mode": (
                    response.live_mode
                    if response.live_mode is not None
                    else connection.live_mode
                ),
                "currency": response.currency or currency,
            }
        )
def connection_response(connection: MercadoPagoConnection) -> MercadoPagoConnectionResponse:
    return MercadoPagoConnectionResponse(
        tenant_id=UUID(connection.tenant_id),
        connected=bool(connection.status == "active" and connection.access_token_encrypted),
        status=connection.status,
        collector_id=connection.collector_id,
        scopes=connection.scopes or [],
        live_mode=connection.live_mode,
        application_id=connection.application_id,
        integration_id=connection.integration_id,
        token_expires_at=connection.token_expires_at,
        last_refresh_at=connection.last_refresh_at,
        disconnected_at=connection.disconnected_at,
    )


def build_order_body(request: MercadoPagoOrderCreateRequest, settings: Settings) -> dict:
    amount = format(Decimal(request.amount), ".2f")
    body: dict = {
        "type": request.type,
        "external_reference": request.external_reference,
        "total_amount": amount,
        "transactions": {"payments": [{"amount": amount}]},
    }
    if request.description:
        body["description"] = request.description
    if request.expiration_time:
        body["expiration_time"] = request.expiration_time
    if request.items:
        body["items"] = request.items
    if request.type == "point":
        point = {
            "terminal_id": request.terminal_id,
            "print_on_terminal": request.print_on_terminal,
        }
        if request.ticket_number:
            point["ticket_number"] = request.ticket_number
        body["config"] = {"point": point}
    else:
        body["config"] = {
            "qr": {"external_pos_id": request.external_pos_id, "mode": "dynamic"}
        }
    integration_data = {}
    if settings.mercado_pago_platform_id:
        integration_data["platform_id"] = settings.mercado_pago_platform_id
    if settings.mercado_pago_integration_id:
        integration_data["integrator_id"] = settings.mercado_pago_integration_id
    if integration_data:
        body["integration_data"] = integration_data
    return body


def uncertain_order(request: MercadoPagoOrderCreateRequest) -> MercadoPagoOrderResponse:
    return MercadoPagoOrderResponse(
        type=request.type,
        status="unknown",
        external_reference=request.external_reference,
        currency=request.currency,
        total_amount=request.amount,
        payment_amount=request.amount,
        terminal_id=request.terminal_id,
        external_pos_id=request.external_pos_id,
        uncertain=True,
    )


def normalize_scopes(scope: str | list[str] | None) -> list[str]:
    if scope is None:
        return []
    if isinstance(scope, list):
        return sorted({str(value) for value in scope if str(value).strip()})
    return sorted({value for value in scope.replace(",", " ").split() if value})


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _domain_from_client(exc: MercadoPagoClientError) -> MercadoPagoDomainError:
    return MercadoPagoDomainError(exc.code, exc.message, exc.status_code)


def _requires_terminal_intervention(exc: MercadoPagoClientError) -> bool:
    if exc.status_code not in {400, 409, 422}:
        return False
    description = f"{exc.code} {exc.message}".lower()
    return any(
        marker in description
        for marker in ("terminal", "action_required", "action required", "intervention")
    )


def _string_or_none(value) -> str | None:
    return None if value is None else str(value)

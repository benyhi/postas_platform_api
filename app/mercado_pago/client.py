from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.mercado_pago.schemas import MercadoPagoOrderResponse, OAuthTokenData


class MercadoPagoClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.uncertain = uncertain


class MercadoPagoClient:
    """Thin Orders API client. Retries are deliberately owned by the caller."""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http_client or httpx.Client(
            base_url=self.settings.mercado_pago_api_base_url,
            timeout=httpx.Timeout(
                connect=self.settings.mercado_pago_connect_timeout_seconds,
                read=self.settings.mercado_pago_read_timeout_seconds,
                write=self.settings.mercado_pago_write_timeout_seconds,
                pool=self.settings.mercado_pago_connect_timeout_seconds,
            ),
        )

    def exchange_authorization_code(self, code: str, code_verifier: str) -> OAuthTokenData:
        return self._token(
            {
                "client_id": self._required_setting(self.settings.mercado_pago_client_id, "CLIENT_ID"),
                "client_secret": self._required_setting(
                    self.settings.mercado_pago_client_secret, "CLIENT_SECRET"
                ),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._required_setting(
                    self.settings.mercado_pago_redirect_uri, "REDIRECT_URI"
                ),
                "code_verifier": code_verifier,
            }
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokenData:
        return self._token(
            {
                "client_id": self._required_setting(self.settings.mercado_pago_client_id, "CLIENT_ID"),
                "client_secret": self._required_setting(
                    self.settings.mercado_pago_client_secret, "CLIENT_SECRET"
                ),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    def list_terminals(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/terminals/v1/list", access_token=access_token)
        return _extract_results(payload)

    def list_pos(self, access_token: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v2/pos", access_token=access_token)
        return _extract_results(payload)

    def create_order(
        self,
        access_token: str,
        body: dict[str, Any],
        idempotency_key: str,
    ) -> MercadoPagoOrderResponse:
        payload = self._request(
            "POST",
            "/v1/orders",
            access_token=access_token,
            json_body=body,
            idempotency_key=idempotency_key,
            mutable=True,
        )
        return normalize_order(payload)

    def get_order(self, access_token: str, order_id: str) -> MercadoPagoOrderResponse:
        payload = self._request("GET", f"/v1/orders/{order_id}", access_token=access_token)
        return normalize_order(payload)

    def cancel_order(
        self,
        access_token: str,
        order_id: str,
        idempotency_key: str,
    ) -> MercadoPagoOrderResponse:
        payload = self._request(
            "POST",
            f"/v1/orders/{order_id}/cancel",
            access_token=access_token,
            json_body={},
            idempotency_key=idempotency_key,
            mutable=True,
        )
        return normalize_order(payload)

    def refund_order(
        self,
        access_token: str,
        order_id: str,
        idempotency_key: str,
    ) -> MercadoPagoOrderResponse:
        payload = self._request(
            "POST",
            f"/v1/orders/{order_id}/refund",
            access_token=access_token,
            json_body={},
            idempotency_key=idempotency_key,
            mutable=True,
        )
        return normalize_order(payload)

    def _token(self, body: dict[str, Any]) -> OAuthTokenData:
        payload = self._request("POST", "/oauth/token", json_body=body)
        try:
            return OAuthTokenData.model_validate(payload)
        except ValueError as exc:
            raise MercadoPagoClientError(
                "invalid_oauth_response", "Mercado Pago devolvio una respuesta OAuth invalida."
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        mutable: bool = False,
    ) -> dict[str, Any]:
        if mutable and not idempotency_key:
            raise ValueError("Las operaciones mutables requieren X-Idempotency-Key")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        try:
            response = self.http.request(method, path, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise MercadoPagoClientError(
                "mercado_pago_timeout",
                "Mercado Pago no respondio dentro del tiempo configurado.",
                status_code=504,
                uncertain=mutable,
            ) from exc
        except httpx.RequestError as exc:
            raise MercadoPagoClientError(
                "mercado_pago_unavailable",
                "No fue posible conectar con Mercado Pago.",
                status_code=503,
                uncertain=mutable,
            ) from exc
        if response.is_error:
            code, message = _safe_remote_error(response)
            mapped_status = response.status_code if 400 <= response.status_code < 500 else 502
            raise MercadoPagoClientError(code, message, status_code=mapped_status)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MercadoPagoClientError(
                "invalid_mercado_pago_response", "Mercado Pago devolvio una respuesta invalida."
            ) from exc
        if not isinstance(payload, dict):
            raise MercadoPagoClientError(
                "invalid_mercado_pago_response", "Mercado Pago devolvio una respuesta invalida."
            )
        return payload

    @staticmethod
    def _required_setting(value: str | None, suffix: str) -> str:
        if not value:
            raise MercadoPagoClientError(
                "mercado_pago_not_configured",
                f"MERCADO_PAGO_{suffix} no esta configurado.",
                status_code=503,
            )
        return value


def normalize_order(payload: dict[str, Any]) -> MercadoPagoOrderResponse:
    transactions = payload.get("transactions") if isinstance(payload.get("transactions"), dict) else {}
    payments = transactions.get("payments") if isinstance(transactions.get("payments"), list) else []
    payment = payments[0] if payments and isinstance(payments[0], dict) else {}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    point = config.get("point") if isinstance(config.get("point"), dict) else {}
    qr = config.get("qr") if isinstance(config.get("qr"), dict) else {}
    return MercadoPagoOrderResponse(
        id=_string_or_none(payload.get("id")),
        type=_string_or_none(payload.get("type")),
        status=str(payload.get("status") or "unknown"),
        status_detail=_string_or_none(payload.get("status_detail")),
        external_reference=_string_or_none(payload.get("external_reference")),
        collector_id=_string_or_none(payload.get("user_id") or payload.get("collector_id")),
        currency=_string_or_none(
            payload.get("currency")
            or payload.get("currency_id")
            or payment.get("currency")
            or payment.get("currency_id")
        ),
        live_mode=payload.get("live_mode") if isinstance(payload.get("live_mode"), bool) else None,
        total_amount=_decimal_or_none(payload.get("total_amount") or payment.get("amount")),
        payment_id=_string_or_none(payment.get("id")),
        payment_amount=_decimal_or_none(payment.get("amount")),
        payment_status=_string_or_none(payment.get("status")),
        payment_status_detail=_string_or_none(payment.get("status_detail")),
        qr_data=_string_or_none(payload.get("qr_data") or qr.get("qr_data")),
        expires_at=_string_or_none(
            payload.get("expiration_date")
            or payload.get("expires_at")
            or payload.get("date_of_expiration")
        ),
        terminal_id=_string_or_none(point.get("terminal_id")),
        external_pos_id=_string_or_none(qr.get("external_pos_id")),
    )


def _extract_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("results", "terminals", "devices", "pos"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    for key in ("results", "terminals", "devices", "pos"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _safe_remote_error(response: httpx.Response) -> tuple[str, str]:
    code = "mercado_pago_error"
    message = "Mercado Pago rechazo la operacion."
    try:
        payload = response.json()
    except ValueError:
        return code, message
    if not isinstance(payload, dict):
        return code, message
    raw_code = payload.get("code") or payload.get("error")
    raw_message = payload.get("message") or payload.get("error_description")
    cause = payload.get("cause")
    if isinstance(cause, list) and cause and isinstance(cause[0], dict):
        raw_code = raw_code or cause[0].get("code")
        raw_message = raw_message or cause[0].get("description")
    if raw_code:
        code = str(raw_code)[:80]
    if raw_message:
        message = str(raw_message)[:300]
    return code, message


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

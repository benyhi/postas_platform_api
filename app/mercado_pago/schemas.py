from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class OAuthAuthorizationResponse(BaseModel):
    authorization_url: str
    expires_at: datetime


class MercadoPagoConnectionResponse(BaseModel):
    tenant_id: UUID
    connected: bool
    status: str
    collector_id: str | None = None
    scopes: list[str] = Field(default_factory=list)
    live_mode: bool = False
    application_id: str | None = None
    integration_id: str | None = None
    token_expires_at: datetime | None = None
    last_refresh_at: datetime | None = None
    disconnected_at: datetime | None = None


class MercadoPagoTerminalResponse(BaseModel):
    id: str
    status: str | None = None
    operating_mode: str | None = None
    pos_id: int | str | None = None
    store_id: int | str | None = None


class MercadoPagoPosResponse(BaseModel):
    id: int | str
    name: str | None = None
    external_id: str | None = None
    store_id: int | str | None = None
    external_store_id: str | None = None
    fixed_amount: bool | None = None
    category: int | str | None = None


class MercadoPagoTerminalListResponse(BaseModel):
    results: list[MercadoPagoTerminalResponse]


class MercadoPagoPosListResponse(BaseModel):
    results: list[MercadoPagoPosResponse]


class MercadoPagoOrderCreateRequest(BaseModel):
    type: Literal["point", "qr"]
    external_reference: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    amount: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    currency: Literal["ARS"] = "ARS"
    terminal_id: str | None = Field(default=None, min_length=1, max_length=160)
    external_pos_id: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=255)
    expiration_time: str | None = Field(default=None, max_length=40)
    print_on_terminal: Literal["seller_ticket", "no_ticket"] = "no_ticket"
    ticket_number: str | None = Field(default=None, max_length=64)
    items: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_destination(self) -> "MercadoPagoOrderCreateRequest":
        if self.type == "point" and not self.terminal_id:
            raise ValueError("terminal_id es requerido para orders point")
        if self.type == "qr" and not self.external_pos_id:
            raise ValueError("external_pos_id es requerido para orders qr")
        return self


class MercadoPagoOrderResponse(BaseModel):
    id: str | None = None
    type: str | None = None
    status: str
    status_detail: str | None = None
    external_reference: str | None = None
    collector_id: str | None = None
    currency: str | None = None
    live_mode: bool | None = None
    total_amount: Decimal | None = None
    payment_id: str | None = None
    payment_amount: Decimal | None = None
    payment_status: str | None = None
    payment_status_detail: str | None = None
    qr_data: str | None = None
    expires_at: str | None = None
    terminal_id: str | None = None
    external_pos_id: str | None = None
    uncertain: bool = False


class OAuthTokenData(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: int = Field(default=15_552_000, ge=1)
    user_id: int | str | None = None
    scope: str | list[str] | None = None
    live_mode: bool = False
    application_id: int | str | None = None

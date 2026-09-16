from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.billing.dependencies import InternalRequestContext, verify_internal_request
from app.db.session import get_db
from app.mercado_pago.crypto import (
    MercadoPagoCredentialConfigurationError,
    MercadoPagoCredentialDecryptionError,
    MercadoPagoCredentialValidationError,
)
from app.mercado_pago.schemas import (
    MercadoPagoConnectionResponse,
    MercadoPagoOrderCreateRequest,
    MercadoPagoOrderResponse,
    MercadoPagoPosListResponse,
    MercadoPagoTerminalListResponse,
    OAuthAuthorizationResponse,
)
from app.mercado_pago.service import MercadoPagoDomainError, MercadoPagoService


internal_router = APIRouter(
    prefix="/mercado-pago/tenants/{tenant_id}", tags=["mercado-pago-internal"]
)
public_router = APIRouter(prefix="/mercado-pago", tags=["mercado-pago-oauth"])


def get_mercado_pago_service(db: Session = Depends(get_db)) -> MercadoPagoService:
    return MercadoPagoService(db)


def _postas_api_only(
    context: InternalRequestContext = Depends(verify_internal_request),
) -> InternalRequestContext:
    if context.source != "postas_api":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Mercado Pago solo acepta llamadas internas de postas_api",
        )
    return context


def _call(callback):
    try:
        return callback()
    except MercadoPagoDomainError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except (
        MercadoPagoCredentialConfigurationError,
        MercadoPagoCredentialDecryptionError,
        MercadoPagoCredentialValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mercado_pago_keyring_unavailable", "message": str(exc)},
        ) from exc


@internal_router.post("/oauth/authorize", response_model=OAuthAuthorizationResponse)
def authorize(
    tenant_id: UUID,
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: service.start_authorization(tenant_id))


@public_router.get("/oauth/callback", response_model=MercadoPagoConnectionResponse)
def oauth_callback(
    state_value: str = Query(alias="state", min_length=16, max_length=512),
    code: str = Query(min_length=1, max_length=1024),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: service.complete_authorization(state_value, code))


@internal_router.get("/connection", response_model=MercadoPagoConnectionResponse)
def connection_status(
    tenant_id: UUID,
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: service.get_status(tenant_id))


@internal_router.delete("/connection", response_model=MercadoPagoConnectionResponse)
def unlink_connection(
    tenant_id: UUID,
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: service.unlink(tenant_id))


@internal_router.get("/terminals", response_model=MercadoPagoTerminalListResponse)
def list_terminals(
    tenant_id: UUID,
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: MercadoPagoTerminalListResponse(results=service.list_terminals(tenant_id)))


@internal_router.get("/pos", response_model=MercadoPagoPosListResponse)
def list_pos(
    tenant_id: UUID,
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: MercadoPagoPosListResponse(results=service.list_pos(tenant_id)))


@internal_router.post("/orders", response_model=MercadoPagoOrderResponse)
def create_order(
    tenant_id: UUID,
    payload: MercadoPagoOrderCreateRequest,
    response: Response,
    x_idempotency_key: str = Header(alias="X-Idempotency-Key", min_length=1, max_length=160),
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    result = _call(lambda: service.create_order(tenant_id, payload, x_idempotency_key))
    response.status_code = status.HTTP_202_ACCEPTED if result.uncertain else status.HTTP_201_CREATED
    return result


@internal_router.get("/orders/{order_id}", response_model=MercadoPagoOrderResponse)
def get_order(
    tenant_id: UUID,
    order_id: str,
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    return _call(lambda: service.get_order(tenant_id, order_id))


@internal_router.post("/orders/{order_id}/cancel", response_model=MercadoPagoOrderResponse)
def cancel_order(
    tenant_id: UUID,
    order_id: str,
    response: Response,
    x_idempotency_key: str = Header(alias="X-Idempotency-Key", min_length=1, max_length=160),
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    result = _call(lambda: service.cancel_order(tenant_id, order_id, x_idempotency_key))
    if result.uncertain:
        response.status_code = status.HTTP_202_ACCEPTED
    return result


@internal_router.post("/orders/{order_id}/refund", response_model=MercadoPagoOrderResponse)
def refund_order(
    tenant_id: UUID,
    order_id: str,
    response: Response,
    x_idempotency_key: str = Header(alias="X-Idempotency-Key", min_length=1, max_length=160),
    _: InternalRequestContext = Depends(_postas_api_only),
    service: MercadoPagoService = Depends(get_mercado_pago_service),
):
    result = _call(lambda: service.refund_order(tenant_id, order_id, x_idempotency_key))
    if result.uncertain:
        response.status_code = status.HTTP_202_ACCEPTED
    return result

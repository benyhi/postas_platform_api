from fastapi import FastAPI

from app.api.routes import router
from app.billing.router_internal import router as billing_internal_router
from app.arca.router_internal import router as arca_internal_router
from app.core.config import get_settings
from app.mercado_pago.router import internal_router as mercado_pago_internal_router
from app.mercado_pago.router import public_router as mercado_pago_public_router


def create_app() -> FastAPI:
    settings = get_settings()
    fastapi_app = FastAPI(title=settings.app_name, version="0.3.0")
    fastapi_app.include_router(router, prefix=settings.api_prefix)
    fastapi_app.include_router(billing_internal_router, prefix="/internal/v1")
    fastapi_app.include_router(arca_internal_router, prefix="/internal/v1")
    fastapi_app.include_router(mercado_pago_internal_router, prefix="/internal/v1")
    fastapi_app.include_router(mercado_pago_public_router, prefix=settings.api_prefix)
    return fastapi_app


app = create_app()

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing.models import Feature, Plan, PlanFeature


FEATURE_CATALOG: list[dict[str, str | None]] = [
    {"key": "pos_sales", "name": "Ventas POS", "description": "Registro de ventas en punto de venta.", "type": "monthly_usage"},
    {"key": "sales_history", "name": "Historial de ventas", "description": "Consulta de ventas historicas.", "type": "boolean"},
    {"key": "products", "name": "Productos", "description": "Cantidad maxima de productos activos.", "type": "resource_limit"},
    {
        "key": "cashboxes",
        "name": "Cajas",
        "description": "Cantidad maxima de cajas abiertas simultaneamente.",
        "type": "resource_limit",
    },
    {"key": "suppliers", "name": "Proveedores", "description": "Cantidad maxima de proveedores.", "type": "resource_limit"},
    {"key": "users", "name": "Usuarios", "description": "Cantidad maxima de usuarios.", "type": "resource_limit"},
    {"key": "basic_reports", "name": "Reportes basicos", "description": None, "type": "boolean"},
    {"key": "document_extraction", "name": "Extraccion de documentos", "description": None, "type": "monthly_usage"},
    {"key": "advanced_reports", "name": "Reportes avanzados", "description": None, "type": "boolean"},
    {"key": "import_products", "name": "Importar productos", "description": None, "type": "boolean"},
    {"key": "export_products", "name": "Exportar productos", "description": None, "type": "boolean"},
    {"key": "priority_support", "name": "Soporte prioritario", "description": None, "type": "boolean"},
    {"key": "cashbox_email_report", "name": "Reporte de caja por email", "description": None, "type": "monthly_usage"},
    {"key": "arca_invoicing", "name": "Facturacion ARCA", "description": "Emision fiscal electronica multi-tenant.", "type": "boolean"},
]


PLAN_CATALOG: list[dict[str, Any]] = [
    {
        "code": "free",
        "name": "Free",
        "price_amount": Decimal("0.00"),
        "is_public": True,
        "features": {
            "pos_sales": (True, 20, "monthly"),
            "sales_history": (True, None, None),
            "products": (True, 20, None),
            "cashboxes": (True, 1, None),
            "suppliers": (True, 3, None),
            "users": (True, 3, None),
            "basic_reports": (False, None, None),
            "document_extraction": (False, None, None),
            "advanced_reports": (False, None, None),
            "import_products": (False, None, None),
            "export_products": (False, None, None),
            "priority_support": (False, None, None),
            "cashbox_email_report": (False, None, None),
        },
    },
    {
        "code": "starter",
        "name": "Starter",
        "price_amount": Decimal("39999.00"),
        "is_public": True,
        "features": {
            "pos_sales": (True, 100, "monthly"),
            "sales_history": (True, None, "monthly"),
            "products": (True, 100, None),
            "cashboxes": (True, 3, None),
            "suppliers": (True, 5, None),
            "users": (True, 5, None),
            "basic_reports": (True, None, None),
            "document_extraction": (False, None, None),
            "advanced_reports": (False, None, None),
            "import_products": (False, None, None),
            "export_products": (False, None, None),
            "priority_support": (False, None, None),
            "cashbox_email_report": (False, None, None),
        },
    },
    {
        "code": "business",
        "name": "Business",
        "price_amount": Decimal("59999.00"),
        "is_public": True,
        "features": {
            "pos_sales": (True, None, "monthly"),
            "sales_history": (True, None, "monthly"),
            "products": (True, None, None),
            "cashboxes": (True, 5, None),
            "suppliers": (True, None, None),
            "users": (True, None, None),
            "basic_reports": (True, None, None),
            "document_extraction": (False, None, None),
            "advanced_reports": (True, None, None),
            "import_products": (True, None, None),
            "export_products": (True, None, None),
            "priority_support": (False, None, None),
            "cashbox_email_report": (True, 100, "monthly"),
            "arca_invoicing": (True, None, None),
        },
    },
    {
        "code": "business_ai",
        "name": "Business + IA",
        "price_amount": Decimal("79999.00"),
        "is_public": True,
        "features": {
            "pos_sales": (True, None, "monthly"),
            "sales_history": (True, None, "monthly"),
            "products": (True, None, None),
            "cashboxes": (True, None, None),
            "suppliers": (True, None, None),
            "users": (True, None, None),
            "basic_reports": (True, None, None),
            "document_extraction": (True, 100, "monthly"),
            "advanced_reports": (True, None, None),
            "import_products": (True, None, None),
            "export_products": (True, None, None),
            "priority_support": (True, None, None),
            "cashbox_email_report": (True, 100, "monthly"),
            "arca_invoicing": (True, None, None),
        },
    },
    {
        "code": "custom",
        "name": "Custom",
        "price_amount": Decimal("49999.00"),
        "is_public": False,
        "features": {
            **{feature["key"]: (True, None, None) for feature in FEATURE_CATALOG},
            "products": (True, 100, None),
            "pos_sales": (True, 1000, "monthly"),
            "cashboxes": (True, 2, None),
            "suppliers": (True, 10, None),
            "users": (True, 3, None),
            "cashbox_email_report": (True, 30, "monthly"),
            "document_extraction": (False, None, None),
        },
    },
    {
        "code": "test",
        "name": "Demo 1",
        "price_amount": Decimal("0.00"),
        "is_public": False,
        "features": {
            "pos_sales": (True, 10, "monthly"),
            "sales_history": (True, None, None),
            "products": (True, 10, None),
            "cashboxes": (True, 1, None),
            "suppliers": (True, 2, None),
            "users": (True, 3, None),
            "basic_reports": (True, None, None),
            "document_extraction": (False, None, None),
            "advanced_reports": (True, None, None),
            "import_products": (True, None, None),
            "export_products": (True, None, None),
            "priority_support": (True, None, None),
            "cashbox_email_report": (True, 2, "monthly"),
            "arca_invoicing": (True, None, None),
        },
    },
]

for demo_number in (2, 3):
    PLAN_CATALOG.append({
        **PLAN_CATALOG[-1], "code": f"demo_{demo_number}", "name": f"Demo {demo_number}",
        "features": dict(PLAN_CATALOG[-1]["features"]),
    })


@dataclass(frozen=True)
class BillingSeedResult:
    plans: int
    features: int
    plan_features: int


def seed_billing_catalog(db: Session, *, commit: bool = True) -> BillingSeedResult:
    features_by_key: dict[str, Feature] = {}
    for feature_data in FEATURE_CATALOG:
        feature = db.scalar(select(Feature).where(Feature.key == feature_data["key"]))
        if feature is None:
            feature = Feature(
                key=str(feature_data["key"]),
                name=str(feature_data["name"]),
                description=feature_data["description"],
                type=str(feature_data["type"]),
            )
            db.add(feature)
        else:
            feature.name = str(feature_data["name"])
            feature.description = feature_data["description"]
            feature.type = str(feature_data["type"])
        features_by_key[feature.key] = feature

    db.flush()

    plan_feature_count = 0
    for plan_data in PLAN_CATALOG:
        plan = db.scalar(select(Plan).where(Plan.code == plan_data["code"]))
        if plan is None:
            plan = Plan(code=plan_data["code"])
            db.add(plan)
        plan.name = plan_data["name"]
        plan.price_amount = plan_data["price_amount"]
        plan.currency = "ARS"
        plan.billing_interval = "monthly"
        plan.is_active = True
        plan.is_public = bool(plan_data["is_public"])
        db.flush()

        for feature_key, config in plan_data["features"].items():
            enabled, limit_value, reset_period = config
            feature = features_by_key[feature_key]
            plan_feature = db.scalar(
                select(PlanFeature).where(
                    PlanFeature.plan_id == plan.id,
                    PlanFeature.feature_id == feature.id,
                )
            )
            if plan_feature is None:
                plan_feature = PlanFeature(plan_id=plan.id, feature_id=feature.id)
                db.add(plan_feature)
            plan_feature.enabled = bool(enabled)
            plan_feature.limit_value = limit_value
            plan_feature.reset_period = reset_period
            plan_feature.hard_limit = True
            plan_feature_count += 1

    if commit:
        db.commit()
    else:
        db.flush()
    return BillingSeedResult(
        plans=len(PLAN_CATALOG),
        features=len(FEATURE_CATALOG),
        plan_features=plan_feature_count,
    )

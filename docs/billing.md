# Billing & Plans

`postas_platform_api` es la fuente de verdad para planes, features, limites,
suscripciones, pagos y consumo mensual. En esta primera etapa no hay integracion
con MercadoPago, frontend ni cliente dentro de `postas_api`.

## Base de datos

Configurar `DATABASE_URL`. Por defecto se usa SQLite local:

```bash
sqlite:///./postas_platform.db
```

Aplicar migraciones:

```bash
alembic upgrade head
```

## Seed inicial

El catalogo de planes y features se carga de forma idempotente:

```bash
python scripts/seed_billing.py
```

El seed actualiza planes, features y configuraciones `PlanFeature` existentes
sin duplicar registros.

La feature existente `cashboxes` representa la cantidad maxima de cajas que
pueden permanecer abiertas simultaneamente. Para validar el limite, el cliente
debe enviar en `resource_count` la cantidad proyectada de cajas abiertas.

| Plan | Maximo de cajas abiertas simultaneamente |
| --- | ---: |
| `free` | 1 |
| `starter` | 3 |
| `business` | 5 |
| `business_ai` | Sin limite |
| `custom` | 2 |
| `test` | 1 |
| `demo_2` | 1 |
| `demo_3` | 1 |

El despliegue debe ejecutar `python scripts/seed_billing.py` antes de habilitar
la apertura multi-terminal en Postas. El cierre de sesiones existentes no
consulta este entitlement: un downgrade puede impedir nuevas aperturas, pero no
debe impedir el cierre operativo.

El codigo `test` se conserva por compatibilidad y su nombre visible es Demo 1.
Demo 1, Demo 2 (`demo_2`) y Demo 3 (`demo_3`) son privados y gratuitos: habilitan
todas las features salvo `document_extraction`, con 10 productos, 10 ventas/mes,
1 caja abierta, 2 proveedores, 3 usuarios y 2 reportes por email/mes.

Custom es privado, cuesta ARS 49.999/mes y habilita todas las features salvo
`document_extraction`: 100 productos, 1.000 ventas/mes, 2 cajas abiertas,
10 proveedores, 3 usuarios y 30 reportes por email/mes.

Para el dataset coordinado con Postas usar su lanzador `scripts/seed_demo.py`;
la contraparte local de Platform recibe el manifiesto compartido. Ver README.
Los cambios de catalogo afectan las suscripciones existentes al ejecutar el seed.

## Tenants locales por plan

Para generar datos locales de prueba, uno por cada plan activo:

```bash
python scripts/seed_plan_tenants.py
```

El script primero sincroniza el catalogo de billing. Luego reutiliza la primera
suscripcion activa existente por plan y crea suscripciones `active` para los
planes faltantes. Los nuevos tenants usan UUIDs consecutivos al mayor tenant ya
presente en billing. Tambien crea un pago manual aprobado cuando la
suscripcion no tiene pago asociado.

En esta API no hay una tabla `tenant`; para billing, un tenant de prueba queda
representado por su `tenant_id` en `billing_tenant_subscriptions` y
`billing_payments`.

## Seguridad interna

Los endpoints internos viven bajo `/internal/v1`.

Headers:

- `X-Postas-Source`
- `X-Postas-Service-Token`

Variables:

- `POSTAS_SERVICE_TOKEN`
- `ALLOWED_REQUEST_SOURCES`, separado por coma.

Si `POSTAS_SERVICE_TOKEN` esta configurado, el token debe coincidir. Si
`ALLOWED_REQUEST_SOURCES` esta configurado, `X-Postas-Source` debe estar en la
lista. Los fallos devuelven `403`.

## Endpoints

- `GET /internal/v1/tenants/{tenant_id}/status`
- `POST /internal/v1/entitlements/check`
- `POST /internal/v1/usage/consume`
- `POST /internal/v1/usage/check-and-consume`

Si un tenant no tiene suscripcion, el status endpoint devuelve `404` con
`detail: "subscription_not_found"`. Los checks devuelven `allowed: false` y
`reason: "subscription_not_found"`.

## Ejemplos curl

```bash
curl http://localhost:8001/internal/v1/tenants/00000000-0000-0000-0000-000000000001/status \
  -H "X-Postas-Source: postas_api" \
  -H "X-Postas-Service-Token: local-service-token"
```

```bash
curl -X POST http://localhost:8001/internal/v1/entitlements/check \
  -H "Content-Type: application/json" \
  -H "X-Postas-Source: postas_api" \
  -H "X-Postas-Service-Token: local-service-token" \
  -d '{
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "feature_key": "document_extraction",
    "amount": 1,
    "resource_count": null,
    "context": {"source": "document_extraction"}
  }'
```

```bash
curl -X POST http://localhost:8001/internal/v1/usage/consume \
  -H "Content-Type: application/json" \
  -H "X-Postas-Source: postas_ai_api" \
  -H "X-Postas-Service-Token: local-service-token" \
  -d '{
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "feature_key": "document_extraction",
    "amount": 1,
    "external_id": "document_extraction_uuid",
    "idempotency_key": "document-extraction:document_extraction_uuid",
    "metadata": {"provider": "google_genai", "model": "gemini-2.5-flash-lite"}
  }'
```

```bash
curl -X POST http://localhost:8001/internal/v1/usage/check-and-consume \
  -H "Content-Type: application/json" \
  -H "X-Postas-Source: postas_api" \
  -H "X-Postas-Service-Token: local-service-token" \
  -d '{
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "feature_key": "document_extraction",
    "amount": 1,
    "external_id": "document_extraction_uuid",
    "idempotency_key": "document-extraction:document_extraction_uuid"
  }'
```

## TODOs

- Agregar endpoints admin cuando exista autenticacion/roles admin.
- Crear cliente interno en `postas_api` para consultar permisos y registrar
  consumos.

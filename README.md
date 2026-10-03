# Postas Platform API

API de plataforma para el backoffice SaaS de Postas. En esta etapa el servicio
empieza a ser la fuente de verdad para billing: planes, features, limites,
suscripciones, pagos y consumo mensual por tenant.

La API legacy de procesamiento IA sigue disponible durante la transicion, pero
el nuevo modulo de plataforma vive en `/internal/v1`.

## Endpoints principales

- `GET /api/v1/health`
- `GET /internal/v1/tenants/{tenant_id}/status`
- `POST /internal/v1/entitlements/check`
- `POST /internal/v1/usage/consume`
- `POST /internal/v1/usage/check-and-consume`

Endpoints legacy de IA:

- `POST /api/v1/document-extractions/process`
- `POST /api/v1/invoices/extract`

## Configuracion

Crear `.env` desde `.env.example`:

```bash
copy .env.example .env
```

Variables principales:

- `APP_NAME`
- `API_PREFIX`
- `DATABASE_URL`
- `POSTAS_SERVICE_TOKEN`
- `ALLOWED_REQUEST_SOURCES`
- `POSTAS_INTERNAL_REQUIRE_TLS` (por defecto `true`)
- `ARCA_CREDENTIAL_MASTER_KEYS` (objeto JSON `key_id -> Fernet key`, sin fallback)
- `ARCA_CREDENTIAL_ACTIVE_KEY_ID`
- `ARCA_PRODUCTION_CALLS_ENABLED` (por defecto `false`)
- `ARCA_TIMEOUT_SECONDS`
- `ARCA_WORKER_POLL_SECONDS`
- `ARCA_WORKER_BATCH_SIZE`

Para la API legacy de IA tambien siguen disponibles:

- `POSTAS_AI_API_TOKEN`
- `REQUIRE_API_TOKEN`
- `GOOGLE_API_KEY`
- `AI_PROVIDER`
- `GOOGLE_MODEL`

## Base de datos

Por defecto se usa SQLite local:

```bash
sqlite:///./postas_platform.db
```

Aplicar migraciones:

```bash
.\env\Scripts\python.exe -m alembic upgrade head
```

## Seed de billing

El catalogo inicial de planes y features es idempotente:

```bash
.\env\Scripts\python.exe scripts\seed_billing.py
```

El codigo `test` queda incluido como Demo 1, junto a Demo 2 y Demo 3.
Son privados y gratuitos, habilitan todas las features salvo extraccion por IA
y usan los limites reducidos detallados en la seccion de datos demo.

La feature `cashboxes` limita sesiones de caja abiertas simultaneamente por
tenant: Free `1`, Starter `3`, Business `5`, Business + IA sin limite,
Custom `2` y Demo 1/2/3 `1`. Demo 1 conserva el codigo tecnico `test`.
Ejecutar este seed antes de desplegar la apertura multi-terminal.

### Dataset de demostracion anual

El lanzador `scripts/seed_demo.py` de `postas_api` coordina ambas bases mediante
un manifiesto reproducible. Este repositorio ofrece las fases locales:

```powershell
& D:\benja\proyectos\postas_platform_api\env\Scripts\python.exe D:\benja\proyectos\postas_platform_api\scripts\seed_demo.py --manifest <manifest.json> --phase preflight --dry-run
# Fases siguientes: subscriptions, usage, verify (en ese orden; Postas genera usage_events).
```

Usar la configuracion de base de Platform y ejecutar sus migraciones antes.
El script no crea tablas, no llama proveedores y no envia pagos ni correos.
Cada fase de escritura tiene una transaccion; no hay atomicidad entre bases.
Repetir el mismo manifiesto recupera fases incompletas sin duplicar consumos.
UUID con datos ajenos o fingerprints diferentes se rechazan sin adopcion.
Los contadores preservan actividad posterior; verificar reconcilia el total
contra todos los eventos registrados. No existe borrado/reset automatico.

Cada tenant lleva un evento tecnico `__seed_manifest__` de cantidad cero,
sin contador, con fingerprint y referencia a su suscripcion. Esto permite
identificar tambien planes gratuitos sin inventar pagos. Los planes pagos
tienen mensualidades manuales aprobadas desde el mes inicial al actual;
Free y Demo no generan pagos. Los consumos se agrupan por mes UTC.

El catalogo mantiene Free, Starter, Business y Business + IA. Custom es privado,
cuesta ARS 49.999 y limita productos/ventas/cajas/proveedores/usuarios/emails a
100/1000/2/10/3/30. Demo 1 (`test`), Demo 2 (`demo_2`) y Demo 3 (`demo_3`) son
privados y gratuitos, con limites 10/10/1/2/3/2. Custom y Demo habilitan las
features booleanas y deshabilitan extraccion documental por IA.

Para crear tenants locales de prueba, uno por cada plan activo, ejecutar:

```bash
.\env\Scripts\python.exe scripts\seed_plan_tenants.py
```

El script reutiliza cualquier suscripcion activa existente para un plan y crea
los planes faltantes con UUIDs consecutivos al mayor tenant ya presente.

Documentacion del modulo:

- [docs/billing.md](docs/billing.md)
- [docs/arca_invoicing_service.txt](docs/arca_invoicing_service.txt)

## Facturacion ARCA

Los perfiles fiscales y las facturas viven bajo
`/internal/v1/arca/tenants/{tenant_id}`. Todas las llamadas requieren HTTPS,
`X-Postas-Source: postas_api` y `X-Postas-Service-Token`.

Contratos internos agregados para la API publica de Postas:

- `POST /sales-points`: valida entitlement y credenciales en memoria, consulta `getSalesPoints()` y devuelve solo puntos habilitados. No persiste ni devuelve secretos.
- `GET /invoices?offset=0&limit=20`: lista facturas ordenadas por `created_at DESC, id DESC`, estrictamente filtradas por tenant (`limit` maximo 100).

Las facturas vinculadas a una venta conservan `sale_id` como relacion canonica.
Las restricciones unicas `(tenant_id, sale_id)` y `(tenant_id, external_id)`
impiden emitir dos solicitudes distintas para la misma venta; los payloads con
`sale_id` requieren un perfil fiscal con concepto Productos.

El worker fiscal es un proceso separado:

```bash
.\env\Scripts\python.exe -m app.arca.worker
```

El worker requiere PostgreSQL; no inicia sobre SQLite porque ese dialecto no
ofrece las garantias de `SKIP LOCKED` y advisory locks usadas por la secuencia.

`docker compose up --build` inicia tambien `arca_worker`. La reserva del numero
se persiste antes de llamar a ARCA; ante timeout o reinicio, el worker consulta
primero ese mismo comprobante y adopta un CAE existente.

No habilita produccion por tener un perfil productivo: la instancia tambien
debe configurar `ARCA_PRODUCTION_CALLS_ENABLED=true`.

## Mercado Pago Point y QR

Platform concentra OAuth, cifrado de tokens, refresh y llamadas a la Orders
API. Los endpoints internos `/internal/v1/mercado-pago/tenants/{tenant_id}` solo
aceptan llamadas de `postas_api`; el callback publico es
`/api/v1/mercado-pago/oauth/callback`. Las operaciones mutables requieren
`X-Idempotency-Key` y no se reintentan automaticamente.

Las reservas de `pos_sales` se exponen en
`/internal/v1/billing/reservations/{reserve,commit,release}`. Reservar ocupa
capacidad, pero el consumo mensual solo aumenta al confirmar.

Configuracion, contratos y rotacion del keyring:

- [docs/mercado_pago_point_qr.txt](docs/mercado_pago_point_qr.txt)

## Preproduccion conjunta

La orquestacion productiva-simulada de ambos backends vive en el repositorio
hermano `postas_api`, archivo `docker-compose.preprod.yml`. Ese stack usa una
base exclusiva para Platform, ejecuta Alembic y el seed como job one-shot, no
monta el codigo fuente y no publica `/internal/v1`.

Las credenciales son de desarrollo y se cargan desde `postas_api/preprod.env`,
que no debe versionarse. El Compose local de este repositorio se conserva para
trabajar Platform de forma aislada.

## Ejecutar local

Con Docker:

```bash
docker compose up --build
```

Con entorno local:

```bash
.\env\Scripts\python.exe -m pip install -r requirements.txt
.\env\Scripts\python.exe -m alembic upgrade head
.\env\Scripts\python.exe scripts\seed_billing.py
.\env\Scripts\python.exe -m uvicorn main:app --reload --port 8001
```

## Tests

Instalar las dependencias de desarrollo antes de ejecutar la suite:

```bash
.\env\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```bash
.\env\Scripts\python.exe -m pytest tests/test_billing.py -q
.\env\Scripts\python.exe -m pytest tests/test_arca.py -q
```

`tests/test_arca_live.py` es opt-in, rechaza `production` y solo consulta WSFE
en homologacion; las variables necesarias estan documentadas en
`docs/arca_invoicing_service.txt`.
## Hardening de seguridad

- `REQUIRE_API_TOKEN` usa `true` por defecto. Solo debe desactivarse explicitamente en un entorno local aislado.
- La descarga remota de imagenes solo acepta HTTP/HTTPS en puertos 80/443, desactiva redirects automaticos y proxies de entorno, revalida cada redirect y rechaza destinos no publicos.
- Las respuestas crudas de ARCA se sanitizan antes de persistirse; tokens, secretos, credenciales, certificados y claves privadas se redactan.

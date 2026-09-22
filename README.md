# Dashboard Canela

Dashboard cliente Tienda Canela servido por FastAPI.

## Rutas

- `/client-dashboard`
- `/api/client-dashboard/meta`
- `/api/client-dashboard/creators`
- `/api/client-dashboard/business`
- `/api/tiendanube/oauth/callback`

## Railway

Root directory: `backend`

Start command:

```bash
python -m uvicorn server:app --host 0.0.0.0 --port $PORT
```

Variables mínimas:

- `MONGO_URL`
- `DB_NAME`
- `TIENDANUBE_CLIENT_ID`
- `TIENDANUBE_CLIENT_SECRET`
- Meta API settings/tokens según el entorno de Rumbo.

Variables de acceso del dashboard público:

- `CLIENT_DASHBOARD_USERNAME`
- `CLIENT_DASHBOARD_PASSWORD_HASH` (PBKDF2; nunca guardar la contraseña en texto plano)
- `CLIENT_DASHBOARD_SESSION_SECRET` (mínimo 32 caracteres aleatorios)
- `CLIENT_DASHBOARD_ALLOWED_ORIGINS` (opcional; por defecto usa el dominio Railway)

El deployment público queda limitado a las rutas del dashboard de Tienda Canela.
Las rutas administrativas, tokens, Slack, documentación OpenAPI y cuentas de otros
clientes no son accesibles desde este servicio.

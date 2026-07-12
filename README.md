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

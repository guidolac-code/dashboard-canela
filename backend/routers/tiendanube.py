from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from tiendanube_orders import TiendanubeConfigError, fetch_demo_orders, get_tiendanube_oauth_config

router = APIRouter(prefix="/api/tiendanube", tags=["tiendanube"])
db: Optional[AsyncIOMotorDatabase] = None


def configure_tiendanube_router(database: AsyncIOMotorDatabase) -> None:
    global db
    db = database


def _require_db() -> AsyncIOMotorDatabase:
    if db is None:
        raise HTTPException(status_code=500, detail="Tiendanube database no configurada.")
    return db


@router.get("/demo/orders")
async def get_demo_orders(
    per_page: int = Query(30, ge=1, le=200),
    max_pages: int = Query(10, ge=1, le=100),
    raw: bool = False,
    status: Optional[str] = None,
    created_at_min: Optional[str] = None,
    created_at_max: Optional[str] = None,
):
    """Pull Orders from the Tiendanube demo store without persisting them."""
    try:
        result = await fetch_demo_orders(
            per_page=per_page,
            max_pages=max_pages,
            params={
                "status": status,
                "created_at_min": created_at_min,
                "created_at_max": created_at_max,
            },
        )
    except TiendanubeConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Tiendanube orders pull failed: {exc}") from exc

    if not raw:
        result.pop("raw_orders", None)
    return result


@router.get("/oauth/callback")
async def tiendanube_oauth_callback(code: str = Query(...), state: Optional[str] = None):
    """Exchange Tiendanube OAuth code and persist a multi-store connection."""
    database = _require_db()
    try:
        config = get_tiendanube_oauth_config()
    except TiendanubeConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = {
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            config["token_url"],
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    if response.status_code >= 400:
        detail = "Tiendanube rechazó el intercambio OAuth."
        try:
            body = response.json()
            detail = body.get("error") or body.get("message") or body.get("description") or detail
        except ValueError:
            pass
        raise HTTPException(status_code=502, detail=detail)

    token_data = response.json()
    access_token = token_data.get("access_token")
    store_id = token_data.get("user_id") or token_data.get("store_id")
    if not access_token or not store_id:
        raise HTTPException(status_code=502, detail="Tiendanube no devolvió access_token/store_id.")

    now = datetime.now(timezone.utc)
    existing = await database.tiendanube_connections.find_one({"store_id": str(store_id)}, {"client_name": 1})
    document = {
        "store_id": str(store_id),
        "access_token": access_token,
        "token_type": token_data.get("token_type"),
        "scope": token_data.get("scope"),
        "client_name": (existing or {}).get("client_name"),
        "connected_at": now,
        "updated_at": now,
        "oauth_state": state,
        "source": "oauth_callback",
    }
    await database.tiendanube_connections.update_one(
        {"store_id": str(store_id)},
        {
            "$set": document,
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    return {
        "status": "connected",
        "store_id": str(store_id),
        "scope": token_data.get("scope"),
        "client_name": document["client_name"],
    }

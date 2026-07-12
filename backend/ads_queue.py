"""Notion-backed queue for creating Meta ads as PAUSED drafts only."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, HTTPException

from google_integration_service import get_credentials, get_drive_service
from slack_utils import send_slack_message
from uploader import (
    META_API_BASE,
    _meta_error_msg,
    _upload_image,
    _upload_video,
    get_access_token,
)

logger = logging.getLogger(__name__)

CLIENTS_DATA_SOURCE_ID = os.environ.get(
    "NOTION_CLIENTS_DATA_SOURCE_ID", "f0958cbb-102b-4b67-a427-8da5705ef034"
)
ADS_QUEUE_DATA_SOURCE_ID = os.environ.get(
    "NOTION_ADS_QUEUE_DATA_SOURCE_ID", "a7dc99b3-299f-4430-850f-2a38d73e89fd"
)
NOTION_VERSION = "2025-09-03"
ERRORS_SLACK_CHANNEL = os.environ.get("SLACK_ERRORS_CHANNEL", "#errores")

ads_queue_router = APIRouter(prefix="/api/ads-queue", tags=["ads-queue"])
db = None


def set_db(database):
    global db
    db = database


class QueueError(RuntimeError):
    pass


def _plain(prop: dict[str, Any]) -> Any:
    kind = prop.get("type")
    if kind in {"title", "rich_text"}:
        return "".join(part.get("plain_text", "") for part in prop.get(kind, []))
    if kind == "select":
        return (prop.get("select") or {}).get("name", "")
    if kind == "url":
        return prop.get("url") or ""
    if kind == "number":
        return prop.get("number")
    return ""


def _page_values(page: dict[str, Any]) -> dict[str, Any]:
    values = {name: _plain(prop) for name, prop in page.get("properties", {}).items()}
    values["_page_id"] = page.get("id", "")
    return values


class NotionQueueClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("NOTION_TOKEN", "")
        if not self.token:
            raise QueueError("NOTION_TOKEN no está configurado")

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method, f"https://api.notion.com/v1{path}", headers=headers, **kwargs
            )
        if response.status_code >= 400:
            message = response.json().get("message", response.text[:200])
            raise QueueError(f"Notion API: {message}")
        return response.json()

    async def _query_all(self, data_source_id: str, body: Optional[dict] = None) -> list[dict]:
        body = dict(body or {})
        results: list[dict] = []
        while True:
            payload = await self._request(
                "POST", f"/data_sources/{data_source_id}/query", json=body
            )
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                return results
            body["start_cursor"] = payload.get("next_cursor")

    async def ready_rows(self) -> list[dict[str, Any]]:
        pages = await self._query_all(
            ADS_QUEUE_DATA_SOURCE_ID,
            {"filter": {"property": "Status", "select": {"equals": "Listo para subir"}}},
        )
        return [_page_values(page) for page in pages]

    async def clients(self) -> list[dict[str, Any]]:
        return [_page_values(page) for page in await self._query_all(CLIENTS_DATA_SOURCE_ID)]

    async def client(self, name: str) -> dict[str, Any]:
        wanted = name.strip().casefold()
        matches = [row for row in await self.clients() if row.get("Cliente", "").strip().casefold() == wanted]
        if len(matches) != 1:
            raise QueueError(f"Cliente '{name}' no existe o está duplicado en Notion")
        if matches[0].get("Status") != "Activo":
            raise QueueError(f"Cliente '{name}' no está Activo en Notion")
        return matches[0]

    async def mark_uploaded(self, page_id: str, ad_id: str) -> None:
        await self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={
                "properties": {
                    "Meta Ad ID": {"rich_text": [{"text": {"content": ad_id}}]},
                    "Status": {"select": {"name": "Subido borrador"}},
                }
            },
        )


def extract_drive_file_id(url: str) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"/d/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return parse_qs(urlparse(url).query).get("id", [None])[0]


async def download_asset(url: str) -> tuple[bytes, str, str]:
    file_id = extract_drive_file_id(url)
    if file_id:
        creds = get_credentials()
        if not creds:
            raise QueueError("No hay credenciales de Google para descargar el asset")

        def _download():
            service = get_drive_service(creds)
            metadata = service.files().get(fileId=file_id, fields="name,mimeType").execute()
            content = service.files().get_media(fileId=file_id).execute()
            return content, metadata.get("name", file_id), metadata.get("mimeType", "")

        try:
            return await asyncio.to_thread(_download)
        except Exception as exc:
            raise QueueError(f"No se pudo descargar el asset de Drive: {exc}") from exc

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise QueueError(f"No se pudo descargar el asset: HTTP {response.status_code}")
    filename = urlparse(str(response.url)).path.rsplit("/", 1)[-1] or "asset"
    return response.content, filename, response.headers.get("content-type", "")


def _destination_url(client: dict[str, Any]) -> str:
    notes = client.get("Notas", "")
    urls = re.findall(r"https?://[^\s|]+", notes)
    for url in urls:
        if "drive.google.com" not in url:
            return url.rstrip(".,;)")
    raise QueueError("Falta una URL de destino en Notas del cliente")


def build_static_asset_feed(
    square_hash: str,
    vertical_hash: str,
    primary_text: str,
    headline: str,
    description: str,
    destination_url: str,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "images": [
            {"hash": square_hash, "adlabels": [{"name": "feed_asset"}]},
            {"hash": vertical_hash, "adlabels": [{"name": "story_asset"}]},
        ],
        "bodies": [{"text": primary_text or " ", "adlabels": [{"name": "copy"}]}],
        "titles": [{"text": headline or " ", "adlabels": [{"name": "copy"}]}],
        "link_urls": [{"website_url": destination_url, "adlabels": [{"name": "copy"}]}],
        "call_to_action_types": ["SHOP_NOW"],
        "ad_formats": ["SINGLE_IMAGE"],
        "asset_customization_rules": [
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram", "messenger"],
                    "facebook_positions": ["feed", "marketplace", "right_hand_column"],
                    "instagram_positions": ["stream"],
                    "messenger_positions": ["messenger_home"],
                },
                "image_label": {"name": "feed_asset"},
                "body_label": {"name": "copy"},
                "title_label": {"name": "copy"},
                "link_url_label": {"name": "copy"},
                "priority": 1,
            },
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram", "messenger"],
                    "facebook_positions": ["story", "facebook_reels"],
                    "instagram_positions": ["story", "reels"],
                    "messenger_positions": ["story"],
                },
                "image_label": {"name": "story_asset"},
                "body_label": {"name": "copy"},
                "title_label": {"name": "copy"},
                "link_url_label": {"name": "copy"},
                "priority": 2,
            },
        ],
    }
    if description:
        spec["descriptions"] = [{"text": description}]
    return spec


async def _meta_get(token: str, path: str, params: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{META_API_BASE}/{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise QueueError(_meta_error_msg(response))
    return response.json()


async def _meta_post(token: str, path: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{META_API_BASE}/{path}",
            data=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise QueueError(_meta_error_msg(response))
    return response.json()


async def _resolve_named_object(token: str, parent: str, edge: str, value: str) -> dict:
    fields = "id,name,status,effective_status,objective" if edge == "campaigns" else "id,name,status,effective_status"
    payload = await _meta_get(
        token,
        f"{parent}/{edge}",
        {"fields": fields, "limit": 500},
    )
    wanted = value.strip().casefold()
    matches = [
        item
        for item in payload.get("data", [])
        if item.get("id") == value or item.get("name", "").strip().casefold() == wanted
    ]
    if len(matches) != 1:
        raise QueueError(f"No se pudo resolver {edge}: '{value}'")
    return matches[0]


async def _resolve_ig_media(token: str, ig_business_id: str, permalink: str) -> str:
    target = permalink.split("?", 1)[0].rstrip("/")
    path = f"{ig_business_id}/media"
    params = {"fields": "id,permalink", "limit": 100}
    while True:
        payload = await _meta_get(token, path, params)
        for media in payload.get("data", []):
            if media.get("permalink", "").split("?", 1)[0].rstrip("/") == target:
                return media["id"]
        after = payload.get("paging", {}).get("cursors", {}).get("after")
        if not after:
            break
        params["after"] = after
    raise QueueError("El post de Instagram no pertenece al IG Business configurado")


async def _resolve_pixel(token: str, ad_account_id: str) -> str:
    payload = await _meta_get(token, f"{ad_account_id}/adspixels", {"fields": "id,name", "limit": 50})
    pixels = payload.get("data", [])
    if not pixels:
        raise QueueError("La cuenta no tiene un pixel disponible")
    return pixels[0]["id"]


async def _create_paused_adset(
    token: str,
    ad_account_id: str,
    campaign: dict,
    row: dict[str, Any],
) -> str:
    budget = row.get("Presupuesto diario")
    if not budget or float(budget) <= 0:
        raise QueueError("Presupuesto diario debe ser mayor a cero")
    optimization = row.get("Optimización")
    if optimization not in {"Valor", "Compras"}:
        raise QueueError("Optimización debe ser Valor o Compras")
    pixel_id = await _resolve_pixel(token, ad_account_id)
    params = {
        "campaign_id": campaign["id"],
        "name": row.get("Nombre nuevo adset", "").strip(),
        "daily_budget": str(int(float(budget) * 100)),
        "billing_event": "IMPRESSIONS",
        "optimization_goal": "VALUE" if optimization == "Valor" else "OFFSITE_CONVERSIONS",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "targeting": json.dumps({"geo_locations": {"countries": ["AR"]}}),
        "promoted_object": json.dumps({"pixel_id": pixel_id, "custom_event_type": "PURCHASE"}),
        "status": "PAUSED",
    }
    if not params["name"]:
        raise QueueError("Falta Nombre nuevo adset")
    return (await _meta_post(token, f"{ad_account_id}/adsets", params))["id"]


async def _create_asset_creative(
    token: str,
    ad_account_id: str,
    row: dict[str, Any],
    client: dict[str, Any],
) -> str:
    page_id = client.get("Page ID", "").strip()
    ig_id = client.get("IG Business ID", "").strip()
    if not page_id:
        raise QueueError("Falta Page ID en Clientes")
    destination = _destination_url(client)
    oss: dict[str, Any] = {"page_id": page_id}
    if ig_id:
        oss["instagram_user_id"] = ig_id

    creative_type = row.get("Tipo de creative")
    if creative_type == "Estático":
        if not row.get("Asset 1:1") or not row.get("Asset 9:16"):
            raise QueueError("El estático requiere Asset 1:1 y Asset 9:16")
        square, vertical = await asyncio.gather(
            download_asset(row["Asset 1:1"]), download_asset(row["Asset 9:16"])
        )
        square_hash, vertical_hash = await asyncio.gather(
            _upload_image(token, ad_account_id, square[0], square[1]),
            _upload_image(token, ad_account_id, vertical[0], vertical[1]),
        )
        creative = {
            "object_story_spec": oss,
            "asset_feed_spec": build_static_asset_feed(
                square_hash,
                vertical_hash,
                row.get("Copy primary text", ""),
                row.get("Headline", ""),
                row.get("Descripción", ""),
                destination,
            ),
        }
    elif creative_type == "Video":
        if not row.get("Asset 9:16"):
            raise QueueError("El video requiere Asset 9:16")
        content, filename, _ = await download_asset(row["Asset 9:16"])
        video_id, thumbnail = await _upload_video(token, ad_account_id, content, filename)
        video_data = {
            "video_id": video_id,
            "message": row.get("Copy primary text", ""),
            "title": row.get("Headline", ""),
            "link_description": row.get("Descripción", ""),
            "call_to_action": {"type": "SHOP_NOW", "value": {"link": destination}},
        }
        if thumbnail:
            video_data["image_url"] = thumbnail
        creative = {"object_story_spec": {**oss, "video_data": video_data}}
    else:
        raise QueueError("Tipo de creative inválido")

    result = await _meta_post(
        token,
        f"{ad_account_id}/adcreatives",
        {"name": row.get("Nombre del ad", "Creative"), **{k: json.dumps(v) for k, v in creative.items()}},
    )
    return result["id"]


async def _create_instagram_creative(
    token: str,
    ad_account_id: str,
    row: dict[str, Any],
    client: dict[str, Any],
) -> str:
    ig_id = client.get("IG Business ID", "").strip()
    if not ig_id:
        raise QueueError("Falta IG Business ID en Clientes")
    permalink = row.get("Link del post de IG", "").strip()
    if not permalink:
        raise QueueError("Falta Link del post de IG")
    media_id = await _resolve_ig_media(token, ig_id, permalink)
    result = await _meta_post(
        token,
        f"{ad_account_id}/adcreatives",
        {"name": row.get("Nombre del ad", "Creative IG"), "source_instagram_media_id": media_id},
    )
    return result["id"]


@dataclass
class ProcessResult:
    page_id: str
    name: str
    status: str
    ad_id: Optional[str] = None
    error: Optional[str] = None


async def process_row(
    row: dict[str, Any], notion: NotionQueueClient, *, notify_errors: bool = True
) -> ProcessResult:
    page_id = row.get("_page_id", "")
    name = row.get("Nombre del ad", "Sin nombre")
    try:
        if row.get("Meta Ad ID"):
            raise QueueError("La fila ya tiene Meta Ad ID; no se duplica")
        client = await notion.client(row.get("Cliente", ""))
        ad_account_id = client.get("Ad Account ID", "").strip()
        if not ad_account_id:
            raise QueueError("Falta Ad Account ID en Clientes")
        token = await get_access_token(ad_account_id)
        campaign = await _resolve_named_object(
            token, ad_account_id, "campaigns", row.get("Campaña destino", "")
        )
        if campaign.get("effective_status", campaign.get("status")) != "ACTIVE":
            raise QueueError("La campaña destino no está activa")

        if row.get("Tipo de Adset") == "Existente":
            adset = await _resolve_named_object(token, campaign["id"], "adsets", row.get("Adset", ""))
            if adset.get("effective_status", adset.get("status")) != "ACTIVE":
                raise QueueError("El adset existente no está activo")
            adset_id = adset["id"]
        elif row.get("Tipo de Adset") == "Nuevo":
            adset_id = await _create_paused_adset(token, ad_account_id, campaign, row)
        else:
            raise QueueError("Tipo de Adset inválido")

        origin = row.get("Origen del creative")
        if origin == "Asset propio":
            creative_id = await _create_asset_creative(token, ad_account_id, row, client)
        elif origin == "Post de Instagram":
            creative_id = await _create_instagram_creative(token, ad_account_id, row, client)
        else:
            raise QueueError("Origen del creative inválido")

        ad_id = (
            await _meta_post(
                token,
                f"{ad_account_id}/ads",
                {
                    "name": name,
                    "adset_id": adset_id,
                    "creative": json.dumps({"creative_id": creative_id}),
                    "status": "PAUSED",
                },
            )
        )["id"]
        await notion.mark_uploaded(page_id, ad_id)
        return ProcessResult(page_id, name, "Subido borrador", ad_id=ad_id)
    except Exception as exc:
        message = str(exc)[:500]
        logger.error("Ads Queue '%s': %s", name, message)
        if notify_errors and db is not None:
            await send_slack_message(
                db,
                text=f"❌ Ads Queue — {name}: {message}",
                channel=ERRORS_SLACK_CHANNEL,
            )
        return ProcessResult(page_id, name, "Error", error=message)


@ads_queue_router.post("/process")
async def process_ready_queue():
    try:
        notion = NotionQueueClient()
        rows = await notion.ready_rows()
    except QueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    results = [await process_row(row, notion) for row in rows]
    return {"processed": len(results), "results": [result.__dict__ for result in results]}


@ads_queue_router.get("/status")
async def ads_queue_status():
    return {
        "notion_configured": bool(os.environ.get("NOTION_TOKEN")),
        "clients_data_source_id": CLIENTS_DATA_SOURCE_ID,
        "ads_queue_data_source_id": ADS_QUEUE_DATA_SOURCE_ID,
        "meta_mode": "PAUSED_ONLY",
    }

"""
LacUploader — Backend endpoints for ad creation workflow.
Handles: campaign/adset/pixel listing, copy generation, ad creation with SSE streaming.
"""
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Optional
import httpx
import os
import json
import logging
import uuid
from datetime import datetime, timezone
from sse_starlette.sse import EventSourceResponse
import asyncio

# from google_integration_service import process_google_integration_async

logger = logging.getLogger(__name__)

META_API_VERSION = "v22.0"
META_API_BASE = f"https://graph.facebook.com/{META_API_VERSION}"

uploader_router = APIRouter(prefix="/api/uploader", tags=["uploader"])

# Official Meta API v22+ creative_features_spec field names.
# Source: https://developers.facebook.com/docs/marketing-api/reference/ad-creative-features-spec/
# This list is now stored in MongoDB (collection: config, _id: advantage_features)
# and seeded here as the authoritative default on first startup.
# To update the list without code changes, edit the MongoDB document directly.
_DEFAULT_META_FEATURES = [
    "adapt_to_placement",
    "add_text_overlay",
    "ads_with_benefits",
    "biz_ai",
    "creative_stickers",
    "customize_product_recommendation",
    "description_automation",
    "fb_feed_tag",
    "fb_reels_tag",
    "fb_story_tag",
    "generate_cta",
    "hide_price",
    "ig_feed_tag",
    "ig_reels_tag",
    "ig_stream_tag",
    "image_animation",
    "image_background_gen",
    "image_templates",
    "image_touchups",
    "inline_comment",
    "local_store_extension",
    "media_order",
    "media_type_automation",
    "multi_photo_to_video",
    "music_generation",
    "pac_relaxation",
    "product_extensions",
    "profile_card",
    "profile_extension",
    "replace_media_text",
    "reveal_details_over_time",
    "show_destination_blurbs",
    "show_summary",
    "site_extensions",
    "standard_enhancements",
    "standard_enhancements_catalog",
    "text_extraction_for_headline",
    "text_extraction_for_tap_target",
    "text_optimizations",
    "text_overlay_translation",
    "text_translation",
    "translate_voiceover",
    "video_auto_crop",           # Controls 'Retoques de video' in Meta UI
    "video_highlights",
    "video_to_image",
    "wa_mm_image_filtering",
    "wa_mm_text_truncation_length",
]

# Features confirmadas que aceptan {"enroll_status": "OPT_OUT"}.
# Lista conservadora — solo las que Meta acepta con certeza en v22+.
# Para agregar/quitar sin deploy, editar safe_opt_out_features en MongoDB.
_SAFE_OPT_OUT_FEATURES = [
    "adapt_to_placement",
    "add_text_overlay",
    "description_automation",
    "generate_cta",
    "image_background_gen",
    "image_touchups",
    "inline_comment",
    "media_type_automation",
    "music_generation",
    "product_extensions",
    "reveal_details_over_time",
    "show_destination_blurbs",
    "show_summary",
    "text_optimizations",
    "text_translation",
    "video_auto_crop",           # Controls 'Retoques de video' in Meta UI
    "video_highlights",
]

async def get_safe_opt_out_features() -> list:
    """Returns the subset of features safe to send as OPT_OUT.
    Reads from MongoDB; falls back to _SAFE_OPT_OUT_FEATURES."""
    if db is None:
        return list(_SAFE_OPT_OUT_FEATURES)
    doc = await db.config.find_one({"_id": "advantage_features"})
    if doc and doc.get("safe_opt_out_features"):
        return list(doc["safe_opt_out_features"])
    return list(_SAFE_OPT_OUT_FEATURES)

async def get_advantage_features() -> list:
    """Read the current list of Meta creative_features_spec field names from MongoDB.
    Falls back to the hardcoded default if the DB document doesn't exist yet."""
    if db is None:
        return list(_DEFAULT_META_FEATURES)
    doc = await db.config.find_one({"_id": "advantage_features"})
    if doc and doc.get("features"):
        return list(doc["features"])
    return list(_DEFAULT_META_FEATURES)

async def init_advantage_features():
    """Seed/update the advantage_features config document in MongoDB on startup.
    - 'features' uses $setOnInsert (full reference list, safe to edit manually)
    - 'safe_opt_out_features' uses $set (always updated with the code's current safe list)"""
    if db is None:
        return
    # Upsert: insert full doc if not exists, always update safe list
    await db.config.update_one(
        {"_id": "advantage_features"},
        {
            "$setOnInsert": {
                "features": _DEFAULT_META_FEATURES,
                "note": "Edit 'safe_opt_out_features' to control which fields are OPT_OUT in ad creation."
            },
            "$set": {
                "safe_opt_out_features": _SAFE_OPT_OUT_FEATURES,
                "updated_at": __import__('datetime').datetime.utcnow().isoformat(),
            }
        },
        upsert=True,
    )
    logger.info(f"[advantage_features] Config ready: {len(_DEFAULT_META_FEATURES)} features, {len(_SAFE_OPT_OUT_FEATURES)} safe for OPT_OUT.")

# Will be set from server.py
db = None

def set_db(database):
    global db
    db = database


# ─── Models ────────────────────────────────────────

class CopyRequest(BaseModel):
    context: str  # free-form: URL, description, offer, tone, etc.
    creative_name: str = ""
    account_id: str = ""
    files_count: Optional[int] = None  # if set, generate varied copy per creative


class AdCopyItem(BaseModel):
    headline: str = ""
    primary_text: str = ""
    destination_url: str = ""


class CopyResponse(BaseModel):
    headline: str = ""
    primary_text: str = ""
    copies: Optional[List[AdCopyItem]] = None  # multi-copy mode


async def _meta_api_call(method: str, url: str, **kwargs):
    """Specific helper for Meta API calls with BUG 1 retry logic.
    Retries on code 2 ('unexpected error') with exponential backoff.
    """
    max_retries = 3
    for attempt in range(max_retries):
        async with httpx.AsyncClient() as client:
            try:
                if method.upper() == "POST":
                    resp = await client.post(url, **kwargs)
                else:
                    resp = await client.get(url, **kwargs)

                if resp.status_code == 200:
                    return resp

                # BUG 1 retry logic: only on code 2 + specific messages
                try:
                    err_body = resp.json().get("error", {})
                    code = err_body.get("code")
                    msg = err_body.get("message", "").lower()
                    if code == 2 and ("unexpected error" in msg or "retry your request later" in msg):
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1) # 2s, 4s, 8s
                            logger.warning(f"[meta retry] Code 2 error, retrying in {wait}s... (attempt {attempt+1}/{max_retries})")
                            await asyncio.sleep(wait)
                            continue
                except:
                    pass

                # If not retryable or max retries reached, raise exception
                raise Exception(_meta_error_msg(resp))

            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                raise e
    raise Exception("Max retries reached")


# ─── Helper: extract full Meta error message ──────

def _meta_error_msg(resp) -> str:
    """Extract the most useful error message from a Meta API response.
    Prefers error_user_message (human-readable), falls back to message, then raw text."""
    try:
        body = resp.json()
        err = body.get("error", {})
        # error_user_message is Meta's plain-language explanation
        user_msg = err.get("error_user_message") or err.get("error_user_title")
        api_msg  = err.get("message", "")
        code     = err.get("code", "")
        subcode  = err.get("error_subcode", "")
        fb_trace = err.get("fbtrace_id", "")

        parts = []
        if code:
            label = f"({code})" if not subcode else f"({code}/{subcode})"
            parts.append(label)
        if api_msg:
            parts.append(api_msg)
        if user_msg and user_msg != api_msg:
            parts.append(f"→ {user_msg}")
        if fb_trace:
            parts.append(f"[trace:{fb_trace}]")
        return " ".join(parts) if parts else resp.text
    except Exception:
        return resp.text


# ─── Helper: get access token ─────────────────────

async def get_access_token(account_id: str = None):
    """Get Meta Access Token. Checks per-account token first, then DB global, then env var."""
    if db is not None:
        # 1. Per-account token (highest priority)
        if account_id:
            account = await db.accounts.find_one({"id": account_id}, {"_id": 0, "meta_token": 1})
            if account and account.get("meta_token"):
                return account["meta_token"]
        # 2. Global DB token
        settings = await db.settings.find_one({"_id": "meta_config"})
        if settings and settings.get("access_token"):
            return settings["access_token"]
    # 3. Env var fallback
    token = os.environ.get("META_ACCESS_TOKEN", "")
    if not token:
        raise HTTPException(status_code=500, detail="META_ACCESS_TOKEN not configured")
    return token


# ─── GET /campaigns/:account_id ───────────────────

@uploader_router.get("/campaigns/{account_id}")
async def list_campaigns(account_id: str):
    """List active campaigns for an account from Meta API."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    meta_account_id = account["meta_account_id"]
    access_token = await get_access_token()

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_API_BASE}/{meta_account_id}/campaigns",
            params={
                "access_token": access_token,
                "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget",
                "limit": 200,
                "filtering": json.dumps([{"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]}])
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_meta_error_msg(resp))

        data = resp.json().get("data", [])

    return [
        {
            "id": c["id"],
            "name": c["name"],
            "status": c.get("effective_status", c.get("status", "UNKNOWN")),
            "objective": c.get("objective", ""),
            # budget_optimization: present → CBO (budget on campaign), absent → ABO
            "budget_optimization": c.get("daily_budget") is not None or c.get("lifetime_budget") is not None,
        }
        for c in data
    ]


# ─── GET /adsets/:account_id ──────────────────────

@uploader_router.get("/adsets/{account_id}")
async def list_adsets(account_id: str, campaign_id: str = None):
    """List adsets for an account, optionally filtered by campaign."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    meta_account_id = account["meta_account_id"]
    access_token = await get_access_token()

    params = {
        "access_token": access_token,
        "fields": "id,name,status,effective_status,daily_budget,campaign_id",
        "limit": 200,
    }

    # If campaign_id provided, fetch adsets for that campaign
    endpoint = f"{META_API_BASE}/{campaign_id}/adsets" if campaign_id else f"{META_API_BASE}/{meta_account_id}/adsets"

    if not campaign_id:
        params["filtering"] = json.dumps([{"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]}])

    async with httpx.AsyncClient() as client:
        resp = await client.get(endpoint, params=params, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_meta_error_msg(resp))

        data = resp.json().get("data", [])

    return [
        {
            "id": a["id"],
            "name": a["name"],
            "status": a.get("effective_status", a.get("status", "UNKNOWN")),
            "daily_budget": a.get("daily_budget"),
        }
        for a in data
    ]


# ─── GET /pixels/:account_id ─────────────────────

@uploader_router.get("/pixels/{account_id}")
async def list_pixels(account_id: str):
    """List available pixels for an account."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    meta_account_id = account["meta_account_id"]
    access_token = await get_access_token()

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_API_BASE}/{meta_account_id}/adspixels",
            params={
                "access_token": access_token,
                "fields": "id,name",
                "limit": 50,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_meta_error_msg(resp))

        data = resp.json().get("data", [])

    return [{"id": p["id"], "name": p["name"]} for p in data]


# ─── GET /conversion-events/:account_id ──────────────────────────────────────

@uploader_router.get("/conversion-events/{account_id}")
async def list_conversion_events(account_id: str, pixel_id: str = None):
    """List standard conversion events available for a pixel.
    Returns both standard events plus any custom conversions created in the account.
    """
    # Standard Meta conversion events (always shown as fallback)
    STANDARD_EVENTS = [
        {"value": "PURCHASE",              "label": "Compra"},
        {"value": "ADD_TO_CART",           "label": "Agregar al carrito"},
        {"value": "INITIATED_CHECKOUT",    "label": "Iniciar pago"},
        {"value": "ADD_PAYMENT_INFO",      "label": "Agregar info de pago"},
        {"value": "LEAD",                  "label": "Lead"},
        {"value": "COMPLETE_REGISTRATION", "label": "Registro completado"},
        {"value": "CONTENT_VIEW",          "label": "Ver contenido"},
        {"value": "START_TRIAL",           "label": "Iniciar prueba"},
        {"value": "SEARCH",                "label": "Búsqueda"},
        {"value": "CONTACT",               "label": "Contacto"},
        {"value": "SUBSCRIBE",             "label": "Suscripción"},
    ]

    if not pixel_id:
        return STANDARD_EVENTS

    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        return STANDARD_EVENTS

    access_token = await get_access_token()

    try:
        async with httpx.AsyncClient() as client:
            # Try to get custom conversions from the pixel stats endpoint
            resp = await client.get(
                f"{META_API_BASE}/{pixel_id}/stats",
                params={
                    "access_token": access_token,
                    "fields": "id,event_name,count",
                    "since": 0,
                    "until": 9999999999,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                events_data = resp.json().get("data", [])
                # Filter to events with activity, merge with standard list
                active_events = {e.get("event_name") for e in events_data if e.get("count", 0) > 0}
                
                if active_events:
                    # Build list: active pixel events first, then remaining standard ones
                    result = []
                    seen = set()
                    for std in STANDARD_EVENTS:
                        if std["value"] in active_events:
                            result.append({**std, "active": True})
                            seen.add(std["value"])
                    # Add non-standard active events
                    for ev in sorted(active_events):
                        if ev not in seen:
                            result.append({"value": ev, "label": ev.replace("_", " ").title(), "active": True})
                    # Add inactive standard events at the bottom
                    for std in STANDARD_EVENTS:
                        if std["value"] not in seen:
                            result.append(std)
                    return result
    except Exception:
        pass

    return STANDARD_EVENTS


@uploader_router.get("/debug-ig/{account_id}")
async def debug_ig(account_id: str):
    """Diagnostic: returns raw Meta API responses to debug IG account linking."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    meta_account_id = account["meta_account_id"]
    access_token = await get_access_token(account_id)
    result = {"meta_account_id": meta_account_id}

    async with httpx.AsyncClient() as client:
        # 1. Token permissions
        try:
            r = await client.get(f"{META_API_BASE}/me/permissions",
                params={"access_token": access_token}, timeout=10)
            result["permissions"] = r.json()
        except Exception as e:
            result["permissions"] = {"error": str(e)}

        # 2. IG accounts from ad account edge
        try:
            r = await client.get(f"{META_API_BASE}/{meta_account_id}/instagram_accounts",
                params={"access_token": access_token, "fields": "id,name,username", "limit": 10}, timeout=10)
            result["ad_account_instagram_accounts"] = r.json()
        except Exception as e:
            result["ad_account_instagram_accounts"] = {"error": str(e)}

        # 3. promote_pages + IG accounts from each page
        try:
            r_pages = await client.get(f"{META_API_BASE}/{meta_account_id}/promote_pages",
                params={"access_token": access_token, "fields": "id,name,instagram_business_account"}, timeout=10)
            result["promote_pages"] = r_pages.json()
            for page in r_pages.json().get("data", []):
                pid = page["id"]
                try:
                    r_ig = await client.get(f"{META_API_BASE}/{pid}/instagram_accounts",
                        params={"access_token": access_token, "fields": "id,username,name"}, timeout=10)
                    result[f"page_{pid}_instagram_accounts"] = r_ig.json()
                except Exception as e:
                    result[f"page_{pid}_instagram_accounts"] = {"error": str(e)}
        except Exception as e:
            result["promote_pages"] = {"error": str(e)}

        # 4. connected_instagram_account on ad account
        try:
            r = await client.get(f"{META_API_BASE}/{meta_account_id}/connected_instagram_account",
                params={"access_token": access_token, "fields": "id,username"}, timeout=10)
            result["connected_instagram_account"] = r.json()
        except Exception as e:
            result["connected_instagram_account"] = {"error": str(e)}

    return result


@uploader_router.get("/pages/{account_id}")
async def list_pages(account_id: str):
    """List Facebook Pages linked to an ad account.
    Tries multiple strategies in order, gracefully falling back on permission errors.
    Never hits /{page_id} directly — requires Page Public Content Access feature.
    """
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    meta_account_id = account["meta_account_id"]
    access_token = await get_access_token(account_id)  # use account-specific token
    account_name = account.get("name", "")

    async with httpx.AsyncClient() as client:
        # Strategy 0: /{ad_account}/instagram_accounts — the IG accounts that
        # Business Manager shared with this ad account. Most reliable source.
        ig_actor_from_bm = ""
        try:
            r0 = await client.get(
                f"{META_API_BASE}/{meta_account_id}/instagram_accounts",
                params={"access_token": access_token, "fields": "id,name,username", "limit": 10},
                timeout=15,
            )
            logger.info(f"[pages] S0 instagram_accounts → {r0.status_code}: {r0.text[:300]}")
            if r0.status_code == 200:
                ig_accounts = r0.json().get("data", [])
                if ig_accounts:
                    ig_actor_from_bm = ig_accounts[0]["id"]
                    logger.info(f"[pages] S0 found IG actor from BM: {ig_actor_from_bm} ({ig_accounts[0].get('username', '')})")
        except Exception as e:
            logger.info(f"[pages] S0 failed: {e}")

        # Strategy 1: promote_pages — specific to the ad account
        try:
            r1 = await client.get(
                f"{META_API_BASE}/{meta_account_id}/promote_pages",
                params={"access_token": access_token, "fields": "id,name,instagram_business_account", "limit": 50},
                timeout=15,
            )
            if r1.status_code == 200:
                raw = r1.json().get("data", [])
                if raw:
                    pages_s1 = [
                        {
                            "id": p["id"],
                            "name": p["name"],
                            "picture": "",
                            "instagram_user_id": ig_actor_from_bm,
                            "instagram_business_account_id": p.get("instagram_business_account", {}).get("id", "")
                        }
                        for p in raw
                    ]
                    if any(p["instagram_business_account_id"] or p["instagram_user_id"] for p in pages_s1):
                        logger.info(f"[pages] S1 returning w/ IG: business={[p['instagram_business_account_id'] for p in pages_s1]} user={[p['instagram_user_id'] for p in pages_s1]}")
                        return pages_s1
                    logger.info("[pages] S1 promote_pages: all IG accounts empty — trying S2")
        except Exception:
            pass

        # Strategy 2 (was 3): Extract from existing ad creatives
        try:
            r2 = await client.get(
                f"{META_API_BASE}/{meta_account_id}/adcreatives",
                params={"access_token": access_token, "fields": "object_story_spec", "limit": 50},
                timeout=30,
            )
            if r2.status_code == 200:
                page_map = {}  # page_id -> {ig_id, actor_id}
                for cr in r2.json().get("data", []):
                    oss = cr.get("object_story_spec", {})
                    pid = oss.get("page_id", "")
                    ig_id = oss.get("instagram_user_id", "")
                    actor_id = oss.get("instagram_actor_id", "")
                    if pid:
                        if pid not in page_map:
                            page_map[pid] = {"ig_id": ig_id, "actor_id": actor_id}
                        elif actor_id and not page_map[pid]["actor_id"]:
                            # Update if we find an actor_id in another creative
                            page_map[pid]["actor_id"] = actor_id

                if page_map:
                    results = []
                    for pid, info in page_map.items():
                        actor_id = info["actor_id"]
                        if not actor_id:
                            # Fallback: try to get IG business account from the page
                            try:
                                ig_resp = await client.get(f"{META_API_BASE}/{pid}", params={"access_token": access_token, "fields": "instagram_business_account"})
                                raw_ig = ig_resp.json() if ig_resp.status_code == 200 else {}
                                logger.info(f"[pages] S2 GET /{pid}?fields=instagram_business_account → {raw_ig}")
                                if ig_resp.status_code == 200:
                                    actor_id = raw_ig.get("instagram_business_account", {}).get("id", "")
                            except: pass
                        # Use actor_id from creative, or ig_id as last fallback
                        final_ig_id = actor_id or info["ig_id"]
                        results.append({
                            "id": pid,
                            "name": account_name or f"Page {pid}",
                            "picture": "",
                            "instagram_user_id": info["ig_id"],
                            "instagram_business_account_id": final_ig_id
                        })
                    return results
        except Exception:
            pass

        # Strategy 3 (was 1): /me/accounts — Last resort, filter by account name
        try:
            r3 = await client.get(
                f"{META_API_BASE}/me/accounts",
                params={"access_token": access_token, "fields": "id,name,instagram_business_account", "limit": 100},
                timeout=15,
            )
            if r3.status_code == 200:
                data = r3.json().get("data", [])
                if data:
                    # Filter by name similarity if possible to reduce noise
                    items = [
                        {
                            "id": p["id"],
                            "name": p["name"],
                            "picture": "",
                            "instagram_user_id": "",
                            "instagram_business_account_id": p.get("instagram_business_account", {}).get("id", "")
                        }
                        for p in data
                    ]
                    if account_name:
                        import re
                        an_clean = re.sub(r'[^a-zA-Z0-9]', '', account_name.lower())
                        filtered = [
                            p for p in items 
                            if an_clean in re.sub(r'[^a-zA-Z0-9]', '', p["name"].lower()) or \
                               re.sub(r'[^a-zA-Z0-9]', '', p["name"].lower()) in an_clean
                        ]
                        if filtered:
                            return filtered
                    
                    return items
        except Exception:
            pass

    return []

    return []




# ─── POST /generate-copy ─────────────────────────

@uploader_router.post("/generate-copy")
async def generate_copy(req: CopyRequest):
    """Generate ad copy using Claude API. Supports multi-copy mode via files_count."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    # Try to get custom prompt template from settings
    settings = await db.uploader_settings.find_one({}, {"_id": 0}) if db is not None else None
    prompt_template = (settings or {}).get("prompt_template", "")

    multi = req.files_count and req.files_count > 1

    if multi:
        prompt = f"""Sos un copywriter experto en Meta Ads para e-commerce de moda/lifestyle.

Contexto: {req.context}

Generá {req.files_count} variaciones de copy para anuncios de Meta Ads, cada una con un ángulo/hook distinto.

Respondé ÚNICAMENTE en JSON con este formato (array de {req.files_count} elementos):
[{{"headline": "...", "primary_text": "..."}}, ...]

Reglas:
- headline: máximo 40 caracteres, gancho emocional directo
- primary_text: máximo 240 caracteres, persuasivo, con CTA claro
- Cada elemento debe tener un ángulo diferente (ej: precio, beneficio, urgencia, social proof, etc.)

NO incluyas explicaciones, solo el JSON array."""
    else:
        if not prompt_template:
            prompt_template = """Sos un copywriter experto en Meta Ads para e-commerce de moda/lifestyle.

Contexto: {context}

Generá un copy para un anuncio de Meta Ads:
1. **headline**: Máximo 40 caracteres. Directo, con gancho emocional.
2. **primary_text**: Máximo 240 caracteres. Persuasivo, con CTA claro.

Respondé ÚNICAMENTE en JSON con este formato:
{{"headline": "...", "primary_text": "..."}}

NO incluyas explicaciones, solo el JSON."""
        prompt = prompt_template.format(
            context=req.context,
            creative_name=req.creative_name,
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300 * (req.files_count or 1),
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )

        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Claude API error: {resp.text[:200]}")

        result = resp.json()
        text = result.get("content", [{}])[0].get("text", "")

    import re
    try:
        if multi:
            # Expect a JSON array
            arr_match = re.search(r'\[.*\]', text, re.DOTALL)
            arr = json.loads(arr_match.group() if arr_match else text)
            copies = [
                AdCopyItem(
                    headline=item.get("headline", "")[:40],
                    primary_text=item.get("primary_text", "")[:240],
                )
                for item in arr
            ]
            return CopyResponse(copies=copies)
        else:
            json_match = re.search(r'\{[^}]+\}', text)
            parsed = json.loads(json_match.group() if json_match else text)
            return CopyResponse(
                headline=parsed.get("headline", "")[:40],
                primary_text=parsed.get("primary_text", "")[:240],
            )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise HTTPException(status_code=502, detail=f"Could not parse Claude response: {text[:200]}")


# ─── GET /uploads-log ───────────────────────────

@uploader_router.get("/uploads-log")
async def get_uploads_log(limit: int = 20):
    """Return recent upload history with per-ad tracking."""
    if db is None:
        return []
    docs = await db.uploads_log.find(
        {}, {"_id": 0}
    ).sort("timestamp", -1).limit(limit).to_list(limit)
    return docs


# ─── POST /create-ads (SSE) ──────────────────────

@uploader_router.post("/create-ads")
async def create_ads(
    files: List[UploadFile] = File(...),
    config: str = Form(...),
):
    """
    Create ads in Meta. Returns SSE stream with step-by-step progress.
    """
    try:
        cfg = json.loads(config)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid config JSON")

    access_token = await get_access_token(cfg.get("account_id"))
    meta_account_id = cfg.get("meta_account_id", "")

    account_config = {}
    if db is not None and cfg.get("account_id"):
        acc = await db.accounts.find_one({"id": cfg.get("account_id")})
        if acc:
            account_config = acc

    # Read all file contents eagerly — UploadFile handles close before SSE generator runs
    # Files prefixed with 'alt_' are altAssets for the previous main file
    file_data = []
    pending_alt = None
    for f in files:
        content = await f.read()
        is_video = f.content_type and f.content_type.startswith("video")
        if f.filename.startswith("alt_"):
            # Attach to previous file as altAsset
            if file_data:
                file_data[-1]["alt_content"] = content
                file_data[-1]["alt_filename"] = f.filename[4:]  # strip 'alt_'
                file_data[-1]["alt_is_video"] = is_video
        else:
            file_data.append({
                "filename": f.filename,
                "content": content,
                "is_video": is_video,
                "alt_content": None,
                "alt_filename": None,
                "alt_is_video": False,
            })

    # adCopies: per-ad copy array from config
    ad_copies = cfg.get("adCopies", [])
    global_headline = cfg.get("headline", "")
    global_primary_text = cfg.get("primary_text", "")
    global_dest_url = cfg.get("destination_url", "")

    # Create upload log entry upfront
    upload_id = str(uuid.uuid4())
    log_entry = {
        "upload_id": upload_id,
        "account_id": cfg.get("account_id"),
        "account_name": cfg.get("account_name", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_ads": len(file_data),
        "successful": 0,
        "failed": 0,
        "ad_ids_created": [],
        "errors": [],
        "files": [{"name": fd["filename"], "is_video": fd["is_video"]} for fd in file_data],
        "status": "pending",
    }
    if db is not None:
        await db.uploads_log.insert_one(log_entry)

    async def event_stream():
        created_ads = []
        try:
            # Step 1: Resolve or create campaign
            campaign_id = cfg.get("campaign_id")
            is_cbo = cfg.get("campaign_budget_type", "ABO") == "CBO"
            if cfg["destination_type"] == "new_all":
                yield _sse("progress", "Creando campaña...")
                campaign_id = await _create_campaign(
                    access_token, meta_account_id,
                    cfg["campaign_name"], cfg["campaign_objective"],
                    is_cbo=is_cbo,
                    campaign_budget=cfg.get("campaign_budget"),
                    pixel_id=cfg.get("pixel_id"),
                    conversion_event_type=cfg.get("conversion_event_type", "PURCHASE"),
                )
                yield _sse("success", f"\u2713 Campaña creada: {cfg['campaign_name']}")
            else:
                # For existing campaigns, is_cbo comes from the selected campaign's budget_optimization flag
                is_cbo = cfg.get("selected_campaign_is_cbo", False)
                yield _sse("info", f"Usando campaña existente: {campaign_id}")

            # Step 2: Resolve or create adset
            adset_id = cfg.get("adset_id")
            # Determine the effective objective (new campaign vs existing)
            if cfg["destination_type"] == "new_all":
                effective_objective = cfg.get("campaign_objective", "OUTCOME_SALES")
            else:
                effective_objective = cfg.get("selected_campaign_objective", "OUTCOME_SALES")
            conversion_event_type = cfg.get("conversion_event_type", "PURCHASE")

            if cfg["destination_type"] != "existing_both":
                yield _sse("progress", "Creando adset...")
                adset_id = await _create_adset(
                    access_token, meta_account_id, campaign_id,
                    cfg["adset_name"], cfg.get("adset_budget", 0),
                    cfg.get("pixel_id"), cfg.get("destination_url"),
                    cfg.get("placements", {}),
                    is_cbo=is_cbo,
                    objective=effective_objective,
                    conversion_event_type=conversion_event_type,
                )
                yield _sse("success", f"\u2713 Adset creado: {cfg['adset_name']}")
            else:
                yield _sse("info", f"Usando adset existente: {adset_id}")

            # Step 3: Upload files and create ads
            for i, fd in enumerate(file_data):
                file_name = fd["filename"]
                content = fd["content"]
                is_video = fd["is_video"]

                # Per-ad copy with global fallback
                copy = ad_copies[i] if i < len(ad_copies) else {}
                headline = copy.get("headline") or global_headline
                primary_text = copy.get("primaryText") or global_primary_text
                dest_url = copy.get("destinationUrl") or global_dest_url
                # CTA: per-ad override, then global, then default SHOP_NOW
                cta = copy.get("cta") or cfg.get("global_cta") or "SHOP_NOW"

                yield _sse("progress", f"Subiendo {file_name} ({i+1}/{len(file_data)})...")

                try:
                    thumbnail_url = None
                    if is_video:
                        creative_id, thumbnail_url = await _upload_video(access_token, meta_account_id, content, file_name)
                    else:
                        creative_id = await _upload_image(access_token, meta_account_id, content, file_name)

                    yield _sse("success", f"✓ Archivo subido: {file_name}")

                    # Build ad name from template
                    now = datetime.now()
                    ad_name = cfg.get("ad_name_template", file_name)
                    ad_name = ad_name.replace("{fecha}", now.strftime("%Y-%m-%d"))
                    ad_name = ad_name.replace("{cliente}", cfg.get("account_name", ""))
                    ad_name = ad_name.replace("{concepto}", file_name.rsplit(".", 1)[0])
                    ad_name = ad_name.replace("{prefijo_custom}", "")
                    
                    ad_index = cfg.get("ad_index_offset", i) + 1
                    ad_name = ad_name.replace("{numeracion}", str(ad_index).zfill(2))
                    ad_name = ad_name.replace("{formato}", "VID" if is_video else "IMG")

                    # Upload alt asset FIRST if present (for feed+stories in one ad)
                    alt_id = None
                    alt_is_video = False
                    alt_thumb = None

                    if fd.get("alt_content"):
                        alt_is_video = fd["alt_is_video"]
                        yield _sse("progress", f"Subiendo asset alternativo para stories...")
                        if alt_is_video:
                            alt_id, alt_thumb = await _upload_video(access_token, meta_account_id, fd["alt_content"], fd["alt_filename"])
                        else:
                            alt_id = await _upload_image(access_token, meta_account_id, fd["alt_content"], fd["alt_filename"])
                        yield _sse("success", f"✓ Asset stories subido")

                    yield _sse("progress", f"Creando ad: {ad_name}...")

                    # Create a SINGLE ad — with alt asset if present, standard otherwise
                    if alt_id:
                        ad_id = await _create_ad(
                            access_token, meta_account_id, adset_id,
                            ad_name, creative_id, is_video,
                            headline, primary_text, dest_url,
                            cfg.get("pixel_id"),
                            cfg.get("page_id", ""),
                            cfg.get("instagram_user_id", ""),
                            thumbnail_url=thumbnail_url,
                            enhancements=cfg.get("enhancements", {}),
                            instagram_business_account_id=cfg.get("instagram_business_account_id", ""),
                            alt_creative_id=alt_id,
                            alt_is_video=alt_is_video,
                            alt_thumbnail_url=alt_thumb,
                            cta=cta,
                        )
                    else:
                        ad_id = await _create_ad(
                            access_token, meta_account_id, adset_id,
                            ad_name, creative_id, is_video,
                            headline, primary_text, dest_url,
                            cfg.get("pixel_id"),
                            cfg.get("page_id", ""),
                            cfg.get("instagram_user_id", ""),
                            thumbnail_url=thumbnail_url,
                            enhancements=cfg.get("enhancements", {}),
                            instagram_business_account_id=cfg.get("instagram_business_account_id", ""),
                            cta=cta,
                        )

                    created_ads.append(ad_id)
                    log_entry["successful"] += 1
                    log_entry["ad_ids_created"].append(ad_id)
                    yield _sse("success", f"✓ Ad creado: {ad_name} ({ad_id})")

                    # Integración con Google (síncrono/async) luego de que Meta fue exitoso
                    if db is not None:
                        try:
                            # Account Name fallback a la config de la request
                            if "name" not in account_config:
                                account_config["name"] = cfg.get("account_name", "General")
                            google_res = await process_google_integration_async(
                                account_config=account_config,
                                ad_name=ad_name,
                                file_content=content,
                                is_video=is_video,
                                dest_url=dest_url
                            )
                            if google_res["status"] == "success":
                                yield _sse("success", "✓ Sincronizado con Drive/Sheets")
                            elif google_res["status"] == "error":
                                yield _sse("info", f"⚠️ Error Drive/Sheets: {google_res['message']}")
                        except Exception as google_err:
                            logger.error(f"Google integration error for {ad_name}: {google_err}")
                            yield _sse("info", "⚠️ Falló sincronización con Drive/Sheets")

                except Exception as ad_err:
                    err_msg = str(ad_err)[:200]
                    log_entry["failed"] += 1
                    log_entry["errors"].append({"ad_name": file_name, "error": err_msg})
                    yield _sse("error", f"✗ Falló {file_name}: {err_msg}")
                    # Continue with remaining files

            # Update log: completed
            if db is not None:
                total = len(file_data)
                successful = log_entry["successful"]
                final_status = "completed" if successful == total else ("partial" if successful > 0 else "failed")
                await db.uploads_log.update_one(
                    {"upload_id": upload_id},
                    {"$set": {
                        "successful": successful,
                        "failed": total - successful,
                        "ad_ids_created": log_entry["ad_ids_created"],
                        "errors": log_entry["errors"],
                        "status": final_status,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "campaign_id": campaign_id,
                        "adset_id": adset_id,
                    }},
                )

            yield _sse("complete", f"🎉 Ad(s) creados exitosamente!", created_ads=created_ads, campaign_id=campaign_id, adset_id=adset_id)

        except Exception as e:
            logger.error(f"Error creating ads: {e}")
            if db is not None:
                await db.uploads_log.update_one(
                    {"upload_id": upload_id},
                    {"$set": {
                        "status": "failed" if log_entry["successful"] == 0 else "partial",
                        "errors": log_entry["errors"] + [{"ad_name": "general", "error": str(e)}],
                        "failed": len(file_data) - log_entry["successful"],
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
            yield _sse("error", f"Error: {str(e)}")

    return EventSourceResponse(event_stream())


# ─── Internal helpers ─────────────────────────────

def _sse(event_type, message, created_ads=None, campaign_id=None, adset_id=None):
    data = {"type": event_type, "message": message}
    if created_ads:
        data["created_ads"] = created_ads
    if campaign_id:
        data["campaign_id"] = campaign_id
    if adset_id:
        data["adset_id"] = adset_id
    return {"data": json.dumps(data)}


async def _create_campaign(access_token, meta_account_id, name, objective, is_cbo=False,
                           campaign_budget=None, pixel_id=None, conversion_event_type="PURCHASE"):
    params = {
        "access_token": access_token,
        "name": name,
        "objective": objective,
        "status": "ACTIVE",
        "special_ad_categories": "[]",
    }
    if is_cbo and campaign_budget:
        params["daily_budget"] = int(float(campaign_budget) * 100)  # CBO: budget on campaign
        params["is_adset_budget_sharing_enabled"] = True
        # For CBO with conversion objectives, promoted_object goes on the campaign
        CONVERSION_OBJECTIVES = {"OUTCOME_SALES", "OUTCOME_LEADS"}
        if pixel_id and objective in CONVERSION_OBJECTIVES:
            params["promoted_object"] = json.dumps({
                "pixel_id": pixel_id,
                "custom_event_type": conversion_event_type or "PURCHASE"
            })
    else:
        params["is_adset_budget_sharing_enabled"] = False  # ABO: adsets own their budget

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_BASE}/{meta_account_id}/campaigns",
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            raise Exception(f"Failed to create campaign: {_meta_error_msg(resp)}")
        return resp.json()["id"]


async def _create_adset(access_token, meta_account_id, campaign_id, name, daily_budget,
                        pixel_id=None, destination_url=None, placements=None, is_cbo=False,
                        objective="OUTCOME_SALES", conversion_event_type="PURCHASE"):
    # Map objective to the correct optimization_goal + billing_event
    OBJECTIVE_CONFIG = {
        "OUTCOME_SALES":         {"optimization_goal": "OFFSITE_CONVERSIONS", "billing_event": "IMPRESSIONS"},
        "OUTCOME_LEADS":         {"optimization_goal": "LEAD_GENERATION",     "billing_event": "IMPRESSIONS"},
        "OUTCOME_TRAFFIC":       {"optimization_goal": "LINK_CLICKS",         "billing_event": "IMPRESSIONS"},
        "OUTCOME_AWARENESS":     {"optimization_goal": "REACH",               "billing_event": "IMPRESSIONS"},
        "OUTCOME_ENGAGEMENT":    {"optimization_goal": "POST_ENGAGEMENT",     "billing_event": "IMPRESSIONS"},
        "OUTCOME_APP_PROMOTION": {"optimization_goal": "APP_INSTALLS",        "billing_event": "IMPRESSIONS"},
    }
    obj_config = OBJECTIVE_CONFIG.get(objective, OBJECTIVE_CONFIG["OUTCOME_SALES"])

    params = {
        "access_token": access_token,
        "campaign_id": campaign_id,
        "name": name,
        "billing_event": obj_config["billing_event"],
        "optimization_goal": obj_config["optimization_goal"],
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "status": "ACTIVE",
        "targeting": json.dumps({
            "geo_locations": {"countries": ["AR"]},
            "age_min": 18,
            "age_max": 65,
        }),
    }
    # ABO: adset carries its own budget; CBO: campaign owns the budget
    if not is_cbo:
        params["daily_budget"] = int(float(daily_budget) * 100)

    # promoted_object for conversion-based objectives (ALWAYS on adset)
    CONVERSION_OBJECTIVES = {"OUTCOME_SALES", "OUTCOME_LEADS"}
    if pixel_id and objective in CONVERSION_OBJECTIVES:
        params["promoted_object"] = json.dumps({
            "pixel_id": pixel_id,
            "custom_event_type": conversion_event_type or "PURCHASE"
        })

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_BASE}/{meta_account_id}/adsets",
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            raise Exception(f"Failed to create adset: {_meta_error_msg(resp)}")
        return resp.json()["id"]


async def _upload_image(access_token, meta_account_id, content, filename):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_BASE}/{meta_account_id}/adimages",
            data={"access_token": access_token},
            files={"filename": (filename, content)},
            timeout=60,
        )
        if resp.status_code != 200:
            raise Exception(f"Failed to upload image: {_meta_error_msg(resp)}")
        images = resp.json().get("images", {})
        # Return the hash of the first image
        for key, val in images.items():
            return val.get("hash", "")
        raise Exception("No image hash returned")


async def _upload_video(access_token, meta_account_id, content, filename):
    """Upload video to Meta, poll until ready, then fetch auto-generated thumbnail."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_BASE}/{meta_account_id}/advideos",
            data={"access_token": access_token, "title": filename},
            files={"source": (filename, content)},
            timeout=300,
        )
        if resp.status_code != 200:
            raise Exception(f"Failed to upload video: {_meta_error_msg(resp)}")
        video_id = resp.json().get("id", "")

        # ── Poll until Meta finishes processing the video ──────────────────
        MAX_ATTEMPTS = 10
        POLL_INTERVAL = 3  # seconds
        ready = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await asyncio.sleep(POLL_INTERVAL)
            status_resp = await client.get(
                f"{META_API_BASE}/{video_id}",
                params={"access_token": access_token, "fields": "status"},
                timeout=15,
            )
            if status_resp.status_code == 200:
                status_body = status_resp.json().get("status", {})
                progress   = status_body.get("processing_progress", 0)
                vid_status = status_body.get("video_status", "")
                logger.info(
                    f"[video poll] attempt {attempt}/{MAX_ATTEMPTS} "
                    f"video_id={video_id} status={vid_status} progress={progress}%"
                )
                if vid_status == "ready" or progress >= 100:
                    ready = True
                    break
            else:
                logger.warning(f"[video poll] status request failed: {status_resp.text[:120]}")

        if not ready:
            raise Exception(
                f"El video tardó demasiado en procesar (>{MAX_ATTEMPTS * POLL_INTERVAL}s). "
                "Intentá de nuevo en unos minutos."
            )

        # ── Fetch auto-generated thumbnail (video is ready) ────────────────
        thumbnail_url = ""
        for attempt in range(4):  # up to 4 extra tries for thumbnails
            thumb_resp = await client.get(
                f"{META_API_BASE}/{video_id}/thumbnails",
                params={"access_token": access_token},
                timeout=15,
            )
            if thumb_resp.status_code == 200:
                thumbs = thumb_resp.json().get("data", [])
                if thumbs:
                    thumbnail_url = thumbs[0].get("uri", "")
                    break
            await asyncio.sleep(2)

        return video_id, thumbnail_url


async def _create_ad(access_token, meta_account_id, adset_id, ad_name,
                     creative_id, is_video, headline, primary_text,
                     destination_url, pixel_id=None, page_id=None,
                     instagram_user_id=None, thumbnail_url=None, enhancements=None,
                     instagram_business_account_id=None, alt_creative_id=None,
                     alt_is_video=False, alt_thumbnail_url=None, cta="SHOP_NOW"):
    # Prepare creative components
    page_id = page_id or os.environ.get("META_PAGE_ID", "")
    logger.info(f"[_create_ad] '{ad_name}' | page_id={page_id!r} | ig_user={instagram_user_id!r} | ig_actor={instagram_business_account_id!r} | alt={bool(alt_creative_id)}")
    
    # Meta deprecates instagram_actor_id in favor of instagram_user_id
    final_ig_actor = instagram_business_account_id or instagram_user_id

    if alt_creative_id:
        # ── 1. Create Advantage+ Asset Feed Spec for Full Placements Control ──
        oss = {"page_id": page_id}
        if final_ig_actor:
            oss["instagram_user_id"] = final_ig_actor

        images = []
        videos = []
        
        if is_video:
            videos.append({"video_id": creative_id, "adlabels": [{"name": "feed_asset"}]})
        else:
            images.append({"hash": creative_id, "adlabels": [{"name": "feed_asset"}]})
            
        if alt_is_video:
            videos.append({"video_id": alt_creative_id, "adlabels": [{"name": "story_asset"}]})
        else:
            images.append({"hash": alt_creative_id, "adlabels": [{"name": "story_asset"}]})

        asset_feed_spec = {
            "images": images,
            "videos": videos,
            "bodies": [{"text": primary_text, "adlabels": [{"name": "t1"}]}],
            "titles": [{"text": headline, "adlabels": [{"name": "t1"}]}],
            "link_urls": [{"website_url": destination_url, "adlabels": [{"name": "t1"}]}],
            "call_to_action_types": [cta],
            "ad_formats": ["AUTOMATIC_FORMAT"],
            "asset_customization_rules": [
                {
                    "customization_spec": {
                        "publisher_platforms": ["facebook", "instagram", "messenger", "audience_network"],
                        "facebook_positions": ["facebook_reels", "story"],
                        "instagram_positions": ["ig_search", "story", "reels"],
                        "messenger_positions": ["story"],
                        "audience_network_positions": ["classic", "rewarded_video"]
                    },
                    "body_label": {"name": "t1"},
                    "title_label": {"name": "t1"},
                    "link_url_label": {"name": "t1"},
                    "priority": 1
                },
                {
                    "customization_spec": {"age_min": 13},
                    "body_label": {"name": "t1"},
                    "title_label": {"name": "t1"},
                    "link_url_label": {"name": "t1"},
                    "priority": 2
                }
            ]
        }
        
        # Inject explicit formats into rules
        if alt_is_video:
            asset_feed_spec["asset_customization_rules"][0]["video_label"] = {"name": "story_asset"}
        else:
            asset_feed_spec["asset_customization_rules"][0]["image_label"] = {"name": "story_asset"}

        if is_video:
            asset_feed_spec["asset_customization_rules"][1]["video_label"] = {"name": "feed_asset"}
        else:
            asset_feed_spec["asset_customization_rules"][1]["image_label"] = {"name": "feed_asset"}
            
        # Clean empty lists for Meta API validation
        if not images: del asset_feed_spec["images"]
        if not videos: del asset_feed_spec["videos"]

        full_creative = {"object_story_spec": oss, "asset_feed_spec": asset_feed_spec}

    else:
        # ── 2. Standard Baseline Ad (Single Asset) ──
        if is_video:
            creative_spec = {
                "video_id": creative_id,
                "title": headline,
                "message": primary_text,
                "call_to_action": {"type": cta, "value": {"link": destination_url}},
            }
            if thumbnail_url: creative_spec["image_url"] = thumbnail_url
            link_or_video = {"video_data": creative_spec}
        else:
            link_or_video = {
                "link_data": {
                    "image_hash": creative_id,
                    "link": destination_url,
                    "message": primary_text,
                    "name": headline,
                    "call_to_action": {"type": cta, "value": {"link": destination_url}}
                }
            }

        oss = {
            "page_id": page_id,
            **link_or_video
        }
        
        if final_ig_actor:
            oss["instagram_user_id"] = final_ig_actor

        full_creative = {"object_story_spec": oss}

    # ── Advantage+ Creative: v22+ field names ────────────────────────────────
    # Maps frontend UI checkbox keys → official Meta creative_features_spec
    # field names. Only fields present in the DB features list are valid.
    key_maps = {
        "image": {
            "image_add_music":           "music_generation",
            "image_brightness_contrast": "image_touchups",
            "image_enhance_cta":         "generate_cta",
            "image_relevant_comments":   "inline_comment",
            "image_reveal_details":      "reveal_details_over_time",
            "image_text_improvements":   "text_optimizations",
            "image_translate_text":      "text_translation",
            "image_visual_touchups":     "image_touchups",
        },
        "video": {
            "video_add_music":           "music_generation",
            "video_add_effects":         "video_highlights",
            "video_enhance_cta":         "generate_cta",
            "video_relevant_comments":   "inline_comment",
            "video_reveal_details":      "reveal_details_over_time",
            "video_text_improvements":   "text_optimizations",
            "video_translate_text":      "text_translation",
            "video_visual_touchups":     "video_auto_crop",
        }
    }

    type_key = "video" if is_video else "image"
    creative_features = {}

    # ── Load feature list from MongoDB (evergreen, editable without deploy) ──
    all_meta_features = await get_advantage_features()

    # ── OPT_OUT loop: only features confirmed to accept enroll_status ─────────
    # Full feature list is in MongoDB for reference/sync, but we only send
    # OPT_OUT for features whose schema is {"enroll_status": "OPT_OUT|OPT_IN"}.
    # Features with a different schema (tags, WA fields, etc.) are excluded
    # to avoid error 100/2061015 (Invalid parameter).
    safe_features = await get_safe_opt_out_features()
    for feature in safe_features:
        creative_features[feature] = {"enroll_status": "OPT_OUT"}

    # Overwrite with OPT_IN only for those checked in the frontend UI
    for frontend_key, meta_field in key_maps[type_key].items():
        if enhancements and enhancements.get(frontend_key):
            creative_features[meta_field] = {"enroll_status": "OPT_IN"}

    # degrees_of_freedom_spec is INCOMPATIBLE with asset_customization_rules.
    # Meta rejects the combination with error 100/2061015.
    # Only add it for standard single-asset ads.
    if not alt_creative_id:
        full_creative["degrees_of_freedom_spec"] = {
            "creative_features_spec": creative_features
    }

    # Tracking specs
    if pixel_id:
        full_creative["tracking_specs"] = [
            {"action.type": ["offsite_conversion"], "fb_pixel": [pixel_id]}
        ]

    # ── Create ad with auto-retry for common Meta API errors ──────
    ad_params = {
        "access_token": access_token,
        "name": ad_name,
        "adset_id": adset_id,
        "creative": json.dumps(full_creative),
        "status": "ACTIVE",
    }

    retries = 0
    last_error = None
    while retries < 3:
        try:
            resp = await _meta_api_call(
                "POST", f"{META_API_BASE}/{meta_account_id}/ads",
                params=ad_params, timeout=30,
            )
            return resp.json()["id"]
        except Exception as e:
            error_msg = str(e)
            last_error = e
            
            # Retry 1: degrees_of_freedom_spec incompatibility
            if "degrees_of_freedom_spec" in error_msg and "degrees_of_freedom_spec" in full_creative:
                logger.warning(f"[_create_ad] degrees_of_freedom_spec rejected for '{ad_name}', retrying without it")
                full_creative.pop("degrees_of_freedom_spec")
                ad_params["creative"] = json.dumps(full_creative)
                retries += 1
                continue
                
            # Retry 2: Instagram account invalid or unlinked
            if "Instagram account" in error_msg or "instagram_actor_id" in error_msg or "instagram_user_id" in error_msg:
                oss = full_creative.get("object_story_spec", {})
                has_ig = "instagram_actor_id" in oss or "instagram_user_id" in oss
                
                # If we haven't stripped IG fields yet, do it to force implicit connection
                if has_ig:
                    logger.warning(f"[_create_ad] IG account '{ad_name}' rejected or not available. Stripping IG fields to let Meta implicitly fallback to Page-backed IG.")
                    oss.pop("instagram_user_id", None)
                    oss.pop("instagram_actor_id", None)
                    
                    full_creative["object_story_spec"] = oss
                    ad_params["creative"] = json.dumps(full_creative)
                    retries += 1
                    continue

            # Unhandled error, bubble up immediately
            raise
            
    # Exhausted retries
    if last_error:
        raise last_error


@uploader_router.get("/check-advantage-features")
async def check_advantage_features():
    """
    Verifica el estado de los Advantage+ features en Rumbo comparando
    la lista actual en MongoDB contra los features que Meta devuelve
    en un ad creative real de la cuenta.
    """
    from server import get_meta_access_token  # type: ignore
    access_token = await get_meta_access_token()
    account = await db["accounts"].find_one({"meta_account_id": {"$exists": True, "$ne": ""}})

    if not account or not access_token:
        raise HTTPException(status_code=400, detail="No se encontró token o cuenta en la DB.")

    meta_acc_id = account.get("meta_account_id") or account.get("id")
    if not meta_acc_id:
        raise HTTPException(status_code=400, detail="La cuenta no tiene meta_account_id configurado.")

    # Leer features actuales de MongoDB
    rumbo_features = set(await get_advantage_features())

    # Buscar un ad creative real y leer su degrees_of_freedom_spec
    url_adcreatives = f"{META_API_BASE}/{meta_acc_id.lstrip('act_') and meta_acc_id}/adcreatives"
    meta_features = set()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{META_API_BASE}/{meta_acc_id}/adcreatives",
            params={"fields": "degrees_of_freedom_spec", "limit": 20, "access_token": access_token}
        )
        for c in resp.json().get("data", []):
            keys = c.get("degrees_of_freedom_spec", {}).get("creative_features_spec", {}).keys()
            meta_features.update(keys)

    return {
        "rumbo_features_count": len(rumbo_features),
        "meta_observed_count": len(meta_features),
        "in_sync": sorted(rumbo_features & meta_features),
        "new_in_meta_not_in_rumbo": sorted(meta_features - rumbo_features),
        "in_rumbo_not_observed_in_meta": sorted(rumbo_features - meta_features),
        "source": "mongodb:config.advantage_features"
    }


@uploader_router.post("/internal/sync-advantage-features")
async def sync_advantage_features():
    """
    Endpoint interno llamado por cron mensual de Railway.
    Hace fetch del HTML de la documentación de Meta, usa Claude para extraer
    los field names, compara contra MongoDB y actualiza si hay cambios.
    """
    import anthropic
    import httpx as _httpx

    META_DOCS_URL = "https://developers.facebook.com/docs/marketing-api/reference/ad-creative-features-spec/"

    # 1. Fetch HTML de docs de Meta
    async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(META_DOCS_URL, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"No se pudo acceder a la docs de Meta: HTTP {resp.status_code}")
        html_content = resp.text[:40000]  # Limitar tokens para Claude

    # 2. Llamar a Claude para extraer los field names
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY no configurado")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)
    message = claude_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "The following is HTML from the Meta Marketing API documentation page for AdCreativeFeatureSpec.\n"
                "Extract ONLY the field names (snake_case identifiers) listed as valid fields for creative_features_spec.\n"
                "Return ONLY a JSON array of strings, no explanation, no markdown, no code blocks.\n"
                "Example output: [\"adapt_to_placement\", \"add_text_overlay\", \"image_touchups\"]\n\n"
                f"HTML:\n{html_content}"
            )
        }]
    )

    try:
        raw = message.content[0].text.strip()
        # Strip markdown code fences if Claude added them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        extracted: list = json.loads(raw.strip())
        if not isinstance(extracted, list):
            raise ValueError("Response is not a list")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude devolvió una respuesta no parseable: {e} — raw: {message.content[0].text[:200]}")

    # 3. Comparar contra la lista actual en MongoDB
    current_features = set(await get_advantage_features())
    new_features = set(extracted)
    added = sorted(new_features - current_features)
    removed = sorted(current_features - new_features)

    if not added and not removed:
        return {"status": "no_changes", "total": len(new_features), "added": [], "removed": []}

    # 4. Actualizar MongoDB solo si hubo cambios
    await db.config.update_one(
        {"_id": "advantage_features"},
        {"$set": {
            "features": sorted(new_features),
            "updated_at": __import__('datetime').datetime.utcnow().isoformat(),
            "last_sync_source": "meta_docs_claude",
        }},
        upsert=True,
    )

    logger.info(f"[sync-advantage-features] Updated: +{len(added)} added, -{len(removed)} removed. Total: {len(new_features)}")
    return {
        "status": "updated",
        "total": len(new_features),
        "added": added,
        "removed": removed,
    }


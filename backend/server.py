from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
import httpx
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import asyncio
import re
import unicodedata
from difflib import SequenceMatcher

from meta_api import (
    fetch_account_data, fetch_account_data_custom, fetch_ads_lifetime_insights,
    fetch_structure_only, fetch_insights_for_period, fetch_client_dashboard_data,
    fetch_client_ad_insights, fetch_client_creator_active_structure,
)
from routers.tiendanube import configure_tiendanube_router, router as tiendanube_router
from tiendanube_orders import TiendanubeConfigError, fetch_demo_orders

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

MONGO_URL = os.environ.get("MONGO_URL") or os.environ.get("mongo_url")
DB_NAME = os.environ.get("DB_NAME") or os.environ.get("db_name") or os.environ.get("mongo_db")

if not MONGO_URL:
    raise ValueError("MONGO_URL environment variable is not set")
if not DB_NAME:
    raise ValueError("DB_NAME environment variable is not set")

client = AsyncIOMotorClient(
    MONGO_URL,
    tls=True,
    tlsAllowInvalidCertificates=True,
    serverSelectionTimeoutMS=3000,
    connectTimeoutMS=3000,
    socketTimeoutMS=3000,
)
db = client[DB_NAME]
configure_tiendanube_router(db)

app = FastAPI()
api_router = APIRouter(prefix="/api")

# Meta API config
from meta_api import fetch_account_data as meta_fetch
from meta_api import fetch_account_data_custom as meta_fetch_custom

# ── Cache TTLs (seconds) ──────────────────────────────────────────────────────
CACHE_TTL_STRUCTURE = 7_200   # 2 hours  — campaigns/adsets/ads structure
CACHE_TTL_INSIGHTS  = 1_800   # 30 min   — spend/ROAS/conversions per period
CACHE_TTL_LIFETIME  = 86_400  # 24 hours — all-time ad metrics

ACCOUNTS_CONFIG = [
    {"id": "act_722500875980510", "name": "itsmumma", "meta_account_id": "act_722500875980510"},
    {"id": "act_2699461910320121", "name": "Tienda canela", "meta_account_id": "act_2699461910320121"},
    {"id": "act_2679743139034474", "name": "Falhaus", "meta_account_id": "act_2679743139034474"},
    {"id": "act_1008079433410223", "name": "CANDY FAJAS", "meta_account_id": "act_1008079433410223"},
    {"id": "act_3919683401399689", "name": "SFS", "meta_account_id": "act_3919683401399689"},
]

# --- Pydantic Models ---

class RoasObjetivoUpdate(BaseModel):
    roas_objetivo: float

class AccountResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    meta_account_id: str
    display_name: Optional[str] = None
    roas_objetivo: Optional[float] = None
    last_refreshed: Optional[str] = None

class DashboardOverview(BaseModel):
    roas_actual: float
    roas_objetivo: Optional[float] = None
    total_ads_activos: int
    total_facturacion: float
    total_spend: float
    adsets_fuera_de_roas: int
    adsets_fuera_percentage: float
    adsets_fuera_daily_budget: float
    total_active_adsets: int
    hit_rate: float
    hit_count: int
    evaluable_count: int
    cpa_promedio: Optional[float] = None
    avg_cpa: Optional[float] = None  # Account-level CPA for batch threshold
    cbo_adsets_review: int = 0  # Count of CBO adsets needing review

class BudgetItemFuera(BaseModel):
    name: str
    budget: float
    roas: float
    tipo: str

class BudgetData(BaseModel):
    budget_diario_total: float
    budget_fuera_de_roas: float
    budget_dentro_de_roas: float
    items_fuera: List[BudgetItemFuera] = []

class TopAd(BaseModel):
    ad_name: str
    adset_name: str
    ventas: int
    roas: float
    facturacion: float
    gasto: float = 0
    status: str

class RecentAdUpload(BaseModel):
    ad_name: str
    adset_name: str
    created_date: str
    status: str

class ActiveAdset(BaseModel):
    id: str = ""
    campaign_id: str = ""
    adset_name: str
    tipo: str
    ads_activos: int
    roas: float
    estado: str
    ventas: int
    gasto: float
    budget: str
    spend: float = 0
    status: str = ""

class BatchData(BaseModel):
    adset_name: str
    batch_label: str
    fecha: str
    ads_count: int
    estado: str
    mejor_roas: float
    dias: int
    gasto_total: float

class BatchSummary(BaseModel):
    hit_rate: float
    hit_count: int
    miss_count: int
    esperando_count: int
    total_evaluable: int

class CampaignData(BaseModel):
    id: str
    name: str
    budget_optimization: str
    daily_budget: float
    status: str
    spend: float
    conversions: int
    conversion_value: float
    roas: float

class LosingAd(BaseModel):
    ad_name: str
    adset_name: str
    ventas: int
    roas: float
    gasto: float
    status: str

class DashboardResponse(BaseModel):
    account: AccountResponse
    overview: DashboardOverview
    budget: BudgetData
    top_ads: List[TopAd]
    losing_ads: List[LosingAd] = []
    active_adsets: List[ActiveAdset]
    batches: List[BatchData]
    batch_summary: BatchSummary
    campaigns: List[CampaignData] = []
    recent_uploads: List[RecentAdUpload] = []
    cached_at: Optional[str] = None  # ISO timestamp of last Meta API fetch


class AccountCardData(BaseModel):
    id: str
    name: str
    display_name: Optional[str] = None
    roas_actual: float
    roas_objetivo: Optional[float] = None
    total_facturacion: float
    total_spend: float
    ventas: int
    adsets_fuera: int
    total_active_adsets: int
    budget_fuera: float
    total_daily_budget: float = 0
    ultimo_test_date: Optional[str] = None
    ultimo_test_days: Optional[int] = None
    error: Optional[str] = None
    error_type: Optional[str] = None  # "permission_denied" for accounts without API access
    cbo_review_needed: bool = False  # True when CBO adset is underperforming but campaign is OK
    stale_warning: Optional[str] = None
    cache_age_minutes: Optional[int] = None


class OverviewResponse(BaseModel):
    cards: List[AccountCardData]
    cached_at: Optional[str] = None


# --- Seed Data ---

# --- Meta API Data Fetch ---


# --- Settings & Config ---

async def get_meta_access_token():
    """Get global Meta Access Token from DB (priority) or Env Var (fallback)."""
    settings = await db.settings.find_one({"_id": "meta_config"})
    if settings and settings.get("access_token"):
        return settings["access_token"]
    return os.environ.get("META_ACCESS_TOKEN", "")


async def get_access_token_for_account(account_id: str = None):
    """Get Meta token for a specific account. Uses per-account token if set, else global."""
    if account_id:
        account = await db.accounts.find_one({"id": account_id}, {"_id": 0, "meta_token": 1})
        if account and account.get("meta_token"):
            return account["meta_token"]
    return await get_meta_access_token()

class MetaTokenRequest(BaseModel):
    token: str

@api_router.get("/settings/meta-token")
async def get_meta_token_status():
    """Check if Meta Access Token is configured (masked)."""
    token = await get_meta_access_token()
    if not token:
        return {"configured": False, "source": None}
    
    # Mask key: show first 4 and last 4 chars
    masked = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"
    return {
        "configured": True,
        "masked_token": masked,
        "source": "database" if await db.settings.find_one({"_id": "meta_config"}) else "env"
    }

@api_router.post("/settings/meta-token")
async def save_meta_token(request: MetaTokenRequest):
    """Save Meta Access Token to database."""
    token = request.token.strip()
    if not token:
        # If empty, delete from DB (revert to env var if exists)
        await db.settings.delete_one({"_id": "meta_config"})
        return {"message": "Token eliminado de la base de datos"}
    
    # Validate token format (basic check)
    if not token.startswith("EAAG"): # Valid Meta tokens usually start with EAAG or EAA
         pass # We allow it for now, but could be stricter

    await db.settings.update_one(
        {"_id": "meta_config"},
        {
            "$set": {
                "access_token": token,
                "auth_method": "manual",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "$unset": {
                "expires_at": "",
                "user_name": "",
            },
        },
        upsert=True
    )
    return {"message": "Token guardado exitosamente"}


# --- Meta API Data Fetch ---

def _is_stale(cached_doc, ttl_seconds: int) -> bool:
    """Return True if the cache doc is missing or older than ttl_seconds."""
    if not cached_doc:
        return True
    fetched_at = cached_doc.get("fetched_at")
    if not fetched_at:
        return True
    dt = parse_created_time(fetched_at)
    if not dt:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() > ttl_seconds


async def _save_cache(cache_type: str, account_id: str, period_key, data: dict):
    """Upsert a typed cache document."""
    now = datetime.now(timezone.utc).isoformat()
    await db.meta_cache.update_one(
        {"account_id": account_id, "cache_type": cache_type, "period": period_key},
        {"$set": {"account_id": account_id, "cache_type": cache_type,
                  "period": period_key, "fetched_at": now, "data": data}},
        upsert=True,
    )
    return now


async def refresh_account_data(account_id, meta_account_id, period):
    """Smart multi-TTL refresh: only hits Meta for data whose TTL has expired.

    Cache tiers (each tier fails independently — partial refresh is OK):
      structure  (campaigns/adsets/ads sans métricas)  2h
      insights   (spend/ROAS/conversions por período)   30min
      lifetime   (métricas all-time por ad)             24h
    """
    access_token = await get_access_token_for_account(account_id)
    if not access_token:
        raise HTTPException(status_code=500,
            detail="Meta API access token no configurado. Ir a Configuración.")

    now_iso = datetime.now(timezone.utc).isoformat()
    tier_errors = []

    # ── 1. Structure cache ──────────────────────────────────────────────────
    struct_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "structure", "period": "_"}, {"_id": 0}
    )
    if _is_stale(struct_cache, CACHE_TTL_STRUCTURE):
        try:
            logger.info(f"[cache:structure] miss → fetching {account_id}")
            structure = await fetch_structure_only(meta_account_id, access_token)
            now_iso = await _save_cache("structure", account_id, "_", structure)
        except Exception as e:
            tier_errors.append(f"structure: {e}")
            logger.error(f"❌ [cache:structure] FAILED for {account_id}: {e}")
            if struct_cache:
                logger.warning(f"[cache:structure] Using stale data for {account_id}")
                structure = struct_cache["data"]
            else:
                raise  # No fallback possible - structure is required
    else:
        logger.info(f"[cache:structure] HIT  {account_id} (age < 2h)")
        structure = struct_cache["data"]

    campaigns = structure["campaigns"]
    adsets    = structure["adsets"]
    ads_base  = structure["ads"]          # no metrics yet
    ad_ids    = [a["id"] for a in ads_base]
    adset_id_map = {a["id"]: a for a in adsets}

    # ── 2. Insights cache (period-specific) ─────────────────────────────────
    ins_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "insights", "period": period}, {"_id": 0}
    )
    if _is_stale(ins_cache, CACHE_TTL_INSIGHTS):
        try:
            logger.info(f"[cache:insights] miss → fetching {account_id} period={period}")
            ins_data = await fetch_insights_for_period(
                meta_account_id, access_token, period, ad_ids
            )
            await _save_cache("insights", account_id, period, ins_data)
        except Exception as e:
            tier_errors.append(f"insights: {e}")
            logger.error(f"❌ [cache:insights] FAILED for {account_id}: {e}")
            if ins_cache:
                logger.warning(f"[cache:insights] Using stale data for {account_id}")
                ins_data = ins_cache["data"]
            else:
                # No cached insights at all — use empty defaults
                logger.warning(f"[cache:insights] No fallback, using empty insights for {account_id}")
                ins_data = {"ad_insights": {}, "account_insights": {"spend": 0, "conversions": 0, "conversion_value": 0, "roas": 0}}
    else:
        logger.info(f"[cache:insights] HIT  {account_id} period={period} (age < 30min)")
        ins_data = ins_cache["data"]

    ad_insights     = ins_data["ad_insights"]      # {ad_id: {spend, roas, ...}}
    account_insights = ins_data["account_insights"]

    # ── 3. Lifetime cache ───────────────────────────────────────────────────
    lt_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "lifetime", "period": "_"}, {"_id": 0}
    )
    if _is_stale(lt_cache, CACHE_TTL_LIFETIME):
        try:
            logger.info(f"[cache:lifetime] miss → fetching {account_id}")
            lifetime_data = await fetch_ads_lifetime_insights(meta_account_id, access_token, ad_ids)
            await _save_cache("lifetime", account_id, "_", lifetime_data)
        except Exception as e:
            tier_errors.append(f"lifetime: {e}")
            logger.error(f"❌ [cache:lifetime] FAILED for {account_id}: {e}")
            if lt_cache:
                logger.warning(f"[cache:lifetime] Using stale data for {account_id}")
                lifetime_data = lt_cache["data"]
            else:
                lifetime_data = {}
    else:
        logger.info(f"[cache:lifetime] HIT  {account_id} (age < 24h)")
        lifetime_data = lt_cache["data"]

    if tier_errors:
        logger.warning(f"⚠️ Partial refresh for {account_id}: {'; '.join(tier_errors)}")

    # ── 4. Merge: attach insights + lifetime to each ad ──────────────────────
    adset_campaign_map = {a["id"]: a.get("campaign_id", "") for a in adsets}
    ads = []
    for ad in ads_base:
        ins  = ad_insights.get(ad["id"], {})
        lt   = lifetime_data.get(ad["id"], {})
        adset = adset_id_map.get(ad["adset_id"], {})
        ads.append({
            **ad,
            "campaign_id":           adset_campaign_map.get(ad["adset_id"], ""),
            # period metrics (from insights cache)
            "spend":            ins.get("spend", 0),
            "conversions":      ins.get("conversions", 0),
            "conversion_value": ins.get("conversion_value", 0),
            "roas":             ins.get("roas", 0),
            # lifetime metrics (from lifetime cache)
            "lifetime_spend":            lt.get("spend", 0),
            "lifetime_conversions":      lt.get("conversions", 0),
            "lifetime_conversion_value": lt.get("conversion_value", 0),
            "lifetime_roas":             lt.get("roas", 0),
        })

    # Attach period insights to adsets too
    # (adset-level insights come from the structure's adset daily_budget; metrics from ads rollup)
    for adset in adsets:
        adset_ads = [a for a in ads if a.get("adset_id") == adset["id"]]
        adset["spend"]            = round(sum(a["spend"] for a in adset_ads), 2)
        adset["conversions"]      = sum(a["conversions"] for a in adset_ads)
        adset["conversion_value"] = round(sum(a["conversion_value"] for a in adset_ads), 2)
        adset["roas"]             = round(
            adset["conversion_value"] / adset["spend"], 2
        ) if adset["spend"] > 0 else 0

    # Campaigns: rollup from adsets
    for camp in campaigns:
        camp_adsets = [a for a in adsets if a.get("campaign_id") == camp["id"]]
        camp["spend"]            = round(sum(a["spend"] for a in camp_adsets), 2)
        camp["conversions"]      = sum(a["conversions"] for a in camp_adsets)
        camp["conversion_value"] = round(sum(a["conversion_value"] for a in camp_adsets), 2)
        camp["roas"]             = round(
            camp["conversion_value"] / camp["spend"], 2
        ) if camp["spend"] > 0 else 0

    merged = {
        "campaigns":       campaigns,
        "adsets":          adsets,
        "ads":             ads,
        "account_insights": account_insights,
    }

    # Persist a legacy combined cache doc (for dashboard endpoint that reads it directly)
    await db.meta_cache.update_one(
        {"account_id": account_id, "cache_type": "combined", "period": period},
        {"$set": {"account_id": account_id, "cache_type": "combined",
                  "period": period, "fetched_at": now_iso, "data": merged}},
        upsert=True,
    )
    await db.accounts.update_one({"id": account_id}, {"$set": {"last_refreshed": now_iso}})

    return merged


async def refresh_account_data_custom(account_id, meta_account_id, since, until):
    """Fetch data for custom date range. Structure cached 2h, insights 30min."""
    access_token = await get_access_token_for_account(account_id)
    if not access_token:
        raise HTTPException(status_code=500,
            detail="Meta API access token no configurado. Ir a Configuración.")

    period_key = f"custom_{since}_{until}"

    # Structure (2h TTL, shared across all periods)
    struct_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "structure", "period": "_"}, {"_id": 0}
    )
    if _is_stale(struct_cache, CACHE_TTL_STRUCTURE):
        structure = await fetch_structure_only(meta_account_id, access_token)
        await _save_cache("structure", account_id, "_", structure)
    else:
        structure = struct_cache["data"]

    ad_ids = [a["id"] for a in structure["ads"]]

    # Insights for custom range (30min TTL)
    ins_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "insights", "period": period_key}, {"_id": 0}
    )
    if _is_stale(ins_cache, CACHE_TTL_INSIGHTS):
        ins_data = await fetch_insights_for_period(
            meta_account_id, access_token, 0, ad_ids,
            custom_range={"since": since, "until": until}
        )
        await _save_cache("insights", account_id, period_key, ins_data)
    else:
        ins_data = ins_cache["data"]

    ad_insights      = ins_data["ad_insights"]
    account_insights = ins_data["account_insights"]

    ads = []
    adset_campaign_map = {a["id"]: a.get("campaign_id", "") for a in structure["adsets"]}
    for ad in structure["ads"]:
        ins = ad_insights.get(ad["id"], {})
        ads.append({
            **ad,
            "campaign_id":           adset_campaign_map.get(ad.get("adset_id", ""), ""),
            "spend":            ins.get("spend", 0),
            "conversions":      ins.get("conversions", 0),
            "conversion_value": ins.get("conversion_value", 0),
            "roas":             ins.get("roas", 0),
        })

    for adset in structure["adsets"]:
        adset_ads = [a for a in ads if a.get("adset_id") == adset["id"]]
        adset["spend"]            = round(sum(a["spend"] for a in adset_ads), 2)
        adset["conversions"]      = sum(a["conversions"] for a in adset_ads)
        adset["conversion_value"] = round(sum(a["conversion_value"] for a in adset_ads), 2)
        adset["roas"]             = round(adset["conversion_value"] / adset["spend"], 2) if adset["spend"] > 0 else 0

    for camp in structure["campaigns"]:
        camp_adsets = [a for a in structure["adsets"] if a.get("campaign_id") == camp["id"]]
        camp["spend"]            = round(sum(a["spend"] for a in camp_adsets), 2)
        camp["conversions"]      = sum(a["conversions"] for a in camp_adsets)
        camp["conversion_value"] = round(sum(a["conversion_value"] for a in camp_adsets), 2)
        camp["roas"]             = round(camp["conversion_value"] / camp["spend"], 2) if camp["spend"] > 0 else 0

    return {
        "campaigns":       structure["campaigns"],
        "adsets":          structure["adsets"],
        "ads":             ads,
        "account_insights": account_insights,
    }


# --- Calculation Logic ---

def calculate_cpa_promedio(ads, roas_objetivo):
    """Calculate average CPA from ads with ROAS >= objetivo in last 30 days."""
    qualifying_ads = [a for a in ads if a.get("roas", 0) >= roas_objetivo and a.get("conversions", 0) > 0]
    total_spend = sum(a["spend"] for a in qualifying_ads)
    total_conversions = sum(a["conversions"] for a in qualifying_ads)
    if total_conversions == 0:
        return None
    return total_spend / total_conversions


def calculate_account_avg_cpa(account_insights):
    """Calculate average CPA for the entire account from last 30 days.
    Used for batch evaluation threshold (3× CPA).
    """
    spend = account_insights.get("spend", 0)
    conversions = account_insights.get("conversions", 0)
    if conversions == 0:
        return None
    return spend / conversions

def calculate_budget_disponible(campaigns, adsets, roas_objetivo):
    """
    Calculate budget allocation between winning and losing units.
    CRITICAL: Only include campaigns/adsets that have actual spend in the period.
    Campaigns with 0 spend are likely old/inactive and should be excluded.
    Also returns list of items that are "fuera" for tooltip display.
    """
    total_budget = 0
    budget_fuera = 0
    items_fuera = []  # List of {"name", "budget", "roas", "tipo"} for tooltip

    for campaign in campaigns:
        if campaign.get("budget_optimization") == "CBO" and campaign.get("status") == "ACTIVE":
            budget = campaign.get("daily_budget", 0)
            spend = campaign.get("spend", 0)
            
            # Only include if campaign has actual spend in the period
            if spend <= 0 or budget <= 0:
                continue
                
            total_budget += budget
            if campaign.get("roas", 0) < roas_objetivo:
                budget_fuera += budget
                items_fuera.append({
                    "name": campaign.get("name", "Unknown"),
                    "budget": budget,
                    "roas": campaign.get("roas", 0),
                    "tipo": "CBO"
                })

    for adset in adsets:
        camp = next((c for c in campaigns if c["id"] == adset.get("campaign_id")), None)
        if camp and camp.get("budget_optimization") == "ABO" and adset.get("status") == "ACTIVE":
            budget = adset.get("daily_budget", 0)
            spend = adset.get("spend", 0)
            
            # Only include if adset has actual spend in the period
            if spend <= 0 or budget <= 0:
                continue
                
            total_budget += budget
            if adset.get("roas", 0) < roas_objetivo:
                budget_fuera += budget
                items_fuera.append({
                    "name": adset.get("name", "Unknown"),
                    "budget": budget,
                    "roas": adset.get("roas", 0),
                    "tipo": "ABO"
                })

    budget_dentro = total_budget - budget_fuera
    return total_budget, budget_fuera, budget_dentro, items_fuera


def get_top_ads(ads, adsets, include_paused):
    """Get top ads by conversions for the period (regardless of creation date)."""
    adset_map = {a["id"]: a["name"] for a in adsets}

    filtered = []
    for ad in ads:
        if not include_paused and ad.get("status") != "ACTIVE":
            continue
        # Include all ads that have conversions in this period
        if ad.get("conversions", 0) > 0 or ad.get("spend", 0) > 0:
            ad_with_adset = dict(ad)
            ad_with_adset["adset_name"] = adset_map.get(ad.get("adset_id", ""), "Unknown")
            filtered.append(ad_with_adset)

    # Sort by conversions descending
    sorted_ads = sorted(filtered, key=lambda a: a.get("conversions", 0), reverse=True)
    return sorted_ads


def get_losing_ads(ads, adsets, roas_objetivo, include_paused):
    """Get ads with low ROAS (below objetivo) but with spend."""
    if not roas_objetivo:
        return []
    
    adset_map = {a["id"]: a["name"] for a in adsets}

    filtered = []
    for ad in ads:
        if not include_paused and ad.get("status") != "ACTIVE":
            continue
        spend = ad.get("spend", 0)
        roas = ad.get("roas", 0)
        # Include ads with spend but below ROAS objetivo
        if spend > 100 and roas < roas_objetivo:  # Minimum $100 spend to be considered
            ad_with_adset = dict(ad)
            ad_with_adset["adset_name"] = adset_map.get(ad.get("adset_id", ""), "Unknown")
            filtered.append(ad_with_adset)

    # Sort by spend descending (most money wasted first)
    sorted_ads = sorted(filtered, key=lambda a: a.get("spend", 0), reverse=True)
    return sorted_ads


def get_all_active_adsets(adsets, ads, campaigns, roas_objetivo):
    """Get ALL active adsets with operational metrics using adset-level insights."""
    camp_map = {c["id"]: c for c in campaigns}

    # Count active ads per adset
    active_ads_per_adset = {}
    for ad in ads:
        if ad.get("status") == "ACTIVE":
            aid = ad.get("adset_id", "")
            active_ads_per_adset[aid] = active_ads_per_adset.get(aid, 0) + 1

    result = []
    for adset in adsets:
        if adset.get("status") != "ACTIVE":
            continue

        adset_id = adset["id"]
        campaign_id = adset.get("campaign_id", "")
        camp = camp_map.get(campaign_id, {})

        # Determine ABO vs CBO
        adset_budget = adset.get("daily_budget", 0)
        camp_budget = camp.get("daily_budget", 0)
        if adset_budget > 0:
            tipo = "ABO"
            budget_str = f"${adset_budget:,.0f}"
        elif camp_budget > 0:
            tipo = "CBO"
            budget_str = f"${camp_budget:,.0f} (CBO)"
        else:
            tipo = "CBO" if camp.get("budget_optimization") == "CBO" else "ABO"
            budget_str = "$0"

        # Adset-level metrics from insights
        roas = adset.get("roas", 0)
        ventas = adset.get("conversions", 0)
        gasto = adset.get("spend", 0)

        # Estado
        if roas_objetivo and roas_objetivo > 0:
            estado = "dentro" if roas >= roas_objetivo else "fuera"
        else:
            estado = "neutral"

        ads_count = active_ads_per_adset.get(adset_id, 0)

        result.append(ActiveAdset(
            id=adset_id,
            campaign_id=campaign_id,
            adset_name=adset["name"],
            tipo=tipo,
            ads_activos=ads_count,
            roas=round(roas, 2),
            estado=estado,
            ventas=ventas,
            gasto=round(gasto, 2),
            budget=budget_str,
            spend=round(gasto, 2),
            status="ACTIVE",
        ))

    # Sort by ROAS descending by default
    result.sort(key=lambda a: a.roas, reverse=True)
    return result


def get_recent_ad_uploads(ads, adsets, max_days=60):
    """Get recently uploaded ads sorted by creation date (newest first)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_days)
    
    adset_map = {a["id"]: a["name"] for a in adsets}
    
    recent = []
    for ad in ads:
        created_str = ad.get("created_time", "")
        created_dt = parse_created_time(created_str)
        if not created_dt or created_dt < cutoff:
            continue
        
        status_raw = ad.get("status", "UNKNOWN")
        effective_status = ad.get("effective_status", status_raw)
        
        # Determine display status
        if effective_status == "ACTIVE":
            status = "Activo"
        elif effective_status == "PAUSED":
            status = "Pausado"
        elif effective_status in ("DISAPPROVED", "WITH_ISSUES"):
            status = "Rechazado"
        else:
            status = status_raw.title()
        
        recent.append({
            "ad_name": ad.get("name", "Unknown"),
            "adset_name": adset_map.get(ad.get("adset_id", ""), "Unknown"),
            "created_dt": created_dt,
            "created_date": created_dt.strftime("%d %b"),
            "status": status,
        })
    
    # Sort by created_dt descending (newest first)
    recent.sort(key=lambda x: x["created_dt"], reverse=True)
    
    return [RecentAdUpload(
        ad_name=r["ad_name"],
        adset_name=r["adset_name"],
        created_date=r["created_date"],
        status=r["status"]
    ) for r in recent]


def parse_created_time(created_str):
    """Parse ISO datetime string to timezone-aware datetime. Handles Meta API formats."""
    if not isinstance(created_str, str) or not created_str:
        return None
    try:
        # Handle Meta API format: +0000 → +00:00
        s = created_str.replace('Z', '+00:00').replace('+0000', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def calculate_batches(ads, adsets, roas_objetivo, period_days, avg_cpa, min_days=5):
    """
    Calculate batch/iteration data.
    A batch = group of ads sharing same adset_id + same creation date.
    
    EVALUATION CRITERIA:
    - Evaluable: >= 5 days old AND spent >= 3× average CPA
    - Hit: Evaluable AND at least 1 ad has ROAS >= target
    - Miss: Evaluable AND no ads have ROAS >= target
    - Esperando: NOT evaluable AND adset is ACTIVE
    - Desactivado: Adset is NOT ACTIVE (show with historical results)
    
    CRITICAL: Paused/deactivated batches should show as "desactivado" with their
    historical performance, NOT be hidden. They represent completed tests.
    
    Hit Rate = Hits / (Hits + Misses) - "Esperando" and "Desactivado" excluded
    """
    now = datetime.now(timezone.utc)
    # Always use 60 days for batch lookback (iterations are about testing history)
    cutoff = now - timedelta(days=60)
    
    # Calculate spend threshold: 3× average CPA
    # If no CPA available (no conversions), threshold is None and batch can't be evaluated
    spend_threshold = (avg_cpa * 3) if avg_cpa and avg_cpa > 0 else None

    # Build adset name and status lookup
    adset_map = {a["id"]: a["name"] for a in adsets}
    adset_status_map = {a["id"]: a.get("status", "UNKNOWN") for a in adsets}

    # Group ads by (adset_id, date_str)
    batch_groups = {}
    for ad in ads:
        created_dt = parse_created_time(ad.get("created_time", ""))
        if not created_dt or created_dt < cutoff:
            continue
        adset_id = ad.get("adset_id", "")
        date_key = created_dt.strftime("%Y-%m-%d")
        key = (adset_id, date_key)
        if key not in batch_groups:
            batch_groups[key] = []
        batch_groups[key].append(ad)

    # Track batch numbering per adset
    adset_batch_counts = {}
    for (adset_id, _) in sorted(batch_groups.keys(), key=lambda k: k[1]):
        adset_batch_counts.setdefault(adset_id, 0)
        adset_batch_counts[adset_id] += 1

    # Reassign for ordered labeling
    adset_batch_order = {}
    for (adset_id, date_str) in sorted(batch_groups.keys(), key=lambda k: k[1]):
        adset_batch_order.setdefault(adset_id, [])
        adset_batch_order[adset_id].append(date_str)

    batches_result = []
    hit_count = 0
    miss_count = 0
    esperando_count = 0
    desactivado_count = 0

    for (adset_id, date_str), group_ads in batch_groups.items():
        adset_name = adset_map.get(adset_id, "Unknown Adset")
        adset_status = adset_status_map.get(adset_id, "UNKNOWN")
        is_active = adset_status == "ACTIVE"

        # Determine batch label
        dates_for_adset = adset_batch_order.get(adset_id, [])
        if len(dates_for_adset) > 1:
            batch_num = dates_for_adset.index(date_str) + 1
            batch_label = f"{adset_name} batch {batch_num}"
        else:
            batch_label = adset_name

        created_dt = parse_created_time(group_ads[0].get("created_time", ""))
        dias = (now - created_dt).days if created_dt else 0

        ads_count = len(group_ads)
        
        # USE LIFETIME DATA for batch evaluation (not period data)
        # This gives the true historical performance of the batch
        gasto_total = sum(a.get("lifetime_spend", a.get("spend", 0)) for a in group_ads)
        valor_total = sum(a.get("lifetime_conversion_value", a.get("conversion_value", 0)) for a in group_ads)
        
        # Calculate aggregated ROAS for the batch using lifetime data
        mejor_roas = round(valor_total / gasto_total, 2) if gasto_total > 0 else 0

        # Format date
        fecha = created_dt.strftime("%d %b") if created_dt else date_str

        # EVALUATION LOGIC:
        # First check if adset is deactivated
        if not is_active:
            # Deactivated batches: Show with their historical results
            # They can still be hit/miss if they had performance before deactivation
            if gasto_total > 0 and roas_objetivo:
                # Check using LIFETIME ROAS of ads
                any_hit = any(a.get("lifetime_roas", a.get("roas", 0)) >= roas_objetivo for a in group_ads)
                estado = "desactivado"  # Show as deactivated but include ROAS data
                desactivado_count += 1
            else:
                # No spend/data - still show as desactivado
                estado = "desactivado"
                desactivado_count += 1
        else:
            # Active adset - check if evaluable
            is_evaluable = False
            if dias >= min_days and spend_threshold is not None:
                is_evaluable = gasto_total >= spend_threshold
            
            if is_evaluable:
                # Evaluable batch - determine Hit or Miss using lifetime ROAS
                any_hit = any(a.get("lifetime_roas", a.get("roas", 0)) >= roas_objetivo for a in group_ads) if roas_objetivo else False
                if any_hit:
                    estado = "hit"
                    hit_count += 1
                else:
                    estado = "miss"
                    miss_count += 1
            else:
                estado = "esperando"
                esperando_count += 1

        batches_result.append(BatchData(
            adset_name=adset_name,
            batch_label=batch_label,
            fecha=fecha,
            ads_count=ads_count,
            estado=estado,
            mejor_roas=round(mejor_roas, 2),
            dias=dias,
            gasto_total=round(gasto_total, 2),
        ))

    # Sort by most recent first
    batches_result.sort(key=lambda b: b.dias)

    # Hit Rate = Hits / (Hits + Misses) - "Esperando" excluded
    total_evaluable = hit_count + miss_count
    batch_hit_rate = round((hit_count / total_evaluable) * 100, 1) if total_evaluable > 0 else 0

    summary = BatchSummary(
        hit_rate=batch_hit_rate,
        hit_count=hit_count,
        miss_count=miss_count,
        esperando_count=esperando_count,
        total_evaluable=total_evaluable,
    )

    return batches_result, summary


# --- API Endpoints ---

@api_router.get("/")
async def root():
    return {"message": "Meta Ads Dashboard API"}


@api_router.get("/accounts")
async def list_accounts():
    """List all accounts (all, regardless of active state)."""
    accounts = await db.accounts.find({}, {"_id": 0}).to_list(100)
    for acc in accounts:
        # Mask per-account token
        t = acc.get("meta_token")
        acc["meta_token_masked"] = f"...{t[-8:]}" if t else None
        acc.pop("meta_token", None)
        # Ensure active field is always present
        if "active" not in acc:
            acc["active"] = False
        acc["active_source"] = "mongo"
    return accounts


class PatchAccountRequest(BaseModel):
    meta_token: Optional[str] = None
    active: Optional[bool] = None
    drive_folder_id: Optional[str] = None
    sheet_id: Optional[str] = None


@api_router.patch("/accounts/{account_id}")
async def patch_account(account_id: str, request: PatchAccountRequest):
    """Update per-account fields: meta_token and/or active."""
    account = await db.accounts.find_one({"id": account_id})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    updates = {}

    # Clientes en Notion es la única fuente de verdad para este estado.
    if request.active is not None:
        raise HTTPException(
            status_code=409,
            detail="El estado del cliente se administra únicamente en la database Clientes de Notion",
        )

    # Handle per-account token
    token = (request.meta_token or "").strip() if request.meta_token is not None else None
    if token:
        updates["meta_token"] = token
        
    if request.drive_folder_id is not None:
        updates["drive_folder_id"] = request.drive_folder_id
        
    if request.sheet_id is not None:
        updates["sheet_id"] = request.sheet_id

    if updates:
        await db.accounts.update_one({"id": account_id}, {"$set": updates})

    # Clear token if explicitly passed as empty string
    if request.meta_token is not None and not token:
        await db.accounts.update_one({"id": account_id}, {"$unset": {"meta_token": ""}})

    result = {"message": "Cuenta actualizada"}
    if token:
        result["masked"] = f"...{token[-8:]}"
    return result


class DisplayNameUpdate(BaseModel):
    displayName: str

@api_router.patch("/accounts/{account_id}/display-name")
async def update_account_display_name(account_id: str, body: DisplayNameUpdate):
    """Update custom display name for an account."""
    result = await db.accounts.update_one(
        {"id": account_id},
        {"$set": {"display_name": body.displayName}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"message": "Display name updated", "display_name": body.displayName}


class AddAccountRequest(BaseModel):
    account_id: str
    display_name: Optional[str] = None
    roas_objetivo: float = 4.5


@api_router.post("/accounts")
async def add_account(request: AddAccountRequest):
    """Add a new ad account. Validates against Meta API."""
    account_id = request.account_id.strip()
    
    # Validate format
    if not account_id.startswith("act_"):
        raise HTTPException(status_code=400, detail="El ID debe comenzar con 'act_'")
    
    # Check if already exists
    existing = await db.accounts.find_one({"id": account_id})
    if existing:
        raise HTTPException(status_code=400, detail="Esta cuenta ya está agregada")
    
    # Validate against Meta API
    # Validate against Meta API
    access_token = await get_meta_access_token()
    if not access_token:
        raise HTTPException(status_code=500, detail="Meta API not configured. Please go to Settings.")
    
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://graph.facebook.com/v21.0/{account_id}",
                params={
                    "access_token": access_token,
                    "fields": "name,currency,account_status"
                },
                timeout=10
            )
        
        if resp.status_code == 403:
            raise HTTPException(
                status_code=403, 
                detail="No tienes permisos para esta cuenta. Asegúrate de haber dado acceso al System User."
            )
        elif resp.status_code == 404 or (resp.status_code == 400 and "Invalid" in resp.text):
            raise HTTPException(
                status_code=404, 
                detail="ID de cuenta inválido. Verifica el formato (debe ser act_xxxxx)"
            )
        elif resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code, 
                detail="Error al conectar con Meta. Intenta nuevamente."
            )
        
        meta_data = resp.json()
        meta_name = meta_data.get("name", account_id)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Meta API error: {e}")
        raise HTTPException(status_code=500, detail="Error al conectar. Intenta nuevamente en un momento.")
    
    # Save to database
    display_name = request.display_name or meta_name
    await db.accounts.insert_one({
        "id": account_id,
        "name": display_name,
        "meta_account_id": account_id,
        "roas_objetivo": request.roas_objetivo,
        "last_refreshed": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    
    return {"message": "Cuenta agregada exitosamente", "account_id": account_id, "name": display_name}


@api_router.delete("/accounts/{account_id}")
async def delete_account(account_id: str):
    """Delete an ad account."""
    result = await db.accounts.delete_one({"id": account_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Also delete cached data
    await db.meta_cache.delete_many({"account_id": account_id})
    
    return {"message": "Cuenta eliminada", "account_id": account_id}


@api_router.get("/overview")
async def get_overview(period: int = 30, since: str = None, until: str = None):
    """Get summary cards for active accounts only. Parallel async fetch with 5-min cache."""
    # Only show accounts the user has explicitly activated
    accounts = await db.accounts.find({"active": True}, {"_id": 0}).to_list(100)
    if not accounts:
        # Fallback: if no accounts are active yet, return empty list
        return []
    now = datetime.now(timezone.utc)

    use_custom_range = since and until
    cache_key_period = f"custom_{since}_{until}" if use_custom_range else period

    async def build_card_for_account(account):
        account_id = account["id"]
        meta_account_id = account.get("meta_account_id")
        roas_objetivo = account.get("roas_objetivo")

        cache = await db.meta_cache.find_one(
            {"account_id": account_id, "cache_type": "combined", "period": cache_key_period}, {"_id": 0}
        )
        is_stale = _is_stale(cache, CACHE_TTL_INSIGHTS)

        stale_warning = None
        cache_age_minutes = None

        if not cache or is_stale:
            try:
                if use_custom_range:
                    data = await refresh_account_data_custom(account_id, meta_account_id, since, until)
                else:
                    data = await refresh_account_data(account_id, meta_account_id, period)
                cache = {"data": data}
            except Exception as e:
                error_msg = str(e)
                logger.error(f"❌ REFRESH FAILED for {account['name']} ({account_id}): {error_msg}")
                if cache:
                    # Calculate how old the cache is
                    fetched_at = cache.get("fetched_at")
                    if fetched_at:
                        cache_dt = parse_created_time(fetched_at)
                        if cache_dt:
                            age_minutes = int((now - cache_dt).total_seconds() / 60)
                            cache_age_minutes = age_minutes
                            if age_minutes > 60:
                                age_hours = age_minutes // 60
                                if age_hours > 24:
                                    age_days = age_hours // 24
                                    stale_warning = f"Datos de hace {age_days} día{'s' if age_days > 1 else ''} (refresh falló)"
                                else:
                                    stale_warning = f"Datos de hace {age_hours}h (refresh falló)"
                            logger.error(f"⚠️ Using STALE cache for {account['name']} — cache age: {age_minutes} minutes. Error: {error_msg[:200]}")
                    else:
                        stale_warning = "Datos desactualizados (refresh falló)"
                        logger.error(f"⚠️ Using STALE cache for {account['name']} — unknown age. Error: {error_msg[:200]}")
                else:
                    error_type = None
                    if "grant ads_management or ads_read" in error_msg.lower() or \
                       ("error" in error_msg.lower() and "200" in error_msg and "permission" in error_msg.lower()):
                        error_type = "permission_denied"
                        await db.accounts.update_one(
                            {"id": account_id},
                            {"$set": {"last_error": error_type, "last_error_at": now.isoformat()}}
                        )
                    return AccountCardData(
                        id=account_id, name=account["name"],
                        display_name=account.get("display_name"),
                        roas_actual=0, roas_objetivo=roas_objetivo,
                        total_facturacion=0, total_spend=0, ventas=0,
                        adsets_fuera=0, total_active_adsets=0, budget_fuera=0,
                        error=error_msg[:100], error_type=error_type,
                    )

        campaigns = cache["data"]["campaigns"]
        adsets = cache["data"]["adsets"]
        ads = cache["data"]["ads"]
        acct_insights = cache["data"].get("account_insights", {})

        if acct_insights.get("spend", 0) > 0:
            roas_actual = acct_insights["roas"]
            total_facturacion = acct_insights["conversion_value"]
            total_spend = acct_insights["spend"]
            ventas = acct_insights["conversions"]
        else:
            total_spend = sum(a.get("spend", 0) for a in ads)
            total_facturacion = sum(a.get("conversion_value", 0) for a in ads)
            roas_actual = round(total_facturacion / total_spend, 2) if total_spend > 0 else 0
            ventas = sum(a.get("conversions", 0) for a in ads)

        active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
        camp_map = {c["id"]: c for c in campaigns}
        adsets_fuera = 0
        budget_fuera = 0.0
        total_daily_budget = 0.0
        cbo_review_needed = False
        cbo_campaigns_counted = set()

        for adset in active_adsets:
            camp = camp_map.get(adset.get("campaign_id", ""))
            if camp and camp.get("budget_optimization") == "CBO":
                camp_id = camp["id"]
                if camp_id not in cbo_campaigns_counted:
                    cbo_campaigns_counted.add(camp_id)
                    total_daily_budget += camp.get("daily_budget", 0)
            else:
                total_daily_budget += adset.get("daily_budget", 0)

        cbo_campaigns_counted_fuera = set()
        if roas_objetivo:
            for adset in active_adsets:
                adset_roas = adset.get("roas", 0)
                camp = camp_map.get(adset.get("campaign_id", ""))
                is_cbo = camp and camp.get("budget_optimization") == "CBO"
                if adset_roas < roas_objetivo:
                    adsets_fuera += 1
                    if is_cbo:
                        camp_id = camp["id"]
                        if camp.get("roas", 0) < roas_objetivo:
                            if camp_id not in cbo_campaigns_counted_fuera:
                                cbo_campaigns_counted_fuera.add(camp_id)
                                budget_fuera += camp.get("daily_budget", 0)
                        else:
                            cbo_review_needed = True
                    else:
                        budget_fuera += adset.get("daily_budget", 0)

        ultimo_date = None
        ultimo_days = None
        for ad in ads:
            dt = parse_created_time(ad.get("created_time", ""))
            if dt and (ultimo_date is None or dt > ultimo_date):
                ultimo_date = dt
        if ultimo_date:
            ultimo_days = (now - ultimo_date).days
            ultimo_date_str = ultimo_date.strftime("%d %b")
        else:
            ultimo_date_str = None

        return AccountCardData(
            id=account_id, name=account["name"],
            display_name=account.get("display_name"),
            roas_actual=round(roas_actual, 2), roas_objetivo=roas_objetivo,
            total_facturacion=round(total_facturacion, 2),
            total_spend=round(total_spend, 2),
            ventas=ventas,
            adsets_fuera=adsets_fuera,
            total_active_adsets=len(active_adsets),
            budget_fuera=round(budget_fuera, 2),
            total_daily_budget=round(total_daily_budget, 2),
            ultimo_test_date=ultimo_date_str,
            ultimo_test_days=ultimo_days,
            cbo_review_needed=cbo_review_needed,
            stale_warning=stale_warning,
            cache_age_minutes=cache_age_minutes,
        )

    # ── Throttled fetch: max 2 accounts hitting Meta simultaneously ──
    # Without this, 5 accounts × 3 requests = 15 concurrent calls → code 17 rate limit
    sem = asyncio.Semaphore(2)

    async def throttled_card(account):
        async with sem:
            return await build_card_for_account(account)

    results = await asyncio.gather(
        *[throttled_card(acc) for acc in accounts],
        return_exceptions=True,
    )
    cards = [r for r in results if isinstance(r, AccountCardData)]
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            acc = accounts[i]
            cards.append(AccountCardData(
                id=acc["id"], name=acc["name"],
                roas_actual=0, total_facturacion=0, total_spend=0,
                ventas=0, adsets_fuera=0, total_active_adsets=0, budget_fuera=0,
                error=str(r)[:200],
            ))

    def urgency(c):
        if c.error:
            return (999, 0)
        fuera_score = -(c.adsets_fuera or 0)
        roas_gap = (c.roas_actual - (c.roas_objetivo or 0)) if c.roas_objetivo else 0
        return (fuera_score, roas_gap)
    cards.sort(key=urgency)

    return OverviewResponse(cards=cards)


@api_router.get("/accounts/{account_id}/dashboard")
async def get_dashboard(account_id: str, period: int = 30, include_paused: bool = False):
    """Get full dashboard data for an account. Fetches from Meta API if cache is stale."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Only serve data for accounts the user has explicitly activated
    if not account.get("active", False):
        raise HTTPException(
            status_code=403,
            detail="Cuenta no activada. Activala en Configuración → Cuentas activas para ver sus datos."
        )

    roas_objetivo = account.get("roas_objetivo")
    meta_account_id = account.get("meta_account_id")

    cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "combined", "period": period}, {"_id": 0}
    )

    is_stale = _is_stale(cache, CACHE_TTL_INSIGHTS)

    if not cache or is_stale:
        try:
            data = await refresh_account_data(account_id, meta_account_id, period)
            cache = {"data": data, "fetched_at": datetime.now(timezone.utc).isoformat()}
            # Update account last_refreshed for display
            account["last_refreshed"] = cache["fetched_at"]
        except Exception as e:
            if not cache:
                raise HTTPException(status_code=502, detail=f"Failed to fetch Meta data: {str(e)}")
            logger.warning(f"Meta API failed, using stale cache: {e}")

    campaigns = cache["data"]["campaigns"]
    adsets = cache["data"]["adsets"]
    ads = cache["data"]["ads"]
    account_insights = cache["data"].get("account_insights", {})

    # Overview calculations - use account-level insights for accuracy
    active_ads = [a for a in ads if a.get("status") == "ACTIVE"]
    total_ads_activos = len(active_ads)

    # Use account-level totals (most accurate, avoids double-counting)
    if account_insights.get("spend", 0) > 0:
        total_spend = account_insights["spend"]
        total_value = account_insights["conversion_value"]
        roas_actual = account_insights["roas"]
    else:
        # Fallback: aggregate from ads
        total_spend = sum(a.get("spend", 0) for a in ads)
        total_value = sum(a.get("conversion_value", 0) for a in ads)
        roas_actual = round(total_value / total_spend, 2) if total_spend > 0 else 0

    cpa_promedio = None
    avg_cpa = calculate_account_avg_cpa(account_insights)
    
    # Calculate spend threshold for "waiting" rule: 3× CPA
    spend_threshold = (avg_cpa * 3) if avg_cpa and avg_cpa > 0 else None

    # Adsets fuera de ROAS calculation with proper CBO handling
    # NEW: Apply "waiting" rule - adsets < 5 days old OR < 3×CPA spend are not counted as "fuera"
    active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
    total_active_adsets = len(active_adsets)
    adsets_fuera = 0
    adsets_esperando = 0
    adsets_fuera_budget = 0.0
    cbo_adsets_review = 0  # CBO adsets that need distribution review
    
    # Track which CBO campaigns we've already processed for budget
    cbo_campaigns_processed = set()
    now = datetime.now(timezone.utc)

    if roas_objetivo:
        cpa_promedio = calculate_cpa_promedio(ads, roas_objetivo)
        camp_map = {c["id"]: c for c in campaigns}

        for adset in active_adsets:
            adset_roas = adset.get("roas", 0)
            adset_spend = adset.get("spend", 0)
            
            # Check if adset is "evaluable" (meets waiting criteria)
            # For simplicity, we use spend threshold since we don't have adset creation date
            # If adset has insufficient spend, it's "esperando", not "fuera"
            is_evaluable = True
            if spend_threshold:
                is_evaluable = adset_spend >= spend_threshold
            
            if adset_roas < roas_objetivo:
                if not is_evaluable:
                    # Adset hasn't met evaluation criteria - still "waiting"
                    adsets_esperando += 1
                    continue
                    
                adsets_fuera += 1
                
                camp = camp_map.get(adset.get("campaign_id", ""))
                is_cbo = camp and camp.get("budget_optimization") == "CBO"
                
                if is_cbo:
                    camp_id = camp["id"]
                    camp_roas = camp.get("roas", 0)
                    
                    if camp_roas < roas_objetivo:
                        # CBO campaign is underperforming - count budget once
                        if camp_id not in cbo_campaigns_processed:
                            cbo_campaigns_processed.add(camp_id)
                            adsets_fuera_budget += camp.get("daily_budget", 0)
                    else:
                        # CBO campaign is OK but this adset is not - review needed, $0 budget
                        cbo_adsets_review += 1
                else:
                    # ABO adset - count its budget directly
                    adsets_fuera_budget += adset.get("daily_budget", 0)

    adsets_fuera_pct = round((adsets_fuera / total_active_adsets) * 100, 1) if total_active_adsets > 0 else 0

    # Budget calculations
    budget_total = 0
    budget_fuera = 0
    budget_dentro = 0
    items_fuera = []
    if roas_objetivo:
        budget_total, budget_fuera, budget_dentro, items_fuera = calculate_budget_disponible(campaigns, adsets, roas_objetivo)

    # Top ads (with adset name) - based on conversions in this period
    top_ads_list = get_top_ads(ads, adsets, include_paused)
    top_ads_response = [
        TopAd(
            ad_name=a["name"],
            adset_name=a.get("adset_name", "Unknown"),
            ventas=a.get("conversions", 0),
            roas=a.get("roas", 0),
            facturacion=a.get("conversion_value", 0),
            gasto=a.get("spend", 0),
            status="Activo" if a.get("status") == "ACTIVE" else "Pausado"
        )
        for a in top_ads_list
    ]

    # Losing ads (low ROAS with significant spend)
    losing_ads_list = get_losing_ads(ads, adsets, roas_objetivo, include_paused)
    losing_ads_response = [
        LosingAd(
            ad_name=a["name"],
            adset_name=a.get("adset_name", "Unknown"),
            ventas=a.get("conversions", 0),
            roas=a.get("roas", 0),
            gasto=a.get("spend", 0),
            status="Activo" if a.get("status") == "ACTIVE" else "Pausado"
        )
        for a in losing_ads_list
    ]

    # Active adsets (all, not just top 5)
    active_adsets_response = get_all_active_adsets(adsets, ads, campaigns, roas_objetivo)

    # Batch calculations - uses account CPA for threshold
    # CRITICAL: Batches need LIFETIME data, not period data
    batches_list = []
    batch_summary = BatchSummary(hit_rate=0, hit_count=0, miss_count=0, esperando_count=0, total_evaluable=0)
    if roas_objetivo:
        # Calculate average CPA from account insights (last 30 days data)
        avg_cpa = calculate_account_avg_cpa(account_insights)
        
        # OPTIMIZATION: Only fetch lifetime for ads created in last 60 days (batch window)
        # This reduces API calls from ~1200 to ~100-200 ads typically
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=60)
        ads_for_batches = []
        for a in ads:
            created_str = a.get("created_time", "")
            if created_str:
                created_dt = parse_created_time(created_str)
                if created_dt and created_dt >= cutoff_date:
                    ads_for_batches.append(a["id"])
        
        lifetime_data = {}
        if ads_for_batches:
            try:
                access_token = await get_access_token_for_account(account_id)
                lifetime_data = await fetch_ads_lifetime_insights(
                    account["meta_account_id"],
                    access_token,
                    ads_for_batches
                )
            except Exception as e:
                logger.warning(f"Failed to fetch lifetime insights, using period data: {e}")
        
        # Merge lifetime data with ads for batch calculation
        ads_with_lifetime = []
        for ad in ads:
            ad_copy = dict(ad)
            if ad["id"] in lifetime_data:
                lt = lifetime_data[ad["id"]]
                ad_copy["lifetime_spend"] = lt["spend"]
                ad_copy["lifetime_conversions"] = lt["conversions"]
                ad_copy["lifetime_conversion_value"] = lt["conversion_value"]
                ad_copy["lifetime_roas"] = lt["roas"]
            else:
                # Fallback to period data if lifetime not available
                ad_copy["lifetime_spend"] = ad.get("spend", 0)
                ad_copy["lifetime_conversions"] = ad.get("conversions", 0)
                ad_copy["lifetime_conversion_value"] = ad.get("conversion_value", 0)
                ad_copy["lifetime_roas"] = ad.get("roas", 0)
            ads_with_lifetime.append(ad_copy)
        
        batches_list, batch_summary = calculate_batches(ads_with_lifetime, adsets, roas_objetivo, period, avg_cpa)

    # Prepare campaigns response for flow chart
    campaigns_response = [
        CampaignData(
            id=c["id"],
            name=c["name"],
            budget_optimization=c.get("budget_optimization", "ABO"),
            daily_budget=c.get("daily_budget", 0),
            status=c.get("status", "UNKNOWN"),
            spend=c.get("spend", 0),
            conversions=c.get("conversions", 0),
            conversion_value=c.get("conversion_value", 0),
            roas=c.get("roas", 0),
        )
        for c in campaigns
    ]

    return DashboardResponse(
        account=AccountResponse(**account),
        overview=DashboardOverview(
            roas_actual=roas_actual,
            roas_objetivo=roas_objetivo,
            total_ads_activos=total_ads_activos,
            total_facturacion=round(total_value, 2),
            total_spend=round(total_spend, 2),
            adsets_fuera_de_roas=adsets_fuera,
            adsets_fuera_percentage=adsets_fuera_pct,
            adsets_fuera_daily_budget=round(adsets_fuera_budget, 2),
            total_active_adsets=total_active_adsets,
            hit_rate=batch_summary.hit_rate,
            hit_count=batch_summary.hit_count,
            evaluable_count=batch_summary.total_evaluable,
            cpa_promedio=round(cpa_promedio, 2) if cpa_promedio else None,
            avg_cpa=round(avg_cpa, 2) if avg_cpa else None,
            cbo_adsets_review=cbo_adsets_review,
        ),
        budget=BudgetData(
            budget_diario_total=budget_total,
            budget_fuera_de_roas=budget_fuera,
            budget_dentro_de_roas=budget_dentro,
            items_fuera=[BudgetItemFuera(**item) for item in items_fuera],
        ),
        top_ads=top_ads_response,
        losing_ads=losing_ads_response,
        active_adsets=active_adsets_response,
        batches=batches_list,
        batch_summary=batch_summary,
        campaigns=campaigns_response,
        recent_uploads=get_recent_ad_uploads(ads, adsets),
        cached_at=cache.get("fetched_at"),
    )


CLIENT_DASHBOARD_DEFAULT_ACCOUNT = "act_2699461910320121"


def _date_from_iso(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Fecha inválida. Usá formato YYYY-MM-DD.")


def _client_dashboard_range(range_key: str, since: Optional[str], until: Optional[str]):
    today = datetime.now(timezone.utc).date()
    if range_key == "custom":
        if not since or not until:
            raise HTTPException(status_code=400, detail="Custom requiere since y until.")
        start = _date_from_iso(since)
        end = _date_from_iso(until)
        if end < start:
            raise HTTPException(status_code=400, detail="until no puede ser anterior a since.")
    else:
        days_map = {"7d": 7, "14d": 14, "30d": 30}
        if range_key not in days_map:
            raise HTTPException(status_code=400, detail="Rango inválido. Usá 7d, 14d, 30d o custom.")
        days = days_map[range_key]
        end = today
        start = today - timedelta(days=days - 1)

    days_count = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days_count - 1)
    return {
        "current": {"since": start.isoformat(), "until": end.isoformat(), "days": days_count},
        "previous": {"since": prev_start.isoformat(), "until": prev_end.isoformat(), "days": days_count},
    }


def _sum_metric(rows, key):
    return sum(float(row.get(key, 0) or 0) for row in rows)


def _weighted_metric(rows, key, weight_key):
    total_weight = _sum_metric(rows, weight_key)
    if total_weight <= 0:
        return 0
    return round(sum(float(row.get(key, 0) or 0) * float(row.get(weight_key, 0) or 0) for row in rows) / total_weight, 2)


def _aggregate_client_metrics(rows):
    spend = _sum_metric(rows, "spend")
    purchases = int(_sum_metric(rows, "purchases"))
    purchase_value = _sum_metric(rows, "purchase_value")
    impressions = int(_sum_metric(rows, "impressions"))
    landing_views = int(_sum_metric(rows, "landing_page_views"))
    link_clicks = int(_sum_metric(rows, "link_clicks"))
    add_to_cart = int(_sum_metric(rows, "add_to_cart"))
    checkout = int(_sum_metric(rows, "initiate_checkout"))
    return {
        "purchases": purchases,
        "purchase_value": round(purchase_value, 2),
        "roas": round(purchase_value / spend, 2) if spend else 0,
        "spend": round(spend, 2),
        "cost_per_result": round(spend / purchases, 2) if purchases else 0,
        "frequency": _weighted_metric(rows, "frequency", "reach"),
        "ctr_link": round(link_clicks / impressions * 100, 2) if impressions else 0,
        "landing_page_views": landing_views,
        "purchase_landing_rate": round(purchases / landing_views * 100, 2) if landing_views else 0,
        "cpm": round(spend / impressions * 1000, 2) if impressions else 0,
        "reach": int(_sum_metric(rows, "reach")),
        "cost_per_landing_view": round(spend / landing_views, 2) if landing_views else 0,
        "cost_per_add_to_cart": round(spend / add_to_cart, 2) if add_to_cart else 0,
        "cost_per_initiate_checkout": round(spend / checkout, 2) if checkout else 0,
        "add_to_cart": add_to_cart,
        "initiate_checkout": checkout,
        "video_3s": int(_sum_metric(rows, "video_3s")),
        "instagram_follows": int(_sum_metric(rows, "instagram_follows")),
        "comments": int(_sum_metric(rows, "comments")),
        "reactions": int(_sum_metric(rows, "reactions")),
        "link_clicks": link_clicks,
        "impressions": impressions,
    }


def _delta(current, previous):
    if previous == 0:
        return None if current == 0 else 100.0
    return round(((current - previous) / abs(previous)) * 100, 1)


def _kpi(label, key, current_metrics, previous_metrics, fmt="number"):
    return {
        "label": label,
        "key": key,
        "value": current_metrics.get(key, 0),
        "previous": previous_metrics.get(key, 0),
        "delta_pct": _delta(current_metrics.get(key, 0), previous_metrics.get(key, 0)),
        "format": fmt,
    }


def _merge_ad_structure(ad_rows, ad_structure):
    merged = []
    for ad in ad_rows:
        structure = ad_structure.get(str(ad.get("id")), {})
        merged.append({**ad, **{k: v for k, v in structure.items() if k not in ("id", "name")}})
    return merged


def _top_ads_by_revenue(ads, limit=10):
    filtered = [ad for ad in ads if ad.get("purchase_value", 0) > 0 or ad.get("spend", 0) > 0]
    filtered.sort(key=lambda ad: ad.get("purchase_value", 0), reverse=True)
    return [
        {
            "name": ad.get("name", "Unnamed Ad"),
            "adset": ad.get("adset_name", "Unknown"),
            "purchases": ad.get("purchases", 0),
            "roas": ad.get("roas", 0),
            "spend": ad.get("spend", 0),
            "ctr_link": ad.get("ctr_link", 0),
            "purchase_value": ad.get("purchase_value", 0),
            "status": ad.get("status", "UNKNOWN"),
        }
        for ad in filtered[:limit]
    ]


def _format_distribution(ads):
    buckets = {
        "image": {"format": "Imagen", "ads": 0, "spend": 0},
        "video": {"format": "Video", "ads": 0, "spend": 0},
    }
    for ad in ads:
        fmt = ad.get("format") if ad.get("format") in buckets else "image"
        buckets[fmt]["ads"] += 1
        buckets[fmt]["spend"] = round(buckets[fmt]["spend"] + float(ad.get("spend", 0) or 0), 2)
    return list(buckets.values())


def _pareto_summary(ads):
    revenue_ads = [ad for ad in ads if ad.get("purchase_value", 0) > 0]
    revenue_ads.sort(key=lambda ad: ad.get("purchase_value", 0), reverse=True)
    total_revenue = _sum_metric(revenue_ads, "purchase_value")
    total_spend = _sum_metric(ads, "spend")
    if total_revenue <= 0:
        return {"ads_count": 0, "budget_share": 0, "revenue_share": 0, "text": "Sin ingresos atribuidos en el período."}
    target = total_revenue * 0.8
    running_revenue = 0
    running_spend = 0
    count = 0
    for ad in revenue_ads:
        running_revenue += float(ad.get("purchase_value", 0) or 0)
        running_spend += float(ad.get("spend", 0) or 0)
        count += 1
        if running_revenue >= target:
            break
    budget_share = round(running_spend / total_spend * 100, 1) if total_spend else 0
    revenue_share = round(running_revenue / total_revenue * 100, 1) if total_revenue else 0
    return {
        "ads_count": count,
        "budget_share": budget_share,
        "revenue_share": revenue_share,
        "text": f"{count} ads generan el {revenue_share}% de ingresos y reciben el {budget_share}% del budget.",
    }


def _recent_changes(ads, limit=12):
    rows = []
    for ad in ads:
        created = parse_created_time(ad.get("created_time"))
        if not created:
            continue
        rows.append({
            "name": ad.get("name", "Unnamed Ad"),
            "adset": ad.get("adset_name", "Unknown"),
            "campaign": ad.get("campaign_name", "Unknown"),
            "status": "Activo" if ad.get("status") == "ACTIVE" else "Pausado",
            "created_at": created.date().isoformat() if created else "",
            "updated_at": created.date().isoformat(),
            "sort": created,
        })
    rows.sort(key=lambda row: row["sort"], reverse=True)
    return [{k: v for k, v in row.items() if k != "sort"} for row in rows[:limit]]


def _fatigue_signals(current_ads, previous_ads):
    previous_by_id = {ad.get("id"): ad for ad in previous_ads}
    signals = []
    for ad in current_ads:
        previous = previous_by_id.get(ad.get("id"))
        if not previous:
            continue
        if ad.get("impressions", 0) < 500 or previous.get("impressions", 0) < 500:
            continue
        previous_ctr = previous.get("ctr_link", 0)
        current_ctr = ad.get("ctr_link", 0)
        if previous_ctr > 0.5 and current_ctr < previous_ctr * 0.8:
            signals.append({
                "name": ad.get("name", "Unnamed Ad"),
                "adset": ad.get("adset_name", "Unknown"),
                "frequency": ad.get("frequency", 0),
                "ctr_previous": previous_ctr,
                "ctr_current": current_ctr,
                "roas_current": ad.get("roas", 0),
                "spend": ad.get("spend", 0),
            })
    signals.sort(key=lambda item: item["spend"], reverse=True)
    return signals[:8]


def _series_from_daily(daily):
    return [
        {
            "date": row.get("date"),
            "roas": row.get("roas", 0),
            "purchases": row.get("purchases", 0),
            "purchase_value": row.get("purchase_value", 0),
            "spend": row.get("spend", 0),
        }
        for row in daily
    ]


def _funnel(metrics):
    stages = [
        ("Visitas a landing", metrics.get("landing_page_views", 0)),
        ("Agregados al carrito", metrics.get("add_to_cart", 0)),
        ("Pagos iniciados", metrics.get("initiate_checkout", 0)),
        ("Compras", metrics.get("purchases", 0)),
    ]
    result = []
    previous = None
    for label, value in stages:
        drop = None
        conversion = None
        if previous and previous > 0:
            conversion = round(value / previous * 100, 1)
            drop = round(100 - conversion, 1)
        result.append({"label": label, "value": value, "conversion_from_previous": conversion, "drop_pct": drop})
        previous = value
    return result


@api_router.get("/client-dashboard/meta")
async def get_client_meta_dashboard(
    account_id: str = CLIENT_DASHBOARD_DEFAULT_ACCOUNT,
    range: str = Query("7d", pattern="^(7d|14d|30d|custom)$"),
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Read-only client dashboard for Tienda Canela Meta Ads."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.get("active", False):
        raise HTTPException(status_code=403, detail="Cuenta no activada.")

    ranges = _client_dashboard_range(range, since, until)
    period_key = f"{ranges['current']['since']}_{ranges['current']['until']}"
    previous_key = f"{ranges['previous']['since']}_{ranges['previous']['until']}"
    cache_type = "client_meta_dashboard"
    access_token = await get_access_token_for_account(account_id)
    if not access_token:
        raise HTTPException(status_code=500, detail="Meta API access token no configurado.")

    async def load_period(key, date_range):
        cache = await db.meta_cache.find_one(
            {"account_id": account_id, "cache_type": cache_type, "period": key}, {"_id": 0}
        )
        if _is_stale(cache, CACHE_TTL_INSIGHTS):
            data = await fetch_client_dashboard_data(
                account.get("meta_account_id", account_id),
                access_token,
                date_range["since"],
                date_range["until"],
            )
            fetched_at = await _save_cache(cache_type, account_id, key, data)
            return data, fetched_at
        return cache["data"], cache.get("fetched_at")

    current_data, fetched_at = await load_period(period_key, ranges["current"])
    previous_data, _previous_fetched_at = await load_period(previous_key, ranges["previous"])

    current_ads = _merge_ad_structure(current_data.get("ads", []), current_data.get("ad_structure", {}))
    previous_ads = _merge_ad_structure(previous_data.get("ads", []), previous_data.get("ad_structure", {}))
    current_metrics = _aggregate_client_metrics(current_data.get("daily", []))
    previous_metrics = _aggregate_client_metrics(previous_data.get("daily", []))

    return {
        "account": {
            "id": account.get("id"),
            "name": account.get("display_name") or account.get("name"),
            "roas_objetivo": account.get("roas_objetivo"),
        },
        "range": ranges,
        "cached_at": fetched_at,
        "kpis": [
            _kpi("Compras", "purchases", current_metrics, previous_metrics),
            _kpi("Ingresos", "purchase_value", current_metrics, previous_metrics, "currency"),
            _kpi("ROAS", "roas", current_metrics, previous_metrics, "ratio"),
            _kpi("Inversión", "spend", current_metrics, previous_metrics, "currency"),
            _kpi("CPM", "cpm", current_metrics, previous_metrics, "currency"),
            _kpi("Visitas al sitio", "landing_page_views", current_metrics, previous_metrics),
            _kpi("CTR link", "ctr_link", current_metrics, previous_metrics, "percent"),
            _kpi("Frecuencia", "frequency", current_metrics, previous_metrics, "ratio"),
        ],
        "metrics": current_metrics,
        "previous_metrics": previous_metrics,
        "series": _series_from_daily(current_data.get("daily", [])),
        "funnel": _funnel(current_metrics),
        "top_ads": _top_ads_by_revenue(current_ads),
        "format_distribution": _format_distribution(current_ads),
        "pareto": _pareto_summary(current_ads),
        "recent_changes": _recent_changes(current_ads),
        "fatigue_signals": _fatigue_signals(current_ads, previous_ads),
        "engagement": {
            "instagram_follows": current_metrics.get("instagram_follows", 0),
            "comments": current_metrics.get("comments", 0),
            "reactions": current_metrics.get("reactions", 0),
            "video_3s": current_metrics.get("video_3s", 0),
        },
        "read_only": True,
    }


CREATOR_PRODUCT_ALIASES = {
    "Candy": ["candy"],
    "Fancy": ["fancy"],
    "Lolita": ["lolita"],
    "Ritual": ["ritual"],
    "Sophie": ["sophie"],
    "Sugar": ["sugar"],
    "Trópico": ["tropico", "trópico"],
    "Wanda": ["wanda"],
    "Divine": ["divine"],
    "Gaia": ["gaia"],
    "Glam": ["glam"],
    "Iconic": ["iconic"],
    "Pietra": ["pietra"],
    "Venecia": ["venecia", "venezia"],
    "Leona": ["leona"],
    "Savage": ["savage"],
    "Bali": ["bali"],
    "Milán": ["milan", "milán"],
    "Mónaco": ["monaco", "mónaco"],
    "New York": ["new york", "newyork", "ny"],
    "Body Reductor (Slim)": ["body reductor", "bodyreductor", "slim", "body slim"],
    "Nocturna": ["nocturna"],
    "Trampa": ["trampa"],
    "Éxtasis": ["extasis", "éxtasis", "extasis"],
    "Peach": ["peach"],
    "Deseo": ["deseo"],
    "Mamba": ["mamba"],
    "Sassy": ["sassy"],
    "Roma": ["roma"],
    "Agatha": ["agatha", "agata", "ágatha"],
    "Medusa": ["medusa"],
    "Amazonas": ["amazonas"],
    "Romance": ["romance"],
    "Caeli": ["caeli"],
    "Gianni": ["gianni"],
    "Diana": ["diana"],
    "Obsesión": ["obsesion", "obsesión"],
    "Bucaneras": ["bucaneras", "bucanera"],
}


def _normalize_text(value):
    value = str(value or "").lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9+]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _display_name_from_token(token):
    token = str(token or "").strip()
    if not token:
        return ""
    return token[:1].upper() + token[1:].lower()


def _tokenize_ad_name(ad_name):
    raw_tokens = [token for token in re.split(r"[\s\-_|\+]+", ad_name or "") if token]
    cleaned = []
    for token in raw_tokens:
        normalized = _normalize_text(token)
        if not normalized:
            continue
        if normalized in {"vid", "video", "beboteo"}:
            continue
        cleaned.append({"raw": token, "norm": normalized})
    return cleaned


def _token_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _find_product(tokens):
    normalized_tokens = [token["norm"] for token in tokens if token["norm"]]
    normalized_text = " ".join(normalized_tokens)
    best = None
    for product, aliases in CREATOR_PRODUCT_ALIASES.items():
        for alias in aliases:
            alias_norm = _normalize_text(alias)
            alias_parts = alias_norm.split()
            matched_indices = []
            confidence = 0.0
            if len(alias_parts) > 1:
                if alias_norm in normalized_text:
                    start = normalized_text.split().index(alias_parts[0]) if alias_parts[0] in normalized_tokens else -1
                    if start >= 0:
                        matched_indices = list(range(start, min(start + len(alias_parts), len(tokens))))
                    confidence = 1.0
            else:
                for idx, token in enumerate(normalized_tokens):
                    if token == alias_norm or (
                        len(token) >= 4 and len(alias_norm) >= 4 and (alias_norm in token or token in alias_norm)
                    ):
                        matched_indices = [idx]
                        confidence = 1.0
                        break
                    score = _token_similarity(token, alias_norm)
                    if len(token) >= 5 and len(alias_norm) >= 5 and score >= 0.9:
                        matched_indices = [idx]
                        confidence = score
                        break
            if matched_indices and (best is None or confidence > best["confidence"]):
                best = {"product": product, "indices": matched_indices, "confidence": confidence}

    if not best:
        return None, []

    products = [best["product"]]
    if "kit" in normalized_tokens:
        for product, aliases in CREATOR_PRODUCT_ALIASES.items():
            if product in products:
                continue
            for alias in aliases:
                alias_norm = _normalize_text(alias)
                if alias_norm in normalized_text:
                    products.append(product)
                    break
        if len(products) > 1:
            return "Kit " + " + ".join(products), best["indices"]

    return best["product"], best["indices"]


CREATOR_STOP_TOKENS = {
    "body", "medias", "ugc", "categoria", "category", "cat", "tolera", "audio",
    "demo", "unboxig", "unboxing", "paquetes", "que", "esperas",
    "copia", "textos", "overlay", "link", "viejo", "junio", "may26", "rpr",
    "features", "empoderamiento", "outfits", "reds", "push", "up", "x4",
    "enontrar", "encontrar", "rojo", "negro", "beige",
}

CREATOR_COMPOUND_FIRST_TOKENS = {"sofia", "celes", "flor", "lu", "camila", "maria", "ailu"}

CREATOR_NORMALIZATION = {
    "Celes Medonza": "Celes Mendoza",
    "Sofia Shiavo": "Sofia Schiavo",
    "Hoster": "hoster",
}

CREATOR_ALWAYS_BEBOTEO = {"Pau", "Sofia P", "Sofia S"}


def _is_creator_candidate(token):
    norm = token.get("norm", "")
    if not norm or norm in CREATOR_STOP_TOKENS:
        return False
    if norm in {"kit", "x", "y", "con"}:
        return False
    if norm.isdigit():
        return False
    return True


def _creator_name_from_tokens(tokens, start_idx, product_indices):
    first = tokens[start_idx]
    parts = [first["raw"]]
    next_idx = start_idx + 1
    if (
        first["norm"] in CREATOR_COMPOUND_FIRST_TOKENS
        and next_idx < len(tokens)
        and next_idx not in product_indices
        and _is_creator_candidate(tokens[next_idx])
    ):
        parts.append(tokens[next_idx]["raw"])
    name = " ".join(_display_name_from_token(part) for part in parts)
    return CREATOR_NORMALIZATION.get(name, name), set(range(start_idx, start_idx + len(parts)))


def _parse_creator_ad_name(ad_name):
    tokens = _tokenize_ad_name(ad_name)
    product, product_indices = _find_product(tokens)
    product_indices = set(product_indices)
    norm_tokens = [token["norm"] for token in tokens]

    if not product and "medias" in norm_tokens:
        product = "Medias"
        product_indices = {idx for idx, token in enumerate(tokens) if token["norm"] in {"medias", "push", "up", "reds"}}

    if not product and {"beige", "negro"}.issubset(set(norm_tokens)):
        product = "Leona"
        product_indices = {idx for idx, token in enumerate(tokens) if token["norm"] in {"beige", "negro"}}

    category_idx = next((idx for idx, token in enumerate(tokens) if token["norm"] in {"categoria", "category", "cat"}), None)
    if not product and category_idx is not None:
        ignored_after_category = {"audio", "ugc", "demo", "unboxig", "unboxing", "copia"}
        for idx in range(category_idx + 1, len(tokens)):
            token = tokens[idx]
            if token["norm"] in ignored_after_category or token["norm"].isdigit():
                continue
            product = _display_name_from_token(token["raw"])
            product_indices = {idx}
            break
        if not product:
            product = "Todos"
            product_indices = set()

    if not product and any(token["norm"] == "hoster" for token in tokens):
        product = "Todos"
        product_indices = set()

    if not product:
        return {
            "creator": None,
            "product": None,
            "category": "Sin clasificar",
            "confidence": "low",
            "tokens": [token["raw"] for token in tokens],
            "reason": "producto_no_identificado",
        }

    creator_idx = None
    for idx, token in enumerate(tokens):
        if idx in product_indices:
            continue
        if not _is_creator_candidate(token):
            continue
        creator_idx = idx
        break

    if creator_idx is None:
        return {
            "creator": None,
            "product": product,
            "category": "Sin clasificar",
            "confidence": "low",
            "tokens": [token["raw"] for token in tokens],
            "reason": "creadora_no_identificada",
        }

    creator, creator_indices = _creator_name_from_tokens(tokens, creator_idx, product_indices)
    leftovers = []
    ignored_norms = {"kit", "x", "y", "con", "ugc", "copia", "junio", "may26"}
    for idx, token in enumerate(tokens):
        if idx in creator_indices or idx in product_indices:
            continue
        if token["norm"] in ignored_norms or token["norm"].isdigit():
            continue
        leftovers.append(token["raw"])

    category = "Beboteo" if creator in CREATOR_ALWAYS_BEBOTEO else "Narrado" if leftovers else "Beboteo"
    return {
        "creator": creator,
        "product": product,
        "category": category,
        "confidence": "medium" if leftovers else "high",
        "tokens": [token["raw"] for token in tokens],
        "leftovers": leftovers,
        "reason": "",
    }


def _creator_metric_row(name, rows):
    spend = _sum_metric(rows, "spend")
    purchases = int(_sum_metric(rows, "purchases"))
    purchase_value = _sum_metric(rows, "purchase_value")
    active_ads = len(rows)
    oldest = None
    for row in rows:
        created = parse_created_time(row.get("created_time", ""))
        if created and (oldest is None or created < oldest):
            oldest = created
    return {
        "creator": name,
        "active_ads": active_ads,
        "purchases": purchases,
        "roas": round(purchase_value / spend, 2) if spend else 0,
        "spend": round(spend, 2),
        "purchase_value": round(purchase_value, 2),
        "oldest_active_ad_date": oldest.date().isoformat() if oldest else None,
        "oldest_active_ad_days": (datetime.now(timezone.utc) - oldest).days if oldest else None,
    }


def _top_creator_rows(rows, category=None, limit=5, filtered=False):
    grouped = {}
    for row in rows:
        if category and row.get("category") != category:
            continue
        grouped.setdefault(row["creator"], []).append(row)
    result = [_creator_metric_row(name, items) for name, items in grouped.items()]
    if filtered:
        result = [row for row in result if row["purchases"] > 10 and row["roas"] > 5]
    result.sort(key=lambda row: (row["purchases"], row["purchase_value"]), reverse=True)
    return result[:limit]


def _product_rows(rows, limit=5):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["product"], []).append(row)
    result = []
    for product, items in grouped.items():
        spend = _sum_metric(items, "spend")
        purchases = int(_sum_metric(items, "purchases"))
        purchase_value = _sum_metric(items, "purchase_value")
        result.append({
            "product": product,
            "active_ads": len(items),
            "purchases": purchases,
            "roas": round(purchase_value / spend, 2) if spend else 0,
            "spend": round(spend, 2),
            "purchase_value": round(purchase_value, 2),
        })
    result.sort(key=lambda row: row["purchases"], reverse=True)
    return result[:limit]


def _creators_table(rows, total_ads):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["creator"], []).append(row)
    table = []
    for creator, items in grouped.items():
        metric = _creator_metric_row(creator, items)
        metric["participation_pct"] = round(metric["active_ads"] / total_ads * 100, 1) if total_ads else 0
        table.append(metric)
    table.sort(key=lambda row: row["active_ads"], reverse=True)
    return table


def _forgotten_creators(rows):
    table = _creators_table(rows, len(rows))
    forgotten = [
        row for row in table
        if row["purchases"] < 10 and row.get("oldest_active_ad_days") is not None and row["oldest_active_ad_days"] > 14
    ]
    forgotten.sort(key=lambda row: row["oldest_active_ad_days"], reverse=True)
    return forgotten


@api_router.get("/client-dashboard/creators")
async def get_client_creators_dashboard(
    account_id: str = CLIENT_DASHBOARD_DEFAULT_ACCOUNT,
    range: str = Query("30d", pattern="^(7d|14d|30d|custom)$"),
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Read-only Creators module for active Evergreen ads."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.get("active", False):
        raise HTTPException(status_code=403, detail="Cuenta no activada.")

    ranges = _client_dashboard_range("30d", None, None)
    period_key = f"{ranges['current']['since']}_{ranges['current']['until']}"
    access_token = await get_access_token_for_account(account_id)
    if not access_token:
        raise HTTPException(status_code=500, detail="Meta API access token no configurado.")

    meta_account_id = account.get("meta_account_id", account_id)

    structure_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "client_creator_structure", "period": "_"}, {"_id": 0}
    )
    if _is_stale(structure_cache, CACHE_TTL_STRUCTURE):
        structure_ads = await fetch_client_creator_active_structure(meta_account_id, access_token)
        await _save_cache("client_creator_structure", account_id, "_", {"ads": structure_ads})
    else:
        structure_ads = structure_cache.get("data", {}).get("ads", [])

    insights_cache = await db.meta_cache.find_one(
        {"account_id": account_id, "cache_type": "client_creator_insights", "period": period_key}, {"_id": 0}
    )
    if _is_stale(insights_cache, CACHE_TTL_INSIGHTS):
        insight_ads = await fetch_client_ad_insights(
            meta_account_id,
            access_token,
            ranges["current"]["since"],
            ranges["current"]["until"],
        )
        fetched_at = await _save_cache("client_creator_insights", account_id, period_key, {"ads": insight_ads})
    else:
        insight_ads = insights_cache.get("data", {}).get("ads", [])
        fetched_at = insights_cache.get("fetched_at")

    insights_by_id = {str(ad.get("id")): ad for ad in insight_ads}
    classified = []
    unclassified = []
    for ad in structure_ads:
        metrics = insights_by_id.get(str(ad.get("id")), {})
        row = {
            **ad,
            "spend": metrics.get("spend", 0),
            "purchases": metrics.get("purchases", 0),
            "purchase_value": metrics.get("purchase_value", 0),
            "roas": metrics.get("roas", 0),
            "ctr_link": metrics.get("ctr_link", 0),
        }
        parsed = _parse_creator_ad_name(row.get("name", ""))
        row.update(parsed)
        if parsed.get("category") == "Sin clasificar":
            unclassified.append({
                "ad_name": row.get("name", ""),
                "adset": row.get("adset_name", ""),
                "campaign": row.get("campaign_name", ""),
                "reason": parsed.get("reason", ""),
                "tokens": parsed.get("tokens", []),
            })
            continue
        classified.append(row)

    total_ads = len(structure_ads)
    classified_total = len(classified)
    beboteo_count = len([row for row in classified if row["category"] == "Beboteo"])
    narrado_count = len([row for row in classified if row["category"] == "Narrado"])
    creators_detected = sorted({row["creator"] for row in classified})

    return {
        "account": {
            "id": account.get("id"),
            "name": account.get("display_name") or account.get("name"),
        },
        "range": ranges,
        "cached_at": fetched_at,
        "kpis": [
            {"label": "Creadoras identificadas", "value": len(creators_detected), "format": "number"},
            {"label": "Creadoras activas", "value": len(creators_detected), "format": "number"},
            {"label": "Ads Evergreen activos", "value": total_ads, "format": "number"},
            {
                "label": "Beboteo vs Narrado",
                "value": f"{round(beboteo_count / classified_total * 100, 1) if classified_total else 0}% / {round(narrado_count / classified_total * 100, 1) if classified_total else 0}%",
                "format": "text",
            },
        ],
        "summary": {
            "identified_creators": len(creators_detected),
            "active_creators": len(creators_detected),
            "total_active_evergreen_ads": total_ads,
            "classified_ads": classified_total,
            "unclassified_ads": len(unclassified),
            "beboteo_ads": beboteo_count,
            "narrado_ads": narrado_count,
            "beboteo_pct": round(beboteo_count / classified_total * 100, 1) if classified_total else 0,
            "narrado_pct": round(narrado_count / classified_total * 100, 1) if classified_total else 0,
        },
        "detected_creators": creators_detected,
        "unclassified_ads": unclassified,
        "top_beboteo": _top_creator_rows(classified, category="Beboteo"),
        "top_narrado": _top_creator_rows(classified, category="Narrado"),
        "top_general": _top_creator_rows(classified, filtered=False),
        "creators_table": _creators_table(classified, total_ads),
        "top_products": _product_rows(classified),
        "read_only": True,
    }


def _money(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_ars_plain(value):
    return f"$ {float(value or 0):,.0f}".replace(",", ".")


def _parse_order_created_at(value):
    if isinstance(value, dict):
        value = value.get("date")
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _coupon_label(coupons):
    if not coupons:
        return ""
    if isinstance(coupons, dict):
        coupons = [coupons]
    labels = []
    for coupon in coupons:
        if isinstance(coupon, dict):
            labels.append(str(coupon.get("code") or coupon.get("name") or coupon.get("id") or "Cupón"))
        else:
            labels.append(str(coupon))
    return ", ".join(label for label in labels if label)


def _customer_key(order):
    customer = order.get("customer") or {}
    return customer.get("id") or customer.get("email") or order.get("contact_email")


def _aggregate_business_orders(current_orders, all_orders, ranges):
    orders_with_dates = []
    for order in current_orders:
        created = _parse_order_created_at(order.get("created_at") or order.get("completed_at"))
        if not created:
            continue
        orders_with_dates.append((order, created))

    total_orders = len(orders_with_dates)
    total_revenue = round(sum(_money(order.get("total")) for order, _created in orders_with_dates), 2)
    aov = round(total_revenue / total_orders, 2) if total_orders else 0
    coupon_orders = [
        order for order, _created in orders_with_dates
        if _coupon_label(order.get("coupon")) or _money(order.get("discount")) > 0
    ]
    coupon_discount = round(sum(_money(order.get("discount")) for order in coupon_orders), 2)

    day_count = ranges["current"]["days"]
    start_date = datetime.fromisoformat(ranges["current"]["since"]).date()
    daily = {
        (start_date + timedelta(days=idx)).isoformat(): {"date": (start_date + timedelta(days=idx)).isoformat(), "revenue": 0, "orders": 0}
        for idx in range(day_count)
    }
    for order, created in orders_with_dates:
        key = created.date().isoformat()
        if key in daily:
            daily[key]["orders"] += 1
            daily[key]["revenue"] = round(daily[key]["revenue"] + _money(order.get("total")), 2)

    product_units = {}
    product_revenue = {}
    for order, _created in orders_with_dates:
        for product in order.get("products") or []:
            name = product.get("name") or "Sin nombre"
            quantity = int(_money(product.get("quantity")))
            revenue = _money(product.get("price")) * quantity
            product_units.setdefault(name, {"product": name, "units": 0, "revenue": 0})
            product_revenue.setdefault(name, {"product": name, "units": 0, "revenue": 0})
            product_units[name]["units"] += quantity
            product_units[name]["revenue"] = round(product_units[name]["revenue"] + revenue, 2)
            product_revenue[name]["units"] += quantity
            product_revenue[name]["revenue"] = round(product_revenue[name]["revenue"] + revenue, 2)

    payment_status = {}
    for order, _created in orders_with_dates:
        status = order.get("payment_status") or "sin_estado"
        payment_status.setdefault(status, {"status": status, "orders": 0, "revenue": 0})
        payment_status[status]["orders"] += 1
        payment_status[status]["revenue"] = round(payment_status[status]["revenue"] + _money(order.get("total")), 2)

    prior_customers = set()
    current_customers = set()
    current_recurrent = set()
    current_new = set()
    period_start = datetime.fromisoformat(ranges["current"]["since"]).replace(tzinfo=timezone.utc)
    all_have_customer_signal = True
    for order in all_orders:
        key = _customer_key(order)
        created = _parse_order_created_at(order.get("created_at") or order.get("completed_at"))
        if not key:
            all_have_customer_signal = False
            continue
        if not created:
            continue
        if created < period_start:
            prior_customers.add(key)

    seen_current = set()
    for order, _created in sorted(orders_with_dates, key=lambda item: item[1]):
        key = _customer_key(order)
        if not key:
            all_have_customer_signal = False
            continue
        current_customers.add(key)
        if key in prior_customers or key in seen_current:
            current_recurrent.add(key)
        else:
            current_new.add(key)
        seen_current.add(key)

    customers = None
    if all_have_customer_signal and current_customers:
        customers = {
            "new": len(current_new),
            "recurrent": len(current_recurrent),
            "total": len(current_customers),
        }

    latest_orders = []
    for order, created in sorted(orders_with_dates, key=lambda item: item[1], reverse=True)[:10]:
        customer = order.get("customer") or {}
        latest_orders.append({
            "number": order.get("number") or order.get("id"),
            "date": created.isoformat(),
            "customer": customer.get("name") or customer.get("email") or "Sin cliente",
            "total": _money(order.get("total")),
            "payment_status": order.get("payment_status") or "sin_estado",
            "coupon": _coupon_label(order.get("coupon")),
        })

    return {
        "kpis": [
            {"label": "Ventas totales", "value": total_orders, "format": "number"},
            {"label": "Facturación total", "value": total_revenue, "format": "currency"},
            {"label": "Ticket promedio", "value": aov, "format": "currency"},
            {
                "label": "Órdenes con cupón",
                "value": f"{round(len(coupon_orders) / total_orders * 100, 1) if total_orders else 0:g}% · {_format_ars_plain(coupon_discount)}",
                "format": "text",
            },
        ],
        "summary": {
            "orders": total_orders,
            "revenue": total_revenue,
            "aov": aov,
            "coupon_orders": len(coupon_orders),
            "coupon_pct": round(len(coupon_orders) / total_orders * 100, 1) if total_orders else 0,
            "coupon_discount": coupon_discount,
        },
        "series": list(daily.values()),
        "latest_orders": latest_orders,
        "top_products_units": sorted(product_units.values(), key=lambda row: row["units"], reverse=True)[:10],
        "top_products_revenue": sorted(product_revenue.values(), key=lambda row: row["revenue"], reverse=True)[:10],
        "payment_status": sorted(payment_status.values(), key=lambda row: row["orders"], reverse=True),
        "customers": customers,
    }


@api_router.get("/client-dashboard/business")
async def get_client_business_dashboard(
    range: str = Query("7d", pattern="^(7d|14d|30d|custom)$"),
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Read-only business module using Tiendanube demo orders."""
    ranges = _client_dashboard_range(range, since, until)
    created_at_min = f"{ranges['current']['since']}T00:00:00+00:00"
    created_at_max = f"{ranges['current']['until']}T23:59:59+00:00"
    period_key = f"{ranges['current']['since']}_{ranges['current']['until']}"

    try:
        current = await fetch_demo_orders(
            per_page=200,
            max_pages=20,
            params={"created_at_min": created_at_min, "created_at_max": created_at_max, "status": "any"},
        )
        all_data = await fetch_demo_orders(per_page=200, max_pages=100, params={"status": "any"})
    except TiendanubeConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Tiendanube orders pull failed: {exc}") from exc

    data = _aggregate_business_orders(current.get("orders", []), all_data.get("orders", []), ranges)
    data.update({
        "account": {"name": "Tienda Canela Demo"},
        "range": ranges,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "period": period_key,
        "metadata": current.get("metadata", {}),
        "read_only": True,
    })
    return data


@api_router.put("/accounts/{account_id}/roas-objetivo")
async def update_roas_objetivo(account_id: str, body: RoasObjetivoUpdate):
    """Update ROAS objetivo for an account."""
    result = await db.accounts.update_one(
        {"id": account_id},
        {"$set": {"roas_objetivo": body.roas_objetivo}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    # Invalidate cache so dashboard recalculates with new target
    await db.meta_cache.delete_many({"account_id": account_id})
    return {"message": "ROAS objetivo updated", "roas_objetivo": body.roas_objetivo}


@api_router.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: str, period: int = 30):
    """Trigger a manual refresh from Meta API. Only allowed for active accounts."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.get("active", False):
        raise HTTPException(status_code=403, detail="Cuenta no activada.")
    try:
        await refresh_account_data(account_id, account["meta_account_id"], period)
        updated = await db.accounts.find_one({"id": account_id}, {"_id": 0})
        return {"message": "Account refreshed", "last_refreshed": updated.get("last_refreshed")}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Meta API refresh failed: {str(e)}")


@api_router.delete("/accounts/{account_id}/cache")
async def clear_account_cache(account_id: str):
    """Clear ALL cache tiers for an account, forcing a full Meta refresh on next request."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    result = await db.meta_cache.delete_many({"account_id": account_id})
    logger.info(f"Cleared {result.deleted_count} cache docs for {account_id}")
    return {"message": f"Cache limpiado ({result.deleted_count} docs). El próximo refresh traerá datos frescos de Meta."}


@api_router.get("/accounts/{account_id}/cache-status")
async def get_cache_status(account_id: str):
    """Diagnostic: show cache tier ages and status for an account."""
    account = await db.accounts.find_one({"id": account_id}, {"_id": 0})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    now = datetime.now(timezone.utc)
    tiers = {}

    for cache_type in ["structure", "insights", "lifetime", "combined"]:
        docs = await db.meta_cache.find(
            {"account_id": account_id, "cache_type": cache_type},
            {"_id": 0, "fetched_at": 1, "period": 1, "cache_type": 1}
        ).to_list(10)

        for doc in docs:
            fetched_at = doc.get("fetched_at")
            age_minutes = None
            if fetched_at:
                dt = parse_created_time(fetched_at)
                if dt:
                    age_minutes = int((now - dt).total_seconds() / 60)

            ttl_map = {
                "structure": CACHE_TTL_STRUCTURE,
                "insights": CACHE_TTL_INSIGHTS,
                "lifetime": CACHE_TTL_LIFETIME,
                "combined": CACHE_TTL_INSIGHTS,
            }
            ttl = ttl_map.get(cache_type, 1800)
            is_stale = age_minutes > (ttl // 60) if age_minutes is not None else True

            key = f"{cache_type}:{doc.get('period', '_')}"
            tiers[key] = {
                "fetched_at": fetched_at,
                "age_minutes": age_minutes,
                "age_human": f"{age_minutes // 60 // 24}d {(age_minutes // 60) % 24}h {age_minutes % 60}m" if age_minutes else "unknown",
                "is_stale": is_stale,
                "ttl_minutes": ttl // 60,
            }

    return {
        "account_id": account_id,
        "account_name": account.get("name"),
        "last_refreshed": account.get("last_refreshed"),
        "cache_tiers": tiers,
    }

class SlackConfigUpdate(BaseModel):
    slack_token: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    slack_channel: Optional[str] = None

class SlackMessageSend(BaseModel):
    text: str
    channel: Optional[str] = None

@api_router.get("/settings/slack")
async def get_slack_config():
    """Retorna la configuración actual de Slack."""
    settings = await db.settings.find_one({"_id": "slack_config"}) or {}
    return {
        "slack_token": settings.get("slack_token", ""),
        "slack_webhook_url": settings.get("slack_webhook_url", ""),
        "slack_channel": settings.get("slack_channel", "")
    }

@api_router.post("/settings/slack")
async def update_slack_config(config: SlackConfigUpdate):
    """Actualiza la configuración de Slack."""
    await db.settings.update_one(
        {"_id": "slack_config"},
        {"$set": config.model_dump(exclude_unset=True)},
        upsert=True
    )
    return {"message": "Configuración de Slack actualizada"}

@api_router.post("/settings/slack/test")
async def test_slack_config():
    """Envía un mensaje de prueba a Slack utilizando la configuración actual."""
    from slack_utils import send_slack_message
    res = await send_slack_message(
        db,
        text="👋 *¡Hola!* Esta es una notificación de prueba desde RUMBO. La integración con Slack funciona correctamente."
    )
    if res and res.get("ok"):
        return {"message": "Mensaje enviado exitosamente a Slack"}
    else:
        raise HTTPException(status_code=400, detail="Fallo al enviar mensaje a Slack. Verificá la URL o el Token/Canal")

@api_router.post("/settings/slack/send")
async def send_slack_message(payload: SlackMessageSend):
    """Envía un mensaje arbitrario a Slack usando la configuración guardada."""
    from slack_utils import send_slack_message as send_message

    res = await send_message(db, text=payload.text, channel=payload.channel)
    if res and res.get("ok"):
        return {"message": "Mensaje enviado exitosamente a Slack"}
    raise HTTPException(status_code=400, detail="Fallo al enviar mensaje a Slack. Verificá la configuración y el canal destino")

class ReportsConfigUpdate(BaseModel):
    reports_sheet_url: Optional[str] = None

@api_router.get("/settings/reports")
async def get_reports_config():
    """Retorna la configuración de la hoja de reportes."""
    settings = await db.settings.find_one({"_id": "reports_config"}) or {}
    return {
        "reports_sheet_url": settings.get("reports_sheet_url", "")
    }

@api_router.post("/settings/reports")
async def update_reports_config(config: ReportsConfigUpdate):
    """Actualiza la hoja de reportes."""
    await db.settings.update_one(
        {"_id": "reports_config"},
        {"$set": config.model_dump(exclude_unset=True)},
        upsert=True
    )
    return {"message": "Configuración de Reportes actualizada"}

app.include_router(api_router)
app.include_router(tiendanube_router)

import os
STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), follow_symlink=True), name="static")


@app.get("/client-dashboard")
async def client_dashboard_app():
    return FileResponse(STATIC_DIR / "client-dashboard" / "index.html")

# ─── OAuth / Auth router ───────────────────────────────────────────────────── 

META_APP_ID     = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
OAUTH_REDIRECT_URI = "https://rumbo-back.up.railway.app/auth/callback"
FRONTEND_URL    = os.environ.get("FRONTEND_URL", "https://rumbo-app.up.railway.app")
OAUTH_SCOPES    = (
    "ads_management,ads_read,"
    "pages_manage_ads,pages_read_engagement,pages_show_list"
)

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.get("/login")
async def auth_login():
    """Redirect to Facebook OAuth dialog."""
    if not META_APP_ID:
        raise HTTPException(status_code=500, detail="META_APP_ID no configurado")
    url = (
        f"https://www.facebook.com/v21.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={OAUTH_REDIRECT_URI}"
        f"&scope={OAUTH_SCOPES}"
        f"&response_type=code"
    )
    return RedirectResponse(url=url)


@auth_router.get("/callback")
async def auth_callback(code: str = None, error: str = None, error_description: str = None):
    """Handle OAuth callback from Facebook."""
    if error or not code:
        detail = error_description or error or "auth_cancelled"
        logger.warning(f"OAuth error: {detail}")
        return RedirectResponse(url=f"{FRONTEND_URL}?auth=error&reason={detail}")

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Exchange code for short-lived token
        r1 = await client.get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "code": code,
            },
        )
        if r1.status_code != 200:
            logger.error(f"Token exchange failed: {r1.text[:200]}")
            return RedirectResponse(url=f"{FRONTEND_URL}?auth=error&reason=token_exchange")
        short_token = r1.json().get("access_token", "")

        # 2. Extend to long-lived token (~60 days)
        r2 = await client.get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )
        if r2.status_code != 200:
            logger.error(f"Token extension failed: {r2.text[:200]}")
            return RedirectResponse(url=f"{FRONTEND_URL}?auth=error&reason=token_extension")
        token_data   = r2.json()
        long_token   = token_data.get("access_token", "")
        expires_in   = token_data.get("expires_in", 5184000)  # 60 days default
        expires_at   = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        # 3. Get user info
        r3 = await client.get(
            "https://graph.facebook.com/v21.0/me",
            params={"access_token": long_token, "fields": "id,name"},
        )
        user = r3.json() if r3.status_code == 200 else {}

        # 4. Save token to DB
        await db.settings.update_one(
            {"_id": "meta_config"},
            {"$set": {
                "access_token": long_token,
                "user_name": user.get("name", ""),
                "user_id":   user.get("id", ""),
                "expires_at": expires_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "auth_method": "oauth",
            }},
            upsert=True,
        )
        logger.info(f"OAuth success for user: {user.get('name')}")

        # 5. Auto-discover and upsert ad accounts
        r4 = await client.get(
            "https://graph.facebook.com/v21.0/me/adaccounts",
            params={
                "access_token": long_token,
                "fields": "id,name,account_status",
                "limit": 100,
            },
        )
        if r4.status_code == 200:
            ad_accounts = r4.json().get("data", [])
            upserted = 0
            for acc in ad_accounts:
                acc_id = acc["id"]  # already in act_XXXXXX format
                await db.accounts.update_one(
                    {"id": acc_id},
                    {
                        "$set": {
                            "id":              acc_id,
                            "name":            acc.get("name", acc_id),
                            "meta_account_id": acc_id,
                        },
                        "$setOnInsert": {"roas_objetivo": 4.5, "active": False},
                    },
                    upsert=True,
                )
                upserted += 1
            logger.info(f"Auto-discovered {upserted} ad accounts")
        else:
            logger.warning(f"Could not fetch ad accounts: {r4.text[:200]}")

    return RedirectResponse(url=f"{FRONTEND_URL}?auth=success")


@auth_router.get("/status")
async def auth_status():
    """Return current OAuth connection status."""
    settings = await db.settings.find_one({"_id": "meta_config"}, {"_id": 0})
    if settings and settings.get("access_token"):
        token = settings["access_token"]
        # Check expiry
        expires_at = settings.get("expires_at")
        expired = False
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                expired = exp_dt < datetime.now(timezone.utc)
            except Exception:
                pass
        return {
            "connected":   True,
            "method":      settings.get("auth_method", "manual"),
            "user_name":   settings.get("user_name"),
            "expires_at":  expires_at,
            "expired":     expired,
            "masked_token": f"...{token[-8:]}",
        }
    # Fallback: env var (read-only, no user info)
    env_token = os.environ.get("META_ACCESS_TOKEN", "")
    if env_token:
        return {"connected": True, "method": "env", "user_name": None, "expires_at": None, "expired": False, "masked_token": f"...{env_token[-8:]}"}
    return {"connected": False, "method": None, "user_name": None, "expires_at": None, "expired": False}


@auth_router.get("/logout")
async def auth_logout():
    """Remove OAuth token from DB."""
    await db.settings.delete_one({"_id": "meta_config"})
    return {"message": "Desconectado correctamente"}


app.include_router(auth_router)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- MCP Server Integration ---
try:
    from mcp_server import MCP_AVAILABLE, mcp_sse_app
    if MCP_AVAILABLE and mcp_sse_app is not None:
        # Mount the FastMCP SSE ASGI app at /mcp
        # Claude Desktop connects to: https://host/mcp/sse
        # Messages go to:             https://host/mcp/messages
        app.mount("/mcp", mcp_sse_app)
        logger.info("[MCP] FastMCP SSE app mounted at /mcp  (sse=/mcp/sse  messages=/mcp/messages)")
    else:
        logger.warning("[MCP] MCP not available, /mcp/* routes not mounted")
except Exception as e:
    logger.error(f"[MCP] Failed to mount MCP endpoints: {e}", exc_info=True)
@app.on_event("startup")
async def startup_event():
    """Seed accounts on startup if none exist. También arranca el scheduler de reportes."""
    try:
        logger.info("Verifying MongoDB connection...")
        count = await db.accounts.count_documents({})
        if count == 0:
            logger.info("No accounts found, seeding from config...")
            for acc in ACCOUNTS_CONFIG:
                await db.accounts.insert_one({
                    "id": acc["id"],
                    "name": acc["name"],
                    "meta_account_id": acc["meta_account_id"],
                    "roas_objetivo": 4.5,
                    "last_refreshed": None,
                })
            logger.info(f"Seeded {len(ACCOUNTS_CONFIG)} accounts")
        else:
            logger.info(f"Database already has {count} accounts")

        await db.tiendanube_connections.create_index("store_id", unique=True)

    except Exception as e:
        logger.error(f"FATAL ERROR during startup: {str(e)}")
        # Don't re-raise, let the app start but log the error
        # This prevents the whole container from crashing if DB is just slow/unreachable


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

# --- CORS Middleware (At the end to wrap everything) ---
# FORCE WILDCARD for debugging - "Canilla Libre" mode
print("DEBUG: Forcing CORS allow_origins=['*'] allow_credentials=False")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"GLOBAL ERROR: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

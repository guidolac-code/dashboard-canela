import httpx
import logging
from datetime import datetime, timezone, timedelta
import json
import asyncio

logger = logging.getLogger(__name__)

META_API_VERSION = "v21.0"
META_API_BASE = f"https://graph.facebook.com/{META_API_VERSION}"

PURCHASE_TYPES = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
LANDING_VIEW_TYPES = ["landing_page_view"]
ADD_TO_CART_TYPES = ["omni_add_to_cart", "add_to_cart", "offsite_conversion.fb_pixel_add_to_cart"]
INITIATE_CHECKOUT_TYPES = [
    "omni_initiated_checkout",
    "initiate_checkout",
    "offsite_conversion.fb_pixel_initiate_checkout",
]
IG_FOLLOW_TYPES = ["onsite_conversion.messaging_conversation_started_7d", "follow", "like"]
COMMENT_TYPES = ["comment", "post_comment"]
REACTION_TYPES = ["post_reaction", "like"]

CLIENT_DASHBOARD_FIELDS = (
    "spend,purchase_roas,actions,action_values,impressions,reach,cpm,frequency,"
    "ctr,outbound_clicks,cost_per_action_type,video_play_actions"
)


def get_insights_field(period_days):
    """Build the nested insights field parameter with date range."""
    metrics = "spend,purchase_roas,actions,action_values"
    preset_map = {1: "today", 2: "yesterday", 7: "last_7d", 30: "last_30d"}
    if period_days in preset_map:
        return f"insights.date_preset({preset_map[period_days]}){{{metrics}}}"
    # For 60d or custom, use time_range
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=period_days)).strftime("%Y-%m-%d")
    until = now.strftime("%Y-%m-%d")
    tr = json.dumps({"since": since, "until": until})
    return f"insights.time_range({tr}){{{metrics}}}"


async def meta_get_all(client, endpoint, access_token, extra_params=None):
    """Paginated GET from Meta Marketing API. Returns all items across pages."""
    url = f"{META_API_BASE}/{endpoint}"
    params = {"access_token": access_token, "limit": 500}
    if extra_params:
        params.update(extra_params)

    all_data = []
    retries = 0
    while url:
        try:
            resp = await client.get(url, params=params, timeout=120)

            # Rate limit: 429 HTTP, or code 17 (User BUC limit), or code 4 (App limit)
            body_text = resp.text
            is_rate_limit = resp.status_code == 429
            if not is_rate_limit and resp.status_code in (400, 200):
                try:
                    _b = resp.json()
                    _code = _b.get("error", {}).get("code", 0)
                    is_rate_limit = _code in (4, 17, 32, 613) or "too many calls" in body_text.lower()
                except Exception:
                    pass

            if is_rate_limit:
                retries += 1
                if retries > 5:
                    logger.error("Meta API rate limit exceeded after 5 retries")
                    raise Exception("Meta API rate limit exceeded (code 17). Intentá en unos minutos.")
                wait = min(60, 15 * retries)  # 15s, 30s, 45s, 60s, 60s
                logger.warning(f"Meta API rate limited, waiting {wait}s (retry {retries})")
                await asyncio.sleep(wait)
                continue

            body = resp.json()
            if "error" in body:
                error = body["error"]
                code  = error.get("code", 0)
                msg   = error.get("message", "Unknown error")
                user_msg = error.get("error_user_message", "")
                full_msg  = f"Meta API error (code {code}): {msg}"
                if user_msg and user_msg != msg:
                    full_msg += f" → {user_msg}"
                logger.error(full_msg)
                raise Exception(full_msg)

            all_data.extend(body.get("data", []))
            next_url = body.get("paging", {}).get("next")
            if next_url:
                import re
                url = re.sub(r"v\d+\.\d+", META_API_VERSION, next_url)
                params = {"access_token": access_token}
            else:
                break
        except httpx.RequestError as e:
            logger.error(f"Meta API request failed: {e}")
            raise Exception(f"Meta API request failed: {e}")

    return all_data


def parse_insights(entity):
    """Extract spend, conversions, conversion_value, roas from Meta insights.
    Uses priority-based extraction to avoid double-counting across overlapping action types."""
    insights_data = entity.get("insights", {}).get("data", [])
    if not insights_data:
        return 0.0, 0, 0.0, 0.0

    insight = insights_data[0]
    spend = float(insight.get("spend", 0))

    # Priority order: take first matching type only (they overlap)
    purchase_priority = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]

    # Extract conversions - first match only
    conversions = 0
    for ptype in purchase_priority:
        for action in insight.get("actions", []):
            if action.get("action_type", "") == ptype:
                conversions = int(float(action.get("value", 0)))
                break
        if conversions > 0:
            break

    # Extract conversion value - first match only
    conversion_value = 0.0
    for ptype in purchase_priority:
        for av in insight.get("action_values", []):
            if av.get("action_type", "") == ptype:
                conversion_value = float(av.get("value", 0))
                break
        if conversion_value > 0:
            break

    # Extract ROAS from purchase_roas - first match only
    roas = 0.0
    for ptype in purchase_priority:
        for pr in insight.get("purchase_roas", []):
            if pr.get("action_type", "") == ptype:
                roas = round(float(pr.get("value", 0)), 2)
                break
        if roas > 0:
            break

    if roas == 0 and spend > 0 and conversion_value > 0:
        roas = round(conversion_value / spend, 2)

    return spend, conversions, conversion_value, roas


def _first_action_value(actions, action_types, as_int=False):
    for action_type in action_types:
        for action in actions or []:
            if action.get("action_type") == action_type:
                value = float(action.get("value", 0) or 0)
                return int(value) if as_int else value
    return 0 if as_int else 0.0


def _sum_action_value(actions, action_types, as_int=False):
    total = 0.0
    for action in actions or []:
        if action.get("action_type") in action_types:
            total += float(action.get("value", 0) or 0)
    return int(total) if as_int else total


def _parse_action_cost(costs, action_types):
    for action_type in action_types:
        for cost in costs or []:
            if cost.get("action_type") == action_type:
                return float(cost.get("value", 0) or 0)
    return 0.0


def parse_client_dashboard_insight(insight):
    """Parse the broader Meta metrics used by the client-facing dashboard."""
    spend = float(insight.get("spend", 0) or 0)
    impressions = int(float(insight.get("impressions", 0) or 0))
    reach = int(float(insight.get("reach", 0) or 0))
    cpm = float(insight.get("cpm", 0) or 0)
    frequency = float(insight.get("frequency", 0) or 0)

    actions = insight.get("actions", [])
    action_values = insight.get("action_values", [])
    costs = insight.get("cost_per_action_type", [])
    outbound_clicks = insight.get("outbound_clicks", [])
    video_actions = insight.get("video_play_actions", [])

    purchases = _first_action_value(actions, PURCHASE_TYPES, as_int=True)
    purchase_value = _first_action_value(action_values, PURCHASE_TYPES)
    roas = 0.0
    for ptype in PURCHASE_TYPES:
        for item in insight.get("purchase_roas", []) or []:
            if item.get("action_type") == ptype:
                roas = float(item.get("value", 0) or 0)
                break
        if roas:
            break
    if not roas and spend > 0 and purchase_value > 0:
        roas = purchase_value / spend

    link_clicks = _sum_action_value(outbound_clicks, ["outbound_click"], as_int=True)
    if not link_clicks:
        link_clicks = _sum_action_value(actions, ["link_click", "outbound_click"], as_int=True)

    landing_views = _sum_action_value(actions, LANDING_VIEW_TYPES, as_int=True)
    add_to_cart = _first_action_value(actions, ADD_TO_CART_TYPES, as_int=True)
    initiate_checkout = _first_action_value(actions, INITIATE_CHECKOUT_TYPES, as_int=True)
    video_3s = _sum_action_value(video_actions, ["video_view"], as_int=True)

    return {
        "spend": round(spend, 2),
        "purchases": purchases,
        "purchase_value": round(purchase_value, 2),
        "roas": round(roas, 2),
        "impressions": impressions,
        "reach": reach,
        "cpm": round(cpm, 2),
        "frequency": round(frequency, 2),
        "link_clicks": link_clicks,
        "ctr_link": round((link_clicks / impressions * 100), 2) if impressions else 0,
        "landing_page_views": landing_views,
        "purchase_landing_rate": round((purchases / landing_views * 100), 2) if landing_views else 0,
        "add_to_cart": add_to_cart,
        "initiate_checkout": initiate_checkout,
        "cost_per_result": round(spend / purchases, 2) if purchases else 0,
        "cost_per_landing_view": round(spend / landing_views, 2) if landing_views else 0,
        "cost_per_add_to_cart": round(_parse_action_cost(costs, ADD_TO_CART_TYPES), 2),
        "cost_per_initiate_checkout": round(_parse_action_cost(costs, INITIATE_CHECKOUT_TYPES), 2),
        "video_3s": video_3s,
        "instagram_follows": _sum_action_value(actions, IG_FOLLOW_TYPES, as_int=True),
        "comments": _sum_action_value(actions, COMMENT_TYPES, as_int=True),
        "reactions": _sum_action_value(actions, REACTION_TYPES, as_int=True),
    }


def _date_params(since, until):
    return {"time_range": json.dumps({"since": since, "until": until})}


async def fetch_client_account_daily_insights(meta_account_id, access_token, since, until):
    params = {
        "fields": f"date_start,date_stop,{CLIENT_DASHBOARD_FIELDS}",
        "time_increment": 1,
        **_date_params(since, until),
    }
    async with httpx.AsyncClient() as client:
        raw = await meta_get_all(client, f"{meta_account_id}/insights", access_token, params)

    daily = []
    for item in raw:
        metrics = parse_client_dashboard_insight(item)
        daily.append({"date": item.get("date_start"), **metrics})
    daily.sort(key=lambda x: x.get("date") or "")
    return daily


async def fetch_client_ad_insights(meta_account_id, access_token, since, until):
    params = {
        "level": "ad",
        "fields": (
            "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
            f"{CLIENT_DASHBOARD_FIELDS}"
        ),
        **_date_params(since, until),
    }
    async with httpx.AsyncClient() as client:
        raw = await meta_get_all(client, f"{meta_account_id}/insights", access_token, params)

    ads = []
    for item in raw:
        metrics = parse_client_dashboard_insight(item)
        ads.append({
            "id": item.get("ad_id", ""),
            "name": item.get("ad_name", "Unnamed Ad"),
            "adset_id": item.get("adset_id", ""),
            "adset_name": item.get("adset_name", "Unknown"),
            "campaign_id": item.get("campaign_id", ""),
            "campaign_name": item.get("campaign_name", "Unknown"),
            **metrics,
        })
    return ads


def detect_creative_format(ad):
    creative = ad.get("creative", {}) or {}
    serialized = json.dumps(creative).lower()
    if "video_id" in serialized or creative.get("object_type") == "VIDEO":
        return "video"
    return "image"


async def fetch_client_ad_structure(meta_account_id, access_token):
    fields = (
        "id,name,adset_id,created_time,updated_time,effective_status,"
        "creative{id,name,object_type,video_id,thumbnail_url,image_url,asset_feed_spec,object_story_spec}"
    )
    filtering = json.dumps([
        {"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]},
        {"field": "spend", "operator": "GREATER_THAN", "value": "0"},
    ])
    async with httpx.AsyncClient() as client:
        raw = await meta_get_all(
            client,
            f"{meta_account_id}/ads",
            access_token,
            {"fields": fields, "filtering": filtering, "date_preset": "last_30d"},
        )

    return {
        ad.get("id"): {
            "id": ad.get("id"),
            "name": ad.get("name", "Unnamed Ad"),
            "adset_id": ad.get("adset_id", ""),
            "status": map_status(ad.get("effective_status", "")),
            "created_time": ad.get("created_time", ""),
            "updated_time": ad.get("updated_time", ""),
            "format": detect_creative_format(ad),
        }
        for ad in raw
        if ad.get("id")
    }


async def fetch_client_creator_active_structure(meta_account_id, access_token):
    """Fetch active ads with their active adset/campaign context for the Creators module."""
    fields = (
        "id,name,adset_id,created_time,updated_time,effective_status,"
        "adset{id,name,effective_status,campaign{id,name,effective_status}}"
    )
    filtering = json.dumps([
        {"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]},
    ])
    async with httpx.AsyncClient() as client:
        raw = await meta_get_all(
            client,
            f"{meta_account_id}/ads",
            access_token,
            {"fields": fields, "filtering": filtering},
        )

    ads = []
    for ad in raw:
        adset = ad.get("adset", {}) or {}
        campaign = adset.get("campaign", {}) or {}
        if map_status(ad.get("effective_status", "")) != "ACTIVE":
            continue
        if map_status(adset.get("effective_status", "")) != "ACTIVE":
            continue
        campaign_name = campaign.get("name", "")
        if "evergreen" not in campaign_name.lower():
            continue
        ads.append({
            "id": ad.get("id", ""),
            "name": ad.get("name", "Unnamed Ad"),
            "adset_id": ad.get("adset_id", ""),
            "adset_name": adset.get("name", "Unknown"),
            "adset_status": map_status(adset.get("effective_status", "")),
            "campaign_id": campaign.get("id", ""),
            "campaign_name": campaign_name or "Unknown",
            "campaign_status": map_status(campaign.get("effective_status", "")),
            "status": "ACTIVE",
            "created_time": ad.get("created_time", ""),
            "updated_time": ad.get("updated_time", ""),
        })
    return ads


async def fetch_client_dashboard_data(meta_account_id, access_token, since, until):
    daily, ad_insights, ad_structure = await asyncio.gather(
        fetch_client_account_daily_insights(meta_account_id, access_token, since, until),
        fetch_client_ad_insights(meta_account_id, access_token, since, until),
        fetch_client_ad_structure(meta_account_id, access_token),
    )
    return {"daily": daily, "ads": ad_insights, "ad_structure": ad_structure}


def map_status(effective_status):
    """Map Meta effective_status to ACTIVE/PAUSED."""
    return "ACTIVE" if effective_status == "ACTIVE" else "PAUSED"


async def fetch_campaigns(client, meta_account_id, access_token, insights_field):
    campaign_fields = f"id,name,objective,daily_budget,lifetime_budget,effective_status,{insights_field}"
    raw_campaigns = await meta_get_all(
        client,
        f"{meta_account_id}/campaigns",
        access_token,
        {"fields": campaign_fields}
    )

    campaigns = []
    for rc in raw_campaigns:
        spend, conversions, conversion_value, roas = parse_insights(rc)
        daily_budget = float(rc.get("daily_budget", 0)) / 100  # cents → dollars
        lifetime_budget = float(rc.get("lifetime_budget", 0)) / 100
        budget_optimization = "CBO" if (daily_budget > 0 or lifetime_budget > 0) else "ABO"

        campaigns.append({
            "id": rc["id"],
            "name": rc.get("name", "Unnamed Campaign"),
            "objective": rc.get("objective", "UNKNOWN"),
            "budget_optimization": budget_optimization,
            "daily_budget": round(daily_budget, 2),
            "status": map_status(rc.get("effective_status", "")),
            "spend": round(spend, 2),
            "conversions": conversions,
            "conversion_value": round(conversion_value, 2),
            "roas": roas,
        })
    return campaigns


async def fetch_adsets(client, meta_account_id, access_token, insights_field):
    adset_fields = f"id,name,campaign_id,daily_budget,created_time,effective_status,{insights_field}"
    raw_adsets = await meta_get_all(
        client,
        f"{meta_account_id}/adsets",
        access_token,
        {"fields": adset_fields}
    )

    adsets = []
    for ra in raw_adsets:
        spend, conversions, conversion_value, roas = parse_insights(ra)
        daily_budget = float(ra.get("daily_budget", 0)) / 100

        adsets.append({
            "id": ra["id"],
            "campaign_id": ra.get("campaign_id", ""),
            "name": ra.get("name", "Unnamed Adset"),
            "daily_budget": round(daily_budget, 2),
            "status": map_status(ra.get("effective_status", "")),
            "spend": round(spend, 2),
            "conversions": conversions,
            "conversion_value": round(conversion_value, 2),
            "roas": roas,
            "created_time": ra.get("created_time", ""),
        })
    return adsets


async def fetch_ads(client, meta_account_id, access_token, insights_field, adset_campaign_map=None):
    ad_fields = f"id,name,adset_id,created_time,effective_status,{insights_field}"
    
    # Filter by spend > 0 in the last 30 days to reduce response size for large accounts
    ads_filter = json.dumps([
        {"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]},
        {"field": "spend", "operator": "GREATER_THAN", "value": "0"}
    ])
    
    raw_ads = await meta_get_all(
        client,
        f"{meta_account_id}/ads",
        access_token,
        {"fields": ad_fields, "filtering": ads_filter, "date_preset": "last_30d"}
    )
    
    # Note: adset_campaign_map might be passed or not. 
    # If we run in parallel, we can't depend on adsets being finished before starting ads fetch
    # So we might need to map it AFTER fetching or return raw data and map later.
    # We will map it inside fetch_account_data after both are done.
    
    # However, to keep structure consistent, we can return the list of dicts 
    # and enrich it with campaign_id later.
    
    ads = []
    for rad in raw_ads:
        spend, conversions, conversion_value, roas = parse_insights(rad)
        adset_id = rad.get("adset_id", "")
        
        # campaign_id will be enriched later
        
        ads.append({
            "id": rad["id"],
            "adset_id": adset_id,
            "name": rad.get("name", "Unnamed Ad"),
            "status": map_status(rad.get("effective_status", "")),
            "spend": round(spend, 2),
            "conversions": conversions,
            "conversion_value": round(conversion_value, 2),
            "roas": roas,
            "created_time": rad.get("created_time", ""),
        })
    return ads


async def fetch_account_insights(client, meta_account_id, access_token, period_days, custom_range=None):
    account_insights = {"spend": 0, "conversions": 0, "conversion_value": 0, "roas": 0}
    try:
        acct_params = {"fields": "spend,purchase_roas,actions,action_values"}
        preset_map = {1: "today", 2: "yesterday", 7: "last_7d", 30: "last_30d"}
        
        if custom_range:
            acct_params["time_range"] = json.dumps(custom_range)
        elif period_days in preset_map:
            acct_params["date_preset"] = preset_map[period_days]
        else:
            now = datetime.now(timezone.utc)
            since = (now - timedelta(days=period_days)).strftime("%Y-%m-%d")
            until = now.strftime("%Y-%m-%d")
            acct_params["time_range"] = json.dumps({"since": since, "until": until})

        raw_acct = await meta_get_all(client, f"{meta_account_id}/insights", access_token, acct_params)
        if raw_acct:
            spend, conversions, conversion_value, roas = parse_insights({"insights": {"data": raw_acct}})
            account_insights = {
                "spend": round(spend, 2),
                "conversions": conversions,
                "conversion_value": round(conversion_value, 2),
                "roas": roas,
            }
            logger.info(f"Account insights: spend={spend}, conv={conversions}, value={conversion_value}, roas={roas}")
    except Exception as e:
        logger.warning(f"Failed to fetch account-level insights: {e}")
    
    return account_insights


async def fetch_account_data(meta_account_id, access_token, period_days=30):
    """Fetch campaigns, adsets, and ads with insights for one account in parallel."""
    insights_field = get_insights_field(period_days)
    
    async with httpx.AsyncClient() as client:
        # Run all fetches in parallel
        results = await asyncio.gather(
            fetch_campaigns(client, meta_account_id, access_token, insights_field),
            fetch_adsets(client, meta_account_id, access_token, insights_field),
            fetch_ads(client, meta_account_id, access_token, insights_field),
            fetch_account_insights(client, meta_account_id, access_token, period_days)
        )
        
        campaigns, adsets, ads, account_insights = results
        
        # Post-process: Enrich ads with campaign_id loopups
        adset_campaign_map = {a["id"]: a["campaign_id"] for a in adsets}
        for ad in ads:
            ad["campaign_id"] = adset_campaign_map.get(ad["adset_id"], "")

        logger.info(f"Fetched {meta_account_id}: {len(campaigns)} campaigns, {len(adsets)} adsets, {len(ads)} ads")
        
        return {"campaigns": campaigns, "adsets": adsets, "ads": ads, "account_insights": account_insights}


# ── Split fetchers for granular TTL caching ───────────────────────────────────

async def fetch_structure_only(meta_account_id, access_token):
    """Fetch campaigns, adsets, ads WITHOUT insights. Fast and light.
    Filters to ACTIVE+PAUSED only — excludes ARCHIVED/DELETED to reduce
    response size for large accounts with long history (prevents rate limiting)."""
    struct_fields_campaigns = "id,name,objective,daily_budget,lifetime_budget,effective_status"
    struct_fields_adsets    = "id,name,campaign_id,daily_budget,created_time,effective_status"
    struct_fields_ads       = "id,name,adset_id,created_time,effective_status,insights.date_preset(last_30d){spend}"

    # For ads, we also filter by spend > 0 in the last 30 days to drastically reduce
    # the response size for large accounts (e.g. Tienda Canela).
    active_filter = json.dumps([{"field": "effective_status", "operator": "IN",
                                  "value": ["ACTIVE", "PAUSED"]}])
    
    ads_filter = json.dumps([
        {"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]},
        {"field": "spend", "operator": "GREATER_THAN", "value": "0"}
    ])

    async with httpx.AsyncClient() as client:
        raw_campaigns, raw_adsets, raw_ads = await asyncio.gather(
            meta_get_all(client, f"{meta_account_id}/campaigns", access_token,
                         {"fields": struct_fields_campaigns, "filtering": active_filter}),
            meta_get_all(client, f"{meta_account_id}/adsets", access_token,
                         {"fields": struct_fields_adsets, "filtering": active_filter}),
            meta_get_all(client, f"{meta_account_id}/ads", access_token,
                         {"fields": struct_fields_ads, "filtering": ads_filter, "date_preset": "last_30d"}),
        )

    campaigns = []
    for rc in raw_campaigns:
        daily_budget    = float(rc.get("daily_budget", 0)) / 100
        lifetime_budget = float(rc.get("lifetime_budget", 0)) / 100
        campaigns.append({
            "id":                 rc["id"],
            "name":               rc.get("name", "Unnamed Campaign"),
            "objective":          rc.get("objective", "UNKNOWN"),
            "budget_optimization": "CBO" if (daily_budget > 0 or lifetime_budget > 0) else "ABO",
            "daily_budget":       round(daily_budget, 2),
            "status":             map_status(rc.get("effective_status", "")),
        })

    adsets = []
    adset_campaign_map = {}
    for ra in raw_adsets:
        daily_budget = float(ra.get("daily_budget", 0)) / 100
        adset_campaign_map[ra["id"]] = ra.get("campaign_id", "")
        adsets.append({
            "id":          ra["id"],
            "campaign_id": ra.get("campaign_id", ""),
            "name":        ra.get("name", "Unnamed Adset"),
            "daily_budget": round(daily_budget, 2),
            "status":      map_status(ra.get("effective_status", "")),
            "created_time": ra.get("created_time", ""),
        })

    ads = []
    for rad in raw_ads:
        adset_id = rad.get("adset_id", "")
        ads.append({
            "id":          rad["id"],
            "adset_id":    adset_id,
            "campaign_id": adset_campaign_map.get(adset_id, ""),
            "name":        rad.get("name", "Unnamed Ad"),
            "status":      map_status(rad.get("effective_status", "")),
            "created_time": rad.get("created_time", ""),
        })

    logger.info(f"[structure] {meta_account_id}: {len(campaigns)} camps, {len(adsets)} adsets, {len(ads)} ads (active+paused only)")
    return {"campaigns": campaigns, "adsets": adsets, "ads": ads}


async def fetch_insights_for_period(meta_account_id, access_token, period_days, ad_ids, custom_range=None):
    """Fetch account-level insights + per-ad insights for a given period.
    Filters to ACTIVE+PAUSED ads to match structure cache and avoid over-fetching.
    Use for the insights cache (30min TTL)."""
    insights_field = (
        get_insights_field_custom(custom_range["since"], custom_range["until"])
        if custom_range else get_insights_field(period_days)
    )
    ad_insights_fields = f"id,adset_id,{insights_field}"
    
    # Filter only ACTIVE/PAUSED ads that actually had spend in the last 30 days.
    # This prevents rate limiting on large accounts by reducing raw response size.
    ads_spend_filter = json.dumps([
        {"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]},
        {"field": "spend", "operator": "GREATER_THAN", "value": "0"}
    ])

    async with httpx.AsyncClient() as client:
        raw_ads, account_insights = await asyncio.gather(
            meta_get_all(client, f"{meta_account_id}/ads", access_token,
                         {"fields": ad_insights_fields, "filtering": ads_spend_filter, "date_preset": "last_30d"}),
            fetch_account_insights(client, meta_account_id, access_token, period_days, custom_range),
        )

    # Build per-ad insights dict keyed by ad_id
    ad_insights = {}
    for rad in raw_ads:
        spend, conversions, conversion_value, roas = parse_insights(rad)
        ad_insights[rad["id"]] = {
            "spend":            round(spend, 2),
            "conversions":      conversions,
            "conversion_value": round(conversion_value, 2),
            "roas":             roas,
        }

    logger.info(f"[insights] {meta_account_id}: period={period_days}d, {len(ad_insights)} active ads with metrics")
    return {"ad_insights": ad_insights, "account_insights": account_insights}


async def fetch_ads_lifetime_insights(meta_account_id, access_token, ad_ids):
    """
    Fetch ALL-TIME insights for specific ads. Processes batches sequentially
    with a small delay to avoid rate limiting on large accounts.
    """
    if not ad_ids:
        return {}

    logger.info(f"Fetching lifetime insights for {len(ad_ids)} ads")

    metrics = "spend,purchase_roas,actions,action_values"
    three_years_ago = (datetime.now(timezone.utc) - timedelta(days=1080)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tr = json.dumps({"since": three_years_ago, "until": today})
    insights_field = f"insights.time_range({tr}){{{metrics}}}"
    ad_fields = f"id,{insights_field}"

    lifetime_data = {}

    async with httpx.AsyncClient() as client:
        batch_size = 50
        batches = [ad_ids[i:i+batch_size] for i in range(0, len(ad_ids), batch_size)]
        for idx, batch_ids in enumerate(batches):
            if idx > 0:
                await asyncio.sleep(1)  # 1s pause between batches to stay under rate limit
            batch_data = await fetch_lifetime_batch(client, meta_account_id, access_token, batch_ids, ad_fields)
            if batch_data:
                lifetime_data.update(batch_data)

    logger.info(f"Fetched lifetime data for {len(lifetime_data)} ads")
    return lifetime_data


async def fetch_lifetime_batch(client, meta_account_id, access_token, batch_ids, ad_fields):
    ids_filter = json.dumps(batch_ids)
    try:
        raw_ads = await meta_get_all(
            client,
            f"{meta_account_id}/ads",
            access_token,
            {
                "fields": ad_fields,
                "filtering": f'[{{"field":"id","operator":"IN","value":{ids_filter}}}]'
            }
        )
        
        result = {}
        for rad in raw_ads:
            ad_id = rad["id"]
            spend, conversions, conversion_value, roas = parse_insights(rad)
            result[ad_id] = {
                "spend": round(spend, 2),
                "conversions": conversions,
                "conversion_value": round(conversion_value, 2),
                "roas": roas,
            }
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch lifetime insights for batch: {e}")
        return {}


def get_insights_field_custom(since, until):
    """Build nested insights field with custom date range."""
    metrics = "spend,purchase_roas,actions,action_values"
    tr = json.dumps({"since": since, "until": until})
    return f"insights.time_range({tr}){{{metrics}}}"


async def fetch_account_data_custom(meta_account_id, access_token, since, until):
    """Fetch campaigns, adsets, and ads with insights for custom date range in parallel."""
    insights_field = get_insights_field_custom(since, until)
    custom_range = {"since": since, "until": until}
    
    async with httpx.AsyncClient() as client:
        # Run all fetches in parallel
        results = await asyncio.gather(
            fetch_campaigns(client, meta_account_id, access_token, insights_field),
            fetch_adsets(client, meta_account_id, access_token, insights_field),
            fetch_ads(client, meta_account_id, access_token, insights_field),
            fetch_account_insights(client, meta_account_id, access_token, 0, custom_range=custom_range)
        )
        
        campaigns, adsets, ads, account_insights = results
        
        # Enrich ads
        adset_campaign_map = {a["id"]: a["campaign_id"] for a in adsets}
        for ad in ads:
            ad["campaign_id"] = adset_campaign_map.get(ad["adset_id"], "")

        logger.info(f"Fetched {meta_account_id} ({since} to {until}): {len(campaigns)} campaigns, {len(adsets)} adsets, {len(ads)} ads")
        
        return {"campaigns": campaigns, "adsets": adsets, "ads": ads, "account_insights": account_insights}

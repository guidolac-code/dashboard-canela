import json
import importlib
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

MCP_AVAILABLE = False
FastMCP = None

try:
    # mcp >= 1.2 may ship FastMCP at the top-level fastmcp package
    FastMCP = importlib.import_module("fastmcp").FastMCP
    MCP_AVAILABLE = True
except (ImportError, AttributeError):
    pass

if not MCP_AVAILABLE:
    try:
        # Older mcp packages may still have it here
        FastMCP = importlib.import_module("mcp.server.fastmcp").FastMCP
        MCP_AVAILABLE = True
    except (ImportError, AttributeError):
        pass

if MCP_AVAILABLE:
    mcp_server = FastMCP("rumbo-mcp")
    # Legacy SSE app - what Claude Desktop expects at /mcp/sse + /mcp/messages
    mcp_sse_app = mcp_server.sse_app()
    mcp_http_app = None  # Streamable HTTP (optional)

    async def resolve_account_id(query: str) -> str:
        from server import db
        # Try direct ID
        acc = await db.accounts.find_one({"id": query})
        if acc:
            return acc["id"]
        # Try partial name (case-insensitive)
        acc = await db.accounts.find_one({"name": {"$regex": query, "$options": "i"}})
        if acc:
            return acc["id"]
        return query

    async def _get_raw_cache_data(account_id: str, period: int):
        from server import db
        acc_id = await resolve_account_id(account_id)
        cache = await db.meta_cache.find_one(
            {"account_id": acc_id, "cache_type": "combined", "period": period}, {"_id": 0}
        )
        if not cache:
            raise ValueError(f"No cache data found for account {acc_id} and period {period} days. Period must be pre-synced in MongoDB.")
        return cache["data"]

    @mcp_server.tool()
    async def list_active_accounts() -> str:
        """Returns a list of all active advertising accounts managed in RUMBO, including their IDs and names."""
        from server import get_overview
        try:
            overview_data = await get_overview(period=30)
            cards = getattr(overview_data, "cards", overview_data)
            result = [{"id": card.id, "name": card.name, "roas_actual": card.roas_actual, "total_spend": card.total_spend} for card in cards]
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.error(f"list_active_accounts error: {e}", exc_info=True)
            return f"Error: {e}"

    @mcp_server.tool()
    async def get_account_overview(account_id: str, days: int = 30) -> str:
        """Fetches high-level metrics (ROAS, Spend, Conversions, Billing) for a specific account over the last N days.
        
        Args:
            account_id: Meta Account ID or exact/partial Account Name (e.g. 'Canela')
            days: Period in days (e.g. 7, 14, 30). Must correspond to a cached period.
        """
        from server import get_dashboard
        try:
            acc_id = await resolve_account_id(account_id)
            dash = await get_dashboard(acc_id, period=days)
            res = {
                "account": dash.account.model_dump(),
                "overview": dash.overview.model_dump(),
                "budget": dash.budget.model_dump(),
                "batch_summary": dash.batch_summary.model_dump()
            }
            return json.dumps(res, indent=2, default=str)
        except Exception as e:
            logger.error(f"get_account_overview error: {e}", exc_info=True)
            return f"Error: {e}"

    @mcp_server.tool()
    async def get_underperforming_ads(account_id: str, days: int = 30) -> str:
        """Gets a list of active ads for an account that are currently spending money but failing to meet target ROAS goals (Losing Ads).
        
        Args:
            account_id: Meta Account ID or exact/partial Account Name (e.g. 'Canela')
            days: Period in days (e.g. 7, 14, 30). Must correspond to a cached period.
        """
        from server import get_dashboard
        try:
            acc_id = await resolve_account_id(account_id)
            dash = await get_dashboard(acc_id, period=days)
            res = [ad.model_dump() for ad in dash.losing_ads]
            return json.dumps(res, indent=2, default=str)
        except Exception as e:
            logger.error(f"get_underperforming_ads error: {e}", exc_info=True)
            return f"Error: {e}"

    @mcp_server.tool()
    async def get_top_ads(account_id: str, days: int = 30, limit: int = 10) -> str:
        """Gets the best performing ads by ROAS for the specified period, to see what to scale.
        
        Args:
            account_id: Meta Account ID or exact/partial Account Name (e.g. 'Canela')
            days: Period in days (e.g. 7, 14, 30). Must correspond to a cached period.
            limit: Maximum number of top ads to return.
        """
        from server import get_dashboard
        try:
            acc_id = await resolve_account_id(account_id)
            dash = await get_dashboard(acc_id, period=days)
            res = [ad.model_dump() for ad in dash.top_ads[:limit]]
            return json.dumps(res, indent=2, default=str)
        except Exception as e:
            logger.error(f"get_top_ads error: {e}", exc_info=True)
            return f"Error: {e}"

    @mcp_server.tool()
    async def compare_periods(account_id: str, days_a: int = 7, days_b: int = 7) -> str:
        """Compares two consecutive periods (e.g. this week vs last week) for the account overview metrics. 
        days_a is the recent period length, days_b is the previous period length. 
        Note: The cached period lengths must exist in MongoDB (e.g., 7 + 7 = 14 requires a 14-day cache to exist).
        
        Args:
            account_id: Meta Account ID or exact/partial Account Name (e.g. 'Canela')
            days_a: Days for the most recent period (e.g. 7 for last 7 days)
            days_b: Days for the previous consecutive period (e.g. 7 to compare against the 7 days prior)
        """
        try:
            acc_id = await resolve_account_id(account_id)
            cache_a = await _get_raw_cache_data(acc_id, days_a)
            cache_ab = await _get_raw_cache_data(acc_id, days_a + days_b)
            
            ins_a = cache_a.get("account_insights", {})
            ins_ab = cache_ab.get("account_insights", {})

            spend_recent = float(ins_a.get("spend", 0))
            spend_total = float(ins_ab.get("spend", 0))
            spend_prev = max(0.0, spend_total - spend_recent)

            rev_recent = float(ins_a.get("conversion_value", 0))
            rev_total = float(ins_ab.get("conversion_value", 0))
            rev_prev = max(0.0, rev_total - rev_recent)

            conv_recent = int(ins_a.get("conversions", 0))
            conv_total = int(ins_ab.get("conversions", 0))
            conv_prev = max(0, conv_total - conv_recent)

            roas_recent = round(rev_recent / spend_recent, 2) if spend_recent > 0 else 0
            roas_prev = round(rev_prev / spend_prev, 2) if spend_prev > 0 else 0

            return json.dumps({
                "recent_period_days": days_a,
                "previous_period_days": days_b,
                "recent": {
                    "spend": round(spend_recent, 2),
                    "revenue": round(rev_recent, 2),
                    "conversions": conv_recent,
                    "roas": roas_recent
                },
                "previous": {
                    "spend": round(spend_prev, 2),
                    "revenue": round(rev_prev, 2),
                    "conversions": conv_prev,
                    "roas": roas_prev
                }
            }, indent=2)
        except Exception as e:
            logger.error(f"compare_periods error: {e}", exc_info=True)
            return f"Error: {e}"

    @mcp_server.tool()
    async def get_creative_fatigue(account_id: str, days: int = 14) -> str:
        """Detects ads with significantly falling CTR but stable ROAS by comparing the first half and second half of the period.
        
        Args:
            account_id: Meta Account ID or exact/partial Account Name (e.g. 'Canela')
            days: Period in days (e.g. 14, 30). The system will compare the recent half (days/2) against the previous half.
        """
        try:
            acc_id = await resolve_account_id(account_id)
            half_days = days // 2
            
            cache_all = await _get_raw_cache_data(acc_id, days)
            cache_half = await _get_raw_cache_data(acc_id, half_days)
            
            fatigued_ads = []
            ad_ins_all = cache_all.get("ad_insights", {})
            ad_ins_half = cache_half.get("ad_insights", {})
            
            ads_info = {str(a["id"]): a for a in cache_all.get("ads", [])}
            
            for ad_id, ins_all in ad_ins_all.items():
                ins_half = ad_ins_half.get(ad_id, {})
                
                imp_recent = int(ins_half.get("impressions", 0))
                clicks_recent = int(ins_half.get("clicks", 0))
                spend_recent = float(ins_half.get("spend", 0))
                rev_recent = float(ins_half.get("conversion_value", 0))
                ctr_recent = (clicks_recent / imp_recent * 100) if imp_recent > 0 else 0
                roas_recent = (rev_recent / spend_recent) if spend_recent > 0 else 0
                
                imp_total = int(ins_all.get("impressions", 0))
                clicks_total = int(ins_all.get("clicks", 0))
                spend_total = float(ins_all.get("spend", 0))
                rev_total = float(ins_all.get("conversion_value", 0))
                
                imp_prev = max(0, imp_total - imp_recent)
                clicks_prev = max(0, clicks_total - clicks_recent)
                spend_prev = max(0.0, spend_total - spend_recent)
                rev_prev = max(0.0, rev_total - rev_recent)
                ctr_prev = (clicks_prev / imp_prev * 100) if imp_prev > 0 else 0
                roas_prev = (rev_prev / spend_prev) if spend_prev > 0 else 0
                
                if imp_recent > 500 and imp_prev > 500:
                    if ctr_recent < (ctr_prev * 0.8) and ctr_prev > 0.5:
                        ad_name = ads_info.get(ad_id, {}).get("name", "Unknown Ad")
                        fatigued_ads.append({
                            "ad_id": ad_id,
                            "ad_name": ad_name,
                            "ctr_previous": round(ctr_prev, 2),
                            "ctr_recent": round(ctr_recent, 2),
                            "roas_previous": round(roas_prev, 2),
                            "roas_recent": round(roas_recent, 2),
                            "spend_recent": round(spend_recent, 2)
                        })
                        
            return json.dumps(fatigued_ads, indent=2)
        except Exception as e:
            logger.error(f"get_creative_fatigue error: {e}", exc_info=True)
            return f"Error: {e}"

    @mcp_server.tool()
    async def get_all_accounts_summary(days: int = 7) -> str:
        """Returns ROAS, spend, and status of ALL active accounts in a single call. Excellent for morning checks."""
        from server import db
        try:
            accounts = await db.accounts.find({"active": True}, {"id": 1, "name": 1, "roas_objetivo": 1, "_id": 0}).to_list(100)
            summary = []
            
            for acc in accounts:
                try:
                    cache = await _get_raw_cache_data(acc["id"], days)
                    ins = cache.get("account_insights", {})
                    spend = float(ins.get("spend", 0))
                    rev = float(ins.get("conversion_value", 0))
                    roas = round(rev / spend, 2) if spend > 0 else 0
                    
                    target = acc.get("roas_objetivo", 0)
                    status = "OK"
                    if target and roas < target:
                        status = "UNDERPERFORMING"
                        
                    summary.append({
                        "id": acc["id"],
                        "name": acc["name"],
                        "spend": round(spend, 2),
                        "roas": roas,
                        "target_roas": target,
                        "status": status
                    })
                except Exception:
                    summary.append({
                         "id": acc["id"],
                         "name": acc["name"],
                         "status": "CACHE_MISSING"
                    })
                    
            return json.dumps(summary, indent=2)
        except Exception as e:
            logger.error(f"get_all_accounts_summary error: {e}", exc_info=True)
            return f"Error: {e}"

else:
    mcp_server = None
    mcp_sse_app = None
    mcp_http_app = None
    SseServerTransport = None

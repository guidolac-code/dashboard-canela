import asyncio
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

TIENDANUBE_API_BASE_URL = "https://api.tiendanube.com"
TIENDANUBE_API_VERSION = os.environ.get("TIENDANUBE_API_VERSION", "v1")
TIENDANUBE_OAUTH_TOKEN_URL = "https://www.tiendanube.com/apps/authorize/token"
DEFAULT_USER_AGENT = os.environ.get(
    "TIENDANUBE_USER_AGENT",
    "Rumbo (guido@adversive.com.ar)",
)


class TiendanubeConfigError(RuntimeError):
    pass


class TiendanubeRateLimiter:
    """Token bucket aligned with Tiendanube's 40 burst / 2 req per second limit."""

    def __init__(self, capacity: int = 40, refill_per_second: float = 2.0):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated_at
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._updated_at = now

            if self._tokens >= 1:
                self._tokens -= 1
                return

            wait_seconds = (1 - self._tokens) / self.refill_per_second
            await asyncio.sleep(wait_seconds)
            self._tokens = 0
            self._updated_at = time.monotonic()


def _read_specific_zshrc_value(name: str) -> Optional[str]:
    """Read one allowed variable from ~/.zshrc without executing shell code."""
    if not name.startswith("TIENDANUBE_"):
        return None

    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return None

    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}=(.+?)\s*$")
    for line in zshrc.read_text(errors="ignore").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        raw_value = match.group(1).split(" #", 1)[0].strip()
        try:
            return shlex.split(raw_value)[0]
        except ValueError:
            return raw_value.strip("\"'")
    return None


def get_tiendanube_demo_config() -> dict[str, str]:
    access_token = os.environ.get("TIENDANUBE_ACCESS_TOKEN_DEMO") or _read_specific_zshrc_value(
        "TIENDANUBE_ACCESS_TOKEN_DEMO"
    )
    store_id = os.environ.get("TIENDANUBE_STORE_ID_DEMO") or _read_specific_zshrc_value(
        "TIENDANUBE_STORE_ID_DEMO"
    )

    missing = [
        name
        for name, value in {
            "TIENDANUBE_ACCESS_TOKEN_DEMO": access_token,
            "TIENDANUBE_STORE_ID_DEMO": store_id,
        }.items()
        if not value
    ]
    if missing:
        raise TiendanubeConfigError(f"Faltan variables de Tiendanube: {', '.join(missing)}")

    return {
        "access_token": access_token,
        "store_id": store_id,
        "user_agent": DEFAULT_USER_AGENT,
    }


def get_tiendanube_oauth_config() -> dict[str, str]:
    client_id = (
        os.environ.get("TIENDANUBE_CLIENT_ID")
        or os.environ.get("TIENDANUBE_APP_ID")
        or _read_specific_zshrc_value("TIENDANUBE_CLIENT_ID")
        or _read_specific_zshrc_value("TIENDANUBE_APP_ID")
        or "36445"
    )
    client_secret = (
        os.environ.get("TIENDANUBE_CLIENT_SECRET")
        or os.environ.get("TIENDANUBE_APP_SECRET")
        or _read_specific_zshrc_value("TIENDANUBE_CLIENT_SECRET")
        or _read_specific_zshrc_value("TIENDANUBE_APP_SECRET")
    )

    missing = [
        name
        for name, value in {
            "TIENDANUBE_CLIENT_ID/TIENDANUBE_APP_ID": client_id,
            "TIENDANUBE_CLIENT_SECRET/TIENDANUBE_APP_SECRET": client_secret,
        }.items()
        if not value
    ]
    if missing:
        raise TiendanubeConfigError(f"Faltan variables OAuth de Tiendanube: {', '.join(missing)}")

    return {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
        "token_url": TIENDANUBE_OAUTH_TOKEN_URL,
    }


def parse_link_header(link_header: str) -> dict[str, str]:
    links: dict[str, str] = {}
    if not link_header:
        return links

    for part in link_header.split(","):
        match = re.search(r'<([^>]+)>\s*;\s*rel="([^"]+)"', part.strip())
        if match:
            url, rel = match.groups()
            links[rel] = url
    return links


def summarize_order(order: dict[str, Any]) -> dict[str, Any]:
    customer = order.get("customer") or {}
    products = order.get("products") or []
    coupons = order.get("coupon") or order.get("coupons") or []
    if isinstance(coupons, dict):
        coupons = [coupons]

    return {
        "id": order.get("id"),
        "number": order.get("number"),
        "created_at": order.get("created_at"),
        "completed_at": order.get("completed_at"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "currency": order.get("currency"),
        "subtotal": order.get("subtotal"),
        "discount": order.get("discount"),
        "total": order.get("total"),
        "total_paid_by_customer": order.get("total_paid_by_customer"),
        "products": [
            {
                "id": product.get("id"),
                "product_id": product.get("product_id"),
                "variant_id": product.get("variant_id"),
                "name": product.get("name"),
                "quantity": product.get("quantity"),
                "price": product.get("price"),
            }
            for product in products
        ],
        "coupon": coupons,
        "customer": {
            "id": customer.get("id"),
            "name": customer.get("name"),
            "email": customer.get("email") or order.get("contact_email"),
            "phone": customer.get("phone") or order.get("contact_phone"),
        },
    }


class TiendanubeOrdersClient:
    def __init__(
        self,
        store_id: str,
        access_token: str,
        user_agent: str = DEFAULT_USER_AGENT,
        base_url: str = TIENDANUBE_API_BASE_URL,
        api_version: str = TIENDANUBE_API_VERSION,
        client: Optional[httpx.AsyncClient] = None,
        rate_limiter: Optional[TiendanubeRateLimiter] = None,
    ):
        self.store_id = str(store_id)
        self.access_token = access_token
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version.strip("/")
        self.client = client
        self.rate_limiter = rate_limiter or TiendanubeRateLimiter()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    def orders_url(self) -> str:
        return f"{self.base_url}/{self.api_version}/{self.store_id}/orders"

    async def fetch_orders(
        self,
        per_page: int = 30,
        max_pages: int = 10,
        params: Optional[dict[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        request_params = {"per_page": min(max(per_page, 1), 200)}
        if params:
            request_params.update({key: value for key, value in params.items() if value is not None})

        orders: list[dict[str, Any]] = []
        page_count = 0
        next_url: Optional[str] = self.orders_url()
        rate_limit_headers: dict[str, Optional[str]] = {}

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30)
        try:
            while next_url and page_count < max_pages:
                await self.rate_limiter.acquire()
                response = await client.get(next_url, headers=self.headers, params=request_params)
                if response.status_code == 429:
                    reset_ms = int(response.headers.get("x-rate-limit-reset", "500"))
                    await asyncio.sleep(max(reset_ms / 1000, 0.5))
                    response = await client.get(next_url, headers=self.headers, params=request_params)

                response.raise_for_status()
                batch = response.json()
                if not isinstance(batch, list):
                    raise ValueError("Tiendanube devolvió una respuesta inesperada para /orders")

                orders.extend(batch)
                page_count += 1
                rate_limit_headers = {
                    "limit": response.headers.get("x-rate-limit-limit"),
                    "remaining": response.headers.get("x-rate-limit-remaining"),
                    "reset": response.headers.get("x-rate-limit-reset"),
                    "total_count": response.headers.get("x-total-count"),
                }

                next_url = parse_link_header(response.headers.get("Link", "")).get("next")
                request_params = {}
                if next_url and not next_url.startswith("http"):
                    next_url = urljoin(self.base_url, next_url)
        finally:
            if owns_client:
                await client.aclose()

        metadata = {
            "store_id": self.store_id,
            "orders_count": len(orders),
            "pages_fetched": page_count,
            "has_more": bool(next_url),
            "rate_limit": rate_limit_headers,
        }
        return orders, metadata


async def fetch_demo_orders(
    per_page: int = 30,
    max_pages: int = 10,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    config = get_tiendanube_demo_config()
    client = TiendanubeOrdersClient(**config)
    orders, metadata = await client.fetch_orders(per_page=per_page, max_pages=max_pages, params=params)
    summaries = [summarize_order(order) for order in orders]

    logger.info(
        "Tiendanube demo orders pull ok: store_id=%s orders=%s pages=%s",
        metadata["store_id"],
        metadata["orders_count"],
        metadata["pages_fetched"],
    )
    for summary in summaries[:5]:
        logger.info(
            "Order %s total=%s discount=%s products=%s customer=%s",
            summary.get("number") or summary.get("id"),
            summary.get("total"),
            summary.get("discount"),
            [product.get("name") for product in summary.get("products", [])],
            summary.get("customer", {}).get("email"),
        )

    return {
        "metadata": metadata,
        "orders": summaries,
        "raw_orders": orders,
    }

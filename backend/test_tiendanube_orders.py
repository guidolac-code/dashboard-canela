import asyncio

from tiendanube_orders import parse_link_header, summarize_order


def test_parse_link_header_returns_next_url():
    header = (
        '<https://api.tiendanube.com/v1/123/orders?page=3&per_page=100>; rel="next", '
        '<https://api.tiendanube.com/v1/123/orders?page=50&per_page=100>; rel="last"'
    )

    links = parse_link_header(header)

    assert links["next"] == "https://api.tiendanube.com/v1/123/orders?page=3&per_page=100"
    assert links["last"] == "https://api.tiendanube.com/v1/123/orders?page=50&per_page=100"


def test_summarize_order_keeps_commercial_fields():
    order = {
        "id": 10,
        "number": 100,
        "created_at": "2026-07-12T10:00:00+0000",
        "total": "1200.00",
        "subtotal": "1500.00",
        "discount": "300.00",
        "currency": "ARS",
        "coupon": [{"code": "TEST10", "type": "percentage"}],
        "contact_email": "comprador@example.com",
        "products": [
            {
                "id": 1,
                "product_id": 2,
                "variant_id": 3,
                "name": "Producto demo",
                "quantity": 2,
                "price": "750.00",
            }
        ],
        "customer": {"id": 55, "name": "Comprador Demo"},
    }

    summary = summarize_order(order)

    assert summary["total"] == "1200.00"
    assert summary["discount"] == "300.00"
    assert summary["coupon"][0]["code"] == "TEST10"
    assert summary["products"][0]["name"] == "Producto demo"
    assert summary["customer"]["email"] == "comprador@example.com"


def test_rate_limiter_importable():
    from tiendanube_orders import TiendanubeRateLimiter

    limiter = TiendanubeRateLimiter()
    asyncio.run(limiter.acquire())

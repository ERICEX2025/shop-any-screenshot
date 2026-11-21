"""Shop MCP client (Shopify's hosted agent commerce server). No API key, no session, no initialize call.

Every call is one stateless JSON-RPC POST to https://mcp.shop.com/ (docs: https://shop.dev/docs/quickstart).
We use three tools: search_catalog, create_cart, create_checkout. We never call complete_checkout.
"""
import re
import time
import uuid

import httpx

URL = "https://mcp.shop.com/"
PROTO = "2026-07-28"
_http = httpx.Client(timeout=40)


class ShopError(RuntimeError):
    pass


def call(tool, arguments, retries=2):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": tool, "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTO,
                "io.modelcontextprotocol/clientInfo": {"name": "shelfcart", "version": "0.1.0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/call", "Mcp-Name": tool,
    }
    for attempt in range(retries + 1):
        r = _http.post(URL, json=body, headers=headers)
        if r.status_code == 429 and attempt < retries:
            time.sleep(min(float(r.headers.get("Retry-After", 1.5)), 5))
            continue
        if r.status_code >= 500 and attempt < retries:
            time.sleep(0.8)
            continue
        break
    if r.status_code != 200:
        raise ShopError(f"{tool}: HTTP {r.status_code} {r.text[:200]}")
    j = r.json()
    if "error" in j:
        raise ShopError(f"{tool}: {j['error']}")
    res = j["result"]
    if res.get("isError"):
        text = (res.get("content") or [{}])[0].get("text", "")
        raise ShopError(f"{tool}: {text[:300]}")
    return res["structuredContent"]


def search(query, country="US", currency="USD", limit=6):
    """Global Catalog search across Shopify merchants. Returns flat candidates (one featured variant each)."""
    d = call("search_catalog", {"catalog": {
        "query": query,
        "context": {"address_country": country, "currency": currency},
        "filters": {"ships_to": {"country": country}},
        "pagination": {"limit": limit},
    }})
    out = []
    for p in d.get("products", []):
        v = (p.get("variants") or [None])[0]
        if not v:
            continue
        price = v.get("price") or {}
        if price.get("currency") != currency:  # context is a soft hint: some offers come back in other currencies
            continue
        media = v.get("media") or p.get("media") or [{}]
        seller = v.get("seller") or {}
        out.append({
            "product_id": p["id"],
            "variant_id": v["id"],
            "title": re.sub(r"<[^>]+>", "", p.get("title", "")).strip(),
            "variant_title": v.get("title", ""),
            "options": {o["name"]: [x["label"] for x in o.get("values", [])][:8] for o in p.get("options", [])},
            "price": price.get("amount"),
            "currency": price.get("currency"),
            "image": media[0].get("url", ""),
            "url": v.get("url", ""),
            "seller": seller.get("name", ""),
            "shop": seller.get("domain", ""),
            "rating": (p.get("rating") or {}).get("value"),
            "rating_count": (p.get("rating") or {}).get("count"),
        })
    return out


def _lines(items):
    return [{"item": {"id": i["variant_id"]}, "quantity": int(i.get("quantity", 1))} for i in items]


def _summary(d):
    return {
        "id": d["id"],
        "currency": d.get("currency"),
        "status": d.get("status"),
        "continue_url": d.get("continue_url"),
        "expires_at": d.get("expires_at"),
        "totals": [{"type": t["type"], "label": t.get("display_text", t["type"]), "amount": t["amount"]} for t in d.get("totals", [])],
        "discounts": [x.get("title") for x in (d.get("discounts") or {}).get("applied", [])],
        "lines": [{
            "variant_id": li["item"]["id"], "title": li["item"].get("title"), "price": li["item"].get("price"),
            "image": li["item"].get("image_url"), "quantity": li["quantity"],
        } for li in d.get("line_items", [])],
        "messages": [m.get("content") for m in d.get("messages", []) if m.get("type") == "error"],
    }


def create_cart(shop, items, country="US", currency="USD"):
    """One cart per merchant. Places no order. continue_url opens the merchant's own cart page."""
    d = call("create_cart", {
        "shop": shop,
        "cart": {"line_items": _lines(items), "context": {"address_country": country, "currency": currency}},
        "meta": {"idempotency-key": str(uuid.uuid4())},
    })
    return _summary(d)


def create_checkout(shop, cart_id, country="US", currency="USD"):
    """Turn a cart into a checkout session. Creates no order: continue_url is the page where the BUYER reviews and pays."""
    d = call("create_checkout", {
        "shop": shop,
        "checkout": {"cart_id": cart_id, "line_items": [], "context": {"address_country": country, "currency": currency}},
        "meta": {"idempotency-key": str(uuid.uuid4())},
    })
    return _summary(d)

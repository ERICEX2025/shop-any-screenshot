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
_token = None  # Shop access token after an optional "Sign in with Shop" (app/shopauth.py). Memory only, never logged or saved.


def set_token(tok):
    global _token
    _token = tok or None


class ShopError(RuntimeError):
    pass


def call(tool, arguments, retries=2, need_auth=False):
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
        auth = {"Authorization": "Bearer " + _token} if _token else {}
        r = _http.post(URL, json=body, headers={**headers, **auth})
        if r.status_code == 401 and auth and not need_auth:
            set_token(None)  # an expired token fails the call instead of running signed out: drop it and go on anonymously
            continue
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


def _cand(p, v):
    """One offer (a product and one of its variants) as the flat dict the rest of the app uses."""
    price, lp = v.get("price") or {}, v.get("list_price") or {}
    media = v.get("media") or p.get("media") or [{}]
    seller = v.get("seller") or {}
    return {
        "product_id": p["id"],
        "variant_id": v["id"],
        "title": re.sub(r"<[^>]+>", "", p.get("title", "")).strip(),
        "variant_title": v.get("title", ""),
        "options": {o["name"]: [x["label"] for x in o.get("values", [])][:8] for o in p.get("options", [])},
        "selected": {o["name"]: o["label"] for o in v.get("options") or []},   # the option values of THIS variant
        "price": price.get("amount"),
        "currency": price.get("currency"),
        # list_price is the merchant's compare-at price: when it is higher than price the item is discounted
        "list_price": lp.get("amount") if lp.get("currency") == price.get("currency") and (lp.get("amount") or 0) > (price.get("amount") or 0) else None,
        "available": (v.get("availability") or {}).get("available"),   # True, False, or None when the API does not say
        "image": media[0].get("url", ""),
        "url": v.get("url", ""),
        "seller": seller.get("name", ""),
        "shop": seller.get("domain", ""),
        "shop_url": seller.get("url", ""),
        "rating": (p.get("rating") or {}).get("value"),
        "rating_count": (p.get("rating") or {}).get("count"),
    }


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
        if (v.get("price") or {}).get("currency") != currency:  # context is a soft hint: some offers come back in other currencies
            continue
        out.append(_cand(p, v))
    return out


def product(product_id, selected=None, country="US", currency="USD"):
    """A listing's real options (colour, size...) with per-value availability and swatch photos, and the variant that
    the given selection resolves to. selected: {option name: label}. Verified Nov 21 2026: about 0.2 s, no key."""
    cat = {"id": product_id, "context": {"address_country": country, "currency": currency}}
    if selected:
        cat["selected"] = [{"name": k, "label": v} for k, v in selected.items()]
    p = call("get_product", {"catalog": cat})["product"]
    v = (p.get("variants") or [None])[0]
    cand = _cand(p, v) if v else None
    if cand and selected and any(cand["selected"].get(k) != val for k, val in selected.items()):
        cand["dropped"] = True   # Shopify may relax a selection that does not exist: tell the page it is not what was asked
    return {
        "options": [{"name": o["name"], "values": [{
            "label": x["label"], "available": x.get("available"), "exists": x.get("exists", True),
            "swatch": (x.get("swatch") or {}).get("image_url") or "", "color": (x.get("swatch") or {}).get("color") or "",
        } for x in o.get("values", [])][:24]} for o in p.get("options", [])],
        "variant": cand,
    }


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

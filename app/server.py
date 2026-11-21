"""ShelfCart: pan a camera across a shelf, get a real Shopify cart.

Hydrogen    looks at 1 to 5 camera frames and lists the distinct products (brand, type, size, box, search query).
Shop MCP    searches Shopify's Global Catalog for each product (real, no API key).
Decision 1  picks which search result is the same product as the photo crop and says how sure it is;
            unsure matches are not pre-selected, the user has to tick them.
Shop MCP    builds one cart per merchant, and a checkout session only when the user asks. We never complete a checkout.
Run: uvicorn app.server:app --port 8202
"""
import base64
import io
import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from google import genai  # noqa: E402
from PIL import Image  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import shopify  # noqa: E402

HYDROGEN, DECISION = "gemini-4-hydrogen", "gemini-decision-1"
AUTO_MATCH = 0.8    # Decision 1 P(picked listing is the same product) needed to pre-select a match
AUTO_DETECT = 0.6   # Hydrogen confidence needed to pre-select a match
AUTO_LEGIBLE = 0.6  # Decision 1 P(label readable in the crop) needed to pre-select a match
beta = genai.Client(http_options={"api_version": "v1beta"})
app = FastAPI()
pool = ThreadPoolExecutor(max_workers=12)

DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string", "description": "Brand as printed on the item, or empty if not readable"},
                    "name": {"type": "string", "description": "Product line and type, e.g. 'Gentle Skin Cleanser'"},
                    "variant": {"type": "string", "description": "Flavor, scent, color or model if visible, else empty"},
                    "size": {"type": "string", "description": "Size or count as printed (e.g. '16 fl oz'), else empty. Never guess."},
                    "frame": {"type": "integer", "description": "Index (0-based) of the frame where this item is clearest"},
                    "box_2d": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "[ymin, xmin, ymax, xmax] of the item in that frame, each 0-1000",
                    },
                    "confidence": {"type": "number", "description": "0-1: how sure you are of brand and product"},
                    "query": {"type": "string", "description": "Short shopping search query: brand + product + variant + size"},
                },
                "required": ["brand", "name", "variant", "size", "frame", "box_2d", "confidence", "query"],
            },
        }
    },
    "required": ["products"],
}

DETECT_PROMPT = """These are {n} frames (in order, frame 0 first) from a camera panning across a shelf, desk or the inside of a bag.
List every DISTINCT purchasable product you can see. The same item often appears in several frames: list it once,
using the frame where its label is clearest. Two units of the same product count once.
Read brand, product name, variant and size from the packaging. If a field is not readable leave it empty; never invent it.
Skip furniture, the shelf itself, people, and anything you cannot name at least by product type.
confidence: 0.9+ only if you can read the brand and product name; below 0.5 if you are guessing from shape or color.
Maximum 12 products, most recognizable first."""


def _wait(it):
    while getattr(it, "status", None) in ("in_progress", "queued"):
        time.sleep(0.5)
        it = beta.interactions.get(id=it.id)
    return it


def _split(data_url):
    head, b64 = data_url.split(",", 1)
    return b64, head.split(":")[1].split(";")[0]


def _crop(img, box, pad=0.06):
    """box is [ymin, xmin, ymax, xmax] on a 0-1000 grid. Returns the crop as JPEG base64."""
    w, h = img.size
    try:
        y0, x0, y1, x1 = [max(0, min(1000, int(v))) / 1000 for v in box[:4]]
    except Exception:
        y0, x0, y1, x1 = 0, 0, 1, 1
    if y1 - y0 < 0.02 or x1 - x0 < 0.02:
        y0, x0, y1, x1 = 0, 0, 1, 1
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    c = img.crop((int(max(0, x0 - px) * w), int(max(0, y0 - py) * h), int(min(1, x1 + px) * w), int(min(1, y1 + py) * h)))
    c.thumbnail((640, 640))
    buf = io.BytesIO()
    c.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def detect(frames):
    items = [{"type": "image", "data": b, "mime_type": m} for b, m in frames]
    items.append({"type": "text", "text": DETECT_PROMPT.format(n=len(frames))})
    it = _wait(beta.interactions.create(
        model=HYDROGEN, input=items,
        response_format={"type": "text", "mime_type": "application/json", "schema": DETECT_SCHEMA},
        generation_config={"thinking_level": "low"},
    ))
    return json.loads(it.output_text)["products"][:12]


def _label(p):
    return " ".join(x for x in (p["brand"], p["name"], p["variant"], p["size"]) if x).strip()


def decide(crop_b64, product, cands):
    """Decision 1: which catalog result is the same product as the crop? Returns (index or None, whether choice and yes/no agree, choice probs, per-listing P(same), legible)."""
    criteria, questions = {}, {}
    for i, c in enumerate(cands):
        opts = "; ".join(f"{k}: {', '.join(v)}" for k, v in c["options"].items())
        desc = f"{c['title']}" + (f" (options: {opts})" if opts else "") + f". Sold by {c['seller']}."
        criteria[f"c{i}"] = desc
        # Several stores often list the identical product, which splits the choice probabilities.
        # So each listing also gets its own yes/no: the gate uses this, the choice only ranks.
        questions[f"same{i}"] = {
            "type": "noul",
            "instructions": f"Is this store listing the same product (same brand, same product line, same variant) as the item "
                            f"in the photo? A different size is still yes; a different product from the brand is no. Listing: {desc}",
        }
    criteria["none"] = "None of the listings is this product (different brand, different product type, or cannot tell)."
    questions["pick"] = {
        "type": "choice",
        "instructions": "Which store listing is the SAME product as the item in the photo? Brand and product line must "
                        "match. Prefer the listing whose size, count, scent or flavor matches what is printed on the item. "
                        "A multi-pack or a different product from the same brand is not a match if a closer one exists.",
        "criteria": criteria,
    }
    questions["legible"] = {
        "type": "noul",
        "instructions": "Is the brand name or product name on the item clearly readable in the photo?",
    }
    d = beta.decisions.create(
        model=DECISION,
        input=[
            {"type": "image", "data": crop_b64, "mime_type": "image/jpeg"},
            {"type": "text", "text": f"The photo shows an item a shopper owns and wants to buy again. "
                                     f"A vision model read its label as: {_label(product)}."},
        ],
        questions=questions,
    )
    a = d.answers
    pick = a["pick"].choice
    probs = {k: round(float(v), 3) for k, v in (a["pick"].probabilities or {}).items()}
    same = [round(float(a[f"same{i}"].noul), 3) for i in range(len(cands))]
    idx = int(pick[1:]) if pick and pick != "none" and pick[1:].isdigit() else None
    chosen = idx is not None and same[idx] >= 0.5   # the choice and the yes/no agree
    if not chosen:
        # No agreed winner. Offer the most plausible listing as a suggestion the user must tick, or nothing.
        alt = max(range(len(same)), key=lambda i: same[i] + probs.get(f"c{i}", 0))
        idx = alt if same[alt] >= 0.5 else None
    return idx, chosen, probs, same, round(float(a["legible"].noul), 3)


def match(product, img):
    t = {}
    t0 = time.time()
    crop = _crop(img, product.get("box_2d") or [])
    try:
        cands = shopify.search(product["query"] or _label(product))
        if not cands and product["size"]:  # retry without the size, which often over-constrains
            cands = shopify.search(" ".join(x for x in (product["brand"], product["name"], product["variant"]) if x))
        err = None
    except Exception as e:  # keep the other products alive if one search fails
        cands, err = [], str(e)[:200]
    t["search_ms"] = round((time.time() - t0) * 1000)
    out = {**product, "label": _label(product), "crop": "data:image/jpeg;base64," + crop, "candidates": cands,
           "pick": None, "match_confidence": 0.0, "probs": {}, "legible": None, "status": "none", "error": err}
    if cands:
        t1 = time.time()
        try:
            idx, chosen, probs, same, legible = decide(crop, product, cands)
            conf = same[idx] if idx is not None else 0.0
            out.update(pick=idx, match_confidence=conf, probs=probs, legible=legible)
            for i, c in enumerate(cands):
                c["p"], c["same"] = probs.get(f"c{i}"), same[i]
            if idx is None:
                out["status"] = "none"
            elif chosen and conf >= AUTO_MATCH and product["confidence"] >= AUTO_DETECT and legible >= AUTO_LEGIBLE:
                out["status"] = "auto"
            else:
                out["status"] = "confirm"
        except Exception as e:
            out["error"] = "decision: " + str(e)[:200]
        t["decide_ms"] = round((time.time() - t1) * 1000)
    out["timing"] = t
    return out


def consolidate(results):
    """Fewer merchants means fewer checkouts. If another listing that Decision 1 also accepts is sold by a merchant
    we are already buying from, switch to it. Only swaps between listings the gate already passed."""
    def ok(p):
        if p["pick"] is None:
            return []
        top = p["candidates"][p["pick"]].get("p") or 0
        return [i for i, c in enumerate(p["candidates"])
                if i == p["pick"] or ((c.get("same") or 0) >= AUTO_MATCH and (c.get("p") or 0) >= 0.5 * top)]
    count = {}
    for p in results:
        for shop in {p["candidates"][i]["shop"] for i in ok(p)}:
            count[shop] = count.get(shop, 0) + 1
    for p in results:
        idxs = ok(p)
        if not idxs:
            continue
        best = max(idxs, key=lambda i: (count[p["candidates"][i]["shop"]], i == p["pick"], p["candidates"][i].get("same") or 0))
        if best != p["pick"] and count[p["candidates"][best]["shop"]] > count[p["candidates"][p["pick"]]["shop"]]:
            p["pick"], p["consolidated"] = best, True
            p["match_confidence"] = p["candidates"][best]["same"]


class ScanIn(BaseModel):
    frames: list[str]  # data URLs, 1 to 5 JPEG frames


@app.post("/api/scan")
def scan(body: ScanIn):
    if not body.frames:
        raise HTTPException(400, "no frames")
    t0 = time.time()
    frames = [_split(f) for f in body.frames[:5]]
    imgs = [Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB") for b, _ in frames]  # decode now: PIL lazy loading is not thread-safe
    products = detect(frames)
    t_detect = time.time() - t0
    t1 = time.time()
    results = list(pool.map(lambda p: match(p, imgs[min(max(int(p.get("frame", 0)), 0), len(imgs) - 1)]), products))
    consolidate(results)
    return {
        "products": results,
        "timing": {"detect_ms": round(t_detect * 1000), "match_ms": round((time.time() - t1) * 1000),
                   "total_ms": round((time.time() - t0) * 1000), "frames": len(frames)},
    }


class Line(BaseModel):
    shop: str
    variant_id: str
    quantity: int = 1


class CartIn(BaseModel):
    lines: list[Line]


@app.post("/api/cart")
def cart(body: CartIn):
    """Called only after the user ticked items and pressed Build cart. One real Shopify cart per merchant."""
    t0 = time.time()
    by_shop = {}
    for line in body.lines:
        by_shop.setdefault(line.shop, []).append(line.model_dump())

    def one(kv):
        shop, items = kv
        try:
            return {"shop": shop, **shopify.create_cart(shop, items)}
        except Exception as e:
            return {"shop": shop, "error": str(e)[:300]}

    carts = list(pool.map(one, by_shop.items()))
    return {"carts": carts, "ms": round((time.time() - t0) * 1000)}


class CheckoutIn(BaseModel):
    shop: str
    cart_id: str


@app.post("/api/checkout")
def checkout(body: CheckoutIn):
    """Prepares a checkout session and returns the page where the buyer reviews and pays. Never completes it."""
    t0 = time.time()
    try:
        return {**shopify.create_checkout(body.shop, body.cart_id), "ms": round((time.time() - t0) * 1000)}
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")

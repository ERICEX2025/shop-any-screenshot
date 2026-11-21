"""ShelfCart: shop any screenshot (or camera pan). Items in the image become real, buyable Shopify products.

Hydrogen    looks at 1 to 5 camera frames and lists the distinct products (brand, type, size, box, search query).
Shop MCP    searches Shopify's Global Catalog for each product (real, no API key).
Decision 1  picks which search result is the same product as the photo crop and says how sure it is.
Neon        second look whenever Decision 1 has no confident exact match (common for clothes, furniture, decor):
            reads the crop and the listings carefully, checks pack size and variant, then confirms an exact listing,
            or picks the closest SIMILAR item with one line on how it differs, or says nothing is close.
            Only confident exact matches are pre-selected; similar items are suggestions the user has to tick.
Shop MCP    builds one cart per merchant, and a checkout session only when the user asks. We never complete a checkout.
Run: uvicorn app.server:app --port 8202
"""
import base64
import io
import json
import pathlib
import time

import httpx
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from google import genai  # noqa: E402
from PIL import Image  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import shopify  # noqa: E402

HYDROGEN, NEON, DECISION = "gemini-4-hydrogen", "gemini-4-neon", "gemini-decision-1"
NEON_TIMEOUT = 30  # seconds; after that the item keeps Decision 1's verdict
NEON_HEDGE = 14    # seconds before a second, parallel Neon call is started for the same item
AUTO_MATCH = 0.8    # Decision 1 P(picked listing is the same product) needed to pre-select a match
AUTO_DETECT = 0.6   # Hydrogen confidence needed to pre-select a match
AUTO_LEGIBLE = 0.6  # Decision 1 P(label readable in the crop) needed to pre-select a match
beta = genai.Client(http_options={"api_version": "v1beta"})
app = FastAPI()
pool = ThreadPoolExecutor(max_workers=12)
thumb_pool = ThreadPoolExecutor(max_workers=16)
neon_pool = ThreadPoolExecutor(max_workers=32)  # separate pool so a slow Neon call can be abandoned at NEON_TIMEOUT

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

DETECT_PROMPT = """These are {n} image(s) (frame 0 first). Each is either a SCREENSHOT (a social media post, a product page,
a room or outfit photo, a frame from a video) or a camera frame panning across a shelf, desk or bag.
List every DISTINCT purchasable physical item you can see, including what a person is wearing or holding, furniture,
lighting, decor and gear. The same item can appear in several frames: list it once, using the frame where it is clearest.
Ignore app chrome and UI (status bar, buttons, icons, avatars, like counts), text overlays, captions and watermarks,
people's faces and bodies, pets, walls, floors and windows. Use caption text only as a hint for a brand or product name.
Branded items: read brand, product name, variant and size from the packaging or logo. If a field is not readable leave
it empty; never invent a brand. Unbranded items (clothes, furniture, decor): leave brand empty, put the item type in
name (e.g. 'Wool Overcoat', 'Arc Floor Lamp') and colour + material + style in variant.
box_2d must tightly frame the item itself, not the whole image.
query: for a branded item, brand + product + variant + size. For an unbranded item, a descriptive shopping query a
person would type: type, colour, material, style (e.g. 'camel wool long overcoat women', 'mid century walnut coffee table round').
confidence: 0.9+ only if you can read the brand and product name; 0.5 to 0.7 for an unbranded item you can describe well.
Maximum 12 items, the most prominent first."""


def _wait(it, timeout=120):
    end = time.time() + timeout
    while getattr(it, "status", None) in ("in_progress", "queued"):
        if time.time() > end:
            raise TimeoutError("model call timed out")
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


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "integer", "description": "Number of the best listing, or -1 if nothing is close"},
        "kind": {"type": "string", "enum": ["exact", "similar", "none"],
                 "description": "exact = same brand, product line and variant as the item. similar = a different product that "
                                "looks and works alike, or the same product in a different pack size/size. none = nothing close."},
        "confident": {"type": "boolean", "description": "For exact only: true if the brand is readable on the item and brand, line, variant and quantity all fit"},
        "pack": {"type": "string", "enum": ["same", "different", "unknown"],
                 "description": "Does the chosen listing sell the same quantity as the photo (single item vs multi-pack/case, same size)?"},
        "reason": {"type": "string", "description": "One short phrase (max 14 words) for the shopper. exact: why it is the same. "
                                                     "similar: how it differs, e.g. 'same shape, darker wood' or 'single bar, not the 12-pack'. none: why."},
    },
    "required": ["choice", "kind", "confident", "pack", "reason"],
}

REVIEW_PROMPT = """The first image is a crop from a screenshot or camera frame showing one item a shopper wants to buy.
A fast vision model described it as: {label}. Search query used: {query}.
A fast matcher found NO confident exact match among these store listings. Take a careful second look.
Where a store photo of a listing is attached, compare it with the shopper's photo; otherwise go by the title and options.

Listings:
{listings}

1. Look at the item: type, colour, material, shape, style, and any brand, variant, size or count printed on it.
2. If one listing is the SAME product (same brand, product line, variant): kind = exact. Check quantity: a single item
   is not matched by a multi-pack, case, bundle or sampler when a single-item listing exists; check size too.
   confident = true only if you can read the brand on the item and nothing about variant or quantity clearly differs.
   If only a different pack size or size of the same product is offered, answer kind = similar and say so.
3. Otherwise (clothes, furniture, decor, unbranded gear usually have no exact listing): pick the listing that LOOKS
   closest: same type of thing with the nearest colour, material, shape and style. kind = similar.
4. If no listing is even the same type of item, or all look clearly different in style: choice = -1, kind = none.

reason for a similar item: name the most visible DIFFERENCE between the listing and the photo in a few plain words,
e.g. 'same shape, darker wood', 'shorter, double-breasted', 'cover only, no insert', 'single bar, not the 12-pack'.
If you see no difference, say what matches ('same colour and cut'). Never write that the brand is unknown,
unconfirmed or unverified: the shopper knows a similar item is another brand."""


_img_http = httpx.Client(timeout=4, follow_redirects=True)


def _thumb(url):
    """Listing photo as small JPEG base64, or None. Neon compares these with the crop (shape, colour, material)."""
    if not url:
        return None
    try:
        r = _img_http.get(url + ("&" if "?" in url else "?") + "width=320")
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def review(crop_b64, product, cands):
    """Neon's second look when Decision 1 has no confident exact match: confirm an exact one, pick the closest similar item, or nothing."""
    lines = []
    for i, c in enumerate(cands):
        opts = "; ".join(f"{k}: {', '.join(v)}" for k, v in c["options"].items())
        price = f"${c['price'] / 100:.2f}" if c.get("price") is not None else "?"
        lines.append(f"{i}. {c['title']}" + (f" | variant shown: {c['variant_title']}" if c.get("variant_title") else "")
                     + (f" | options: {opts}" if opts else "") + f" | {price} | sold by {c['seller']}"
                     + f" | fast matcher P(same)={c.get('same')}")
    thumbs = list(thumb_pool.map(lambda c: _thumb(c.get("image")), cands))
    items = [{"type": "text", "text": "PHOTO of the item the shopper wants:"},
             {"type": "image", "data": crop_b64, "mime_type": "image/jpeg"}]
    for i, th in enumerate(thumbs):
        if th:
            items += [{"type": "text", "text": f"Store photo of listing {i}:"}, {"type": "image", "data": th, "mime_type": "image/jpeg"}]
    items.append({"type": "text", "text": REVIEW_PROMPT.format(label=_label(product), query=product.get("query", ""), listings="\n".join(lines))})
    it = _wait(beta.interactions.create(
        model=NEON, input=items,
        response_format={"type": "text", "mime_type": "application/json", "schema": REVIEW_SCHEMA},
        generation_config={"thinking_level": "low"},
    ), timeout=NEON_TIMEOUT)
    r = json.loads(it.output_text)
    if not isinstance(r.get("choice"), int) or not 0 <= r["choice"] < len(cands) or r.get("kind") == "none":
        r["choice"], r["kind"] = None, "none"
    r["reason"] = str(r.get("reason") or "")[:200]
    return r


def match(product, img, use_neon=True):
    t = {}
    t0 = time.time()
    crop = _crop(img, product.get("box_2d") or [])
    try:
        cands = shopify.search(product["query"] or _label(product))
        if not cands:  # retry with a shorter query (size and variant often over-constrain)
            cands = shopify.search(" ".join(x for x in (product["brand"], product["name"], product["variant"]) if x))
        if not cands:
            cands = shopify.search(" ".join(x for x in (product["brand"], product["name"]) if x))
        err = None
    except Exception as e:  # keep the other products alive if one search fails
        cands, err = [], str(e)[:200]
    t["search_ms"] = round((time.time() - t0) * 1000)
    out = {**product, "label": _label(product), "crop": "data:image/jpeg;base64," + crop, "candidates": cands,
           "pick": None, "match_confidence": 0.0, "probs": {}, "legible": None, "status": "none", "error": err,
           "decided_by": None, "reason": "", "neon": None, "kind": None}
    if cands:
        t1 = time.time()
        try:
            idx, chosen, probs, same, legible = decide(crop, product, cands)
            conf = same[idx] if idx is not None else 0.0
            out.update(pick=idx, match_confidence=conf, probs=probs, legible=legible, decided_by=DECISION)
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
    out["pending"] = bool(cands) and out["status"] in ("confirm", "none") and not out["error"]  # wants Neon's second look
    if out["pending"] and use_neon == "defer":
        return _finish(out)          # the page paints now and asks /api/review for this item
    if out["pending"] and use_neon:
        second_look(out)
    return _finish(out)


def _finish(out):
    out["kind"] = {"auto": "exact", "confirm": "exact", "similar": "similar"}.get(out["status"])
    return out


def _hedged(fn):
    """Most Neon reviews take 7 to 12 s, about 1 in 15 hangs for 30 s or more. If the first call is not back after
    NEON_HEDGE seconds, start a second one and take whichever answers first. Gives up at NEON_TIMEOUT overall."""
    end = time.time() + NEON_TIMEOUT
    futs = [neon_pool.submit(fn)]
    done, _ = wait(futs, timeout=NEON_HEDGE)
    if not done:
        futs.append(neon_pool.submit(fn))
    err = None
    while futs and time.time() < end:
        done, rest = wait(futs, timeout=max(0.1, end - time.time()), return_when=FIRST_COMPLETED)
        for f in done:
            if f.exception() is None:
                return f.result()
            err = f.exception()
        futs = list(rest)
        if not done:
            break
    raise err or TimeoutError(f"Neon did not answer in {NEON_TIMEOUT}s")


def second_look(out):
    """Everything without a confident exact match goes to Neon; items Decision 1 is sure about never do.
    Runs inside the per-product worker (or its own /api/review request), so the reviews are parallel."""
    cands, t2 = out["candidates"], time.time()
    try:
        r = _hedged(lambda: review(out["crop"].split(",", 1)[1], out, cands))
        out["neon"] = {**r, "decision_pick": out["pick"], "decision_status": out["status"]}
        out["reason"], out["decided_by"] = r["reason"], NEON
        if r["choice"] is None:
            out.update(status="none", pick=None, match_confidence=0.0)
        else:
            out["pick"] = r["choice"]
            out["match_confidence"] = cands[r["choice"]].get("same") or 0.0
            if r["kind"] == "exact" and r["confident"] and r.get("pack") != "different":
                out["status"] = "auto"      # Exact, confirmed by Neon: pre-selected
            elif r["kind"] == "exact":
                out["status"] = "confirm"   # probably the same product, but the user must tick it
            else:
                out["status"] = "similar"   # closest similar item: a suggestion, never pre-selected
    except Exception as e:  # Neon failing or being slow must never break a scan: the item keeps Decision 1's verdict
        out["neon"] = {"error": (type(e).__name__ + " " + str(e))[:200]}
    out["pending"] = False
    out.setdefault("timing", {})["neon_ms"] = round((time.time() - t2) * 1000)
    return _finish(out)


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
        if not idxs or p.get("decided_by") == NEON:  # Neon checked pack size on its pick: do not swap it
            continue
        best = max(idxs, key=lambda i: (count[p["candidates"][i]["shop"]], i == p["pick"], p["candidates"][i].get("same") or 0))
        if best != p["pick"] and count[p["candidates"][best]["shop"]] > count[p["candidates"][p["pick"]]["shop"]]:
            p["pick"], p["consolidated"] = best, True
            p["match_confidence"] = p["candidates"][best]["same"]


class ScanIn(BaseModel):
    frames: list[str]  # data URLs, 1 to 5 JPEG frames
    defer: bool = False  # true: return after Decision 1 and let the page call /api/review per pending item


@app.post("/api/scan")
def scan(body: ScanIn):
    if not body.frames:
        raise HTTPException(400, "no frames")
    return pipeline([_split(f) for f in body.frames[:5]], "defer" if body.defer else True)


class ReviewIn(BaseModel):
    products: list[dict]  # items from /api/scan with pending = true, sent back unchanged


@app.post("/api/review")
def review_some(body: ReviewIn):
    """Neon's second look for a few items, so the page can show Decision 1's results first and fill these in.
    The page sends its pending items in up to 5 requests: a browser only opens 6 connections per host."""
    for p in body.products:
        if not p.get("candidates") or not str(p.get("crop", "")).startswith("data:image/"):
            raise HTTPException(400, "each product needs candidates and a crop")
    return {"products": list(pool.map(second_look, body.products[:12]))}


def pipeline(frames, use_neon=True):
    """frames: [(base64, mime)]. The whole scan; also called directly by tests/eval.py."""
    t0 = time.time()
    imgs = [Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB") for b, _ in frames]  # decode now: PIL lazy loading is not thread-safe
    products = detect(frames)
    t_detect = time.time() - t0
    t1 = time.time()
    results = list(pool.map(lambda p: match(p, imgs[min(max(int(p.get("frame", 0)), 0), len(imgs) - 1)], use_neon), products))
    consolidate(results)
    return {
        "products": results,
        "timing": {"detect_ms": round(t_detect * 1000), "match_ms": round((time.time() - t1) * 1000),
                   "total_ms": round((time.time() - t0) * 1000), "frames": len(frames),
                   "neon_reviews": sum(1 for p in results if p.get("neon") or p.get("pending"))},
        "models": {"see": HYDROGEN, "match": DECISION, "second_look": NEON},
    }


@app.get("/api/eval")
def eval_results():
    """Latest tests/eval.py run (pipeline vs baseline), shown in the page's "Does it work" section."""
    f = ROOT / "data" / "eval.json"
    if not f.exists():
        raise HTTPException(404, "no eval yet: run tests/eval.py")
    return json.loads(f.read_text())


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

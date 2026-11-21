"""ShelfCart: shop any image on the web, change an item to how you want it, buy the real thing.

Hydrogen    looks at 1 to 5 camera frames and lists the distinct products (brand, type, size, box, search query).
Shop MCP    searches Shopify's Global Catalog for each product (real, no API key).
Decision 1  picks which search result is the same product as the photo crop and says how sure it is.
Neon        second look whenever Decision 1 has no confident exact match (common for clothes, furniture, decor):
            reads the crop and the listings carefully, checks pack size and variant, then confirms an exact listing,
            or picks the closest SIMILAR item with one line on how it differs, or says nothing is close.
            Only confident exact matches are pre-selected; similar items are suggestions the user has to tick.
Remix       Hydrogen suggests variations of an item (or the shopper types one, or picks a real option the merchant
            sells); Shopify is searched for the new version and Gemini's image model redraws the picture (app/remix.py).
Shop MCP    builds one cart per merchant, and a checkout session only when the user asks. We never complete a checkout.
Run: uvicorn app.server:app --port 8202
"""
import base64
import hashlib
import io
import json
import pathlib
import re
import threading
import time

import httpx
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from google import genai  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import remix, shopauth, shopify  # noqa: E402

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


def review(crop_b64, product, cands, prompt=None):
    """Neon's second look when Decision 1 has no confident exact match: confirm an exact one, pick the closest similar item, or nothing.
    prompt: a different instruction text with a {listings} slot (the remix review uses this)."""
    lines = []
    for i, c in enumerate(cands):
        opts = "; ".join(f"{k}: {', '.join(v)}" for k, v in c["options"].items())
        price = f"${c['price'] / 100:.2f}" if c.get("price") is not None else "?"
        lines.append(f"{i}. {c['title']}" + (f" | variant shown: {c['variant_title']}" if c.get("variant_title") else "")
                     + (f" | options: {opts}" if opts else "") + f" | {price} | sold by {c['seller']}"
                     + (f" | fast matcher P(fits)={c.get('fit')}" if prompt else f" | fast matcher P(same)={c.get('same')}"))
    thumbs = list(thumb_pool.map(lambda c: _thumb(c.get("image")), cands))
    items = [{"type": "text", "text": "PHOTO of the item the shopper wants:"},
             {"type": "image", "data": crop_b64, "mime_type": "image/jpeg"}]
    for i, th in enumerate(thumbs):
        if th:
            items += [{"type": "text", "text": f"Store photo of listing {i}:"}, {"type": "image", "data": th, "mime_type": "image/jpeg"}]
    items.append({"type": "text", "text": prompt.format(listings="\n".join(lines)) if prompt else
                  REVIEW_PROMPT.format(label=_label(product), query=product.get("query", ""), listings="\n".join(lines))})
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


# ---------------------------------------------------------------------------------------------------------------
# Image store: the page and the Chrome extension both hand over an image once and refer to it by id afterwards.

UPLOADS, RENDERS = ROOT / "data" / "uploads", ROOT / "data" / "renders"
KEEP_FILES = 120   # per folder; the oldest are deleted (disk is small)


def _prune(folder):
    files = sorted(folder.glob("*.*"), key=lambda p: p.stat().st_mtime)
    for p in files[:-KEEP_FILES]:
        p.unlink(missing_ok=True)


def store_image(data):
    """Normalise to an RGB JPEG of at most 1280 px (what the models are tested with) and store it by content hash."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise HTTPException(400, "that is not an image this server can read")
    im = im.convert("RGB")
    im.thumbnail((1280, 1280))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    b = buf.getvalue()
    iid = hashlib.sha1(b).hexdigest()[:16]
    UPLOADS.mkdir(parents=True, exist_ok=True)
    path = UPLOADS / (iid + ".jpg")
    if not path.exists():
        path.write_bytes(b)
        _prune(UPLOADS)
    return {"id": iid, "width": im.size[0], "height": im.size[1]}


def load_image(iid):
    if not re.fullmatch(r"[0-9a-f]{16}", iid or ""):
        raise HTTPException(400, "bad image id")
    path = UPLOADS / (iid + ".jpg")
    if not path.exists():
        raise HTTPException(404, "that image is no longer stored: give the page the image again")
    return path.read_bytes()


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Only this page and browser extensions may POST: a random website open in the browser must not start scans."""
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and not re.match(r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|chrome-extension://[a-p]{32})$", origin):
        return JSONResponse({"detail": "cross-site requests are not accepted"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"ok": True, "app": "shelfcart", "models": {"see": HYDROGEN, "match": DECISION, "second_look": NEON, "redraw": remix.IMAGE}}


@app.post("/api/image")
async def image_in(request: Request):
    """Body: the raw image bytes (any Content-Type), or JSON {"data_url": "data:image/...;base64,..."}.
    The extension's service worker posts bytes here; the page posts what was pasted or dropped."""
    data = await request.body()
    if len(data) > 30_000_000:
        raise HTTPException(413, "image larger than 30 MB")
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = base64.b64decode(json.loads(data)["data_url"].split(",", 1)[1])
        except Exception:
            raise HTTPException(400, "expected {data_url}")
    if not data:
        raise HTTPException(400, "empty body")
    return store_image(data)


@app.get("/api/image/{iid}")
def image_out(iid: str):
    return Response(load_image(iid), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------------------------------------------------------
# Progressive scan: one streamed response, one JSON object per line, so the page can paint each stage as it lands:
#   items (Hydrogen)  ->  match per item (Shopify search + Decision 1)  ->  review per item (Neon)  ->  done

class StreamIn(BaseModel):
    image_ids: list[str] = []
    frames: list[str] = []   # data URLs, accepted for older callers


def _slim(p):
    return {**p, "label": _label(p)}


@app.post("/api/scan/stream")
def scan_stream(body: StreamIn):
    if body.image_ids:
        frames = [(base64.b64encode(load_image(i)).decode(), "image/jpeg") for i in body.image_ids[:5]]
    elif body.frames:
        frames = [_split(f) for f in body.frames[:5]]
    else:
        raise HTTPException(400, "no image")

    def gen():
        line = lambda o: (json.dumps(o) + "\n").encode()
        t0 = time.time()
        try:
            imgs = [Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB") for b, _ in frames]
            products = detect(frames)
        except Exception as e:
            yield line({"type": "error", "stage": "items", "message": (type(e).__name__ + ": " + str(e))[:300]})
            return
        t_detect = time.time() - t0
        frame_of = lambda p: min(max(int(p.get("frame", 0) or 0), 0), len(imgs) - 1)
        for p in products:
            p["frame"] = frame_of(p)
        yield line({"type": "items", "ms": round(t_detect * 1000), "products": [
            {**_slim(p), "crop": "data:image/jpeg;base64," + _crop(imgs[p["frame"]], p.get("box_2d") or [])} for p in products]})
        t1 = time.time()
        results = [None] * len(products)
        futs = {pool.submit(match, p, imgs[p["frame"]], "defer"): i for i, p in enumerate(products)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except Exception as e:
                results[i] = {**_slim(products[i]), "candidates": [], "pick": None, "status": "none", "pending": False,
                              "error": str(e)[:200], "kind": None, "decided_by": None, "reason": "", "neon": None}
            yield line({"type": "match", "i": i, "ms": round((time.time() - t0) * 1000), "product": results[i]})
        before = [r["pick"] for r in results]
        consolidate([r for r in results if r.get("candidates")])
        for i, r in enumerate(results):
            if r["pick"] != before[i]:
                yield line({"type": "match", "i": i, "ms": round((time.time() - t0) * 1000), "product": r})
        t_match = time.time() - t1
        pend = {pool.submit(second_look, r): i for i, r in enumerate(results) if r.get("pending")}
        yield line({"type": "matched", "ms": round((time.time() - t0) * 1000), "reviews": len(pend)})
        for f in as_completed(pend):
            i = pend[f]
            try:
                results[i] = f.result()
            except Exception:
                results[i]["pending"] = False
            yield line({"type": "review", "i": i, "ms": round((time.time() - t0) * 1000), "product": results[i]})
        yield line({"type": "done", "timing": {"detect_ms": round(t_detect * 1000), "match_ms": round(t_match * 1000),
                                               "total_ms": round((time.time() - t0) * 1000), "neon_reviews": len(pend)}})

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------------------------------------------
# Remix: change one item, then (a) real products for the new version and (b) the picture redrawn. Both start at once.

_suggest_cache, _render_lock, _render_jobs = {}, threading.Lock(), {}
render_pool = ThreadPoolExecutor(max_workers=4)
RENDER_TIMEOUT = 90


class SuggestIn(BaseModel):
    image_id: str
    label: str


@app.post("/api/remix/suggest")
def remix_suggest(body: SuggestIn):
    """Hydrogen proposes 4 variations of this item that suit the scene. About 6 s; cached per image and item."""
    key = (body.image_id, body.label)
    if key not in _suggest_cache:
        t0 = time.time()
        try:
            v = remix.suggest(load_image(body.image_id), body.label[:120])
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, "Hydrogen could not suggest variations: " + str(e)[:200])
        _suggest_cache[key] = {"variations": v, "ms": round((time.time() - t0) * 1000), "model": HYDROGEN}
    return _suggest_cache[key]


_WISH_LEAD = re.compile(r"^\s*(please\s+)?(can you\s+)?(make|turn|change|show|give|i want|i'd like|i would like)?\s*(me\s+)?(it|this|that|the \w+)?\s*"
                        r"(to be|into|to|in|with|as|but)?\s*(an?\s+)?", re.I)


def wish_variation(product, wish):
    """A variation the shopper typed ("make it corduroy"). No model call, so the search can start at once:
    the query is the wish without its lead-in words plus the kind of item."""
    wish = " ".join(wish.split())[:120]
    core = _WISH_LEAD.sub("", wish, count=1).strip(" .!") or wish
    name = (product.get("name") or product.get("label") or "").strip()
    return {"label": core[:28], "kind": "style", "edit": wish, "query": f"{core} {name}".strip()[:80], "typed": True}


REMIX_REVIEW = """The first image is a crop from a picture showing one item: {label}.
The shopper does NOT want it as it is. They want this change: {edit}
(short name of the wanted version: {vlabel}). Search query used: {query}.
Imagine the item with that change applied, then judge the store listings against that imagined version.
Where a store photo of a listing is attached, look at it; otherwise go by the title and options.

Listings:
{{listings}}

Pick the listing that is closest to the WANTED version: same type of thing, with the requested change, and otherwise
as near as possible to the original's shape and style. Because the wanted version is imagined, an answer is never
"exact": use kind = similar for the closest listing, or choice = -1 and kind = none if no listing is that type of
item or none has the requested change. Set confident = false and pack = unknown.
reason: a few plain words (max 14) on how the listing compares with the wanted version, naming the most visible
difference, e.g. 'right colour, squarer arms' or 'corduroy as asked, lighter shade'."""


def remix_decide(crop_b64, product, var, cands):
    """Decision 1, about 1 s: how well does each listing fit the item WITH the change? Ranks the cards right away."""
    criteria, questions = {}, {}
    want = f"{var['label']}: {var['edit']}"
    for i, c in enumerate(cands):
        opts = "; ".join(f"{k}: {', '.join(v)}" for k, v in c["options"].items())
        desc = f"{c['title']}" + (f" (options: {opts})" if opts else "") + f". Sold by {c['seller']}."
        criteria[f"c{i}"] = desc
        questions[f"fit{i}"] = {"type": "noul", "instructions":
            f"Is this store listing the same type of item as the one in the photo AND does it have the change the shopper "
            f"asked for ({want})? Listing: {desc}"}
    criteria["none"] = "None of the listings is that type of item with the requested change."
    questions["pick"] = {"type": "choice", "criteria": criteria, "instructions":
        f"Which store listing is closest to the item in the photo after this change: {want}?"}
    d = beta.decisions.create(model=DECISION, questions=questions, input=[
        {"type": "image", "data": crop_b64, "mime_type": "image/jpeg"},
        {"type": "text", "text": f"The photo shows: {_label(product)}. The shopper wants it changed: {want}."}])
    a = d.answers
    fit = [round(float(a[f"fit{i}"].noul), 3) for i in range(len(cands))]
    probs = {k: round(float(v), 3) for k, v in (a["pick"].probabilities or {}).items()}
    return fit, probs


class RemixIn(BaseModel):
    product: dict            # the found item as the page has it (label, name, crop ...)
    variation: dict = {}     # {label, kind, edit, query}; or
    wish: str = ""           # free text typed by the shopper
    candidates: list[dict] = []   # for /api/remix/review: the listings /api/remix/match returned


def _variation(body):
    v = wish_variation(body.product, body.wish) if body.wish.strip() else dict(body.variation)
    if not v.get("query") or not v.get("edit"):
        raise HTTPException(400, "a variation needs an edit and a query, or send a wish")
    return v


def _crop_b64(product):
    crop = str(product.get("crop", ""))
    if not crop.startswith("data:image/"):
        raise HTTPException(400, "the product needs its crop")
    return crop.split(",", 1)[1]


@app.post("/api/remix/match")
def remix_match(body: RemixIn):
    """Real products for the changed item: Shopify search with the variation's query, ranked by Decision 1.
    Returns in 1 to 3 s with pending = true; the page then asks /api/remix/review for Neon's pick."""
    var, crop, t0 = _variation(body), _crop_b64(body.product), time.time()
    out = {"variation": var, "candidates": [], "pick": None, "status": "none", "reason": "", "decided_by": None,
           "pending": False, "error": None, "timing": {}}
    try:
        cands = shopify.search(var["query"])
        if not cands and body.product.get("name"):
            cands = shopify.search(f"{var['label']} {body.product['name']}")
    except Exception as e:
        cands, out["error"] = [], str(e)[:200]
    out["candidates"], out["timing"]["search_ms"] = cands, round((time.time() - t0) * 1000)
    if cands:
        t1 = time.time()
        try:
            fit, probs = remix_decide(crop, body.product, var, cands)
            for i, c in enumerate(cands):
                c["fit"], c["p"] = fit[i], probs.get(f"c{i}")
            best = max(range(len(cands)), key=lambda i: fit[i] + (probs.get(f"c{i}") or 0))
            if fit[best] >= 0.5:
                out.update(pick=best, status="similar")
            out["decided_by"] = DECISION
        except Exception as e:
            out["error"] = "decision: " + str(e)[:200]
        out["timing"]["decide_ms"] = round((time.time() - t1) * 1000)
        out["pending"] = True
    return out


@app.post("/api/remix/review")
def remix_review(body: RemixIn):
    """Neon's careful look at the listings for the changed item: the closest one and how it differs, or nothing close."""
    var, crop, cands, t0 = _variation(body), _crop_b64(body.product), body.candidates, time.time()
    if not cands:
        raise HTTPException(400, "no candidates")
    prompt = REMIX_REVIEW.format(label=_label(body.product), edit=var["edit"], vlabel=var["label"], query=var["query"])
    try:
        r = _hedged(lambda: review(crop, body.product, cands, prompt=prompt))
    except Exception as e:
        return {"error": (type(e).__name__ + " " + str(e))[:200], "ms": round((time.time() - t0) * 1000)}
    return {"pick": r["choice"], "status": "none" if r["choice"] is None else "similar", "reason": r["reason"],
            "decided_by": NEON, "ms": round((time.time() - t0) * 1000)}


class RenderIn(BaseModel):
    image_id: str
    label: str        # the item, e.g. "Sofa green velvet"
    edit: str         # how it should look
    ref_url: str = ""  # optional: the merchant's photo of a real option, so the redraw follows the real product
    box_2d: list[int] = []   # the item's box, to check that the rest of the picture was kept


def scene_kept(orig, new, box):
    """Did the redraw keep the picture and change only the item? The image model now and then returns a recomposed
    scene (other framing, the app chrome gone). Checks the aspect ratio, then the mean grey-level difference outside
    the item's box on a 96 px copy: kept scenes measure 2 to 6, a recomposed one about 100."""
    if abs(new.size[0] / new.size[1] - orig.size[0] / orig.size[1]) > 0.05 * orig.size[0] / orig.size[1]:
        return False
    w, h = 96, max(8, round(96 * orig.size[1] / orig.size[0]))
    d = ImageChops.difference(orig.convert("L").resize((w, h)), new.convert("L").resize((w, h))).load()
    try:
        y0, x0, y1, x1 = [max(0, min(1000, int(v))) / 1000 for v in box[:4]]
    except Exception:
        y0 = x0 = y1 = x1 = 0
    if (y1 - y0) * (x1 - x0) > 0.7:   # the item fills the picture: nothing outside it to compare
        return True
    tot = n = 0
    for y in range(h):
        for x in range(w):
            if not (x0 - .06 <= x / w <= x1 + .06 and y0 - .06 <= y / h <= y1 + .06):
                tot, n = tot + d[x, y], n + 1
    return n == 0 or tot / n < 22


def _do_render(path, body):
    ref = None
    if body.ref_url.startswith("https://cdn.shopify.com/"):
        t = _thumb(body.ref_url)
        ref = base64.b64decode(t) if t else None
    src = load_image(body.image_id)
    orig = Image.open(io.BytesIO(src)).convert("RGB")
    for attempt in range(2):   # one retry when the model redrew the whole scene
        data, mime = remix.render(src, body.label[:120], body.edit[:300], ref=ref)
        im = Image.open(io.BytesIO(data)).convert("RGB")   # re-encode: known format, bounded size
        if scene_kept(orig, im, body.box_2d):
            break
    else:
        raise RuntimeError("the image model changed more than the item, twice, so no preview is shown")
    im.thumbnail((1600, 1600))
    RENDERS.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=88)
    _prune(RENDERS)
    return im.size


@app.post("/api/remix/render")
def remix_render(body: RenderIn):
    """The picture redrawn with only that item changed (Gemini image model, about 15 s). Cached per image + item + edit.
    The result is a generated preview and the page labels it as one."""
    load_image(body.image_id)   # 404 early
    key = hashlib.sha1("|".join((body.image_id, body.label, body.edit, body.ref_url)).encode()).hexdigest()[:16]
    path, t0 = RENDERS / (key + ".jpg"), time.time()
    cached = path.exists()
    if not cached:
        with _render_lock:   # two requests for the same redraw share one model call
            fut = _render_jobs.get(key)
            if fut is None:
                fut = _render_jobs[key] = render_pool.submit(_do_render, path, body)
        try:
            fut.result(timeout=RENDER_TIMEOUT)
        except Exception as e:
            raise HTTPException(504 if isinstance(e, TimeoutError) else 502,
                                "the preview could not be drawn: " + (type(e).__name__ + " " + str(e))[:200])
        finally:
            if fut.done():
                with _render_lock:
                    _render_jobs.pop(key, None)
    return {"url": f"/api/render/{key}", "cached": cached, "ms": round((time.time() - t0) * 1000), "model": remix.IMAGE}


@app.get("/api/render/{key}")
def render_out(key: str):
    if not re.fullmatch(r"[0-9a-f]{16}", key) or not (RENDERS / (key + ".jpg")).exists():
        raise HTTPException(404, "no such preview")
    return FileResponse(RENDERS / (key + ".jpg"), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


class ProductIn(BaseModel):
    product_id: str
    selected: dict[str, str] = {}


@app.post("/api/product")
def product_options(body: ProductIn):
    """A listing's REAL options from the merchant (colour, size ...), each with availability and a swatch photo where
    the catalog has one, and the exact variant (price, stock, photo) for a selection. About 0.2 s."""
    try:
        return shopify.product(body.product_id, body.selected or None)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


# ---------------------------------------------------------------------------------------------------------------
# Optional Sign in with Shop (device flow, Shop's public client: no key). See app/shopauth.py.

@app.get("/api/shop/status")
def shop_status():
    return shopauth.status()


@app.post("/api/shop/signin")
def shop_signin():
    try:
        return shopauth.start()
    except Exception as e:
        raise HTTPException(502, "Shop sign-in is not available right now: " + type(e).__name__)


@app.post("/api/shop/signout")
def shop_signout():
    return shopauth.signout()


# ---------------------------------------------------------------------------------------------------------------

WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers=_NO_CACHE)


@app.get("/panel")
def panel():
    """The same page, opened by the Chrome extension's side panel as /panel?scan=<image id>: it starts on that image."""
    return FileResponse(WEB / "index.html", headers=_NO_CACHE)

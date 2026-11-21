"""Does it work? Full pipeline vs a simple baseline, on the images in data/test/ with a hand-written answer key.

  (a) pipeline  Hydrogen detect -> Shopify search -> Decision 1 match + gate -> Neon second look (exact / similar / none)
  (b) baseline  ONE gemini-4-hydrogen call that names the products, then the FIRST Shopify search result for each,
                straight into the cart. No Decision gate, no Neon.

Usage: .venv/bin/python tests/eval.py [--runs 2] [--only bag,desk]
Calls the pipeline in-process (no server needed), needs GEMINI_API_KEY in .env, creates no carts.
Writes data/eval.json (shown on the page under "Does it work") and prints a table.

How a listing is judged: by keywords in its title, written by hand per item below. That is crude (see "hides").
  exact    brand + product type (+ variant where printed on the item); "single": True also rejects multi-packs and sets.
  similar  for items with no knowable exact listing (clothes, furniture, unbranded gear): right type of thing + main attribute.
"""
import base64, datetime, io, json, pathlib, re, sys, time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from PIL import Image  # noqa: E402
from app import server, shopify  # noqa: E402

PACK = re.compile(r"pack of|case of|box of|set of|bundle|variety|sampler|assort|\b\d+\s*[- ]?(pack|pk|ct|count|cans|bars|tins|boxes)\b|\b(x|×)\s?\d+\b|\b\d+\s?(x|×)\b")

# det: any one group, all words of it in the detected name/query. exact / similar: {"all": [...], "any": [...], "none": [...]} on the listing title + seller name.
KEY = {
    "bathroom.jpg": [
        {"item": "Dr. Bronner's Peppermint Pure-Castile Soap 32 oz", "det": [["bronner"]], "exact": {"all": ["bronner", "peppermint"], "any": ["soap"], "single": True}},
        {"item": "Cetaphil Gentle Skin Cleanser 16 fl oz", "det": [["cetaphil"]], "exact": {"all": ["cetaphil", "gentle", "cleanser"], "single": True}},
        {"item": "CeraVe Moisturizing Cream (tub)", "det": [["cerave"]], "exact": {"all": ["cerave", "moisturizing cream"], "single": True}},
        {"item": "Native Deodorant Coconut & Vanilla", "det": [["native"]], "exact": {"all": ["native", "deodorant", "coconut", "vanilla"], "single": True}},
        {"item": "The Ordinary Niacinamide serum", "det": [["ordinary"]], "exact": {"all": ["ordinary", "niacinamide"], "single": True}},
        {"item": "Burt's Bees Beeswax Lip Balm (one tube)", "det": [["burt"]], "exact": {"all": ["burt", "beeswax", "lip balm"], "single": True}},
    ],
    "bag.jpg": [
        {"item": "Liquid I.V. Hydration Multiplier Lemon Lime", "det": [["liquid", "i.v"], ["liquid", "iv"]], "exact": {"all": ["liquid", "lemon lime"], "any": ["hydration", "multiplier"]}},
        {"item": "Aquaphor Healing Ointment (tube)", "det": [["aquaphor"]], "exact": {"all": ["aquaphor", "healing ointment"], "single": True}},
        {"item": "KIND nut bar (one bar)", "det": [["kind"]], "exact": {"all": ["kind"], "any": ["bar", "nut"], "single": True}},
        {"item": "Advil (bottle)", "det": [["advil"]], "exact": {"all": ["advil"], "single": True}},
        {"item": "Orbit gum (one pack)", "det": [["orbit"]], "exact": {"all": ["orbit"], "any": ["gum"], "single": True}},
        {"item": "White wired earbuds (no readable brand)", "det": [["earpods"], ["earbuds"], ["earphones"], ["headphones"]], "similar": {"any": ["earpods", "earbuds", "earphones", "headphones"]}},
    ],
    "desk.jpg": [
        {"item": "RXBAR Chocolate Sea Salt (open box of bars)", "det": [["rxbar"]], "exact": {"any": ["rxbar", "rx bar"], "all": ["chocolate sea salt"]}},
        {"item": "LaCroix Pamplemousse (one can)", "det": [["lacroix"], ["la croix"]], "exact": {"all": ["croix", "pamplemousse"], "single": True}},
        {"item": "Hydro Flask Wide Mouth bottle, blue", "det": [["hydro flask"], ["hydroflask"]], "exact": {"any": ["hydro flask", "hydroflask"], "all": ["wide mouth"], "single": True}},
        {"item": "Altoids Peppermint (one tin)", "det": [["altoids"]], "exact": {"all": ["altoids", "peppermint"], "single": True}},
        {"item": "Post-it Notes, yellow", "det": [["post-it"], ["post it"], ["sticky notes"]], "exact": {"all": ["post-it", "yellow"]}},
        {"item": "Anker power bank (model not readable)", "det": [["anker"], ["power bank"], ["portable charger"]], "similar": {"all": ["anker"], "any": ["power", "charger"]}},
        {"item": "Black hardcover notebook (no brand)", "det": [["notebook"], ["journal"]], "similar": {"any": ["notebook", "journal"]}},
        {"item": "White ceramic mug (no brand)", "det": [["mug"]], "similar": {"any": ["mug"]}},
    ],
    "table.jpg": [
        {"item": "Louisiana Hot Sauce Original 6 oz", "det": [["louisiana"]], "exact": {"all": ["louisiana"], "any": ["hot sauce", "original sauce"], "single": True}},
        {"item": "Heinz Tomato Ketchup (one bottle)", "det": [["heinz"]], "exact": {"all": ["heinz", "ketchup"], "single": True}},
        {"item": "Glass salt shaker (no brand)", "det": [["shaker"]], "similar": {"any": ["shaker"]}},
        {"item": "Small stainless creamer pitcher (no brand)", "det": [["creamer"], ["pitcher"], ["jug"]], "similar": {"any": ["creamer", "pitcher", "jug"]}},
        {"item": "White mug (no brand)", "det": [["mug"], ["cup"]], "similar": {"any": ["mug", "cup"]}},
    ],
    "hotsauce.jpg": [  # a wall of ~300 bottles: only the two big display bottles have labels a person can read
        {"item": "Cholula Original Hot Sauce", "det": [["cholula"]], "exact": {"all": ["cholula"], "none": ["chipotle", "green", "lime", "garlic", "habanero", "reaper"], "single": True}},
    ],
    "shot_room.jpg": [
        {"item": "Green velvet sofa", "det": [["sofa"], ["couch"]], "similar": {"any": ["sofa", "couch", "loveseat"], "all": ["green"]}},
        {"item": "Round walnut coffee table", "det": [["coffee table"]], "similar": {"any": ["coffee table", "cocktail table"], "all": ["round"]}},
        {"item": "Brass arc floor lamp", "det": [["lamp"]], "similar": {"all": ["floor lamp"], "any": ["arc", "arch"]}},
        {"item": "Cream boucle armchair", "det": [["chair"]], "similar": {"all": ["chair"], "any": ["boucle", "bouclé", "sherpa", "teddy"]}},
        {"item": "Jute area rug", "det": [["rug"]], "similar": {"all": ["rug"], "any": ["jute", "sisal", "seagrass"]}},
        {"item": "White planter", "det": [["planter"], ["pot"]], "similar": {"any": ["planter", "pot"]}},
        {"item": "Mustard throw pillow", "det": [["pillow", "mustard"], ["pillow", "yellow"], ["cushion", "mustard"]], "similar": {"any": ["pillow", "cushion"], "all": ["mustard"]}},
    ],
    "shot_outfit.jpg": [
        {"item": "Camel long wool coat", "det": [["coat"]], "similar": {"all": ["coat"], "any": ["camel", "tan", "beige"]}},
        {"item": "Cream turtleneck sweater", "det": [["turtleneck"], ["sweater"]], "similar": {"any": ["turtleneck", "mock neck", "roll neck"]}},
        {"item": "Blue straight-leg jeans", "det": [["jeans"], ["denim"]], "similar": {"any": ["jean", "denim"]}},
        {"item": "White leather sneakers", "det": [["sneaker"], ["trainers"]], "similar": {"any": ["sneaker", "trainer", "low top"], "all": ["white"]}},
        {"item": "Black leather tote bag", "det": [["tote"], ["bag"]], "similar": {"any": ["tote"], "all": ["black"]}},
        {"item": "Round sunglasses", "det": [["sunglasses"]], "similar": {"all": ["sunglasses"]}},
    ],
}

BASE_SCHEMA = {"type": "object", "properties": {"products": {"type": "array", "items": {
    "type": "object", "properties": {"name": {"type": "string"}, "query": {"type": "string", "description": "shopping search query"}},
    "required": ["name", "query"]}}}, "required": ["products"]}
BASE_PROMPT = "List the products in this image that a person could buy (maximum 12). For each give its name and a short shopping search query."


def frame(path):
    img = Image.open(path).convert("RGB"); img.thumbnail((1280, 1280))
    b = io.BytesIO(); img.save(b, "JPEG", quality=85)
    return base64.b64encode(b.getvalue()).decode(), "image/jpeg"


def meets(title, req):
    t = (title or "").lower()
    return all(w in t for w in req.get("all", [])) and (not req.get("any") or any(w in t for w in req["any"])) \
        and not any(w in t for w in req.get("none", [])) and not (req.get("single") and PACK.search(t))


def title_of(c):
    # the seller counts too: a brand's own store often leaves the brand out of the title ("Deodorant Stick" sold by Native)
    return f"{c['title']} {c.get('variant_title') or ''} [sold by {c.get('seller') or ''}]" if c else ""


def run_pipeline(path):
    t = time.time()
    r = server.pipeline([frame(path)])
    out = []
    for p in r["products"]:
        pick = p["candidates"][p["pick"]] if p["pick"] is not None else None
        out.append({"text": f"{p['label']} {p['query']}".lower(), "name": p["label"], "status": p["status"], "decided_by": p["decided_by"],
                    "reason": p["reason"], "pre": pick if p["status"] == "auto" else None, "shown": pick,
                    "first": p["candidates"][0] if p["candidates"] else None})
    return out, time.time() - t


def run_baseline(path):
    t = time.time()
    b64, mime = frame(path)
    it = server._wait(server.beta.interactions.create(
        model=server.HYDROGEN, input=[{"type": "image", "data": b64, "mime_type": mime}, {"type": "text", "text": BASE_PROMPT}],
        response_format={"type": "text", "mime_type": "application/json", "schema": BASE_SCHEMA},
        generation_config={"thinking_level": "low"}))
    prods = json.loads(it.output_text)["products"][:12]

    def first(p):
        try:
            c = shopify.search(p["query"] or p["name"])
        except Exception:
            c = []
        return c[0] if c else None
    with ThreadPoolExecutor(12) as ex:
        firsts = list(ex.map(first, prods))
    out = [{"text": f"{p['name']} {p['query']}".lower(), "name": p["name"], "status": "auto" if c else "none", "decided_by": "first search result",
            "reason": "", "pre": c, "shown": c, "first": c} for p, c in zip(prods, firsts)]
    return out, time.time() - t


def score(dets, key):
    """One image, one system. Returns counters and a per-item log."""
    n = dict(found=0, correct_pre=0, wrong_pre=0, held_ok=0, held_missed=0, similar_offered=0, similar_ok=0, extra=0, extra_pre=0)
    log, used = [], set()
    for k in key:
        d = next((d for i, d in enumerate(dets) if i not in used and any(all(w in d["text"] for w in g) for g in k["det"])), None)
        if d is None:
            log.append({"item": k["item"], "result": "not found"}); continue
        used.add(dets.index(d)); n["found"] += 1
        pre, shown = d["pre"], d["shown"] or d["first"]
        if "exact" in k:
            if pre:
                ok = meets(title_of(pre), k["exact"]); n["correct_pre" if ok else "wrong_pre"] += 1
                res = "correct, pre-selected" if ok else "WRONG, pre-selected"
            else:
                ok = shown is not None and meets(title_of(shown), k["exact"]); n["held_missed" if ok else "held_ok"] += 1
                res = "held back although its top listing was right (one extra tap)" if ok else "correctly held back"
        else:  # no knowable exact listing: an honest system offers a similar item and does not claim an exact match
            if d["shown"]:
                n["similar_offered"] += 1
                ok = meets(title_of(d["shown"]), k["similar"]); n["similar_ok"] += ok
                res = ("acceptable similar item" if ok else "similar item of the WRONG kind") + (" (put in the cart unasked)" if pre else " (suggested)")
            else:
                res = "nothing offered"
        log.append({"item": k["item"], "seen_as": d["name"], "result": res, "status": d["status"], "decided_by": d["decided_by"],
                    "listing": title_of(pre or d["shown"])[:120], "reason": d["reason"]})
    for i, d in enumerate(dets):
        if i not in used:
            n["extra"] += 1; n["extra_pre"] += bool(d["pre"])
            log.append({"item": "(not in answer key)", "seen_as": d["name"], "result": "pre-selected, unscored" if d["pre"] else "not pre-selected",
                        "status": d["status"], "listing": title_of(d["pre"] or d["shown"])[:120]})
    return n, log


def main():
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 2
    only = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
    images = [f for f in KEY if (ROOT / "data/test" / f).exists() and (not only or f.split(".")[0] in only)]
    systems = {"Full pipeline": run_pipeline, "Baseline": run_baseline}
    what = {"Full pipeline": "Hydrogen + search + Decision 1 gate + Neon second look", "Baseline": "one Hydrogen call, first search result, everything into the cart"}
    tot = {s: {} for s in systems}; secs = {s: [] for s in systems}; per_run = {s: [] for s in systems}; detail = []
    for run in range(runs):
        for sname, fn in systems.items():
            with ThreadPoolExecutor(3) as ex:   # 3 images at a time: stays under the anonymous Shopify rate limit
                results = list(ex.map(lambda f: fn(ROOT / "data/test" / f), images))
            rn = {}
            for f, (dets, sec) in zip(images, results):
                n, log = score(dets, KEY[f])
                for k, v in n.items():
                    tot[sname][k] = tot[sname].get(k, 0) + v; rn[k] = rn.get(k, 0) + v
                secs[sname].append(sec)
                detail.append({"run": run + 1, "system": sname, "image": f, "seconds": round(sec, 1), **n, "items": log})
            per_run[sname].append(rn)
            print(f"run {run + 1} {sname}: {rn}", flush=True)
    n_key = sum(len(KEY[f]) for f in images); n_exact = sum(1 for f in images for k in KEY[f] if "exact" in k)
    rows = [{"name": s, "what": what[s], **tot[s], "sec_per_image": round(sum(secs[s]) / len(secs[s]), 1), "per_run": per_run[s]} for s in systems]
    out = {
        "date": datetime.date.today().isoformat(), "runs": runs, "images": images,
        "key_items": n_key * runs, "exact_possible": n_exact * runs, "similar_only": (n_key - n_exact) * runs,
        "summary": f"{len(images)} images, {n_key} hand-keyed items ({n_exact} branded with a knowable exact listing, {n_key - n_exact} unbranded where only a similar item exists), each system run {runs} times; numbers are totals over the runs.",
        "rows": rows,
        "hides": [
            "Small sample: 7 images, 2 of them synthetic screenshots made with gemini-nano-banana-2.1, keyed by one person.",
            "A listing is judged by keywords in its title, not by a person opening it. A similar item counts as acceptable if it is the right type of thing with the main attribute; nobody checked that it really looks alike.",
            "The baseline pre-selects everything, so it can never hold an item back; for unbranded items its first result lands in the cart unasked, while the pipeline only suggests it.",
            "The keyword scorer errs in both directions. Seen by hand: baseline listings that were right but spelled differently ('Moisturising', 'Post It') counted as wrong; pipeline items counted as 'held back although right' where Neon had flagged a real size difference (20 oz bottle vs 14 oz, 60 ml vs 30 ml, a 12-pack).",
            "The answer key and scorer were corrected once after a first run (brand taken from the seller name, 'RX Bar' spelling, Cholula flavours), before the run saved here.",
            "Items a system found that are not in the answer key are not scored (see extra and extra_pre in data/eval.json). The baseline put all of those in the cart too.",
            "The pipeline is slower: the baseline is one model call, the pipeline adds a Neon call for every item without a confident exact match. Seconds are per image with 3 images running at once; on the page the first results paint after Hydrogen + Decision 1 (about 8 to 12 s) and Neon fills in after.",
            "Totals over runs hide run-to-run variance; per_run in data/eval.json shows each run.",
        ],
        "failures": [
            "Dense shelves: a wall of hundreds of bottles yields only the few items with large labels (the cap is 12 items per scan).",
            "Generic unbranded items: there is no exact listing, so the result is a look-alike chosen from 6 search results, not the same item.",
            "Pack sizes: a single can or bar often only has multi-pack listings; Neon flags it in words, but the size on the shelf can still differ from the listing's default variant.",
            "One cart per merchant: the best listings usually come from different stores, so 5 items can mean 5 carts and 5 shipping fees.",
            "Run-to-run variance: Hydrogen names and boxes items slightly differently each run, so the same image can give a different item count and different matches.",
        ],
        "detail": detail,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data/eval.json").write_text(json.dumps(out, indent=1))
    print("\n" + out["summary"])
    hdr = ["", "found", "correct pre-sel", "WRONG pre-sel", "held back ok", "held back, was right", "similar ok", "extra (pre-sel)", "s/image"]
    table = [hdr] + [[r["name"], f"{r['found']}/{out['key_items']}", f"{r['correct_pre']}/{out['exact_possible']}", r["wrong_pre"], r["held_ok"], r["held_missed"],
                      f"{r['similar_ok']}/{r['similar_offered']} of {out['similar_only']}", f"{r['extra']} ({r['extra_pre']})", r["sec_per_image"]] for r in rows]
    w = [max(len(str(row[i])) for row in table) for i in range(len(hdr))]
    for row in table:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(row)))
    print("\nwrote data/eval.json")


if __name__ == "__main__":
    main()

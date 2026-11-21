"""API test of the progressive scan and the remix path against a running server (no browser).
Usage: remix_test.py image.jpg [--base http://127.0.0.1:8202] [--wish "make it corduroy"] [--no-render]
Prints when each stage arrives: items, matches, Neon reviews; then for the first item with listings: suggested
variations, products for one variation (Decision 1, then Neon), the redraw, and the real options of its listing."""
import json, sys, time
import httpx


def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


base, path = opt("--base", "http://127.0.0.1:8202"), sys.argv[1]
http = httpx.Client(base_url=base, timeout=180)
up = http.post("/api/image", content=open(path, "rb").read(), headers={"Content-Type": "image/jpeg"}).json()
print("stored image", up)
t0, products = time.time(), []
with http.stream("POST", "/api/scan/stream", json={"image_ids": [up["id"]]}) as r:
    for raw in r.iter_lines():
        if not raw:
            continue
        ev, dt = json.loads(raw), time.time() - t0
        if ev["type"] == "items":
            products = ev["products"]
            print(f"{dt:5.1f}s items: {len(products)}:", ", ".join(p["label"] for p in products))
        elif ev["type"] in ("match", "review"):
            p = products[ev["i"]] = ev["product"]
            pick = p["candidates"][p["pick"]] if p["pick"] is not None else None
            print(f"{dt:5.1f}s {ev['type']:6} {p['label'][:34]:34} {p['status']:8} pending={p['pending']!s:5} "
                  + (f"{pick['seller']} ${pick['price'] / 100:.2f} {'was $%.2f' % (pick['list_price'] / 100) if pick['list_price'] else ''} | {p['reason']}" if pick else ""))
        else:
            print(f"{dt:5.1f}s {ev}")
item = next((p for p in products if p.get("candidates")), None)
if not item:
    sys.exit("no item with listings")
print("\n== remix on:", item["label"])
t = time.time()
sug = http.post("/api/remix/suggest", json={"image_id": up["id"], "label": item["label"]}).json()
print(f"suggest {time.time() - t:.1f}s:", [(v["label"], v["query"]) for v in sug["variations"]])
wish = opt("--wish", "")
body = {"product": item, "wish": wish} if wish else {"product": item, "variation": sug["variations"][0]}
t = time.time()
m = http.post("/api/remix/match", json=body).json()
var = m["variation"]
print(f"match {time.time() - t:.1f}s for {var['label']!r} (query {var['query']!r}): {m['status']} {m['timing']}")
for i, c in enumerate(m["candidates"]):
    print("  ->" if i == m["pick"] else "    ", c.get("fit"), f"${c['price'] / 100:.2f}", c["title"][:60], f"[{c['seller']}]", "in stock" if c["available"] else c["available"])
if m["candidates"]:
    t = time.time()
    rv = http.post("/api/remix/review", json={"product": item, "variation": var, "candidates": m["candidates"]}).json()
    print(f"neon review {time.time() - t:.1f}s:", rv)
if "--no-render" not in sys.argv:
    t = time.time()
    rr = http.post("/api/remix/render", json={"image_id": up["id"], "label": item["label"], "edit": var["edit"]})
    print(f"render {time.time() - t:.1f}s:", rr.status_code, rr.json())
    t = time.time()
    print(f"render again {time.time() - t:.2f}s:", http.post("/api/remix/render", json={"image_id": up["id"], "label": item["label"], "edit": var["edit"]}).json())
withopts = next((c for p in products for c in p.get("candidates", []) if c["options"]), None)
if withopts:
    t = time.time()
    po = http.post("/api/product", json={"product_id": withopts["product_id"]}).json()
    print(f"\nreal options of {withopts['title'][:50]!r} [{withopts['seller']}] in {time.time() - t:.2f}s:")
    for o in po["options"]:
        print("  ", o["name"], [(v["label"], v["available"], bool(v["swatch"])) for v in o["values"]])

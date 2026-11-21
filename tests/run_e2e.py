"""End-to-end test against the running server. Usage: run_e2e.py image.jpg [--pan N] [--cart]
--pan N   cut the image into N overlapping side-by-side crops to imitate frames from a camera pan
--cart    also build real Shopify carts from the auto-selected matches (creates carts, never orders)
"""
import base64, io, json, sys, time
import httpx
from PIL import Image

BASE = "http://127.0.0.1:8202"
path = sys.argv[1]
pan = int(sys.argv[sys.argv.index("--pan") + 1]) if "--pan" in sys.argv else 1


def data_url(img):
    img = img.convert("RGB"); img.thumbnail((1280, 1280))
    b = io.BytesIO(); img.save(b, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


img = Image.open(path)
w, h = img.size
if pan > 1:
    win = int(w * 0.5); step = (w - win) // (pan - 1)
    frames = [data_url(img.crop((i * step, 0, i * step + win, h))) for i in range(pan)]
else:
    frames = [data_url(img)]

t = time.time()
r = httpx.post(BASE + "/api/scan", json={"frames": frames}, timeout=180).json()
print(f"== {path}  frames={len(frames)}  timing={r['timing']}  wall={time.time()-t:.1f}s")
lines = []
for p in r["products"]:
    print(f"\n[{p['status'].upper():7}] {p['label']!r}  detect={p['confidence']:.2f} legible={p['legible']} match_conf={p['match_confidence']} frame={p['frame']} box={p['box_2d']} {p['timing']} {p['error'] or ''}")
    print(f"   query: {p['query']}")
    for i, c in enumerate(p["candidates"]):
        mark = "->" if i == p["pick"] else "  "
        print(f"   {mark} p={c.get('p')} same={c.get('same')}  ${c['price']/100:.2f}  {c['title'][:70]}  [{c['seller']}]")
    if p["status"] == "auto":
        c = p["candidates"][p["pick"]]
        lines.append({"shop": c["shop"], "variant_id": c["variant_id"], "quantity": 1})
json.dump(r, open(path.rsplit(".", 1)[0] + ".result.json", "w"))
if "--cart" in sys.argv and lines:
    c = httpx.post(BASE + "/api/cart", json={"lines": lines}, timeout=120).json()
    print(f"\n== carts: {len(c['carts'])} merchants in {c['ms']} ms")
    for k in c["carts"]:
        if k.get("error"):
            print("   ERROR", k["shop"], k["error"]); continue
        tot = {t["type"]: t["amount"] / 100 for t in k["totals"]}
        print(f"   {k['shop']}  {[l['title'][:40] for l in k['lines']]}  {tot}\n      {k['continue_url'][:110]}")
    first = next((k for k in c["carts"] if not k.get("error")), None)
    if first:
        co = httpx.post(BASE + "/api/checkout", json={"shop": first["shop"], "cart_id": first["id"]}, timeout=60).json()
        print(f"\n== checkout session (not completed): status={co.get('status')} {co.get('ms')} ms\n      {str(co.get('continue_url'))[:110]}\n      messages={co.get('messages')}")

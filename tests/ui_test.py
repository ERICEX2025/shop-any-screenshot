"""Drive the real page in headless Chromium: give it an image by paste, drop or the file picker, wait for the matches
and Neon's second looks, optionally build carts and prepare a checkout (never completed), screenshot.
Needs playwright (not in requirements.txt).
Usage: ui_test.py media_file out_prefix [--how paste|drop|file] [--cart] [--base http://127.0.0.1:8202]"""
import base64, mimetypes, sys, time
from playwright.sync_api import sync_playwright


def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


media, out = sys.argv[1], sys.argv[2]
how, base = opt("--how", "paste"), opt("--base", "http://127.0.0.1:8202")
GIVE = """async ([b64, mime, how]) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const dt = new DataTransfer(); dt.items.add(new File([bytes], 'screenshot.' + mime.split('/')[1], { type: mime }));
  if (how === 'paste') document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  else for (const type of ['dragenter', 'dragover', 'drop'])
    document.querySelector('#drop').dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
}"""
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1100, "height": 900})
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(base + "/")
    print("headline:", page.inner_text("#drop .big"))
    t = time.time()
    if how == "file":
        page.set_input_files("#file", media)
    else:
        page.evaluate(GIVE, [base64.b64encode(open(media, "rb").read()).decode(), mimetypes.guess_type(media)[0] or "image/jpeg", how])
    page.wait_for_selector(".item", timeout=120000)
    print("first paint after %.1fs (%s):" % (time.time() - t, how), page.inner_text("#status"))
    page.wait_for_function("() => !document.querySelector('#status').textContent.includes('second look at')", timeout=120000)
    print("settled after %.1fs:" % (time.time() - t), page.inner_text("#status"))
    for it in page.locator(".item").all():
        print("  -", it.locator("h3").inner_text(), "|", " | ".join(x.inner_text() for x in it.locator(".badge").all()), "|", it.locator(".why").inner_text()[:110])
    print("frames sent:", page.locator("#frames img").count(), "| items:", page.locator(".item").count(),
          "| preselected:", page.locator(".cand.sel").count(), "| suggested:", page.locator(".cand.suggest").count())
    print(page.inner_text("#selCount"), page.inner_text("#selSum"))
    print("eval section:", page.inner_text("#evalBox")[:160].replace("\n", " "))
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out + "_results.png", full_page=True)
    if "--cart" in sys.argv:
        if not page.locator(".cand.sel").count():   # nothing pre-selected: tick the first suggestion, as a person would
            page.locator(".cand.suggest").first.click()
        page.click("#btnCart")
        page.wait_for_selector(".cart", timeout=60000)
        print(page.inner_text("#status"))
        page.locator(".co").first.click()
        page.wait_for_selector("#co0 a", timeout=60000)
        print("checkout:", page.inner_text("#co0"))
        print("links:", [a.get_attribute("href")[:70] for a in page.locator("#carts a").all()])
        page.locator("#carts").screenshot(path=out + "_carts.png")
    print("console errors:", errs)
    b.close()

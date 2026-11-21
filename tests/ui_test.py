"""Drive the real page in Chromium and screenshot every stage: reading, items, matches, an item opened and changed
(real products for the variation, then the redrawn picture), carts and the Shop Pay checkout hand-off (never completed).
Needs playwright (not in requirements.txt): ~/dev/bugrepro/.venv/bin/python tests/ui_test.py ...
Usage: ui_test.py image out_prefix [--width 400] [--height 820] [--dark] [--how paste|drop|file] [--wish "make it corduroy"]
                  [--item N] [--option] [--no-remix] [--cart] [--base http://127.0.0.1:8202]"""
import base64, json, mimetypes, sys, time
from playwright.sync_api import sync_playwright


def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


media, out = sys.argv[1], sys.argv[2]
how, base = opt("--how", "paste"), opt("--base", "http://127.0.0.1:8202")
W, H = int(opt("--width", "400")), int(opt("--height", "820"))
GIVE = """async ([b64, mime, how]) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const dt = new DataTransfer(); dt.items.add(new File([bytes], 'screenshot.' + mime.split('/')[1], { type: mime }));
  if (how === 'paste') document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  else for (const type of ['dragenter', 'dragover', 'drop'])
    document.querySelector('#drop').dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
}"""
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": W, "height": H}, color_scheme="dark" if "--dark" in sys.argv else "light", device_scale_factor=2)
    page = ctx.new_page()
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    shot = lambda name: (page.wait_for_timeout(350), page.screenshot(path=f"{out}_{name}.png"), print("   shot", f"{out}_{name}.png"))
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    shot("0_empty")
    t = time.time()
    if how == "file":
        page.set_input_files("#file", media)
    else:
        page.evaluate(GIVE, [base64.b64encode(open(media, "rb").read()).decode(), mimetypes.guess_type(media)[0] or "image/jpeg", how])
    page.wait_for_selector("#hero:not([hidden])")
    page.wait_for_timeout(900)
    shot("1_reading")
    page.wait_for_selector("#markers button", timeout=120000)
    print("items after %.1fs:" % (time.time() - t), page.inner_text("#status"))
    shot("2_items")
    page.wait_for_function("() => document.documentElement.dataset.marks", timeout=150000)
    print("settled after %.1fs:" % (time.time() - t), page.inner_text("#status"))
    print("stage times (s):", page.evaluate("document.documentElement.dataset.marks"))
    for it in page.locator(".item").all():
        print("  -", " | ".join(it.locator(".rowbtn").inner_text().split("\n")))
    item = opt("--item", "")
    if item:   # open a given item by its number
        if page.locator(f"#item{int(item) - 1}.open").count() == 0:
            page.locator("#markers button").nth(int(item) - 1).click()
    elif page.locator(".item.open").count() == 0:
        shot("3_settled")
        page.locator("#markers button").first.click()
    page.wait_for_selector(".detail")
    page.wait_for_timeout(600)
    shot("3_open")
    if "--no-remix" not in sys.argv:
        page.wait_for_selector(".chip[data-idea]", timeout=60000)
        print("ideas:", [c.inner_text() for c in page.locator(".chip[data-idea]").all()], "| real options:", [c.inner_text() for c in page.locator(".chip[data-opt]").all()])
        wish = opt("--wish", "")
        if "--option" in sys.argv:   # a real option of the matched listing, e.g. another colour the store sells
            for n in range(page.locator("#markers button").count()):   # the first item whose listing has colour-like options
                if page.locator(".chip.sw[data-opt][aria-pressed=false]").count():
                    break
                page.evaluate(f"document.querySelector('#item{n} .rowbtn').click()")
                page.wait_for_selector(f"#item{n}.open .chip[data-idea], #item{n}.open .remix .hint", timeout=60000)
                page.wait_for_timeout(1200)
            page.wait_for_selector(".chip.sw[data-opt][aria-pressed=false]", timeout=5000)
            shot("3_open")
            chip = page.locator(".chip.sw[data-opt][aria-pressed=false]").first
            print("choosing the real option:", chip.inner_text())
            t1 = time.time(); chip.click()
            page.wait_for_selector(".badge.real", timeout=20000)
            print("real variant after %.1fs:" % (time.time() - t1), page.inner_text(".detail .card").replace("\n", " | ")[:200])
            page.evaluate("document.documentElement.dataset.remix = 'neon'")
        elif wish:
            page.fill("#wishInput", wish); page.press("#wishInput", "Enter")
        else:
            page.locator(".chip[data-idea]").first.click()
        page.wait_for_selector(".detail .card", timeout=30000)
        shot("4_remix_products")
        page.wait_for_function("() => (document.documentElement.dataset.remix || '').includes('neon')", timeout=60000)
        page.wait_for_function("() => document.querySelector('#heroAlt.on') || document.querySelector('#retryRender')", timeout=120000)
        print("remix times (s):", page.evaluate("document.documentElement.dataset.remix"), "|", page.inner_text("#heroCap").replace("\n", " "))
        print("remix listing:", page.inner_text(".listhead"), "|", page.inner_text(".listnote"))
        page.wait_for_timeout(700)
        shot("5_preview")
        page.evaluate("window.scrollTo(0, 0)")
        if page.locator("[data-show=original]").count():
            page.click("[data-show=original]"); shot("6_flip_original"); page.click("[data-show=preview]")
    if "--cart" in sys.argv:
        for it in page.locator(".detail .card .acts button").all()[:1]:
            if it.get_attribute("aria-pressed") != "true":
                it.click()
        print("bar:", page.inner_text("#cartbar").replace("\n", " | "))
        page.click("#btnCart")
        page.wait_for_selector(".cart", timeout=60000)
        page.wait_for_timeout(900)
        shot("7_carts")
        with ctx.expect_page() as pop:
            page.locator("[data-co]").first.click()
        page.wait_for_selector(".conote a", timeout=60000)
        print("checkout:", page.inner_text(".cart .conote"), "| opened:", pop.value.url[:60], "| link:", page.locator(".conote a").first.get_attribute("href")[:60])
        pop.value.close()   # never filled in, never completed
        shot("8_checkout")
    page.screenshot(path=f"{out}_9_full.png", full_page=True)
    print("console errors:", errs)
    b.close()

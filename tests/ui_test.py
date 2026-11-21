"""Drive the real page in headless Chromium: upload a file, wait for matches, build carts, prepare a checkout, screenshot.
Needs playwright (not in requirements.txt). Usage: ui_test.py media_file out_prefix"""
import sys, time
from playwright.sync_api import sync_playwright

media, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1100, "height": 900})
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1:8202/")
    t = time.time()
    page.set_input_files("#file", media)
    page.wait_for_selector(".item", timeout=120000)
    print("scan done in %.1fs:" % (time.time() - t), page.inner_text("#status"))
    print("frames sent:", page.locator("#frames img").count(), "| products:", page.locator(".item").count(),
          "| preselected:", page.locator(".cand.sel").count(), "| suggested:", page.locator(".cand.suggest").count())
    print(page.inner_text("#selCount"), page.inner_text("#selSum"))
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out + "_results.png", full_page=True)
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

"""Load the unpacked extension in Playwright's Chromium and test what can be tested without a person:
the service worker registers, its image path (fetch -> POST to the server -> state for the panel), the capture
fallback, the toolbar capture path, the panel page starting a scan in its iframe, and the server-down message.
NOT covered (needs a person): the real right-click menu entry and Chrome actually opening the side panel.
Usage: ~/dev/bugrepro/.venv/bin/python tests/ext_test.py image.jpg out_prefix [--base http://127.0.0.1:8202]"""
import json, pathlib, sys, tempfile, urllib.request
from playwright.sync_api import sync_playwright


def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


img, out, base = sys.argv[1], sys.argv[2], opt("--base", "http://127.0.0.1:8202")
ext = str(pathlib.Path(__file__).parent.parent / "extension")
seed = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/image", data=open(img, "rb").read(), headers={"Content-Type": "image/jpeg"})))["id"]
img_url = f"{base}/api/image/{seed}"
with sync_playwright() as p, tempfile.TemporaryDirectory() as prof:
    ctx = p.chromium.launch_persistent_context(prof, channel="chromium", headless=True, viewport={"width": 1100, "height": 800},
                                               args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    ext_id = sw.url.split("/")[2]
    print("1. service worker registered:", sw.url)
    print("   manifest:", sw.evaluate("(() => { const m = chrome.runtime.getManifest(); return [m.name, m.version, m.permissions, !!chrome.sidePanel, !!chrome.contextMenus] })()"))
    sw.evaluate("base => chrome.storage.local.set({ server: base })", base)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_content(f"<body style='margin:0;background:#fff'><h1>A page with a picture</h1><img src='{img_url}' style='height:520px'></body>")
    page.goto(base + "/")   # an http page to capture
    page.bring_to_front()
    tab = sw.evaluate("chrome.tabs.query({ active: true, lastFocusedWindow: true }).then(t => ({ id: t[0].id, windowId: t[0].windowId, url: t[0].url }))")
    print("   active tab:", tab)
    r = sw.evaluate("([u, tab]) => shopImage(u, tab)", [img_url, tab])
    print("2. shopImage(image URL) ->", r, "| panel state:", sw.evaluate("chrome.storage.session.get('shelfcart')"))
    assert r.get("source") == "image" and r.get("id")
    r2 = sw.evaluate("([u, tab]) => shopImage(u, tab)", [base + "/no-such-image.jpg", tab])
    print("3. shopImage(broken URL) falls back to a capture ->", r2)
    r3 = sw.evaluate("tab => shopCapture(capture(tab))", tab)
    print("4. toolbar path, shopCapture(captureVisibleTab) ->", r3)
    try:
        print("5. sidePanel.open without a click ->", sw.evaluate("tab => chrome.sidePanel.open({ windowId: tab.windowId }).then(() => 'opened', e => 'refused: ' + e.message)", tab))
    except Exception as e:
        print("5. sidePanel.open:", str(e)[:120])
    seed = sw.evaluate("([u, tab]) => shopImage(u, tab)", [img_url, tab])["id"]
    panel = ctx.new_page()
    panel.set_viewport_size({"width": 400, "height": 820})
    panel.goto(f"chrome-extension://{ext_id}/panel.html")
    panel.wait_for_selector("#app:not([hidden])", timeout=15000)
    src = panel.get_attribute("#app", "src")
    print("6. panel iframe:", src)
    assert f"/panel?scan={seed}" in src
    fr = panel.frame_locator("#app")
    fr.locator("#hero:not([hidden])").wait_for(timeout=15000)
    panel.wait_for_timeout(1500)
    panel.screenshot(path=out + "_panel_reading.png")
    fr.locator("#markers button").first.wait_for(timeout=120000)
    print("   scan started in the panel; items:", fr.locator("#markers button").count(), "|", fr.locator("#status").inner_text())
    panel.wait_for_timeout(2500)
    panel.screenshot(path=out + "_panel_items.png")
    sw.evaluate("chrome.storage.local.set({ server: 'http://localhost:8299' })")   # nothing listens there
    panel.reload()
    panel.wait_for_selector("#msg:not([hidden])", timeout=15000)
    print("7. server down ->", panel.inner_text("#msgTitle"), "|", panel.inner_text("#msgCode"))
    panel.screenshot(path=out + "_panel_down.png")
    r4 = sw.evaluate("([u, tab]) => shopImage(u, tab)", [img_url, tab])
    print("   shopImage with the server down ->", r4)
    sw.evaluate("base => chrome.storage.local.set({ server: base })", base)
    sw.evaluate("chrome.storage.session.set({ shelfcart: { status: 'idle' } })")
    panel.wait_for_selector("#app:not([hidden])", timeout=15000)   # the panel retries by itself
    print("8. server back -> panel recovered:", panel.get_attribute("#app", "src"))
    ctx.close()
print("ok")

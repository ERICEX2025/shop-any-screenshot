# ShelfCart Chrome extension

Right-click any image on any site and choose **Shop this image**. Chrome's side panel opens beside the page and
ShelfCart finds the real products in that image on Shopify. Clicking the ShelfCart toolbar button captures the
visible tab instead.

## Load it

1. Start the ShelfCart server (the extension shows its page):
   `cd ~/dev/g4-shelf && .venv/bin/uvicorn app.server:app --port 8202`
2. Open `chrome://extensions` in Chrome (version 116 or newer).
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and choose this folder: `~/dev/g4-shelf/extension`.
5. Optional: click the puzzle icon in the toolbar and pin **ShelfCart: shop this image**.

Pages that were already open need a reload before the right-click entry appears on them.

## Use it

- **Right-click an image, then "Shop this image".** The side panel opens and starts on that image.
- **Click the toolbar button.** The visible part of the current tab is captured and used as the image.
- With nothing chosen yet, the panel shows ShelfCart's own page: paste or drop an image there.

## How it works

- `background.js` (service worker): opens the side panel inside the click, downloads the image with the extension's
  host permission, and if the site refuses (or the image is a `blob:`, an SVG, too small) captures the visible tab
  instead. It posts the bytes to `POST http://localhost:8202/api/image` and stores the returned id in
  `chrome.storage.session`.
- `panel.html` / `panel.js` (side panel): a frame showing `http://localhost:8202/panel?scan=<id>`. A yellow line says
  so when the picture is a capture of the page rather than the image itself.
- If the server is not running the panel says so, shows the start command, and continues by itself once it is up.

Permissions: `contextMenus`, `sidePanel`, `activeTab`, `storage`, and access to all sites (needed to download the
image you right-clicked and to capture the tab). The image goes only to the ShelfCart server on your own computer.

To use a server on another address, run this in the service worker console (chrome://extensions, "service worker"):
`chrome.storage.local.set({ server: 'http://localhost:9000' })`.

## What was tested

`tests/ext_test.py` loads this folder in Playwright's Chromium and checks: the service worker registers; the image
path (download, post, panel state); the fallback to a capture for a broken image address; the toolbar capture path;
the panel page starting a scan; the server-down message and recovery. Not tested without a person: the real
right-click menu entry and Chrome opening the side panel (Chrome only allows `sidePanel.open` inside a real click).

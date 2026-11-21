# ShelfCart

**Shop any screenshot.** Paste a screenshot (an Instagram post, a room, an outfit, a product page, a frame from a
video) or pan a camera across a shelf, a desk or the inside of a bag. ShelfCart finds the items in the image and turns
them into real, buyable Shopify products through Shopify's agent commerce platform (Shop MCP, https://shop.dev).
You choose what goes in the cart and you pay on the merchant's page; the app never pays.

Built on Nov 21, 2026, during the Gemini 4 hackathon.

## What it targets

- **Problem statement "Element 119".** The camera (or a captured image of a screen) is the core input: there is no
  text box anywhere. Input is a pasted or dropped screenshot, a photo, a video clip, or a live 4 second camera pan.
- **Shopify bonus track.** Catalog search, carts and checkout sessions are real calls to Shop MCP, no API key.

## Which model does which job

| Step | Who | Job |
|---|---|---|
| 1. See | `gemini-4-hydrogen` | Reads the screenshot or 1 to 5 camera frames. Ignores app chrome, icons, faces and text overlays. Lists each distinct purchasable item once, including what a person is wearing or holding: brand, name, variant, size, crop box, confidence, and a search query (brand + product for branded items; type, colour, material, style for unbranded ones). |
| 2. Search | Shop MCP `search_catalog` | Searches Shopify's Global Catalog (all merchants) per item. Real, no API key. |
| 3. Exact match + gate | `gemini-decision-1` | Gets the item crop and the listings. One `choice` question ranks the listings, one `noul` per listing answers "same product?", one `noul` answers "is the label readable?". Fast (about 1 s per item) and calibrated, so it decides which matches are safe to pre-select. |
| 4. Second look | `gemini-4-neon` | Runs only for items where Decision 1 has no confident exact match, in parallel, after the first results are on screen. Sees the item crop, the listing titles and the listing photos. It either confirms an exact listing (checking pack size and variant: single item vs multi-pack, size), or picks the closest **similar** item and says in one line how it differs ("same shape, darker wood", "12-pack box, not a single bar"), or says nothing is close. This is the common case for screenshots: clothes, furniture and decor have no exact listing. |
| 5. Cart | Shop MCP `create_cart` | One real cart per merchant, only after the user presses Build cart. |
| 6. Checkout (optional) | Shop MCP `create_checkout` | Only on a second click. Returns Shopify's review-and-pay page. `complete_checkout` is never called. |

Two Gemini 4 models with separate jobs (Hydrogen sees, Neon reasons about hard matches), plus the Decisions API as the
fast gate. The page shows which model decided each match ("decided by Decision 1" or "decided by Gemini 4 Neon").

## How a person stays in control

- Each item is labelled **Exact** (pre-selected), **Exact? Needs your OK**, **Similar** or **Nothing close**.
- Only confident exact matches are pre-selected: Decision 1's choice and yes/no agree, P(same product) >= 0.8,
  Hydrogen confidence >= 0.6, P(label readable) >= 0.6; or Neon confirms an exact listing with matching quantity.
- A similar item is only ever a dashed suggestion with its difference spelled out. The person has to tap it.
- Any listing can be tapped on or off. Prices and merchants are on screen before anything is added.
- No cart exists until the person presses Build cart. A checkout session is a second, separate click, and it only
  returns Shopify's own review-and-pay page. The app never completes a checkout and never sees payment details.
- If two accepted listings exist and one is from a merchant already in the cart, that one is used (Decision 1 matches
  only), because Shopify needs one checkout per merchant.

## Run

```
cd ~/dev/g4-shelf
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp ~/dev/bugrepro/.env .env        # GEMINI_API_KEY; .env is git-ignored
.venv/bin/uvicorn app.server:app --port 8202
```

Open http://localhost:8202 and press Cmd+V with a screenshot on the clipboard (on a Mac, Ctrl+Shift+Cmd+4 copies a
screenshot to the clipboard), or drop an image anywhere on the page, or click the box to choose a file. The scan starts
by itself. The camera is under "Or use the camera"; it needs `localhost` or HTTPS, so for a phone camera put the server
behind an HTTPS tunnel (for example `cloudflared tunnel --url http://localhost:8202`) or upload a clip/photo.

Endpoints: `POST /api/scan` (`{frames, defer}`), `POST /api/review` (Neon's second look for a few items, called by the
page so results fill in after the first paint), `POST /api/cart`, `POST /api/checkout`, `GET /api/eval`.

## Does it work

`tests/eval.py` compares two systems on 7 images in `data/test/` (5 photos: bathroom shelf, bag, desk, cafe table,
hot sauce wall; 2 synthetic screenshots: a living room post and an outfit video frame) against a hand-written answer
key of 39 items: 19 branded items where a correct exact listing can be named (brand + product type, variant and
single vs multi-pack where visible) and 20 unbranded items where only a similar item exists.

- **Full pipeline:** Hydrogen, Shopify search, Decision 1 gate, Neon second look.
- **Baseline:** one `gemini-4-hydrogen` call that names the products, then the first Shopify search result for each,
  straight into the selection. No Decision gate, no Neon.

Run of Nov 21, 2026, each system run twice, totals over both runs (saved in `data/eval.json`, shown on the page):

| | Items found | Correct exact, pre-selected | Wrong pre-selected (the costly error) | Correctly held back | Held back, top listing was right | Similar item acceptable | Seconds per image |
|---|---|---|---|---|---|---|---|
| Full pipeline | 78 / 78 | 29 / 38 | 0 | 2 | 7 | 39 of 39 offered (40 possible) | 29.0 |
| Baseline | 78 / 78 | 26 / 38 | 12 | 0 | 0 | 38 of 40 offered | 5.7 |

Per run: pipeline 14 then 15 correct, 0 and 0 wrong; baseline 14 then 12 correct, 5 then 7 wrong.

Where the baseline wins or ties:

- **Speed.** 5.7 s against 29 s per image (measured with 3 images running at once). On the page the first results
  appear after Hydrogen and Decision 1 (8 to 12 s) and Neon's answers fill in over the next 10 to 20 s.
- **Finding items.** Both found every keyed item. Detection is not where the pipeline adds anything.
- **Unbranded items.** The first search result was an acceptable similar item almost as often (38 of 40 against
  39 of 39). By this crude measure Neon's pick is not better than the first result; what the pipeline adds there is
  the stated difference and that the item is a suggestion, not something already selected.
- **Fewer taps.** The pipeline held back 7 branded items whose top listing was right, so the person has to tap them.

What the numbers hide:

- Small sample: 7 images, 2 of them synthetic screenshots, keyed by one person on the day.
- A listing is judged by keywords in its title and seller name, not by a person opening it. Checked by hand, about 3
  of the baseline's 12 wrong picks were right but spelled differently ("Moisturising", "Post It", a Hydro Flask listing
  without the brand in the title), so the real figure is nearer 9. In the other direction, 6 of the pipeline's 7
  "held back, was right" are cases where Neon flagged a real size difference (Heinz 20 oz against a 14 oz bottle,
  The Ordinary 60 ml against 30 ml, Orbit 12-pack) that the keyword scorer cannot see.
- For unbranded items "acceptable" only means the right type of thing with the main attribute (a green sofa, a jute
  rug). Nobody checked that it really looks alike.
- The key and scorer were corrected once after a first run (brand taken from the seller name, "RX Bar" spelling,
  Cholula flavours). The first run had scored 5 pipeline picks and 16 baseline picks as wrong; most were scorer misses.
- Items found outside the answer key are not scored: 23 for the pipeline (none pre-selected), 39 for the baseline
  (all pre-selected, including a laptop, a duffel bag and greeting cards).

Known failure cases:

- **Dense shelves.** A wall of about 300 hot sauce bottles yields only the few items with large labels; the scan is
  capped at 12 items and small labels are not readable at 1280 px.
- **Generic unbranded items.** A mug, a notebook, a coat: there is no exact listing, so the result is a look-alike
  picked from 6 search results. In one run nothing was offered for the round sunglasses, in another for wired earbuds.
- **Pack sizes.** A single can or bar often only has multi-pack listings (KIND 12-bar box, Orbit 12-pack). Neon says
  so in words and holds it back, but cannot find a single unit that is not in the search results. The cart also uses
  the listing's default variant.
- **One cart per merchant.** The best listings usually come from different stores, so 5 items can mean 5 carts and
  5 shipping fees.
- **Run-to-run variance.** Hydrogen names and boxes items a little differently each run, so the item count changes
  (11 against 12 extra items between the two runs) and a match can flip between Exact and Similar (Post-it Notes did).
- **Speed.** Each Neon second look takes 7 to 12 s and now and then much longer; a screenshot with 11 unbranded
  items settled in 23 s on a good run and 45 s on a bad one (one Neon call timed out and the item stayed "Nothing close").

## Shopify calls (verified Nov 21 2026, no key, no sign-in)

All are stateless JSON-RPC POSTs to `https://mcp.shop.com/` (no `/mcp` path, no `initialize`). See `app/shopify.py`.

Headers: `Content-Type: application/json`, `Accept: application/json, text/event-stream`,
`MCP-Protocol-Version: 2026-07-28`, `Mcp-Method: tools/call`, `Mcp-Name: <tool>`.

Body: `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"<tool>","arguments":{...},"_meta":{
"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"shelfcart","version":"0.1.0"},
"io.modelcontextprotocol/clientCapabilities":{}}}}`. The result is in `result.structuredContent`.

- `search_catalog` arguments: `{"catalog":{"query":"...","context":{"address_country":"US","currency":"USD"},"filters":{"ships_to":{"country":"US"}},"pagination":{"limit":6}}}`.
  Each product has `variants[0]` with `id`, `price.amount` (minor units), `seller.domain`, `url`, `checkout_url`.
- `create_cart` arguments: `{"shop":"<seller.domain>","cart":{"line_items":[{"item":{"id":"gid://shopify/ProductVariant/..."},"quantity":1}],"context":{...}},"meta":{"idempotency-key":"<uuid>"}}`.
  Returns `id`, `totals`, `continue_url` (the merchant's cart page).
- `create_checkout` arguments: `{"shop":"...","checkout":{"cart_id":"<cart id>","line_items":[],"context":{...}},"meta":{"idempotency-key":"<uuid>"}}`.
  Returns `status: requires_escalation`, tax in `totals`, `continue_url` (a shop.app review-and-pay page).

`tools/list` on the same URL returns the full schemas.

## Tests

- `tests/eval.py [--runs N]` is the baseline comparison above. It writes `data/eval.json`, which the page shows.
- `tests/run_e2e.py image.jpg [--pan N] [--cart]` runs the pipeline through the server and prints every candidate with its scores.
- `tests/ui_test.py media out_prefix [--how paste|drop|file] [--cart] [--base URL]` drives the page in headless Chromium (needs playwright).
- `tests/gen_image.py` makes synthetic test images with `gemini-nano-banana-2.1`; `tests/bench_detect.py` compares detection settings.

The test images live in `data/test/` and are not in git (about 700 KB each); `data/eval.json` is.

## Known limits

- Hydrogen takes 6 to 11 s for one image; search plus Decision 1 about 1.5 s for all items in parallel; each Neon
  second look usually 7 to 12 s, sometimes 30 s or more. They run in parallel; a second call is started for an item
  after 14 s and the first answer wins; after 30 s the item keeps Decision 1's verdict.
- A similar item is chosen from only 6 search results per item, so "closest" means closest of those six.
- The cart uses the listing's featured variant. For a listing with several sizes or colours this may not be the one in the image.
- Usually one cart per item, because the best listings come from different merchants.
- Anonymous Shop MCP calls have the lowest rate limits; no 429 was seen in testing, the client retries twice.
- Paste and drop were tested in headless Chromium with synthetic events and the upload path end to end; live camera
  capture was written but not tested on a machine with a camera.

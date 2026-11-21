# ShelfCart

Pan a phone or laptop camera across a shelf, a desk or the inside of a bag for a few seconds.
ShelfCart names the products it sees and builds real Shopify carts of matching products through
Shopify's agent commerce platform (Shop MCP, https://shop.dev). You review and pay on the merchant's page; the app never pays.

## Pipeline

| Step | Who | Job |
|---|---|---|
| 1. See | `gemini-4-hydrogen` | Reads 1 to 5 camera frames, lists each distinct product once (brand, name, variant, size, box, confidence, search query). |
| 2. Search | Shop MCP `search_catalog` | Searches Shopify's Global Catalog (all merchants) per product. Real, no API key. |
| 3. Match + gate | `gemini-decision-1` | Gets the photo crop and the listings. One `choice` question ranks the listings, one `noul` per listing answers "same product?", one `noul` answers "is the label readable?". |
| 4. Cart | Shop MCP `create_cart` | One real cart per merchant, only after the user presses Build cart. |
| 5. Checkout (optional) | Shop MCP `create_checkout` | Only on a second click. Returns Shopify's review-and-pay page. `complete_checkout` is never called. |

A match is pre-selected only if the choice and the yes/no agree, P(same product) >= 0.8, Hydrogen confidence >= 0.6
and P(label readable) >= 0.6. Everything else is shown as "Needs your OK" (a dashed suggestion the user must tap) or
"No sure match". If two accepted listings exist and one is from a merchant already in the cart, that one is used,
because Shopify needs one checkout per merchant.

Frames are sampled in the browser (the sharpest frame from each of 5 time slices), so no video upload is needed and
each product can be cropped for the match step.

## Run

```
cd ~/dev/g4-shelf
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp ~/dev/bugrepro/.env .env        # GEMINI_API_KEY; .env is git-ignored
.venv/bin/uvicorn app.server:app --port 8202
```

Open http://localhost:8202. Camera access needs `localhost` or HTTPS, so to use a phone camera put the server behind
an HTTPS tunnel (for example `cloudflared tunnel --url http://localhost:8202`) or upload a clip/photo from the phone.

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

- `tests/run_e2e.py image.jpg [--pan N] [--cart]` runs the pipeline through the server and prints every candidate with its scores.
- `tests/ui_test.py media out_prefix` drives the page in headless Chromium (needs playwright).
- `tests/gen_image.py` makes synthetic test photos with `gemini-nano-banana-2.1`; `tests/bench_detect.py` compares detection settings.

## Known limits

- Detection takes 6 to 10 s (one Hydrogen call); search plus match is about 1.5 s for 8 products in parallel.
- The cart uses the listing's featured variant. For a listing with several sizes this may not be the size on the shelf.
- Pack size is not checked (a single bar can match a box of 12). The price is on screen before anything is added.
- Usually one cart per product, because the best listings come from different merchants.
- Anonymous Shop MCP calls have the lowest rate limits; no 429 was seen in testing, the client retries twice.
- Live camera capture was written but only the upload path was tested (no camera on the build machine).
- No baseline comparison script yet (the hackathon rubric asks for one).

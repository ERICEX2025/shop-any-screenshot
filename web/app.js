/* ShelfCart page. No framework, no build step. One scan lives in S; every change re-renders the key and the hero.
   Order on screen: items (Hydrogen) -> matches (Shopify + Decision 1) -> second look (Neon) -> redraw (image model). */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (cents, cur = 'USD') => cents == null ? '' : new Intl.NumberFormat('en-US', { style: 'currency', currency: cur || 'USD' }).format(cents / 100);
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const MODEL = { 'gemini-decision-1': 'Decision 1', 'gemini-4-neon': 'Gemini 4 Neon' };
const VISUAL = /colou?r|finish|material|fabric|pattern|print|style|shade|upholstery|wood|metal|frame|tone|wash|leather/i;
const cdn = (url, w) => url ? url + (url.includes('?') ? '&' : '?') + 'width=' + w : '';

let S = null;       // the current scan
let runNo = 0;      // bumps on every new scan so late answers from an old one are dropped
const shop = { state: 'signed_out' };
if (window.top !== window) document.body.classList.add('embedded');

// ---------------------------------------------------------------- image in
function grab(source, w, h) {
  const scale = Math.min(1, 1280 / Math.max(w, h));
  const c = document.createElement('canvas'); c.width = Math.round(w * scale); c.height = Math.round(h * scale);
  c.getContext('2d').drawImage(source, 0, 0, c.width, c.height);
  return c;
}
function sharpness(canvas) { // mean absolute horizontal gradient on a small grey copy: higher = less motion blur
  const s = document.createElement('canvas'); s.width = 160; s.height = Math.round(160 * canvas.height / canvas.width);
  const g = s.getContext('2d'); g.drawImage(canvas, 0, 0, s.width, s.height);
  const d = g.getImageData(0, 0, s.width, s.height).data; let sum = 0;
  for (let i = 4; i < d.length; i += 4) sum += Math.abs((d[i] + d[i + 1] + d[i + 2]) - (d[i - 4] + d[i - 3] + d[i - 2]));
  return sum / (d.length / 4);
}
function pickFrames(shots, n) { // keep the sharpest shot from each of n time buckets
  if (shots.length <= n) return shots.map(s => s.canvas);
  const out = [];
  for (let b = 0; b < n; b++) {
    const part = shots.slice(Math.floor(b * shots.length / n), Math.floor((b + 1) * shots.length / n));
    out.push(part.reduce((a, x) => x.score > a.score ? x : a).canvas);
  }
  return out;
}
function loadImage(file) {
  return new Promise((res, rej) => { const im = new Image(); im.onload = () => res(grab(im, im.naturalWidth, im.naturalHeight)); im.onerror = () => rej(new Error('unsupported image')); im.src = URL.createObjectURL(file); });
}
async function sampleVideo(file, n = 5) {
  const v = document.createElement('video'); v.muted = true; v.playsInline = true; v.preload = 'auto'; v.src = URL.createObjectURL(file);
  await new Promise((res, rej) => { v.onloadeddata = res; v.onerror = () => rej(new Error('cannot decode this video')); });
  let dur = v.duration;
  if (!isFinite(dur)) { v.currentTime = 1e7; await new Promise(r => v.onseeked = r); dur = v.currentTime; } // MediaRecorder webm
  const shots = [], steps = Math.min(15, Math.max(n, Math.floor(dur * 3)));
  for (let i = 0; i < steps; i++) {
    v.currentTime = dur * (i + 0.5) / steps;
    await new Promise(r => v.onseeked = r);
    const c = grab(v, v.videoWidth, v.videoHeight); shots.push({ canvas: c, score: sharpness(c) });
  }
  return pickFrames(shots, n);
}
const toBlob = c => new Promise(r => c.toBlob(r, 'image/jpeg', 0.88));

async function intake(files, how) {
  files = files.filter(f => f && (f.type.startsWith('image/') || f.type.startsWith('video/')));
  if (!files.length) return setStatus('That was not an image. Copy a screenshot or an image and paste again, or drop an image file.', true);
  try {
    const vid = files.find(f => f.type.startsWith('video/'));
    const canvases = vid ? await sampleVideo(vid) : await Promise.all(files.filter(f => f.type.startsWith('image/')).slice(0, 5).map(loadImage));
    startCanvases(canvases);
  } catch (err) { setStatus('Could not read that ' + (how || 'file') + ': ' + (err && err.message || 'unsupported image'), true); }
}

async function startCanvases(canvases) {
  const frames = canvases.map(c => ({ url: c.toDataURL('image/jpeg', 0.88), w: c.width, h: c.height }));
  const scan = newScan(frames);
  try {
    const ids = await Promise.all(canvases.map(async c => {
      const r = await fetch('/api/image', { method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: await toBlob(c) });
      if (!r.ok) throw new Error('the server did not take the image (' + r.status + ')');
      return (await r.json()).id;
    }));
    if (scan !== S) return;
    scan.ids = ids;
    runScan(scan);
  } catch (e) { if (scan === S) fail(scan, e.message); }
}

async function startFromId(id) {   // the Chrome extension opens /panel?scan=<id>
  const url = '/api/image/' + encodeURIComponent(id);
  const im = new Image();
  im.onload = () => { const scan = newScan([{ url, w: im.naturalWidth, h: im.naturalHeight }]); scan.ids = [id]; runScan(scan); };
  im.onerror = () => { $('#app').dataset.state = 'empty'; setStatus('That image is no longer on the server. Paste or drop it here instead.', true); };
  im.src = url;
}

function newScan(frames) {
  if (S && S.abort) S.abort.abort();
  S = { run: ++runNo, frames, ids: [], products: null, open: null, frame: 0, phase: 'items', carts: null, t0: performance.now(), marks: {}, timing: null, abort: new AbortController(), userOpened: false };
  $('#app').dataset.state = 'working';
  $('#hero').hidden = false; $('#newBtn').hidden = false; $('#colophon').hidden = false;
  $('#carts').innerHTML = '';
  window.scrollTo(0, 0);
  render();
  return S;
}
function fail(scan, msg) { scan.phase = 'error'; scan.error = msg; render(); }

// ---------------------------------------------------------------- the scan, streamed
async function runScan(scan) {
  try {
    const r = await fetch('/api/scan/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_ids: scan.ids }), signal: scan.abort.signal });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || ('the server said ' + r.status));
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (line.trim() && scan === S) onEvent(scan, JSON.parse(line));
      }
    }
    if (scan === S && scan.phase !== 'done' && scan.phase !== 'error') fail(scan, 'The scan stopped before it finished. Try the image again.');
  } catch (e) { if (scan === S && e.name !== 'AbortError') fail(scan, 'The scan failed: ' + e.message); }
}

function adopt(old, p) {   // keep what the person did (selection, variations) when a later stage replaces an item
  const q = { ...p, sel: old.sel, touched: old.touched, view: old.view, remix: old.remix || {}, ideas: old.ideas, opts: old.opts, optSel: old.optSel, showAll: old.showAll, fresh: false };
  if (!q.touched) q.sel = q.status === 'auto' && q.pick != null ? q.candidates[q.pick] : null;
  if (old.opts && (!q.candidates || q.pick == null || q.candidates[q.pick].product_id !== old.opts.product_id)) { q.opts = null; q.optSel = null; }
  return q;
}
function onEvent(scan, ev) {
  const sec = () => +((performance.now() - scan.t0) / 1000).toFixed(1);
  if (ev.type === 'items') {
    scan.products = ev.products.map(p => ({ ...p, candidates: null, pick: null, status: 'searching', pending: false, sel: null, remix: {}, view: null, fresh: true }));
    scan.phase = 'matching'; scan.marks.items = sec();
  } else if (ev.type === 'match' || ev.type === 'review') {
    scan.products[ev.i] = adopt(scan.products[ev.i], ev.product);
    if (ev.type === 'match' && !scan.marks.match) scan.marks.match = sec();
    if (ev.type === 'review') scan.marks.review = sec();
    if (!scan.userOpened && scan.open == null && window.innerWidth >= 900) {   // wide screens have room: open the first item that has listings
      const first = scan.products.findIndex(p => p.candidates && p.candidates.length);
      if (ev.type === 'match' && first === ev.i) { openItem(first, false); return; }
    }
    if (scan.open === ev.i) lazyLoad(scan.products[ev.i]);
  } else if (ev.type === 'matched') {
    scan.phase = ev.reviews ? 'reviewing' : 'done'; scan.reviews = ev.reviews; scan.marks.matched = sec();
  } else if (ev.type === 'done') {
    scan.phase = 'done'; scan.timing = ev.timing; scan.marks.done = sec();
    document.documentElement.dataset.marks = JSON.stringify(scan.marks);   // for tests: when each stage painted
  } else if (ev.type === 'error') {
    return fail(scan, 'Gemini 4 Hydrogen could not read the image (' + ev.message + '). Try again.');
  }
  render();
}

// ---------------------------------------------------------------- item helpers
const pickOf = p => p.candidates && p.pick != null ? p.candidates[p.pick] : null;
const viewOf = p => (p.view && p.remix[p.view]) || null;
const key = s => s.toLowerCase().replace(/\s+/g, ' ').trim();
function nameParts(p) {
  const head = [p.brand, p.name].filter(Boolean).join(' ') || p.label;
  return [head, [p.variant, p.size].filter(Boolean).join(', ')];
}
function rowState(p) {
  if (p.status === 'searching') return '<span class="badge wait">Searching Shopify</span>';
  const c = p.sel || pickOf(p);
  const price = c ? `<span class="price">${money(c.price, c.currency)}</span>` : '';
  if (p.sel) return `<span class="badge exact">In selection</span>${price}`;
  if (p.pending) return `<span class="badge wait">Second look</span>`;
  const [cls, txt] = { auto: ['exact', 'Exact'], confirm: ['similar', 'Exact?'], similar: ['similar', 'Similar'] }[p.status] || ['none', 'Nothing close'];
  return `<span class="badge ${cls}">${txt}</span>${price}`;
}
function verdict(p) {
  const neon = p.decided_by === 'gemini-4-neon', by = p.decided_by ? `<span class="by">Decided by ${MODEL[p.decided_by] || esc(p.decided_by)}${p.legible != null && !neon ? `, which is ${Math.round(p.match_confidence * 100)}% sure` : ''}.</span>` : '';
  if (p.status === 'searching') return '<span class="badge wait">Searching Shopify</span> Looking across Shopify stores for this.';
  if (p.pending) return '<span class="badge wait">Second look</span> Decision 1 found no sure exact match. Gemini 4 Neon is comparing the store photos to find the closest one.';
  if (p.status === 'auto') return `<span class="badge exact">Exact</span> ${neon ? esc(cap(p.reason)) + '.' : (p.consolidated ? 'Same product from a store already in your selection.' : 'This is the same product as in the image.')} It is in your selection; remove it if you do not want it.${by}`;
  if (p.status === 'confirm') return `<span class="badge similar">Exact?</span> Probably the same product${neon && p.reason ? ': ' + esc(clean(p.reason)) : ''}. Not added until you add it.${by}`;
  if (p.status === 'similar') return `<span class="badge similar">Similar</span> Not the same product. The closest one: ${esc(clean(p.reason) || 'see below')}.${by}`;
  if (!p.candidates || !p.candidates.length) return `<span class="badge none">Nothing close</span> Shopify's catalog returned nothing for this${p.error ? ' (' + esc(p.error) + ')' : ''}. Try changing it below.`;
  return `<span class="badge none">Nothing close</span> ${neon && p.reason ? esc(cap(p.reason)) + '.' : 'None of these listings looked close.'} They are listed in case one is right.${by}`;
}
const clean = s => String(s || '').trim().replace(/[.\s]+$/, '');
const cap = s => { s = clean(s); return s ? s[0].toUpperCase() + s.slice(1) : ''; };

// ---------------------------------------------------------------- render
function setStatus(t, isErr) {
  const el = $('#status'); el.textContent = t; el.className = isErr ? 'err' : '';
  $('#emptyMsg').textContent = isErr && $('#app').dataset.state === 'empty' ? t : '';
}

function statusText() {
  if (S.phase === 'error') return S.error;
  if (!S.products) return `Gemini 4 Hydrogen is finding what can be bought in ${S.frames.length > 1 ? 'these ' + S.frames.length + ' frames' : 'your image'}.`;
  const P = S.products, n = k => P.filter(p => !p.pending && p.status === k).length;
  if (!P.length) return 'Nothing to buy was found in that image. Try one where the items are larger.';
  if (S.phase === 'matching') return `${plural(P.length, 'item')} found. Searching Shopify stores for each.`;
  const parts = [n('auto') && `${n('auto')} exact`, n('similar') && `${n('similar')} similar`, n('confirm') && `${n('confirm')} to confirm`, n('none') && `${n('none')} with nothing close`].filter(Boolean).join(', ');
  const left = P.filter(p => p.pending).length;
  if (left) return `${plural(P.length, 'item')}${parts ? ': ' + parts : ''}. Gemini 4 Neon is taking a second look at ${parts ? left + ' more' : left === P.length ? 'each of them' : left + ' of them'}.`;
  return `${plural(P.length, 'item')}: ${parts}. Tap a number to shop it or change it.`;
}

function render() {
  if (!S) return;
  renderHero();
  setStatus(statusText(), S.phase === 'error');
  renderItems();
  renderBar();
  renderCarts();
}

function renderHero() {
  const f = S.frames[S.frame] || S.frames[0], sheet = $('#sheet');
  sheet.style.setProperty('--ar', (f.w / f.h).toFixed(4));
  if ($('#heroImg').getAttribute('src') !== f.url) $('#heroImg').src = f.url;
  $('#reading').hidden = !!S.products || S.phase === 'error';
  const P = S.products || [], open = S.open != null ? P[S.open] : null;
  const at = spread(P, sheet.clientWidth || 300, (sheet.clientWidth || 300) * f.h / f.w);
  $('#markers').innerHTML = P.map((p, i) => {
    if (!at[i]) return '';
    return `<button type="button" class="${p.fresh ? 'in' : ''}" style="left:${(at[i][0] * 100).toFixed(1)}%;top:${(at[i][1] * 100).toFixed(1)}%;animation-delay:${i * 45}ms" data-i="${i}" aria-pressed="${S.open === i}" aria-label="${esc('Item ' + (i + 1) + ': ' + p.label)}">${i + 1}</button>`;
  }).join('');
  P.forEach(p => p.fresh = false);
  $('#markers').querySelectorAll('button').forEach(b => b.onclick = () => openItem(+b.dataset.i, true, true));
  const bm = $('#boxmark'), v = open && viewOf(open), rd = v && v.render;
  if (open && (open.frame || 0) === S.frame) {
    const [y0, x0, y1, x1] = box(open);
    Object.assign(bm.style, { left: x0 * 100 + '%', top: y0 * 100 + '%', width: (x1 - x0) * 100 + '%', height: (y1 - y0) * 100 + '%' });
    bm.hidden = false; bm.classList.toggle('busy', !!rd && rd.state === 'loading');
  } else bm.hidden = true;
  const alt = $('#heroAlt'), showPrev = !!rd && rd.state === 'ready' && v.show !== 'original';
  if (rd && rd.state === 'ready') { if (alt.getAttribute('src') !== rd.url) alt.src = rd.url; alt.hidden = false; }
  alt.classList.toggle('on', showPrev);
  $('#genmark').hidden = !showPrev;
  alt.alt = showPrev ? 'Generated preview of the image with the ' + open.label + ' changed' : '';
  const capEl = $('#heroCap');
  if (rd && rd.state === 'loading') capEl.innerHTML = `<span class="note">Redrawing the picture with ${esc(v.v.label)}. This takes about 15 seconds; the real products are listed already.</span>`;
  else if (rd && rd.state === 'ready') capEl.innerHTML = `<span class="flip" role="group" aria-label="Which picture to show"><button type="button" data-show="original" aria-pressed="${!showPrev}">Original</button><button type="button" data-show="preview" aria-pressed="${showPrev}">Preview</button></span><span class="note">${showPrev ? 'Preview generated by Gemini, the real product may differ.' : 'Your original image.'}</span>`;
  else if (rd && rd.state === 'error') capEl.innerHTML = `<span class="note">No preview: ${esc(rd.msg)} The products listed are real either way.</span><button type="button" class="quiet" id="retryRender">Try the redraw again</button>`;
  else capEl.innerHTML = S.products && S.products.length ? `<span class="note">${open ? esc(open.label) : 'Each number is something you can buy.'}</span>` : '';
  capEl.querySelectorAll('[data-show]').forEach(b => b.onclick = () => { v.show = b.dataset.show; renderHero(); });
  if ($('#retryRender')) $('#retryRender').onclick = () => startRender(open, v);
  const fr = $('#frames'); fr.hidden = S.frames.length < 2;
  if (S.frames.length > 1) {
    fr.innerHTML = S.frames.map((x, i) => `<button type="button" data-f="${i}" aria-pressed="${i === S.frame}" aria-label="Frame ${i + 1}"><img src="${x.url}" alt=""></button>`).join('');
    fr.querySelectorAll('button').forEach(b => b.onclick = () => { S.frame = +b.dataset.f; renderHero(); });
  }
}
function spread(P, w, h) {   // marker centres (0..1) for the shown frame, nudged apart so that no two overlap
  const pts = P.map(p => { if ((p.frame || 0) !== S.frame) return null; const [y0, x0, y1, x1] = box(p); return [(x0 + x1) / 2 * w, (y0 + y1) / 2 * h]; });
  const GAP = 28;
  for (let round = 0; round < 8; round++) for (let a = 0; a < pts.length; a++) for (let b = a + 1; b < pts.length; b++) {
    if (!pts[a] || !pts[b]) continue;
    let dx = pts[b][0] - pts[a][0], dy = pts[b][1] - pts[a][1], d = Math.hypot(dx, dy);
    if (d >= GAP) continue;
    if (d < 0.5) { dx = 0; dy = 1; d = 1; }
    const push = (GAP - d) / 2;
    pts[a][0] -= dx / d * push; pts[a][1] -= dy / d * push; pts[b][0] += dx / d * push; pts[b][1] += dy / d * push;
  }
  return pts.map(q => q && [Math.min(w - 14, Math.max(14, q[0])) / w, Math.min(h - 14, Math.max(14, q[1])) / h]);
}
function box(p) {
  let b = (p.box_2d || []).slice(0, 4).map(v => Math.max(0, Math.min(1000, +v || 0)) / 1000);
  if (b.length < 4 || b[2] - b[0] < .02 || b[3] - b[1] < .02) b = [0.4, 0.4, 0.6, 0.6];
  return b;
}

function renderItems() {
  const ol = $('#items'), act = document.activeElement, keep = act && act.id === 'wishInput' ? { v: act.value, s: act.selectionStart } : null;
  const typed = $('#wishInput') ? $('#wishInput').value : '';
  if (!S.products) {
    ol.innerHTML = S.phase === 'error' ? '' : [62, 44, 71, 50].map(w => `<li class="item skeleton"><div class="rowbtn"><span class="num">&nbsp;</span><span class="bar" style="width:${w}%"></span><span></span></div></li>`).join('');
    return;
  }
  ol.innerHTML = S.products.map((p, i) => {
    const [head, sub] = nameParts(p), open = S.open === i;
    return `<li class="item${open ? ' open' : ''}" id="item${i}">
      <button type="button" class="rowbtn" data-i="${i}" aria-expanded="${open}" aria-controls="detail${i}">
        <span class="num">${i + 1}</span><span class="nm"><b>${esc(head)}</b>${sub ? `<small>${esc(sub)}</small>` : ''}</span><span class="st">${rowState(p)}</span>
      </button>${open ? `<div class="detail" id="detail${i}">${detail(p, i)}</div>` : ''}</li>`;
  }).join('');
  ol.querySelectorAll('.rowbtn').forEach(b => b.onclick = () => openItem(+b.dataset.i, true));
  if (S.open != null) wire(S.products[S.open]);
  const inp = $('#wishInput');
  if (inp && typed) inp.value = typed;
  if (inp && keep) { inp.focus(); try { inp.setSelectionRange(keep.s, keep.s); } catch (e) { /* not a text field */ } }
}

function detail(p, i) {
  const v = viewOf(p), a = pickOf(p);
  let h = `<p class="verdict">${verdict(p)}</p><div class="remix"><h3>Change it</h3>`;
  // real options the merchant sells: grounded in the catalog, so they come first
  if (p.opts && p.opts.state === 'ready' && a) {
    const shown = p.opts.options.filter(o => o.values.length > 1);
    if (shown.length) {
      h += `<p class="lead">${p.status === 'auto' || p.status === 'confirm' ? 'This exact listing' : 'The closest listing'}, from ${esc(a.seller)}, really comes in:</p>`;
      h += shown.map(o => `<div class="chips" style="margin-top:6px"><span class="optname">${esc(o.name)}</span>${o.values.slice(0, 12).map(x => {
        const on = (p.optSel || {})[o.name] === x.label, sw = VISUAL.test(o.name) && x.swatch;
        return `<button type="button" class="chip${sw ? ' sw' : ''}${x.available === false ? ' na' : ''}" data-opt="${esc(o.name)}" data-val="${esc(x.label)}" aria-pressed="${on}" ${x.available === false ? 'title="Sold out"' : ''}>${sw ? `<i style="background-image:url('${esc(cdn(x.swatch, 60))}')"></i>` : ''}${esc(x.label)}</button>`;
      }).join('')}${o.values.length > 12 ? `<span class="hint" style="align-self:center">and ${o.values.length - 12} more at the store</span>` : ''}</div>`).join('');
      if (p.opts.note) h += `<p class="hint" style="margin-top:6px">${esc(p.opts.note)}</p>`;
    }
  }
  h += `<p class="lead">Ideas from Gemini 4 Hydrogen</p><div class="chips">`;
  if (!p.ideas || p.ideas.state === 'loading') h += '<span class="chip ghost"></span><span class="chip ghost"></span><span class="chip ghost"></span><span class="chip ghost"></span>';
  else if (p.ideas.state === 'error') h += `<span class="hint">No ideas arrived (${esc(p.ideas.msg)}). You can still describe a change.</span>`;
  else h += p.ideas.list.map((x, n) => `<button type="button" class="chip" data-idea="${n}" aria-pressed="${p.view === 'idea:' + key(x.label)}">${esc(x.label)}</button>`).join('');
  h += `</div><form class="wish" id="wishForm"><input id="wishInput" type="text" maxlength="120" autocomplete="off" aria-label="Describe how you want it" placeholder="Or say how you want it"><button type="submit">Show me</button></form>`;
  if (v) h += `<p class="showing">Showing <b>${esc(v.v.label)}</b><button type="button" class="quiet" id="backOrig">Back to the original</button></p>`;
  h += '</div>';

  // listings
  const cands = v ? v.cands : p.candidates, pick = v ? v.pick : p.pick;
  if (v) {
    if (v.kind === 'real') h += `<h3 class="listhead">The real thing in ${esc(v.v.label)}</h3><p class="listnote">This is the merchant's own option for that listing, with its live price.</p>`;
    else {
      h += `<h3 class="listhead">Real products for ${esc(v.v.label)}</h3>`;
      if (v.state === 'loading') h += '<p class="listnote"><span class="badge wait">Searching Shopify</span></p>';
      else if (v.state === 'error') h += `<p class="listnote err">${esc(v.msg)}</p>`;
      else if (!cands.length) h += `<p class="nothing">Shopify's catalog has nothing for "${esc(v.v.query)}". Try other words.</p>`;
      else if (v.reviewing) h += '<p class="listnote"><span class="badge wait">Second look</span> Ranked by Decision 1. Gemini 4 Neon is picking the closest one.</p>';
      else if (v.status === 'similar') h += `<p class="listnote">Similar, not identical: these are real listings close to the version you asked for.${v.decided_by ? ' Chosen by ' + (MODEL[v.decided_by] || v.decided_by) + '.' : ''}</p>`;
      else h += `<p class="listnote">${v.reason ? esc(cap(v.reason)) + '. ' : ''}None of these is really close; they are listed in case one works.</p>`;
    }
  } else if (p.candidates && p.candidates.length) h += '<h3 class="listhead">Real products</h3>';
  if (cands && cands.length) {
    const score = c => (c.same ?? c.fit ?? 0) + (c.p || 0);
    const order = cands.map((c, k) => k).sort((x, y) => (y === pick) - (x === pick) || score(cands[y]) - score(cands[x]));
    const lim = (v ? v.showAll : p.showAll) ? order.length : Math.min(3, order.length);
    h += '<div class="cards">' + order.slice(0, lim).map(k => card(p, v, cands[k], k, k === pick)).join('') + '</div>';
    if (order.length > lim) h += `<button type="button" class="quiet more" id="showAll">Show ${plural(order.length - lim, 'more listing')}</button>`;
  } else if (!v && p.status === 'searching') h += '<p class="nothing">Searching Shopify stores.</p>';
  return h;
}

function card(p, v, c, k, isPick) {
  const sel = p.sel && p.sel.variant_id === c.variant_id, busy = v ? v.reviewing : p.pending;
  let tag = '', diff = '';
  if (v && v.kind === 'real') tag = '<span class="badge real">Real option</span>';
  else if (isPick && !busy) {
    const st = v ? v.status : p.status, reason = v ? v.reason : p.reason;
    if (st === 'auto') tag = '<span class="badge exact">Exact</span>';
    else if (st === 'confirm') tag = '<span class="badge similar">Exact?</span>';
    else if (st === 'similar') { tag = '<span class="badge similar">Similar</span>'; diff = reason ? `<p class="diff">${esc(cap(reason))}.</p>` : ''; }
  }
  const off = c.list_price ? Math.round((1 - c.price / c.list_price) * 100) : 0;
  const chosen = Object.entries(c.selected || {}).map(([n, l]) => `${n}: ${l}`).join(', ');
  const stock = c.available === true ? 'In stock' : c.available === false ? '<span class="out">Sold out</span>' : '';
  const rating = c.rating && c.rating_count ? `${(+c.rating).toFixed(1)} of 5 from ${plural(c.rating_count, 'review')}` : '';
  return `<div class="card${sel ? ' sel' : ''}">
    <button type="button" class="ph" data-k="${k}" aria-label="${sel ? 'Remove from selection' : 'Add to selection'}: ${esc(c.title)}"><img src="${esc(cdn(c.image, 240))}" alt="" loading="lazy" referrerpolicy="no-referrer"></button>
    <div>${tag}
      <p class="store">${esc(c.seller || c.shop)}</p>
      <p class="ttl">${esc(c.title)}</p>
      ${chosen ? `<p class="var">${esc(chosen)}</p>` : ''}
      ${diff}
      <p class="money"><span class="now">${money(c.price, c.currency)}</span>${off > 0 ? `<span class="was">${money(c.list_price, c.currency)}</span><span class="cut">${off}% off</span>` : ''}</p>
      <p class="meta">${[stock, rating].filter(Boolean).join(', ')}${c.dropped ? ' <span class="out">The store does not have that exact combination; this is the nearest.</span>' : ''}</p>
      <p class="acts"><button type="button" class="${sel ? '' : 'solid'}" data-k="${k}" aria-pressed="${sel}" ${c.available === false ? 'disabled' : ''}>${sel ? 'In selection. Remove' : 'Add to selection'}</button><a href="${esc(c.url)}" target="_blank" rel="noopener">View at ${esc(c.seller || 'store')}</a></p>
    </div></div>`;
}

function wire(p) {
  const d = $('.detail'); if (!d) return;
  const v = viewOf(p), cands = v ? v.cands : p.candidates;
  d.querySelectorAll('.card [data-k]').forEach(b => b.onclick = () => {
    const c = cands[+b.dataset.k];
    p.sel = p.sel && p.sel.variant_id === c.variant_id ? null : c; p.touched = true; render();
  });
  d.querySelectorAll('[data-idea]').forEach(b => b.onclick = () => applyIdea(p, p.ideas.list[+b.dataset.idea], 'idea'));
  d.querySelectorAll('[data-opt]').forEach(b => b.onclick = () => chooseOption(p, b.dataset.opt, b.dataset.val));
  const form = $('#wishForm');
  if (form) form.onsubmit = e => { e.preventDefault(); const w = $('#wishInput').value.trim(); if (!w) return; $('#wishInput').value = ''; applyIdea(p, { label: w, wish: w }, 'wish'); };
  if ($('#backOrig')) $('#backOrig').onclick = () => { p.view = null; if (p.opts && pickOf(p)) p.optSel = { ...p.opts.base }; render(); };
  if ($('#showAll')) $('#showAll').onclick = () => { (v || p).showAll = true; render(); };
}

function openItem(i, byUser, scroll) {
  if (byUser) S.userOpened = true;
  S.open = S.open === i && byUser ? null : i;
  if (S.open != null) { const p = S.products[i]; S.frame = p.frame || 0; lazyLoad(p); }
  render();
  if (S.open != null && scroll) $('#item' + i).scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && S && S.open != null && e.target.tagName !== 'INPUT') { const i = S.open; S.open = null; render(); const b = document.querySelector(`#item${i} .rowbtn`); if (b) b.focus(); } });

// ---------------------------------------------------------------- remix
function lazyLoad(p) {   // on opening an item: Hydrogen's ideas, and the real options of its listing
  const scan = S;
  if (!p.ideas) {
    p.ideas = { state: 'loading' };
    post('/api/remix/suggest', { image_id: scan.ids[p.frame || 0], label: p.label }).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      p.ideas = { state: 'ready', list: (await r.json()).variations };
    }).catch(e => { p.ideas = { state: 'error', msg: String(e.message).slice(0, 80) }; }).finally(() => { if (scan === S) renderItems(); });
  }
  const a = pickOf(p);
  if (a && !p.pending && !p.opts && Object.values(a.options || {}).some(vals => vals.length > 1)) {
    p.opts = { state: 'loading', product_id: a.product_id };
    post('/api/product', { product_id: a.product_id }).then(async r => {
      if (!r.ok) throw new Error('no options');
      const j = await r.json(), base = { ...((j.variant || {}).selected || a.selected || {}) };
      p.opts = { state: 'ready', product_id: a.product_id, options: j.options, base }; p.optSel = { ...base };
    }).catch(() => { p.opts = { state: 'none', product_id: a.product_id }; }).finally(() => { if (scan === S) renderItems(); });
  }
}

function applyIdea(p, idea, kind) {
  const k = kind + ':' + key(idea.label);
  if (p.remix[k]) { p.view = k; return render(); }
  const scan = S, v = p.remix[k] = { kind, v: { label: idea.label, edit: idea.edit || idea.wish, query: idea.query || '' }, cands: [], pick: null, status: 'none', state: 'loading', reviewing: false, show: 'preview', t0: performance.now(), marks: {} };
  p.view = k; if (p.opts && p.opts.base) p.optSel = { ...p.opts.base };
  const slim = { label: p.label, brand: p.brand, name: p.name, variant: p.variant, size: p.size, crop: p.crop };
  const body = idea.wish ? { product: slim, wish: idea.wish } : { product: slim, variation: idea };
  const mark = n => { v.marks[n] = +((performance.now() - v.t0) / 1000).toFixed(1); document.documentElement.dataset.remix = JSON.stringify(v.marks); };
  startRender(p, v, mark);                      // (b) the redraw, about 15 s
  (async () => {                                // (a) real products for the new version, a few seconds
    try {
      const r = await post('/api/remix/match', body);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      const j = await r.json();
      Object.assign(v, { cands: j.candidates, pick: j.pick, status: j.status, decided_by: j.decided_by, state: 'ready', reviewing: j.pending });
      v.v = { ...v.v, ...j.variation, label: idea.wish ? j.variation.label : v.v.label }; mark('products');
      if (scan === S) render();
      if (j.pending) {
        const rr = await (await post('/api/remix/review', { product: slim, variation: j.variation, candidates: j.candidates })).json();
        if (!rr.error) Object.assign(v, { pick: rr.pick, status: rr.status, reason: rr.reason, decided_by: rr.decided_by });
        v.reviewing = false; mark('neon');
      }
    } catch (e) { v.state = 'error'; v.msg = 'Could not search Shopify for that (' + e.message + ').'; v.reviewing = false; }
    if (scan === S) render();
  })();
  render();
}

function startRender(p, v, mark) {
  const scan = S;
  v.render = { state: 'loading' }; v.show = 'preview';
  post('/api/remix/render', { image_id: scan.ids[p.frame || 0], label: p.label, edit: v.v.edit, ref_url: v.ref || '', box_2d: (p.box_2d || []).map(n => Math.round(+n || 0)) }).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || 'the server said ' + r.status);
    await new Promise(res => { const im = new Image(); im.onload = im.onerror = res; im.src = j.url; });   // decode first, then cross-fade
    v.render = { state: 'ready', url: j.url }; if (mark) mark('redraw');
  }).catch(e => { v.render = { state: 'error', msg: cap(String(e.message).slice(0, 140)) + '.' }; }).finally(() => { if (scan === S) render(); });
  if (scan === S) renderHero();
}

async function chooseOption(p, name, label) {   // a real option of the matched listing (colour, size ...)
  const scan = S, a = pickOf(p); if (!a || !p.opts) return;
  p.optSel = { ...p.optSel, [name]: label }; p.opts.note = '';
  renderItems();
  let j;
  try {
    const r = await post('/api/product', { product_id: p.opts.product_id, selected: p.optSel });
    if (!r.ok) throw new Error();
    j = await r.json(); if (!j.variant) throw new Error();
  } catch (e) { p.opts.note = 'The store did not return that option just now.'; if (scan === S) renderItems(); return; }
  if (scan !== S) return;
  const variant = j.variant, base = p.opts.base;
  const changed = Object.keys(p.optSel).filter(n => VISUAL.test(n) && p.optSel[n] !== base[n]);
  const wasSel = p.sel && (p.sel.product_id === a.product_id);
  if (!changed.length) {          // only size or the like differs: same picture, the listing just points at that variant
    Object.assign(a, { variant_id: variant.variant_id, price: variant.price, list_price: variant.list_price, available: variant.available, selected: variant.selected, url: variant.url, dropped: variant.dropped });
    p.view = null; if (wasSel) p.sel = a;
    return render();
  }
  const lab = changed.map(n => p.optSel[n]).join(', '), k = 'real:' + changed.map(n => n + '=' + p.optSel[n]).join('|');
  let v = p.remix[k];
  if (v) { v.cands = [variant]; p.view = k; if (wasSel) p.sel = variant; return render(); }
  v = p.remix[k] = { kind: 'real', cands: [variant], pick: 0, status: 'real', state: 'ready', show: 'preview',
    v: { label: lab, edit: `change its ${changed.map(n => n.toLowerCase() + ' to ' + p.optSel[n]).join(' and its ')}, as in the store's product "${variant.title}". Keep the item's own shape and size.` } };
  const opt = p.opts.options.find(o => o.name === changed[0]), val = opt && opt.values.find(x => x.label === p.optSel[changed[0]]);
  v.ref = (variant.image && variant.image !== a.image ? variant.image : (val && val.swatch)) || '';
  p.view = k; if (wasSel) p.sel = variant;
  startRender(p, v);
  render();
}

// ---------------------------------------------------------------- cart bar, carts, checkout
const chosen = () => S && S.products ? S.products.filter(p => p.sel).map(p => p.sel) : [];
const sig = list => list.map(c => c.variant_id).sort().join(',');
function renderBar() {
  const bar = $('#cartbar'), list = chosen(), stores = new Set(list.map(c => c.shop)).size, btn = $('#btnCart');
  bar.hidden = !S.products || !S.products.length;
  if (!list.length) {
    $('#selCount').textContent = 'Nothing selected yet'; $('#selSum').textContent = 'Add listings and they collect here.';
    btn.textContent = 'Build cart'; btn.disabled = true; return;
  }
  const total = list.reduce((a, c) => a + (c.price || 0), 0), fresh = S.carts && S.carts.sig === sig(list);
  $('#selCount').textContent = `${plural(list.length, 'item')}, ${money(total, list[0].currency)}`;
  $('#selSum').textContent = stores === 1 ? '1 cart, 1 store. Shipping and tax come at checkout.' : `${stores} carts, ${stores} stores: Shopify needs one cart per store.`;
  btn.disabled = !!S.building;
  btn.textContent = S.building ? 'Building' : fresh ? 'View ' + (stores === 1 ? 'cart' : 'carts') : `Build ${stores === 1 ? 'the cart' : stores + ' carts'}`;
}
$('#btnCart').onclick = async () => {
  const list = chosen(), scan = S; if (!list.length) return;
  if (S.carts && S.carts.sig === sig(list)) return $('#carts').scrollIntoView({ behavior: 'smooth', block: 'start' });
  S.building = true; renderBar();
  try {
    const r = await post('/api/cart', { lines: list.map(c => ({ shop: c.shop, variant_id: c.variant_id, quantity: 1 })) });
    if (!r.ok) throw new Error('the server said ' + r.status);
    const j = await r.json();
    scan.carts = { sig: sig(list), carts: j.carts, ms: j.ms, by: Object.fromEntries(list.map(c => [c.variant_id, c])), names: Object.fromEntries(list.map(c => [c.shop, c.seller])) };
  } catch (e) { scan.carts = { sig: '', carts: [], error: 'The carts could not be built: ' + e.message }; }
  scan.building = false;
  if (scan !== S) return;
  render(); refreshShop();
  $('#carts').scrollIntoView({ behavior: 'smooth', block: 'start' });
};

function renderCarts() {
  const el = $('#carts'), C = S.carts;
  if (!C) { if (el.innerHTML) el.innerHTML = ''; return; }
  const stale = C.sig !== sig(chosen()), ok = C.carts.filter(c => !c.error);
  let h = `<h2>${ok.length === 1 ? 'Your cart' : 'Your carts'}</h2>`;
  if (C.error) h += `<p class="promise err">${esc(C.error)}</p>`;
  else h += `<p class="promise">${plural(ok.length, 'real cart')} at ${plural(ok.length, 'Shopify store')}, one per store. Nothing is bought yet: you review and pay on Shopify's own checkout page, and ShelfCart never sees your payment details.${stale ? ' <b>Your selection changed since these were built.</b>' : ''}</p>`;
  h += C.carts.map((c, n) => {
    const name = C.names[c.shop] || c.shop;
    if (c.error) return `<div class="cart"><h3>${esc(name)}</h3><p class="err">This cart could not be created: ${esc(c.error)}</p></div>`;
    const total = c.totals.find(t => t.type === 'total'), co = c.checkout;
    return `<div class="cart${stale ? ' stale' : ''}"><h3>${esc(name)}<small>${plural(c.lines.length, 'item')}</small></h3>
      ${c.lines.map(l => { const m = C.by[l.variant_id] || {}; return `<div class="line"><img src="${esc(cdn(l.image || m.image, 120))}" alt=""><span>${esc(m.title || l.title)}${Object.keys(m.selected || {}).length ? `<br><span class="hint">${esc(Object.entries(m.selected).map(([a, b]) => a + ': ' + b).join(', '))}</span>` : ''}</span><span class="amt">${money(l.price, c.currency)}</span></div>`; }).join('')}
      ${c.discounts.length ? `<p class="hint" style="padding-top:8px"><span class="cut">Discount</span> ${esc(c.discounts.join(', '))}</p>` : ''}
      ${c.messages && c.messages.length ? `<p class="err" style="padding-top:8px">${esc(c.messages.join(' '))}</p>` : ''}
      <div class="tot"><span>${co && co.total ? 'Total with tax' : 'Estimated total'}</span><span class="amt">${money(co && co.total ? co.total : total && total.amount, c.currency)}</span></div>
      <div class="row"><button type="button" class="solid" data-co="${n}" ${co && co.busy ? 'disabled' : ''}>${co && co.busy ? 'Opening Shopify checkout' : 'Check out with Shop Pay'}</button><a href="${esc(c.continue_url)}" target="_blank" rel="noopener">Open cart at ${esc(name)}</a></div>
      <p class="conote">${co && co.url ? `Shopify's checkout opened in a new tab${co.tax ? ' (' + money(co.tax, c.currency) + ' tax)' : ''}. <a href="${esc(co.url)}" target="_blank" rel="noopener">Open it again</a>. You pay there, with Shop Pay or a card.` : co && co.error ? `<span class="err">Checkout is not available for this cart: ${esc(co.error)}</span>` : "Opens Shopify's own checkout page in a new tab. You review the order and pay there."}</p></div>`;
  }).join('');
  h += `<div class="shopacct" id="shopAcct">${shopAcctHtml()}</div>`;
  el.innerHTML = h;
  el.querySelectorAll('[data-co]').forEach(b => b.onclick = () => checkout(C.carts[+b.dataset.co]));
  wireShop();
}

async function checkout(c) {   // the person pressed "Check out with Shop Pay": make a checkout session and hand over to Shopify
  const scan = S, w = window.open('', '_blank');   // opened inside the click so the browser allows it
  if (w) { try { w.document.title = 'Opening Shopify checkout'; w.document.body.innerHTML = '<p style="font:16px system-ui;padding:24px">Opening Shopify checkout.</p>'; } catch (e) { /* cross-origin already */ } }
  c.checkout = { busy: true }; renderCarts();
  try {
    const r = await post('/api/checkout', { shop: c.shop, cart_id: c.id });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    if (!j.continue_url) throw new Error('Shopify returned no checkout page');
    const t = k => (j.totals.find(x => x.type === k) || {}).amount;
    c.checkout = { url: j.continue_url, total: t('total'), tax: t('tax') };
    if (w) { w.opener = null; w.location.replace(j.continue_url); }
  } catch (e) { c.checkout = { error: String(e.message).slice(0, 160) }; if (w) w.close(); }
  if (scan === S) renderCarts();
}

// ---------------------------------------------------------------- optional: sign in with Shop
function shopAcctHtml() {
  if (shop.state === 'signed_in') return `Signed in with Shop${shop.email ? ' as ' + esc(shop.email) : ''}. Searches use your Shop preferences and checkout can offer your saved Shop Pay details. <button type="button" class="quiet" id="shopOut">Sign out</button>`;
  if (shop.state === 'pending') return `Approve the sign-in on Shop's own page: <a href="${esc(shop.link)}" target="_blank" rel="noopener">open Shop</a>${shop.code ? ' and check that it shows the code <code>' + esc(shop.code) + '</code>' : ''}. Waiting for you there. <button type="button" class="quiet" id="shopOut">Cancel</button>`;
  return `Optional: <button type="button" class="quiet" id="shopIn">sign in with Shop</button> so checkout can use your saved address and Shop Pay. Everything works without it.${shop.error ? ' <span class="err">' + esc(shop.error) + '</span>' : ''}`;
}
let shopTimer = null;
async function refreshShop() {
  try { Object.assign(shop, await (await fetch('/api/shop/status')).json()); } catch (e) { return; }
  const el = $('#shopAcct'); if (el) { el.innerHTML = shopAcctHtml(); wireShop(); }
  clearTimeout(shopTimer);
  if (shop.state === 'pending') shopTimer = setTimeout(refreshShop, 3000);
}
function wireShop() {
  if ($('#shopIn')) $('#shopIn').onclick = async () => {
    const r = await post('/api/shop/signin', {}), j = await r.json().catch(() => ({}));
    if (r.ok) Object.assign(shop, j); else Object.assign(shop, { state: 'signed_out', error: j.detail || 'Shop sign-in is not available.' });
    const el = $('#shopAcct'); el.innerHTML = shopAcctHtml(); wireShop();
    if (shop.state === 'pending') { clearTimeout(shopTimer); shopTimer = setTimeout(refreshShop, 3000); }
  };
  if ($('#shopOut')) $('#shopOut').onclick = async () => { Object.assign(shop, await (await post('/api/shop/signout', {})).json()); const el = $('#shopAcct'); el.innerHTML = shopAcctHtml(); wireShop(); };
}

// ---------------------------------------------------------------- input wiring
const drop = $('#drop');
$('#file').onchange = e => { const f = [...e.target.files]; e.target.value = ''; if (f.length) intake(f, 'file'); };
drop.onclick = () => $('#file').click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#file').click(); } };
$('#newBtn').onclick = () => $('#file').click();
document.addEventListener('paste', e => {
  if (e.target && e.target.tagName === 'INPUT' && !(e.clipboardData && [...e.clipboardData.items].some(i => i.kind === 'file'))) return;   // pasting text into the wish box
  const items = [...(e.clipboardData?.items || [])].filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
  const files = items.length ? items : [...(e.clipboardData?.files || [])];
  if (!files.length) { if (!S) setStatus('The clipboard has no image. Copy a screenshot first, then paste.', true); return; }
  e.preventDefault(); intake(files, 'pasted image');
});
let dragDepth = 0;
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
window.addEventListener('dragenter', e => { if (hasFiles(e)) { e.preventDefault(); dragDepth++; document.body.classList.add('dragging'); } });
window.addEventListener('dragover', e => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
window.addEventListener('dragleave', e => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
window.addEventListener('drop', e => {   // anywhere on the page counts, so a near miss does not navigate away
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; document.body.classList.remove('dragging');
  intake([...e.dataTransfer.files], 'dropped file');
});

// camera pan (kept from the first version of the app)
let stream = null; const cam = $('#cam');
$('#btnCam').onclick = async () => {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false });
    cam.srcObject = stream; $('#btnScan').disabled = false;
  } catch (e) { $('.camera .hint').textContent = 'The camera is not available (' + e.message + '). Choose a photo or a video instead.'; }
};
$('#btnScan').onclick = async () => {
  $('#btnScan').disabled = true;
  const shots = [], t0 = performance.now(), DUR = 4000;
  while (performance.now() - t0 < DUR) {
    const c = grab(cam, cam.videoWidth, cam.videoHeight); shots.push({ canvas: c, score: sharpness(c) });
    $('#btnScan').textContent = 'Scanning, ' + Math.ceil((DUR - (performance.now() - t0)) / 1000) + ' s. Keep moving slowly';
    await new Promise(r => setTimeout(r, 250));
  }
  $('#btnScan').disabled = false; $('#btnScan').textContent = 'Scan for 4 seconds';
  startCanvases(pickFrames(shots, 5));
};

// ---------------------------------------------------------------- "How well it works": the saved tests/eval.py run
$('#evalBox').addEventListener('toggle', async () => {
  const box = $('#evalBody'); if (box.dataset.done) return; box.dataset.done = '1';
  try {
    const r = await fetch('/api/eval'); if (!r.ok) throw new Error('none');
    const e = await r.json(), cell = (v, t) => t ? `${v} of ${t}` : v;
    box.innerHTML = `<p class="hint" style="margin-top:8px">${esc(e.summary || '')} Run on ${esc(e.date || '?')}. The baseline is one Hydrogen call and the first search result, with everything pre-selected.</p>
      <div class="evalwrap"><table class="eval"><tr><th></th><th>Items found</th><th>Correct exact, pre-selected</th><th>Wrong pre-selected</th><th>Correctly held back</th><th>Held back, was right</th><th>Similar acceptable</th><th>Seconds per image</th></tr>
      ${e.rows.map(x => `<tr><td><b>${esc(x.name)}</b></td><td>${cell(x.found, e.key_items)}</td><td>${cell(x.correct_pre, e.exact_possible)}</td><td>${x.wrong_pre}</td><td>${x.held_ok}</td><td>${x.held_missed ?? ''}</td><td>${x.similar_ok == null ? 'n/a' : cell(x.similar_ok, x.similar_offered)}</td><td>${x.sec_per_image ?? ''}</td></tr>`).join('')}</table></div>
      <p><b>What the numbers hide</b></p><ul>${(e.hides || []).map(t => `<li>${esc(t)}</li>`).join('')}</ul>
      <p><b>Known failure cases</b></p><ul>${(e.failures || []).map(t => `<li>${esc(t)}</li>`).join('')}</ul>`;
  } catch (err) { box.innerHTML = '<p class="hint">No test run is saved yet.</p>'; }
});

new ResizeObserver(() => { if (S && S.products) renderHero(); }).observe($('#sheet'));

// ---------------------------------------------------------------- start
const scanId = new URLSearchParams(location.search).get('scan');
if (scanId) startFromId(scanId);

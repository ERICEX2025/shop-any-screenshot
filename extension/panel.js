// Side panel page. It only decides what the iframe shows: the ShelfCart page from the local server, started on the
// image the service worker stored. If the server is not running it says so and keeps checking.
const DEFAULT_SERVER = 'http://localhost:8202', KEY = 'shelfcart';
const $ = id => document.getElementById(id);
let server = DEFAULT_SERVER, shown = '', downTimer = null;

function message(title, body, code, retry) {
  $('app').hidden = true; $('note').hidden = true; $('msg').hidden = false;
  $('msgTitle').textContent = title; $('msgBody').textContent = body;
  $('msgCode').hidden = !code; $('msgCode').textContent = code || '';
  $('retry').hidden = !retry;
}
function frame(url, note) {
  $('msg').hidden = true; $('app').hidden = false;
  $('note').hidden = !note; $('note').textContent = note || '';
  if (shown !== url) { shown = url; $('app').src = url; }
}
async function alive() {
  try { const r = await fetch(server + '/api/health', { cache: 'no-store' }); return r.ok && (await r.json()).app === 'shelfcart'; }
  catch (e) { return false; }
}
function down() {
  shown = '';
  message('The ShelfCart server is not running',
    'This panel shows the ShelfCart app from your own computer (' + server + '), and nothing answered there. Start it in a terminal, then this panel continues by itself:',
    'cd ~/dev/g4-shelf && .venv/bin/uvicorn app.server:app --port 8202', true);
  clearTimeout(downTimer); downTimer = setTimeout(show, 3000);
}

async function show() {
  clearTimeout(downTimer);
  server = ((await chrome.storage.local.get('server')).server || DEFAULT_SERVER).replace(/\/+$/, '');
  const s = (await chrome.storage.session.get(KEY))[KEY] || { status: 'idle' };
  if (s.status === 'working') return message('One moment', s.note || 'Getting the image.');
  if (s.status === 'error') return message('That image could not be used', s.message + ' Try another image, or click the ShelfCart toolbar button to capture the visible page.');
  if (s.status === 'down' || !(await alive())) return down();
  if (s.status === 'ready') {
    const note = s.source === 'capture' ? 'The image itself could not be downloaded (' + s.why + '), so this is a capture of the visible page.'
      : s.source === 'page' ? 'This is a capture of the visible page.' : '';
    return frame(server + '/panel?scan=' + encodeURIComponent(s.scan) + '&t=' + s.at, note);
  }
  frame(server + '/panel');   // nothing chosen yet: the app's own paste and drop page
}

chrome.storage.onChanged.addListener((changes, area) => { if (area === 'session' && changes[KEY]) show(); });
$('retry').onclick = show;
show();

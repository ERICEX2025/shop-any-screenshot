// ShelfCart extension service worker. It stays thin: get the image bytes, hand them to the local ShelfCart server,
// tell the side panel which image to show. All the shopping happens in the server's own page inside the panel.
const DEFAULT_SERVER = 'http://localhost:8202';
const KEY = 'shelfcart';   // chrome.storage.session: what the side panel should show

async function serverUrl() {
  const { server } = await chrome.storage.local.get('server');   // optional override, see README
  return (server || DEFAULT_SERVER).replace(/\/+$/, '');
}
const setState = s => chrome.storage.session.set({ [KEY]: { ...s, at: Date.now() } });

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => chrome.contextMenus.create({ id: 'shop-image', title: 'Shop this image', contexts: ['image'] }));
});

// sidePanel.open only works while the click is still "fresh": call it first, before anything is awaited.
function openPanel(tab) {
  try {
    const p = chrome.sidePanel.open({ windowId: tab.windowId });
    if (p && p.catch) p.catch(e => console.warn('side panel:', e.message));
  } catch (e) { console.warn('side panel:', e.message); }
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== 'shop-image' || !tab) return;
  openPanel(tab);
  shopImage(info.srcUrl, tab);
});

chrome.action.onClicked.addListener(tab => {
  const shot = capture(tab);   // started before the panel opens, so the page is captured at its full width
  openPanel(tab);
  shopCapture(shot);
});

class ServerDown extends Error {}

async function capture(tab) {
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 90 });
  return (await fetch(dataUrl)).blob();
}

async function fetchImage(url) {
  if (!url) throw new Error('no image address');
  const r = await fetch(url, { credentials: 'include', cache: 'force-cache' });
  if (!r.ok) throw new Error('the site answered ' + r.status);
  const blob = await r.blob();
  if (blob.type && !blob.type.startsWith('image/')) throw new Error('not an image (' + blob.type + ')');
  if (blob.size < 1500) throw new Error('image too small');
  return blob;
}

async function upload(blob) {
  const base = await serverUrl();
  let r;
  try {
    r = await fetch(base + '/api/image', { method: 'POST', headers: { 'Content-Type': blob.type || 'application/octet-stream' }, body: blob });
  } catch (e) { throw new ServerDown(base); }
  if (!r.ok) throw new Error('the server could not read the image (' + r.status + ')');
  return (await r.json()).id;
}

// Right-click path: download the image itself; if the site refuses (or it is a blob:, an SVG ...), capture the visible tab.
async function shopImage(srcUrl, tab) {
  await setState({ status: 'working', note: 'Getting the image.' });
  try {
    let id, source = 'image', why = '';
    try {
      id = await upload(await fetchImage(srcUrl));
    } catch (e) {
      if (e instanceof ServerDown) throw e;
      why = e.message;
      id = await upload(await capture(tab));
      source = 'capture';
    }
    await setState({ status: 'ready', scan: id, source, why });
    return { id, source, why };
  } catch (e) { return fail(e); }
}

// Toolbar button path: the visible tab as one image.
async function shopCapture(shotPromise) {
  await setState({ status: 'working', note: 'Capturing the visible page.' });
  try {
    const id = await upload(await shotPromise);
    await setState({ status: 'ready', scan: id, source: 'page' });
    return { id, source: 'page' };
  } catch (e) { return fail(e); }
}

async function fail(e) {
  const s = e instanceof ServerDown ? { status: 'down', server: e.message } : { status: 'error', message: String(e && e.message || e).slice(0, 200) };
  await setState(s);
  return s;
}

// for tests (tests/ext_test.py calls these in the worker)
Object.assign(self, { shopImage, shopCapture, capture, fetchImage, upload, serverUrl });

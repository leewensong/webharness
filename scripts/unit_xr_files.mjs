/* Real-browser regression tests for the XR file panel. No app/database writes.
   Run: PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" node --test scripts/unit_xr_files.mjs
   Or install Playwright in a local environment and run node --test directly. */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'node:http';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : 'playwright');
let browser, server, origin;

const harness = `<!doctype html><meta charset="utf-8">
<script type="importmap">{"imports":{"three":"/static/vendor/three/three.module.js","three/addons/":"/static/vendor/three/addons/","@pixiv/three-vrm":"/static/vendor/three-vrm/three-vrm.module.min.js"}}</script>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { createXRFiles } from '/static/xr/xr-files.js';
import { mergeXRI18n } from '/static/xr/xr-i18n.js';
const pose = { position: [1, .5, -2], rotation: [0, .2, 0], scale: [1, 1, 1] };
const file = (id, visible = false) => ({ id, name: 'model-' + id + '.gltf', kind: 'model', size: 128,
  contentUrl: '/api/models/' + id, updatedAt: '2026-10-03 00:00:00', updatedBy: 'test',
  world: { visible, pose: visible ? structuredClone(pose) : null } });
const state = { files: [file(1), file(2)], revision: 0, calls: [], pending: [], delayed: new Set(),
  parsed: [], parseDelayed: new Set(), fail: new Set(), loads: new Map(), temps: new Set(),
  removedTemps: [], editable: true, disposals: 0, lang: 'zh', delayGets: false, pendingGets: [],
  delayWrites: new Set(), pendingWrites: [], rejectWrites: new Set(), sentTexts: [], exited: false };
const I18N = { zh: { filePlaced: '已摆入房间' }, en: { filePlaced: 'Placed in room' } };
mergeXRI18n(I18N);
const t = key => I18N[state.lang][key] || key;
const tf = (key, vars) => t(key).replace(/{{(.*?)}}/g, (_, name) => vars?.[name] ?? '');
const clone = value => structuredClone(value);
let poll;
window.setInterval = callback => { poll = callback; return 1; };
window.clearInterval = () => {};
const originalFetch = window.fetch.bind(window);
const positions = new Float32Array([-.5, -.5, 0, .5, -.5, 0, 0, .5, .5]);
const buffer = btoa(String.fromCharCode(...new Uint8Array(positions.buffer)));
const gltf = JSON.stringify({ asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }],
  nodes: [{ mesh: 0 }], meshes: [{ primitives: [{ attributes: { POSITION: 0 } }] }],
  buffers: [{ byteLength: positions.byteLength, uri: 'data:application/octet-stream;base64,' + buffer }],
  bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: positions.byteLength }],
  accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', min: [-.5, -.5, 0], max: [.5, .5, .5] }] });
const parse = GLTFLoader.prototype.parse;
GLTFLoader.prototype.parse = function(data, path, onLoad, onError) {
  return parse.call(this, data, path, result => {
    const id = result.parser.json.asset.extras.fileId;
    if (state.parseDelayed.has(id)) state.parsed.push({ id, resolve: () => onLoad(result) });
    else onLoad(result);
  }, onError);
};
window.fetch = async (url, init) => {
  const pathname = new URL(typeof url === 'string' ? url : url.url, location.href).pathname;
  if (!pathname.startsWith('/api/models/')) return originalFetch(url, init);
  const id = Number(pathname.split('/').pop());
  state.loads.set(id, (state.loads.get(id) || 0) + 1);
  if (state.delayed.has(id)) await new Promise(resolve => state.pending.push({ id, resolve }));
  const json = JSON.parse(gltf);
  json.asset.extras = { fileId: id };
  return new Response(JSON.stringify(json), { status: state.fail.has(id) ? 500 : 200, headers: { 'Content-Type': 'model/gltf+json' } });
};
const api = async (url, options) => {
  if (!url.includes('/files')) return { users: [], logId: null };
  if (!options) {
    const result = clone({ files: state.files, revision: state.revision });
    if (state.delayGets) return new Promise(resolve => state.pendingGets.push(() => resolve(result)));
    return result;
  }
  const id = Number(url.split('/').at(-2));
  const body = JSON.parse(options.body);
  state.calls.push({ id, body });
  if (state.delayWrites.has(id)) await new Promise(resolve => state.pendingWrites.push({ id, resolve }));
  if (state.rejectWrites.has(id)) throw new Error('placement failed');
  const f = state.files.find(f => f.id === id);
  f.world.visible = body.visible;
  if (body.visible) f.world.pose = { position: body.position, rotation: body.rotation, scale: body.scale };
  return clone({ file: f, revision: ++state.revision });
};
let scene, camera, files, app;
if (location.search.includes('main')) {
  const { createXR } = await import('/static/xr/xr-main.js');
  const root = document.createElement('div');
  document.body.appendChild(root);
  app = await createXR({ root, api, roomName: () => 'test', filesCanEdit: () => state.editable,
    roomInfo: () => ({ onlineUsers: [] }), username: () => 'test', token: () => 'test-token',
    logElement: document.createElement('div'), msgById: () => null, clearStaged: () => {},
    msgEvents: { subscribe: () => () => {} }, roomEvents: { subscribe: () => () => {} },
    immersive: () => false, setVoiceSpatial: () => {},
    sendText: async text => { state.sentTexts.push(text); },
    onExit: () => { state.exited = true; },
    I18N, t, tf });
  files = window.__xrDebug.files(); camera = window.__xrDebug.camera; scene = camera.parent.parent;
} else {
scene = new THREE.Scene(); camera = new THREE.PerspectiveCamera();
camera.position.set(0, 1.6, 0); scene.add(camera);
files = createXRFiles({ scene, camera, api, roomName: () => 'test', canEdit: () => state.editable,
  t, tf, statusEl: document.createElement('span'), hud: document.createElement('div'),
  placeChatModel: async id => { state.temps.add(id); return true; },
  removeChatModel: id => { state.temps.delete(id); state.removedTemps.push(id); return true; },
  disposeObjectTree: object => { state.disposals++; object.traverse(o => { o.geometry?.dispose();
    for (const m of (Array.isArray(o.material) ? o.material : [o.material])) m?.dispose(); }); },
});
scene.add(files.group);
}
window.__test = { files, state, scene, camera, app, poll: () => poll(),
  open: () => { files.openPanel(); scene.updateMatrixWorld(true); },
  click: (act, id) => { scene.updateMatrixWorld(true); const h = files._dbg.state().hotspots.find(h => h.act === act && (id == null || h.fileId === id));
    if (!h) return false; return files._dbg.click(h.x + h.w / 2, h.y + h.h / 2); },
  screenAt: (act, id) => { scene.updateMatrixWorld(true); const s = files._dbg.state();
    const h = s.hotspots.find(h => h.act === act && (id == null || h.fileId === id));
    const p = files.group.children[1].localToWorld(new THREE.Vector3((h.x + h.w / 2) / s.panelW - .5,
      .5 - (h.y + h.h / 2) / s.panelH, 0)).project(camera);
    return { x: (p.x + 1) * innerWidth / 2, y: (1 - p.y) * innerHeight / 2 }; },
  remote: (id, visible) => { const f = state.files.find(f => f.id === id); f.world.visible = visible;
    if (visible && !f.world.pose) f.world.pose = clone(pose); state.revision++; },
  release: id => { const p = state.pending.findIndex(p => p.id === id); if (p < 0) return false;
    state.pending.splice(p, 1)[0].resolve(); return true; },
  releaseParsed: id => { const p = state.parsed.findIndex(p => p.id === id); if (p < 0) return false;
    state.parsed.splice(p, 1)[0].resolve(); return true; },
  releaseWrite: id => { const p = state.pendingWrites.findIndex(p => p.id === id); if (p < 0) return false;
    state.pendingWrites.splice(p, 1)[0].resolve(); return true; },
  records: () => files.group.children[0].children.map(h => ({ id: h.userData.fileId, children: h.children.map(c => c.type) })),
  tempModels: () => scene.children.filter(o => o.type === 'Group' && o.children[0]?.parser == null
    && o.children[0]?.type === 'Group' && o.children[0]?.children[0]?.type === 'Mesh'),
  dispose: () => app ? app.dispose() : files.dispose(),
};
</script>`;

before(async () => {
  server = createServer(async (req, res) => {
    try {
      if (new URL(req.url, 'http://localhost').pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end(harness); return; }
      const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
      if (!path.startsWith(join(root, 'static') + '/')) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', 'text/javascript'); res.end(await readFile(path));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });

async function withPage(fn, fullXR = false) {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    page.setDefaultTimeout(10000);
    await page.goto(origin + (fullXR ? '/?main' : '/'));
    await page.waitForFunction(() => window.__test?.files._dbg.files().length === 2);
    await fn(page);
    assert.deepEqual(errors, [], 'no uncaught browser errors');
  } catch (error) {
    if (errors.length) error.message += '\nBrowser errors: ' + errors.join('\n');
    throw error;
  } finally { await page.close(); }
}
async function ready(page, id) {
  await page.waitForFunction(id => __test.records().some(r => r.id === id && r.children[0] === 'Group'), id);
}

test('each model has a list toggle; showing/hiding one does not affect another', () => withPage(async page => {
  await page.evaluate(() => __test.open());
  assert.equal(await page.evaluate(() => __test.click('togglePlace', 1)), true, 'model 1 has its own list button');
  await ready(page, 1);
  assert.equal(await page.evaluate(() => __test.click('togglePlace', 2)), true);
  await ready(page, 2);
  await page.evaluate(() => __test.click('togglePlace', 1));
  await page.waitForFunction(() => __test.files._dbg.state().world === 1);
  assert.deepEqual(await page.evaluate(() => __test.records().map(r => r.id)), [2]);
  if (process.env.XR_QA_PNG) {
    const image = await page.evaluate(() => __test.files.group.children[1].material.map.image.toDataURL('image/png').split(',')[1]);
    await writeFile(process.env.XR_QA_PNG, Buffer.from(image, 'base64'));
  }
  const pose = await page.evaluate(() => __test.files._dbg.files()[0].pose);
  await page.evaluate(() => __test.click('togglePlace', 1));
  await ready(page, 1);
  assert.deepEqual(await page.evaluate(() => __test.files._dbg.files()[0].pose), pose, 'saved pose restored');
}));

test('XR file action buttons are not swallowed by panel drag handling', () => withPage(async page => {
  const action = await page.evaluate(() => {
    __test.open();
    const state = __test.files._dbg.state();
    const h = state.hotspots.find(h => h.act === 'togglePlace' && h.fileId === 1);
    const ray = __test.files._dbg.rayAt(h.x + h.w / 2, h.y + h.h / 2);
    const drag = __test.files.beginDrag(ray);
    __test.files.handlePick(ray);
    return { drag, view: __test.files._dbg.state().view };
  });
  assert.equal(action.drag, null, 'button click is handled immediately on selectstart');
  assert.equal(action.view, 'list', 'button click does not open the preview');
  await ready(page, 1);

  const row = await page.evaluate(() => {
    const state = __test.files._dbg.state();
    const h = state.hotspots.find(h => h.act === 'row');
    const ray = __test.files._dbg.rayAt(80, h.y + 18);
    const drag = __test.files.beginDrag(ray);
    __test.files.handlePick(ray);
    return { kind: drag && drag.kind, view: __test.files._dbg.state().view };
  });
  assert.equal(row.kind, 'xrfile-panel', 'file row still supports scroll dragging');
  assert.equal(row.view, 'preview', 'file row click opens the preview');
}));

test('initial placement survives an unrelated file revision during a slow load', () => withPage(async page => {
  await page.evaluate(() => { __test.state.delayed.add(1); __test.files._dbg.placeById(1); });
  await page.waitForFunction(() => __test.state.pending.length === 1);
  await page.evaluate(async () => { __test.remote(2, true); await __test.poll(); });
  assert.equal(await page.evaluate(() => __test.records().some(r => r.id === 1)), true, 'pending model retained');
  await page.evaluate(() => __test.release(1));
  await ready(page, 1);
  await page.waitForFunction(() => __test.state.calls.some(c => c.id === 1 && c.body.visible));
}));

test('remote visibility refreshes an open preview before its next toggle', () => withPage(async page => {
  await page.evaluate(() => __test.files._dbg.openById(1));
  await page.evaluate(async () => { __test.remote(1, true); await __test.poll(); });
  await ready(page, 1);
  await page.evaluate(() => __test.click('togglePlace'));
  await page.waitForFunction(() => __test.state.calls.length > 0);
  assert.equal(await page.evaluate(() => __test.state.calls.at(-1).body.visible), false, 'next click hides, not places again');
}));

test('a stale load cannot attach to a replacement with the same file id', () => withPage(async page => {
  await page.evaluate(async () => { __test.state.parseDelayed.add(1); __test.remote(1, true); await __test.poll(); });
  await page.waitForFunction(() => __test.state.parsed.length === 1);
  await page.evaluate(async () => { __test.remote(1, false); await __test.poll(); __test.remote(1, true); await __test.poll(); });
  await page.waitForFunction(() => __test.state.parsed.length === 2);
  await page.evaluate(() => __test.releaseParsed(1));
  await page.waitForFunction(() => __test.state.disposals >= 2);
  assert.equal(await page.evaluate(() => __test.state.disposals), 2, 'old placeholder and stale decoded model disposed');
  assert.deepEqual(await page.evaluate(() => __test.records()[0].children), ['Mesh'], 'new record still loading');
  await page.evaluate(() => __test.releaseParsed(1));
  await ready(page, 1);
}));

test('temporary preview has an explicit hide action and is removed when placed', () => withPage(async page => {
  await page.evaluate(() => __test.files._dbg.openById(1));
  await page.evaluate(() => __test.click('temp'));
  assert.equal(await page.evaluate(() => __test.state.temps.has('file:1')), true);
  assert.equal(await page.evaluate(() => __test.click('temp')), true);
  assert.equal(await page.evaluate(() => __test.state.temps.has('file:1')), false, 'second preview click hides');
  await page.evaluate(() => { __test.click('temp'); __test.click('togglePlace'); });
  await ready(page, 1);
  assert.equal(await page.evaluate(() => __test.state.temps.has('file:1')), false, 'no duplicate temporary and shared model');
}));

test('read-only viewers cannot change shared model visibility', () => withPage(async page => {
  await page.evaluate(() => { __test.state.editable = false; __test.open(); });
  assert.equal(await page.evaluate(() => __test.click('togglePlace', 1)), false);
  await page.evaluate(() => __test.files._dbg.placeById(1));
  assert.deepEqual(await page.evaluate(() => __test.state.calls), []);
}));

test('repeated placement clicks during loading submit only one placement', () => withPage(async page => {
  await page.evaluate(() => { __test.state.delayed.add(1); __test.files._dbg.openById(1); __test.click('togglePlace'); __test.click('togglePlace'); });
  await page.waitForFunction(() => __test.state.pending.length === 1);
  await page.evaluate(() => __test.release(1));
  await ready(page, 1);
  await page.waitForFunction(() => __test.state.calls.length > 0);
  assert.equal(await page.evaluate(() => __test.state.calls.filter(c => c.id === 1).length), 1);
}));

test('hiding an adjusted model persists its latest pose before unplacing it', () => withPage(async page => {
  await page.evaluate(async () => { __test.remote(1, true); await __test.poll(); });
  await ready(page, 1);
  await page.evaluate(() => { __test.files._dbg.tryAdjust(1); __test.files._dbg.nudge(.8, .2, -.4); });
  await page.evaluate(() => __test.files._dbg.unplaceById(1));
  await page.waitForFunction(() => __test.state.calls.length === 2);
  assert.deepEqual(await page.evaluate(() => __test.state.calls.map(c => c.body.visible)), [true, false]);
  assert.deepEqual(await page.evaluate(() => __test.state.files[0].world.pose.position), [1.8, .7, -2.4]);
}));

test('hiding waits for an in-flight pose save so it cannot show the model again', () => withPage(async page => {
  await page.evaluate(async () => { __test.remote(1, true); await __test.poll(); });
  await ready(page, 1);
  await page.evaluate(() => { __test.state.delayWrites.add(1); __test.files._dbg.tryAdjust(1);
    __test.files._dbg.nudge(.4, 0, .3); __test.files.exitAdjust(); });
  await page.waitForFunction(() => __test.state.pendingWrites.length === 1);
  await page.evaluate(() => __test.files._dbg.unplaceById(1));
  assert.equal(await page.evaluate(() => __test.state.calls.length), 1, 'hide not sent before pose save finishes');
  await page.evaluate(() => { __test.state.delayWrites.delete(1); __test.releaseWrite(1); });
  await page.waitForFunction(() => __test.files._dbg.state().world === 0);
  assert.deepEqual(await page.evaluate(() => __test.state.calls.map(c => c.body.visible)), [true, false]);
}));

test('a placement response revision cannot suppress an unseen remote model', () => withPage(async page => {
  await page.evaluate(() => { __test.remote(2, true); __test.files._dbg.placeById(1); });
  await ready(page, 1);
  await page.waitForFunction(() => __test.state.calls.length > 0);
  await page.evaluate(() => __test.poll());
  await ready(page, 2);
  assert.equal(await page.evaluate(() => __test.files._dbg.state().world), 2);
}));

test('a stale list response cannot roll back a completed local placement', () => withPage(async page => {
  await page.evaluate(() => { __test.state.delayGets = true; __test.poll(); __test.files._dbg.placeById(1); });
  await ready(page, 1);
  await page.waitForFunction(() => __test.state.calls.length > 0);
  await page.evaluate(() => { __test.state.delayGets = false; __test.state.pendingGets.shift()(); });
  await page.waitForTimeout(50);
  assert.equal(await page.evaluate(() => __test.files._dbg.files()[0].visible), true);
  assert.equal(await page.evaluate(() => __test.files._dbg.state().world), 1);
}));

test('failed placement leaves the UI retryable and preserves other files undo entries', () => withPage(async page => {
  await page.evaluate(() => { __test.state.delayWrites.add(1); __test.state.rejectWrites.add(1); __test.files._dbg.placeById(1); });
  await page.waitForFunction(() => __test.state.pendingWrites.length === 1);
  await page.evaluate(() => __test.files._dbg.placeById(2));
  await ready(page, 2);
  await page.evaluate(() => __test.releaseWrite(1));
  await page.waitForFunction(() => __test.files._dbg.state().world === 1);
  assert.equal(await page.evaluate(() => __test.files._dbg.undoState().top.name), 'model-2.gltf');
  await page.evaluate(() => { __test.state.delayWrites.delete(1); __test.state.rejectWrites.delete(1); __test.files._dbg.placeById(1); });
  await ready(page, 1);
}));

test('full XR temporary previews do not duplicate after rapid off/on toggles', () => withPage(async page => {
  await page.evaluate(() => { __test.state.parseDelayed.add(1); __test.files._dbg.openById(1); __test.click('temp'); });
  await page.waitForFunction(() => __test.state.parsed.length === 1);
  await page.evaluate(() => { __test.click('temp'); __test.click('temp'); });
  await page.waitForFunction(() => __test.state.parsed.length === 2);
  await page.evaluate(() => __test.releaseParsed(1));
  await page.evaluate(() => __test.releaseParsed(1));
  await page.waitForFunction(() => __test.tempModels().length === 1);
  await page.evaluate(() => __test.click('temp'));
  assert.equal(await page.evaluate(() => __test.tempModels().length), 0);
}, true));

test('full XR mouse clicks on a list toggle show and hide without opening the preview', () => withPage(async page => {
  await page.evaluate(() => __test.open());
  const point = await page.evaluate(() => __test.screenAt('togglePlace', 1));
  await page.mouse.click(point.x, point.y);
  await ready(page, 1);
  assert.equal(await page.evaluate(() => __test.files._dbg.state().view), 'list');
  await page.mouse.click(point.x, point.y);
  await page.waitForFunction(() => __test.files._dbg.state().world === 0);
  assert.equal(await page.evaluate(() => __test.files._dbg.state().view), 'list');
}, true));

test('full XR world console renders the migrated top and composer controls', () => withPage(async page => {
  await page.evaluate(() => __xrDebug.worldUiSetVisible(true));
  const ui = await page.evaluate(() => __xrDebug.worldUi());
  assert.equal(ui.visible, true);
  assert.equal(ui.top.length, 3);
  assert.equal(ui.send.length, 3);
  if (process.env.XR_WORLD_UI_PNG) await page.screenshot({ path: process.env.XR_WORLD_UI_PNG });
}, true));

test('full XR world console buttons drive the same follow/native handlers as the DOM HUD', () => withPage(async page => {
  await page.evaluate(() => __xrDebug.worldUiSetVisible(true));
  const before = await page.evaluate(() => ({ follow: __xrDebug.scroll().follow, native: __xrDebug.nativeOn() }));
  assert.equal(await page.evaluate(() => __xrDebug.worldUiClick('follow')), true);
  assert.equal(await page.evaluate(() => __xrDebug.scroll().follow), !before.follow);
  assert.equal(await page.evaluate(() => __xrDebug.worldUiClick('native')), true);
  assert.equal(await page.evaluate(() => __xrDebug.nativeOn()), !before.native);
}, true));

test('full XR world console exposes files, input, microphone and send hotspots', () => withPage(async page => {
  await page.evaluate(() => __xrDebug.worldUiSetVisible(true));
  const ui = await page.evaluate(() => __xrDebug.worldUi());
  assert.deepEqual(ui.topHotspots, ['exit', 'follow', 'native', 'files']);
  assert.deepEqual(ui.sendHotspots, ['input', 'mic', 'send']);

  assert.equal(await page.evaluate(() => __xrDebug.worldUiClick('files')), true);
  assert.equal(await page.evaluate(() => __test.files._dbg.state().open), true);
  await page.evaluate(() => __test.files.closePanel());

  await page.evaluate(() => {
    const input = document.querySelector('.xr-send-input');
    input.value = 'hello from world console';
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  assert.equal(await page.evaluate(() => __xrDebug.worldUiClick('input')), true);
  assert.equal(await page.evaluate(() => __xrDebug.worldUi().inputFocused), true);
  assert.equal(await page.evaluate(() => __xrDebug.worldUiClick('send')), true);
  await page.waitForFunction(() => __test.state.sentTexts.includes('hello from world console'));
}, true));

test('full XR closing a preview while loading cancels the temporary model', () => withPage(async page => {
  await page.evaluate(() => { __test.state.parseDelayed.add(1); __test.files._dbg.openById(1); __test.click('temp'); });
  await page.waitForFunction(() => __test.state.parsed.length === 1);
  await page.evaluate(() => { __test.files.closePanel(); __test.releaseParsed(1); });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(() => __test.tempModels().length), 0);
}, true));

test('full XR exiting while a model is loading does not add it back to the disposed scene', () => withPage(async page => {
  await page.evaluate(() => { __test.state.parseDelayed.add(1); __test.files._dbg.openById(1); __test.click('temp'); });
  await page.waitForFunction(() => __test.state.parsed.length === 1);
  await page.evaluate(() => { __test.dispose(); __test.releaseParsed(1); });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(() => __test.tempModels().length), 0);
}, true));

test('loading failure can be retried without leaving a placeholder', () => withPage(async page => {
  await page.evaluate(() => { __test.state.fail.add(1); __test.files._dbg.placeById(1); });
  await page.waitForFunction(() => __test.files._dbg.state().world === 0);
  await page.evaluate(() => { __test.state.fail.delete(1); __test.files._dbg.placeById(1); });
  await ready(page, 1);
}));

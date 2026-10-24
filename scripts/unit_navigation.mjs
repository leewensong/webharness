/* Navigation regression in an isolated browser with synthetic API responses.
   WEBHARNESS_UI_HTML can select the production-based staged HTML for deployment QA. */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const root = resolve(import.meta.dirname, '..');
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || '/opt/homebrew/lib/node_modules/playwright/index.mjs').href);
let browser, server, origin;
before(async () => {
  server = createServer(async (req, res) => {
    try {
      const pathname = new URL(req.url, 'http://localhost').pathname;
      if (pathname.startsWith('/api/')) { res.setHeader('Content-Type', 'application/json'); res.end('{}'); return; }
      const path = pathname === '/' ? (process.env.WEBHARNESS_UI_HTML || resolve(root, 'static/index.html')) : resolve(root, '.' + pathname);
      if (pathname !== '/' && !path.startsWith(resolve(root, 'static') + '/')) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', pathname === '/' ? 'text/html' : 'text/javascript');
      res.end(await readFile(path));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await new Promise(done => server?.close(done)); });
async function pageFor(fn, viewport = { width: 1280, height: 800 }, timezoneId = 'UTC') {
  const page = await browser.newPage({ viewport, timezoneId });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin);
    await page.evaluate(() => {
      window.__calls = [];
      api = async (path, options) => {
        __calls.push(path);
        if (path === '/api/me') return { username: 'synthetic-user', kind: 'human', avatarUrl: '' };
        if (path === '/api/agents') return { agents: [{ username: 'synthetic-agent', status: 'active' }] };
        return { members: [], bans: [], rules: [], scenes: [], templates: [] };
      };
      store.token = 'synthetic-token'; store.username = 'synthetic-user';
      document.getElementById('auth').classList.add('hidden');
      document.getElementById('app').classList.remove('hidden');
      applyRoom({ roomId: 1, roomName: 'synthetic-room', visibility: 'public', canManage: false });
    });
    await fn(page);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
}
test('members see room settings, not governance, and can open voice and agent controls', () => pageFor(async page => {
  assert.equal(await page.locator('#manageBtn').isVisible(), true);
  assert.equal(await page.locator('.chat-top #voiceBtn, .chat-top #bringAgentBtn, .chat-top #leaveRoomBtn').count(), 0);
  assert.equal(await page.locator('.chat-top #filesBtn').isVisible(), true);
  await page.click('#manageBtn');
  assert.equal(await page.locator('#ownerSettings').isVisible(), false);
  assert.equal(await page.locator('#saveRoomBtn').isVisible(), false);
  assert.equal(await page.locator('#archiveBtn').isVisible(), false);
  assert.equal(await page.locator('#bringAgentBtn').isVisible(), true);
  await page.click('#voiceBtn');
  assert.equal(await page.locator('#voiceDlg').isVisible(), true);
  await page.evaluate(() => document.getElementById('voiceDlg').close());
  await page.click('#bringAgentBtn');
  await page.waitForFunction(() => __calls.includes('/api/agents'));
  assert.equal(await page.locator('#settings').isVisible(), false);
  assert.equal(await page.getByText('synthetic-agent', { exact: true }).isVisible(), true);
  assert.equal(await page.evaluate(() => __calls.some(path => /\/members|\/bans|\/whisper-rules/.test(path))), false);
}));
test('personal tools have a single entry and preserve superadmin visibility', () => pageFor(async page => {
  await page.evaluate(() => openProfile());
  assert.equal(await page.locator('#profileTools a[href="/guide"]').isVisible(), true);
  assert.equal(await page.locator('#adminBtn').isVisible(), false);
  await page.evaluate(() => setSuperadmin(true));
  assert.equal(await page.locator('#adminBtn').isVisible(), true);
  await page.click('#profileTools button[data-i18n="suggest"]');
  assert.equal(await page.locator('#suggest').isVisible(), true);
  const duplicates = await page.evaluate(() => {
    const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
    return ids.filter((id, i) => ids.indexOf(id) !== i);
  });
  assert.deepEqual(duplicates, []);
}));
test('mobile room settings fit the viewport; archive hides active actions', () => pageFor(async page => {
  await page.click('#manageBtn');
  const box = await page.locator('#settings').boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= 391);
  assert.equal(await page.locator('#voiceBtn').isVisible(), true);
  assert.equal(await page.locator('#leaveRoomBtn').isVisible(), true);
  if (process.env.NAV_QA_PNG) await page.screenshot({ path: process.env.NAV_QA_PNG });
  await page.evaluate(() => {
    document.getElementById('settings').close();
    applyRoom({ roomId: 1, roomName: 'synthetic-room', readOnly: true, canManage: false });
  });
  assert.equal(await page.locator('#manageBtn').isVisible(), false);
}, { width: 390, height: 844 }));

for (const [zone, expected] of [['Asia/Shanghai', '20:40:50'], ['UTC', '12:40:50'], ['America/Los_Angeles', '05:40:50']]) {
  test('chat timestamps use browser timezone ' + zone, () => pageFor(async page => {
    const result = await page.evaluate(() => ({
      utc: messageTime('2026-10-06 12:40:50'),
      offset: messageTime('2026-10-06T20:40:50+08:00'),
      invalid: messageTime('invalid timestamp'),
    }));
    assert.ok(result.utc.includes(expected), result.utc);
    assert.equal(result.utc, result.offset, 'existing offsets must not get another Z suffix');
    assert.equal(result.invalid, 'invalid timestamp');
    await page.evaluate(() => buildBubble({ id: 1, username: 'alice', content: 'synthetic message', createdAt: '2026-10-06 12:40:50' }));
    assert.ok((await page.locator('#log .meta').textContent()).includes(expected));
  }, { width: 1280, height: 800 }, zone));
}

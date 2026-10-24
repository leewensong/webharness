/* 消息时间显示（个人设置「时间显示」选项）回归：UTC/Asia 两种浏览器时区下的渲染与持久化。
   沿用 unit_navigation.mjs 的合成 API 假服务器模式，不需要后端与登录。 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const root = resolve(import.meta.dirname, '..');
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || '/opt/homebrew/lib/node_modules/playwright/index.mjs').href);
const SAMPLE = '2026-10-06 12:40:53';   // 服务器存的 UTC 时刻

let browser, server, origin;
before(async () => {
  server = createServer(async (req, res) => {
    try {
      const pathname = new URL(req.url, 'http://localhost').pathname;
      if (pathname.startsWith('/api/')) { res.setHeader('Content-Type', 'application/json'); res.end('{}'); return; }
      const path = pathname === '/' ? resolve(root, 'static/index.html') : resolve(root, '.' + pathname);
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

async function pageFor(fn, timezoneId = 'UTC') {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, timezoneId });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin);
    await page.evaluate(() => {
      api = async () => ({});
      store.token = 'synthetic-token'; store.username = 'synthetic-user';
      document.getElementById('auth').classList.add('hidden');
      document.getElementById('app').classList.remove('hidden');
    });
    await fn(page);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
}

test('messageTime 默认跟随浏览器时区并带时区名', () => pageFor(async page => {
  const out = await page.evaluate((s) => messageTime(s), SAMPLE);
  assert.match(out, /(GMT|UTC)/);          // 带时区名
  assert.match(out, /12:40:53/);       // UTC 浏览器 → UTC 时刻
  assert.ok(!/20:40/.test(out));
}, 'UTC'));

test('个人设置切换「固定北京时间」后消息时间用 GMT+8', () => pageFor(async page => {
  // 缺省跟随浏览器
  assert.match(await page.evaluate((s) => messageTime(s), SAMPLE), /(GMT|UTC)/);
  // 选择固定北京时间
  await page.evaluate(() => { document.getElementById('timeZoneMode').value = 'asia'; saveTimezoneMode(); });
  assert.equal(await page.evaluate(() => localStorage.getItem('timeZoneMode')), 'asia');
  const out = await page.evaluate((s) => messageTime(s), SAMPLE);
  assert.match(out, /20:40:53/);       // 12:40 UTC = 20:40 北京
  assert.match(out, /GMT\+8/);
}, 'UTC'));

test('北京时区浏览器缺省即北京显示，且选项状态正确回显', () => pageFor(async page => {
  await page.evaluate(() => openProfile());
  assert.equal(await page.locator('#timeZoneMode').inputValue(), 'browser');
  assert.match(await page.evaluate((s) => messageTime(s), SAMPLE), /20:40:53/);
  await page.evaluate(() => { document.getElementById('timeZoneMode').value = 'asia'; saveTimezoneMode(); });
  assert.equal(await page.evaluate(() => loadTimezoneMode()), 'asia');
}, 'Asia/Shanghai'));
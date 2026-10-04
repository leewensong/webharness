/* Real-browser regression tests for remote Avatar keyframe playback.
   Run: PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" node --test scripts/unit_xr_animation.mjs */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : 'playwright');
let browser;
let server;
let origin;

const harness = `<!doctype html><meta charset="utf-8">
<script type="importmap">{"imports":{"three":"/static/vendor/three/three.module.js","three/addons/":"/static/vendor/three/addons/","@pixiv/three-vrm":"/static/vendor/three-vrm/three-vrm.module.min.js"}}</script>
<script type="module">
import { createAvatarSystem } from '/static/xr/xr-avatars.js';
const avatars = createAvatarSystem({
  t: key => key,
  username: () => 'viewer',
  token: () => 'test-token',
});
avatars.applyRoom({ onlineUsers: [
  { username: 'animator', kind: 'agent', model3dUrl: 'builtin:rabbit', model3dHumanoid: true },
  { username: 'human', kind: 'human', model3dUrl: 'builtin:rabbit', model3dHumanoid: true },
] });
window.__test = { avatars };
</script>`;

before(async () => {
  server = createServer(async (req, res) => {
    try {
      const pathname = new URL(req.url, 'http://localhost').pathname;
      if (pathname === '/') {
        res.setHeader('Content-Type', 'text/html');
        res.end(harness);
        return;
      }
      const path = resolve(root, '.' + pathname);
      if (!path.startsWith(join(root, 'static') + '/')) {
        res.writeHead(404).end();
        return;
      }
      res.setHeader('Content-Type', 'text/javascript');
      res.end(await readFile(path));
    } catch {
      res.writeHead(404).end();
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
});

after(async () => {
  await browser?.close();
  await new Promise(resolve => server?.close(resolve));
});

test('remote animation interpolates, appends, replaces and stops', async () => {
  const page = await browser.newPage();
  const errors = [];
  const logs = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => logs.push(message.type() + ': ' + message.text()));
  page.on('requestfailed', request => logs.push('requestfailed: ' + request.url() + ' ' + request.failure()?.errorText));
  try {
    page.setDefaultTimeout(15000);
    await page.goto(origin + '/');
    try {
      await page.waitForFunction(() => !!window.__test?.avatars.debugBone('animator', 'rightUpperArm'));
    } catch (error) {
      if (errors.length) error.message += '\nBrowser errors: ' + errors.join('\n');
      if (logs.length) error.message += '\nBrowser logs: ' + logs.join('\n');
      throw error;
    }

    const q90z = [0, 0, Math.SQRT1_2, Math.SQRT1_2];
    const q0 = [0, 0, 0, 1];
    assert.equal(await page.evaluate((q90z) => __test.avatars.setAnimationCommand('animator', {
      action: 'replace', id: 'wave', keyframes: [
        { t: 0, bones: { rightUpperArm: [0, 0, 0, 1] } },
        { t: 1, bones: { rightUpperArm: q90z } },
      ],
    }), q90z), true);

    await page.evaluate(() => __test.avatars.update(0.5));
    const halfway = await page.evaluate(() => __test.avatars.debugBone('animator', 'rightUpperArm').q);
    assert.ok(Math.abs(halfway[2] - Math.sin(Math.PI / 8)) < 0.03, `unexpected halfway z: ${halfway}`);
    assert.ok(Math.abs(halfway[3] - Math.cos(Math.PI / 8)) < 0.03, `unexpected halfway w: ${halfway}`);

    assert.equal(await page.evaluate(() => __test.avatars.setAnimationCommand('animator', {
      action: 'append', id: 'settle', keyframes: [
        { t: 0, bones: { rightUpperArm: [0, 0, Math.SQRT1_2, Math.SQRT1_2] } },
        { t: 1, bones: { rightUpperArm: [0, 0, 0, 1] } },
      ],
    })), true);
    assert.deepEqual(await page.evaluate(() => __test.avatars.debugAnimation('animator')), {
      current: 'wave', time: 0.5, queued: 1, bones: ['rightUpperArm'],
    });

    await page.evaluate(() => __test.avatars.update(0.6));
    assert.equal(await page.evaluate(() => __test.avatars.debugAnimation('animator').current), 'settle');

    assert.equal(await page.evaluate((q0) => __test.avatars.setAnimationCommand('animator', {
      action: 'replace', id: 'newest', keyframes: [
        { t: 0, bones: { rightUpperArm: q0 } },
        { t: 0.2, bones: { rightUpperArm: q0 } },
      ],
    }), q0), true);
    assert.deepEqual(await page.evaluate(() => __test.avatars.debugAnimation('animator')), {
      current: 'newest', time: 0, queued: 0, bones: ['rightUpperArm'],
    });

    assert.equal(await page.evaluate(() => __test.avatars.setAnimationCommand('animator', { action: 'stop' })), true);
    assert.deepEqual(await page.evaluate(() => __test.avatars.debugAnimation('animator')), {
      current: null, time: 0, queued: 0, bones: [],
    });
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});

test('human and Agent yaw conventions stay distinct', async () => {
  const page = await browser.newPage();
  try {
    page.setDefaultTimeout(10000);
    await page.goto(origin + '/');
    const result = await page.evaluate(() => {
      __test.avatars.setRemotePose('animator', { p: [0, 1.6, 0], yaw: 0 });
      __test.avatars.setRemotePose('human', { p: [1, 1.6, 0], yaw: 0 });
      return {
        agent: __test.avatars.debugAvatarPose('animator'),
        human: __test.avatars.debugAvatarPose('human'),
      };
    });
    assert.equal(result.agent.isAgent, true);
    assert.ok(Math.abs(result.agent.yaw) < 1e-6);
    assert.equal(result.human.isAgent, false);
    assert.ok(Math.abs(result.human.yaw - Math.PI) < 1e-6);
  } finally {
    await page.close();
  }
});

test('human hand targets activate the avatar arm IK chain', async () => {
  const page = await browser.newPage();
  try {
    page.setDefaultTimeout(15000);
    await page.goto(origin + '/');
    await page.waitForFunction(() => !!window.__test?.avatars.debugBone('human', 'rightUpperArm'));
    await page.evaluate(() => {
      __test.avatars.setRemotePose('human', {
        p: [0, 1.6, 0], yaw: 0, pitch: 0.3,
        hands: [{ handedness: 'right', p: [0.65, 1.25, -0.35], q: [0, 0, 0, 1] }],
      });
      __test.avatars.update(1 / 60);
    });
    const ik = await page.evaluate(() => __test.avatars.debugAvatarIK('human'));
    assert.equal(ik.right.active, true);
    assert.deepEqual(new Set(ik.bones), new Set(['rightUpperArm', 'rightLowerArm', 'rightHand']));
    assert.ok(ik.head && Math.abs(ik.head.target - 0.3) < 1e-6);
  } finally {
    await page.close();
  }
});

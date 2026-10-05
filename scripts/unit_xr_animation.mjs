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

// Exercise the VRM 1.0 retargeting path too. Reuse rabbit's real mesh/bind pose,
// changing only the humanoid declaration; no network/downloaded fixture needed.
async function vrm1Fixture() {
  const input = await readFile(join(root, 'static/avatars/rabbit.vrm'));
  const jsonSize = input.readUInt32LE(12);
  const json = JSON.parse(input.subarray(20, 20 + jsonSize).toString());
  const bones = Object.fromEntries(json.extensions.VRM.humanoid.humanBones.map(b => [b.bone, {node:b.node}]));
  delete json.extensions.VRM;
  json.extensions.VRMC_vrm = {specVersion:'1.0',humanoid:{humanBones:bones},
    meta:{name:'VRM1 test rabbit',authors:['test'],licenseUrl:'https://vrm.dev/licenses/1.0/'}};
  json.extensionsUsed = json.extensionsUsed.filter(e => e !== 'VRM').concat('VRMC_vrm');
  const bytes = Buffer.from(JSON.stringify(json));
  const padded = Buffer.alloc(Math.ceil(bytes.length / 4) * 4, 32); bytes.copy(padded);
  const chunk = Buffer.alloc(8);chunk.writeUInt32LE(padded.length);chunk.writeUInt32LE(0x4e4f534a,4);
  const output=Buffer.concat([input.subarray(0,12),chunk,padded,input.subarray(20+jsonSize)]);
  output.writeUInt32LE(output.length,8);return output;
}

const harness = `<!doctype html><meta charset="utf-8">
<script type="importmap">{"imports":{"three":"/static/vendor/three/three.module.js","three/addons/":"/static/vendor/three/addons/","@pixiv/three-vrm":"/static/vendor/three-vrm/three-vrm.module.min.js"}}</script>
<script type="module">
import { createAvatarSystem } from '/static/xr/xr-avatars.js';
import * as THREE from 'three';
import * as handPose from '/static/xr/xr-hand-pose.js';
const avatars = createAvatarSystem({
  t: key => key,
  username: () => 'viewer',
  token: () => 'test-token',
});
avatars.applyRoom({ onlineUsers: [
  { username: 'animator', kind: 'agent', model3dUrl: 'builtin:rabbit', model3dHumanoid: true },
  { username: 'human', kind: 'human', model3dUrl: 'builtin:' + (new URLSearchParams(location.search).get('model') || 'rabbit'), model3dHumanoid: true },
] });
window.__test = { avatars, THREE, handPose };
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
      if (pathname === '/static/avatars/test-vrm1.vrm') {
        res.setHeader('Content-Type','model/gltf-binary'); res.end(await vrm1Fixture()); return;
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
    const target = await page.evaluate(() => {
      __test.avatars.setRemotePose('human', { p: [0, 1.6, 0], yaw: 0, pitch: 0.3 });
      __test.avatars.update(1 / 60);
      const dbg = __test.avatars.debugAvatarIK('human');
      const base = dbg.right.hand;
      const offset = (dbg.calibration.headHeight || 0) - 1.6;
      return [base[0] + 0.08, base[1] + 0.02 - offset, base[2] - 0.08];
    });
    await page.evaluate((target) => {
      __test.avatars.setRemotePose('human', {
        p: [0, 1.6, 0], yaw: 0, pitch: 0.3,
        hands: [{ handedness: 'right', p: target, q: [0, 0, 0, 1] }],
      });
      for (let i = 0; i < 20; i++) __test.avatars.update(1 / 60);
    }, target);
    const ik = await page.evaluate(() => __test.avatars.debugAvatarIK('human'));
    assert.equal(ik.right.active, true);
    assert.deepEqual(new Set(ik.bones), new Set(['rightUpperArm', 'rightLowerArm', 'rightHand']));
    assert.ok(ik.right.error < 0.12, `IK target error too large: ${ik.right.error}`);
    assert.ok(ik.head && Math.abs(ik.head.target - 0.3) < 1e-6);
  } finally {
    await page.close();
  }
});

for (const model of ['rabbit', 'witch', 'astrodisco', 'polybot', 'test-vrm1']) {
  test(`${model}: actual skinned hand follows palm-v1 axes, not bone axes`, async () => {
    const page = await browser.newPage();
    try {
      await page.goto(origin + '/?model=' + model);
      await page.waitForFunction(() => !!window.__test?.avatars.debugAvatarIK('human')?.right);
      const trials = await page.evaluate(() => {
        const { avatars: a, THREE: T } = __test;
        const result = [];
        // Flat/down, palm up, fingers up, yaw+roll, plus equivalent q/-q.
        const rotations = [new T.Quaternion(), new T.Quaternion().setFromAxisAngle(new T.Vector3(0,0,1),Math.PI),
          new T.Quaternion().setFromEuler(new T.Euler(Math.PI/2,0,0)),
          new T.Quaternion().setFromEuler(new T.Euler(.2,1.1,-.7)),
          new T.Quaternion(0,0,0,-1)];
        for (const yaw of [0, 1.3, -2.4]) {
          for (const q of rotations) {
            a.setRemotePose('human', {p:[2,1.8,-1],yaw,hands:[]});
            for(let i=0;i<90;i++) a.update(1/60);
            const dbg = a.debugAvatarIK('human');
            const dy = dbg.calibration.poseHeadY - dbg.calibration.headHeight;
            a.setRemotePose('human', {p:[2,1.8,-1],yaw,
              state:{handOrientation:'palm-v1'}, hands:['left','right'].map(side=>({
                handedness:side,p:[dbg[side].hand[0],dbg[side].hand[1]+dy,dbg[side].hand[2]],q:q.toArray(),
              }))});
            for(let i=0;i<90;i++) a.update(1/60);
            const now=a.debugAvatarIK('human');
            const fingers=new T.Vector3(0,0,-1).applyQuaternion(q), palm=new T.Vector3(0,-1,0).applyQuaternion(q);
            for (const side of ['left','right']) {
              const raw=now[side].rawAxes;
              result.push({side,yaw,fingerDot:new T.Vector3().fromArray(raw.fingers).dot(fingers),
                palmDot:new T.Vector3().fromArray(raw.palm).dot(palm)});
            }
          }
        }
        return result;
      });
      for (const trial of trials) {
        assert.ok(trial.fingerDot > .99 && trial.palmDot > .99,
          `${model} mesh mismatch: ${JSON.stringify(trial)}`);
      }
    } finally { await page.close(); }
  });
}

test('WebXR grip axes differ for left/right; wrists are already palm-v1', async () => {
  const page = await browser.newPage();
  try {
    await page.goto(origin + '/');
    const trials = await page.evaluate(() => {
      const { THREE:T,handPose:H }=__test;
      const result=[];
      for(const side of ['left','right']) {
        for(const world of [new T.Quaternion(),new T.Quaternion().setFromEuler(new T.Euler(.7,-1.2,.3))]) {
          const palm=H.xrOrientationToPalm(world,'grip',side);
          const forward=new T.Vector3(0,0,-1).applyQuaternion(palm);
          const normal=new T.Vector3(0,-1,0).applyQuaternion(palm);
          result.push({side,space:'grip',f:forward.dot(new T.Vector3(0,-1,0).applyQuaternion(world)),
            p:normal.dot(new T.Vector3(side==='left'?1:-1,0,0).applyQuaternion(world))});
          result.push({side,space:'wrist',dot:Math.abs(H.xrOrientationToPalm(world,'wrist',side).dot(world))});
        }
      }
      return result;
    });
    for(const t of trials) {
      if(t.space==='grip') assert.ok(t.f>.99999&&t.p>.99999,JSON.stringify(t));
      else assert.ok(t.dot>.99999,JSON.stringify(t));
    }
  } finally {await page.close();}
});

test('XR collection uses grip/wrist, includes rig transforms, and drops lost hands', async () => {
  const page=await browser.newPage();
  try {
    await page.goto(origin+'/');
    const r=await page.evaluate(()=>{
      const {THREE:T,handPose:H}=__test;
      const rig=new T.Group(); rig.position.set(3,0,-2); rig.rotation.y=1.2;
      const c=[new T.Group(),new T.Group()],g=[new T.Group(),new T.Group()],h=[new T.Group(),new T.Group()];
      c.forEach((node,i)=>{rig.add(node,g[i],h[i]);node.userData.inputSource={handedness:i?'right':'left'};});
      g[0].position.set(-.3,1.2,-.4);g[1].position.set(.3,1.2,-.4);
      const grip=H.readXRHands(c,g,h);
      const wrist=new T.Group(); wrist.position.set(.3,1.2,-.4);h[1].add(wrist);h[1].joints={wrist};
      c[1].userData.inputSource.hand={};
      const tracked=H.readXRHands(c,g,h);
      wrist.visible=false;
      const lost=H.readXRHands(c,g,h);
      c[0].userData.inputSource=null;
      const disconnected=H.readXRHands(c,g,h);
      return {grip,tracked,lost,disconnected,expected:g[0].getWorldPosition(new T.Vector3()).toArray(),
        expectedWristQ:wrist.getWorldQuaternion(new T.Quaternion()).toArray()};
    });
    assert.deepEqual(r.grip[0].p,r.expected);
    assert.deepEqual(r.tracked[1].q,r.expectedWristQ);
    assert.deepEqual(r.lost.map(h=>h.handedness),['left']);
    assert.equal(r.disconnected.length,0);
  } finally {await page.close();}
});

test('legacy grip, canonical palm and tracking-mode switch render the same orientation', async () => {
  const page=await browser.newPage();
  try {
    await page.goto(origin+'/?model=witch');
    await page.waitForFunction(()=>!!window.__test?.avatars.debugAvatarIK('human')?.right);
    const samples=await page.evaluate(()=>{
      const {avatars:a,THREE:T,handPose:H}=__test;
      const desired=new T.Quaternion().setFromEuler(new T.Euler(.4,-.8,.2));
      const out=[];
      a.setRemotePose('human',{p:[0,1.8,0],yaw:.7,hands:[]});a.update(1/60);
      const base=a.debugAvatarIK('human'); const dy=1.8-base.calibration.headHeight;
      for(const canonical of [false,true,false]) {
        a.setRemotePose('human',{p:[0,1.8,0],yaw:.7,
          state:canonical?{handOrientation:'palm-v1'}:null,
          hands:['left','right'].map(side=>({handedness:side,
            p:[base[side].hand[0],base[side].hand[1]+dy,base[side].hand[2]],
            q:(canonical?desired.clone():desired.clone().multiply(
              H.xrOrientationToPalm(new T.Quaternion(),'grip',side).invert())).toArray(),
          }))});
        for(let i=0;i<90;i++)a.update(1/60);
        const current=a.debugAvatarIK('human');
        for(const side of ['left','right'])out.push(current[side].rawAxes);
      }
      return {out,expected:{fingers:new T.Vector3(0,0,-1).applyQuaternion(desired).toArray(),
        palm:new T.Vector3(0,-1,0).applyQuaternion(desired).toArray()}};
    });
    for(const axes of samples.out)for(const axis of ['fingers','palm']) {
      const dot=axes[axis].reduce((sum,x,i)=>sum+x*samples.expected[axis][i],0);
      assert.ok(dot>.99,`switched ${axis} mismatch: ${dot}`);
    }
  } finally {await page.close();}
});

test('invalid tracking quaternions do not produce a palm rotation',async()=>{
  const page=await browser.newPage();
  try {
    await page.goto(origin+'/');
    const invalid=await page.evaluate(()=>{
      const {THREE:T,handPose:H}=__test;
      return [new T.Quaternion(0,0,0,0),new T.Quaternion(NaN,0,0,1),
        new T.Quaternion(Infinity,0,0,1)].map(q=>H.xrOrientationToPalm(q,'wrist','left'));
    });
    assert.deepEqual(invalid,[null,null,null]);
  }finally {await page.close();}
});

/* WebXR 3D 渲染端主模块：场景生命周期、弧形消息墙（面板模式管线）、桌面第一人称预览。
   架构不变量：3D 只订阅 2D 端的 msgEvents/roomEvents（单一数据流），不建第二套轮询；
   历史回填是沿墙向前的一次性 beforeId 分页拉取（需求 2.7）。退出时资源全量 dispose，
   任何 3D 故障不得影响 2D 正常聊天（需求 1.5、7.4）。 */

import * as THREE from "three";
import { mergeXRI18n } from "./xr-i18n.js";
import { createPanelSystem } from "./xr-panels.js";

const R = 6;             // 消息墙半径（米）
const PITCH = 1.26;      // 面板弧距（米）
const PANEL_W = 1.12;    // 面板世界宽（米）
const ANCHOR = Math.PI;  // 最新消息方位角（相机默认在 +Z 侧面向 −Z 看墙正面）
const WIN_NEW = 1.05;    // 新侧可视角（弧度）——窗口保持在前向弧段，越过 2π 的面板会被投影翻转
const WIN_OLD = 1.7;     // 旧侧可视角（弧度）
const FLOOR_Y = 0.42;    // 面板底边离地高度
const EYE_Y = 1.55;
const MAX_RADIUS = 5.1;  // 相机水平活动半径（离墙 0.9m）

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function createXR(ctx) {
  if (!ctx.root) throw new Error("xrRoot missing");
  if (ctx.I18N) mergeXRI18n(ctx.I18N);
  const t = ctx.t || ((k) => k);
  const tf = ctx.tf || ((k, v) => t(k));

  if (!document.createElement("canvas").getContext("webgl2")) {
    throw new Error(t("xrFallback"));
  }

  let disposed = false;

  /* ---------- 渲染器与场景 ---------- */

  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.domElement.style.cssText = "position:absolute;inset:0;display:block;";
  ctx.root.classList.remove("hidden");
  ctx.root.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 0.05, 200);
  camera.rotation.order = "YXZ";
  camera.position.set(0, EYE_Y, 2.6);

  /* 渐变天空（安静展厅，需求 2.x 视觉基调） */
  const skyMat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      top: { value: new THREE.Color(0x0b1226) },
      horizon: { value: new THREE.Color(0x233457) },
      bottom: { value: new THREE.Color(0x0a0e16) },
    },
    vertexShader: "varying vec3 vDir; void main(){ vDir = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: `varying vec3 vDir; uniform vec3 top; uniform vec3 horizon; uniform vec3 bottom;
      void main(){
        float h = vDir.y;
        vec3 c = h >= 0.0 ? mix(horizon, top, pow(min(h * 1.4, 1.0), 0.75)) : mix(horizon, bottom, min(-h * 2.2, 1.0));
        gl_FragColor = vec4(c, 1.0);
      }`,
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(70, 32, 16), skyMat);
  scene.add(sky);

  const hemi = new THREE.HemisphereLight(0x93a9d4, 0x1f2733, 1.0);
  scene.add(hemi);
  const dirLight = new THREE.DirectionalLight(0xbccbe8, 0.35);
  dirLight.position.set(3, 8, 2);
  scene.add(dirLight);

  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(18, 64),
    new THREE.MeshBasicMaterial({ color: 0x0d1420 })
  );
  ground.rotation.x = -Math.PI / 2;
  scene.add(ground);
  const grid = new THREE.GridHelper(36, 36, 0x24344f, 0x131b2a);
  grid.position.y = 0.02;
  scene.add(grid);
  const centerRing = new THREE.Mesh(
    new THREE.RingGeometry(0.5, 0.56, 48),
    new THREE.MeshBasicMaterial({ color: 0x2e4a78, side: THREE.DoubleSide })
  );
  centerRing.rotation.x = -Math.PI / 2;
  centerRing.position.y = 0.03;
  scene.add(centerRing);

  /* ---------- 面板系统 ---------- */

  const panels = createPanelSystem({
    t,
    panelWidth: PANEL_W,
    minH: 0.42,
    maxH: 2.35,
    maxTextures: 24,
  });
  scene.add(panels.group);

  /* ---------- HUD（xrRoot 内 HTML 覆盖层） ---------- */

  const hudStyle = document.createElement("style");
  hudStyle.textContent = `
    #xrRoot .xr-hud { position: absolute; inset: 0; pointer-events: none; z-index: 2;
      font-family: "SF Pro Text", "PingFang SC", "Noto Sans SC", sans-serif; color: #dfe8f4; }
    #xrRoot .xr-top { position: absolute; top: 14px; left: 16px; display: flex; gap: 10px; align-items: center; pointer-events: auto; }
    #xrRoot .xr-btn { pointer-events: auto; border: 1px solid #2c3d58; background: rgba(16, 22, 34, 0.82);
      color: #dfe8f4; font-size: 13px; padding: 8px 14px; border-radius: 999px; cursor: pointer; }
    #xrRoot .xr-btn:hover { border-color: #5b8cff; }
    #xrRoot .xr-title { color: #8fa3bd; font-size: 13px; }
    #xrRoot .xr-hint { position: absolute; top: 18px; right: 18px; color: #7c90aa; font-size: 12px; max-width: 46vw; text-align: right; }
    #xrRoot .xr-status { position: absolute; bottom: 70px; left: 50%; transform: translateX(-50%); color: #8fa3bd; font-size: 13px; }
    #xrRoot .xr-slider { position: absolute; bottom: 28px; left: 50%; transform: translateX(-50%);
      width: min(560px, 70vw); pointer-events: auto; accent-color: #5b8cff; cursor: pointer; }`;
  ctx.root.appendChild(hudStyle);

  const hud = document.createElement("div");
  hud.className = "xr-hud";
  hud.innerHTML = `
    <div class="xr-top">
      <button type="button" class="xr-btn" data-act="exit">‹ ${t("xrExit")}</button>
      <button type="button" class="xr-btn" data-act="follow"></button>
      <span class="xr-title"></span>
    </div>
    <div class="xr-hint">${t("xrHintDesktop")}</div>
    <div class="xr-status"></div>
    <input class="xr-slider" type="range" min="0" max="1000" value="1000" />`;
  ctx.root.appendChild(hud);
  const exitBtn = hud.querySelector('[data-act="exit"]');
  const followBtn = hud.querySelector('[data-act="follow"]');
  const titleEl = hud.querySelector(".xr-title");
  const statusEl = hud.querySelector(".xr-status");
  const slider = hud.querySelector(".xr-slider");
  exitBtn.addEventListener("click", () => doExit());

  /* ---------- 消息条带（最旧 → 最新） ---------- */

  let strip = [];
  const byId = new Map();
  let follow = true;
  let hArc = 0;
  let backfilling = false;
  let historyEnd = false;
  let sliderActive = false;

  function collectFromLog() {
    const out = [];
    ctx.logElement.querySelectorAll(".bubble[data-msg-id]").forEach((el) => {
      const id = el.dataset.msgId;
      const msg = ctx.msgById(id);
      if (!msg) return;
      out.push({ id, msg, el, staged: false, widthCss: el.offsetWidth, heightCss: el.offsetHeight });
    });
    return out;
  }

  function rebuildFromLog() {
    ctx.clearStaged();
    strip = collectFromLog();
    byId.clear();
    for (const e of strip) byId.set(e.id, e.msg);
    panels.removeAll();
    historyEnd = false;
    backfillPages = 0;
    refreshTitle();
  }

  let backfillPages = 0;
  async function backfill() {
    if (backfilling || historyEnd || disposed) return;
    const oldest = strip[0];
    if (!oldest || !oldest.msg || !oldest.msg.id) { historyEnd = true; return; }
    backfilling = true;
    statusEl.textContent = t("xrBackfilling");
    try {
      while (!historyEnd && !disposed && backfillPages < 12) {
        const anchor = strip[0].msg.id;
        const data = await ctx.api(
          `/api/rooms/${encodeURIComponent(ctx.roomName())}/messages?limit=200&beforeId=${encodeURIComponent(anchor)}`
        );
        if (disposed) break;
        const msgs = (data && data.messages) || [];
        const fresh = [];
        for (const m of msgs) {
          const mid = String(m.id);
          if (byId.has(mid)) continue;
          byId.set(mid, m);
          const el = ctx.buildStaged(m);
          fresh.push({ id: mid, msg: m, el, staged: true, widthCss: el.offsetWidth, heightCss: el.offsetHeight });
        }
        if (fresh.length) strip = fresh.concat(strip);
        backfillPages++;
        refreshTitle();
        /* 服务器返回不足一页 → 已到最早 */
        if (msgs.length < 200 || strip[0].msg.id === anchor) {
          historyEnd = true;
          statusEl.textContent = t("xrHistoryEnd");
          break;
        }
        await sleep(30);
      }
    } catch (err) {
      if (!disposed) statusEl.textContent = t("xrBackfillFail");
    } finally {
      backfilling = false;
    }
  }

  function refreshTitle() {
    titleEl.textContent = `${ctx.roomName() || ""} · ${tf("xrMsgCount", { n: strip.length })}`;
    statusEl.textContent = strip.length ? statusEl.textContent : t("xrRoomEmpty");
  }

  /* ---------- 布局（弧长传送带：a_i = 保守弧长 − 滚动偏移，φ_i = ANCHOR + a_i / R；
     hArc > 0 表示已向历史方向滚动——面板整体右移，旧消息从左侧进入窗口） ---------- */

  function maxArc() {
    return strip.length > 1 ? (strip.length - 1) * PITCH + 0.5 : 0;
  }

  function scrollBy(d) {
    if (follow) { follow = false; refreshFollowBtn(); }
    hArc = THREE.MathUtils.clamp(hArc + d, 0, maxArc());
    syncSlider();
  }

  function layout(dt) {
    const n = strip.length;
    const entries = [];
    for (let i = 0; i < n; i++) {
      const phi = ANCHOR + ((n - 1 - i) * PITCH - hArc) / R;
      if (phi > ANCHOR - WIN_NEW && phi < ANCHOR + WIN_OLD) {
        strip[i]._phi = phi;
        entries.push(strip[i]);
      }
    }
    window.__xrDebug && (window.__xrDebug.lastLayout = { n, entries: entries.length, hArc, follow });
    panels.sync({
      entries,
      keepIds: strip.map((e) => e.id),
      place: (e, hWorld) => ({
        x: R * Math.sin(e._phi),
        y: FLOOR_Y + hWorld / 2,
        z: R * Math.cos(e._phi),
        rotY: e._phi + Math.PI,
      }),
      dt,
    });
  }

  /* ---------- 控制（桌面第一人称：拖拽环视 / 滚轮走近 / WASD / ←→ 翻历史） ---------- */

  const look = { yaw: 0, pitch: 0 };
  const keys = new Set();
  let dragInfo = null;

  renderer.domElement.addEventListener("pointerdown", (e) => {
    dragInfo = { x: e.clientX, y: e.clientY, t: performance.now(), moved: 0, id: e.pointerId };
    try { renderer.domElement.setPointerCapture(e.pointerId); } catch (err) {}
  });
  renderer.domElement.addEventListener("pointermove", (e) => {
    if (!dragInfo || dragInfo.id !== e.pointerId) return;
    const dx = e.clientX - dragInfo.x, dy = e.clientY - dragInfo.y;
    dragInfo.x = e.clientX; dragInfo.y = e.clientY;
    dragInfo.moved += Math.abs(dx) + Math.abs(dy);
    look.yaw -= dx * 0.0042;
    look.pitch = THREE.MathUtils.clamp(look.pitch - dy * 0.003, -1.2, 1.2);
  });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (dragInfo && dragInfo.id === e.pointerId) {
      if (dragInfo.moved < 6 && performance.now() - dragInfo.t < 400) handlePanelClick(e);
      dragInfo = null;
    }
  });
  renderer.domElement.addEventListener("pointercancel", () => { dragInfo = null; });
  renderer.domElement.addEventListener("contextmenu", (e) => e.preventDefault());

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  function handlePanelClick(e) {
    const rect = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const panelId = panels.raycast(raycaster);
    if (panelId) panels.cycleSegment(panelId); /* 多段长文点击续读；聚焦模式 Phase 2 接入 */
  }

  renderer.domElement.addEventListener("wheel", (e) => {
    e.preventDefault();
    const dir = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
    camera.position.addScaledVector(dir, -e.deltaY * 0.0022);
    clampCamera();
  }, { passive: false });

  function clampCamera() {
    const p = camera.position;
    const hr = Math.hypot(p.x, p.z);
    if (hr > MAX_RADIUS) { p.x *= MAX_RADIUS / hr; p.z *= MAX_RADIUS / hr; }
    p.y = THREE.MathUtils.clamp(p.y, 0.85, 3.4);
  }

  function onKeyDown(e) {
    if (disposed) return;
    if (e.key === "Escape") { doExit(); return; }
    const k = e.key.toLowerCase();
    if (k === "f") { toggleFollow(); return; }
    if ("wasd".includes(k) || k.startsWith("arrow")) {
      keys.add(k);
      if (k.startsWith("arrow")) e.preventDefault();
    }
  }
  function onKeyUp(e) { keys.delete(e.key.toLowerCase()); }
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);

  function updateControls(dt) {
    let fwd = 0, side = 0;
    if (keys.has("w") || keys.has("arrowup")) fwd += 1;
    if (keys.has("s") || keys.has("arrowdown")) fwd -= 1;
    if (keys.has("d")) side += 1;
    if (keys.has("a")) side -= 1;
    if (fwd || side) {
      const speed = 2.6 * dt;
      const fx = -Math.sin(look.yaw), fz = -Math.cos(look.yaw);
      const rx = Math.cos(look.yaw), rz = -Math.sin(look.yaw);
      camera.position.x += (fx * fwd + rx * side) * speed;
      camera.position.z += (fz * fwd + rz * side) * speed;
      clampCamera();
    }
    if (keys.has("arrowleft")) scrollBy(dt * 2.2);    /* ← 把左侧（更旧）面板转向正前 */
    if (keys.has("arrowright")) scrollBy(-dt * 2.2);
    camera.rotation.set(look.pitch, look.yaw, 0);
  }

  /* ---------- 跟随与滑杆 ---------- */

  function refreshFollowBtn() {
    followBtn.textContent = follow ? t("xrFollowOn") : t("xrFollowOff");
  }
  function toggleFollow() {
    follow = !follow;
    refreshFollowBtn();
  }
  followBtn.addEventListener("click", toggleFollow);
  refreshFollowBtn();

  function syncSlider() {
    if (sliderActive) return;
    const ma = maxArc();
    slider.value = String(ma > 0 ? Math.round(1000 * (1 - hArc / ma)) : 1000);
  }
  slider.addEventListener("pointerdown", () => { sliderActive = true; });
  const releaseSlider = () => { sliderActive = false; };
  slider.addEventListener("pointerup", releaseSlider);
  slider.addEventListener("pointercancel", releaseSlider);
  slider.addEventListener("input", () => {
    const ma = maxArc();
    if (ma > 0) {
      hArc = (1 - slider.value / 1000) * ma;
      /* 拖回最右（最新）自然恢复跟随 */
      if (Number(slider.value) >= 995) { follow = true; }
      else if (follow) { follow = false; }
      refreshFollowBtn();
    }
  });

  /* ---------- 2D 事件订阅（3D 只读，不写共享状态） ---------- */

  const unsubMsg = ctx.msgEvents.subscribe((type, m) => {
    if (disposed) return;
    try {
      if (type === "reset") { rebuildFromLog(); return; }
      if (!m) return;
      const id = String(m.id);
      if (!id || id === "undefined") return;
      if (type === "add") {
        if (!byId.has(id)) {
          const el = ctx.logElement.querySelector(`[data-msg-id="${CSS.escape(id)}"]`);
          if (!el) return;
          byId.set(id, m);
          strip.push({ id, msg: m, el, staged: false, widthCss: el.offsetWidth, heightCss: el.offsetHeight });
        }
      } else if (type === "update") {
        const entry = byId.get(id);
        if (entry) {
          entry.msg = m;
          if (entry.el && entry.el.isConnected) entry.heightCss = entry.el.offsetHeight;
          panels.invalidate(id, !m.streaming);
        }
      } else if (type === "remove") {
        byId.delete(id);
        const i = strip.findIndex((e) => e.id === id);
        if (i >= 0) strip.splice(i, 1);
        panels.remove(id);
      }
      refreshTitle();
    } catch (err) { /* 3D 事件处理故障不得反噬 2D 管线 */ }
  });

  const unsubRoom = ctx.roomEvents.subscribe((info) => {
    if (disposed) return;
    try {
      if (info && info.closed) doExit();
    } catch (err) {}
  });

  /* ---------- 主循环 ---------- */

  let raf = 0;
  let lastT = 0;

  function frame(tNow) {
    if (disposed) return;
    raf = requestAnimationFrame(frame);
    /* 页面隐藏时浏览器本就停发 rAF（需求 7.3 的暂停由浏览器保证）；
       这里不再叠加 running 门控，避免可见性误报导致黑屏。 */
    const dt = Math.min(0.05, (tNow - lastT) / 1000 || 0.016);
    lastT = tNow;
    updateControls(dt);
    if (follow && hArc > 0.001) {
      hArc *= Math.exp(-dt * 4);
      if (hArc < 0.005) hArc = 0;
      syncSlider();
    }
    layout(dt);
    /* 接近已加载的最旧一端 → 向前分页回填（一次性拉全，需求 2.7） */
    const endArc = (strip.length - 1) * PITCH;
    if (strip.length && !historyEnd && hArc + WIN_OLD * R > endArc - 1.2 && (endArc < WIN_OLD * R - 1 || hArc > 0)) {
      backfill();
    }
    renderer.render(scene, camera);
  }
  lastT = performance.now();
  raf = requestAnimationFrame(frame);

  let lastViewportW = window.innerWidth;
  let resizeTimer = null;
  function onResize() {
    if (disposed) return;
    renderer.setSize(window.innerWidth, window.innerHeight);
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    /* 宽度显著变化 → 2D 气泡换行宽度随之变化 → 全量重栅格化（去抖） */
    if (Math.abs(window.innerWidth - lastViewportW) >= 80) {
      lastViewportW = window.innerWidth;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if (!disposed) panels.invalidateAll(); }, 300);
    }
  }
  window.addEventListener("resize", onResize);

  function onVis() {
    lastT = performance.now(); /* 回前台时重置时钟，避免 dt 跳变 */
  }
  document.addEventListener("visibilitychange", onVis);

  /* 初始：从 2D 已渲染的 #log 收集当前窗口消息，并按需后台回填全史 */
  rebuildFromLog();
  if (strip.length && !historyEnd) backfill();

  /* 桌面调试句柄（验证/排查用；退出 3D 时移除） */
  window.__xrDebug = {
    camera,
    stats: () => ({ ...panels.stats(), children: panels.group.children.length }),
    strip: () => strip.length,
    lastLayout: null,
    scene: () => scene.children.map((o) => o.type),
    childPos: (i) => {
      const m = panels.group.children[i || 0];
      return m ? [+m.position.x.toFixed(2), +m.position.y.toFixed(2), +m.position.z.toFixed(2), +m.scale.y.toFixed(2)] : null;
    },
    debugRec: (id) => panels.debugRec(String(id)),
    ids: () => panels.group.children.map((m) => m.userData.panelId),
  };

  /* ---------- 退出与释放（需求 7.4） ---------- */

  function disposeScene() {
    scene.traverse((obj) => {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        for (const mt of mats) {
          if (mt.map && mt.map !== null) mt.map.dispose();
          mt.dispose();
        }
      }
    });
  }

  function doExit() {
    if (disposed) return;
    dispose();
    if (ctx.onExit) ctx.onExit();
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(raf);
    if (resizeTimer) clearTimeout(resizeTimer);
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("keyup", onKeyUp);
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVis);
    try { unsubMsg(); } catch (e) {}
    try { unsubRoom(); } catch (e) {}
    panels.dispose();
    disposeScene();
    renderer.dispose();
    try { renderer.forceContextLoss(); } catch (e) {}
    ctx.clearStaged();
    ctx.root.innerHTML = "";
    ctx.root.classList.add("hidden");
    delete window.__xrDebug;
  }

  return { dispose };
}
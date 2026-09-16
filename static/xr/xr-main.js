/* WebXR 3D 渲染端主模块：场景生命周期、弧形消息墙（面板模式管线）、桌面第一人称预览。
   架构不变量：3D 只订阅 2D 端的 msgEvents/roomEvents（单一数据流），不建第二套轮询；
   历史回填是沿墙向前的一次性 beforeId 分页拉取（需求 2.7）。退出时资源全量 dispose，
   任何 3D 故障不得影响 2D 正常聊天（需求 1.5、7.4）。 */

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { isModelFilename } from "../model-preview.js";
import { mergeXRI18n } from "./xr-i18n.js";
import { createPanelSystem } from "./xr-panels.js";
import { createNativeSystem } from "./xr-native.js";
import { createAvatarSystem } from "./xr-avatars.js";

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
  renderer.xr.enabled = true; /* 沉浸式会话接入（任务 10）；无会话时为普通桌面渲染 */
  ctx.root.classList.remove("hidden");
  ctx.root.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 0.05, 200);
  camera.rotation.order = "YXZ";
  camera.position.set(0, EYE_Y, 2.6);
  /* rig：用户在场景中的「载体」。桌面模式 rig 恒等（相机行为与此前完全一致）；
     沉浸式下 XR 位姿写入 camera（相对 rig），传送/平移/转向作用于 rig。 */
  const rig = new THREE.Group();
  rig.add(camera);
  scene.add(rig);
  /* 语音空间音频监听者（需求 5.1）：挂相机随视点移动 */
  const audioListener = new THREE.AudioListener();
  camera.add(audioListener);

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

  /* 原生 3D 图表（```chart 数据驱动，需求 3.1/3.2；面板模式保留为兜底开关） */
  const native = createNativeSystem({ a2ui: ctx.a2ui, fetchImage: fetchImageObjectURL });
  scene.add(native.group);

  /* ---------- 成员形象（需求 4）：xr-avatars 加载 GLB/VRM + 缺省胶囊，站位按用户名散列。
     初始成员取 roomInfo()（进入 3D 时已错过的最后一次 roomEvents 快照），之后随
     roomEvents 增量同步。加载失败静默回退胶囊（需求 4.5）。 */
  const avatars = createAvatarSystem({ t, tf, username: ctx.username, token: ctx.token });
  scene.add(avatars.group);
  try { avatars.applyRoom(ctx.roomInfo && ctx.roomInfo()); } catch (err) {}

  /* ---------- 语音空间音频（需求 5.1/5.2）：2D 播放链创建的 audio 元素经
     setVoiceSpatial 注册的钩子路由进 PositionalAudio(HRTF)；声源绑定发送者形象
     站位（随站位），无形象回退消息面板位。播放结束自动摘除；进 3D 时 2D 端
     已停止播放（enterXR 侧），2D/3D 不并存出声。 */
  const spatialVoices = new Map(); // msgId → { pa, srcNode, el, username, onEnd }
  function dropSpatialVoice(id) {
    const e = spatialVoices.get(String(id));
    if (!e) return;
    spatialVoices.delete(String(id));
    try { e.el.removeEventListener("ended", e.onEnd); } catch (err) {}
    try { e.srcNode.disconnect(); } catch (err) {}
    /* MediaElementSource 路由是永久的：断开声像图后接回 destination，
       兜底 2D 侧对同一元素的续播（平铺出声，静默失败也无碍） */
    try { e.srcNode.connect(audioListener.context.destination); } catch (err) {}
    try { e.pa.disconnect(); } catch (err) {}
    if (e.pa.parent) e.pa.parent.remove(e.pa);
    if (e.username) { try { avatars.setExpression(e.username, "jawOpen", 0); } catch (err) {} }
  }
  function spatialHook(audioEl, msg) {
    if (!audioEl || !msg || msg.id == null) return;
    const id = String(msg.id);
    dropSpatialVoice(id); /* 同消息重复播放：先清旧路由 */
    const ac = audioListener.context;
    if (ac.state === "suspended") { try { ac.resume(); } catch (err) {} }
    const pa = new THREE.PositionalAudio(audioListener);
    pa.setRefDistance(1.6);
    pa.setRolloffFactor(1.4);
    const srcNode = ac.createMediaElementSource(audioEl);
    pa.setNodeSource(srcNode); /* 元素输出 → panner(HRTF) → 监听者 */
    scene.add(pa);
    const entry = { pa, srcNode, el: audioEl, username: msg.username || "", onEnd: null };
    entry.onEnd = () => dropSpatialVoice(id);
    audioEl.addEventListener("ended", entry.onEnd);
    spatialVoices.set(id, entry);
    audioEl.dataset.xrSpatial = "1";
  }
  try { ctx.setVoiceSpatial(spatialHook); } catch (err) {}

  function updateSpatialVoices() {
    const nowS = performance.now() / 1000;
    for (const [, e] of spatialVoices) {
      const sp = e.username ? avatars.positionOf(e.username) : null;
      if (sp) e.pa.position.set(sp.x, sp.y + 1.45, sp.z); /* 形象嘴部高度 */
      else {
        const pp = panels.positionOf(e.msgId);
        if (pp) e.pa.position.copy(pp);
      }
      /* 口型 best-effort（需求 5.2）：有表情能力的形象 jawOpen 振荡，其余静默 */
      if (e.username) {
        try { avatars.setExpression(e.username, "jawOpen", 0.28 + 0.24 * Math.sin(nowS * 13)); } catch (err) {}
      }
    }
  }

  /* ---------- 聚焦模式（需求 7.2 / 3.6）：对准一条消息放大到舒适阅读（~40° 视角）。
     桌面映射：面板双击进入 / 聚焦中单击返回；图片平面单击进入（指向放大）；
     Esc 先退聚焦再退 3D。头显侧手柄射线在任务 10 接同一 API（enterFocus/exitFocus）。 */

  let focus = null;   // { id, kind: "panel" | "image" } | null
  let lastClick = { id: null, t: 0 }; /* 双击检测（面板聚焦入口） */

  function enterFocus(id, kind) {
    focus = { id: String(id), kind };
  }
  function exitFocus() {
    focus = null;
    native.setOverride(null);
    native.setFocused(null);
  }

  function focusPose(hWorld) {
    const dist = THREE.MathUtils.clamp((hWorld * 0.5) / Math.tan(THREE.MathUtils.degToRad(20)), 0.5, 2.2);
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-8) fwd.set(0, 0, -1);
    fwd.normalize();
    const x = camera.position.x + fwd.x * dist;
    const z = camera.position.z + fwd.z * dist;
    return { x, y: EYE_Y, z, rotY: Math.atan2(camera.position.x - x, camera.position.z - z) };
  }

  async function fetchImageObjectURL(url) {
    const resp = await fetch(url, { headers: ctx.token ? { Authorization: "Bearer " + ctx.token() } : {} });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return URL.createObjectURL(await resp.blob());
  }

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
      <button type="button" class="xr-btn" data-act="vr"></button>
      <button type="button" class="xr-btn" data-act="follow"></button>
      <button type="button" class="xr-btn" data-act="native"></button>
      <span class="xr-title"></span>
    </div>
    <div class="xr-hint">${t("xrHintDesktop")}</div>
    <div class="xr-status"></div>
    <input class="xr-slider" type="range" min="0" max="1000" value="1000" />`;
  ctx.root.appendChild(hud);
  const exitBtn = hud.querySelector('[data-act="exit"]');
  const vrBtn = hud.querySelector('[data-act="vr"]');
  const followBtn = hud.querySelector('[data-act="follow"]');
  const nativeBtn = hud.querySelector('[data-act="native"]');
  const titleEl = hud.querySelector(".xr-title");
  const statusEl = hud.querySelector(".xr-status");
  const slider = hud.querySelector(".xr-slider");
  exitBtn.addEventListener("click", () => doExit());
  /* 沉浸式入口（需求 1.2）：仅当浏览器报告支持 immersive-vr 时显示 */
  function refreshVRBtn() {
    if (!ctx.immersible || !ctx.immersible()) { vrBtn.style.display = "none"; return; }
    vrBtn.style.display = "";
    vrBtn.textContent = xrInImmersive ? t("xrExitVR") : t("xrEnterVR");
  }
  vrBtn.addEventListener("click", () => {
    if (xrInImmersive) exitImmersive();
    else enterImmersive();
  });
  function refreshNativeBtn() {
    nativeBtn.textContent = native.isEnabled() ? t("xrNativeOn") : t("xrNativeOff");
  }
  nativeBtn.addEventListener("click", () => {
    native.setEnabled(!native.isEnabled());
    refreshNativeBtn();
  });
  refreshNativeBtn();

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
    /* 聚焦接管：面板聚焦 → 原生物让位；图片聚焦 → 仅目标平面移到视点前放大 */
    native.setFocused(focus ? focus.id : null);
    native.setOverride(focus && focus.kind === "image" ? focusPose(1.15) : null);
    panels.sync({
      entries,
      keepIds: strip.map((e) => e.id),
      place: (e, hWorld) => {
        if (focus && focus.kind === "panel" && e.id === focus.id) return focusPose(hWorld);
        return {
          x: R * Math.sin(e._phi),
          y: FLOOR_Y + hWorld / 2,
          z: R * Math.cos(e._phi),
          rotY: e._phi + Math.PI,
        };
      },
      dt,
    });
    native.sync(entries, (id) => panels.positionOf(id), dt);
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

  /* ---------- 3D 模型附件放置（需求 3.9）：点击 3D 文件卡片所在面板 → 放置/收起模型。
     取回/解析与形象加载同一思路（服务器附件带 token 取 blob）；场景内上限 LRU，
     退出时随 dispose 全量释放。VRM 也由 GLTFLoader 解析（VRM 即 GLB），专属处理 Phase 3。 */

  const MODEL_CAP = 6;
  const chatModels = new Map();  // msgId → THREE.Group（已归一化）
  const modelOrder = [];
  const placingModels = new Set();
  const gltfLoader = new GLTFLoader();

  function disposeObjectTree(obj) {
    obj.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) {
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        for (const mt of mats) {
          for (const key of ["map", "normalMap", "roughnessMap", "metalnessMap", "emissiveMap", "aoMap"]) {
            if (mt[key] && mt[key].dispose) mt[key].dispose();
          }
          mt.dispose();
        }
      }
    });
  }

  function removeChatModel(id) {
    const obj = chatModels.get(id);
    if (!obj) return false;
    scene.remove(obj);
    disposeObjectTree(obj);
    chatModels.delete(id);
    const i = modelOrder.indexOf(id);
    if (i >= 0) modelOrder.splice(i, 1);
    return true;
  }

  async function placeChatModel(id, msg) {
    if (placingModels.has(id) || disposed) return;
    placingModels.add(id);
    statusEl.textContent = t("xrModelLoading");
    let objUrl = null;
    try {
      const url = msg.downloadUrl;
      let src = url;
      if (url && url.startsWith("/api/")) {
        /* 服务器附件端点要求鉴权，GLTFLoader 不带 Authorization：先取 blob 再加载 */
        const resp = await fetch(url, { headers: ctx.token ? { Authorization: "Bearer " + ctx.token() } : {} });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        objUrl = URL.createObjectURL(await resp.blob());
        src = objUrl;
      }
      const gltf = await gltfLoader.loadAsync(src);
      const model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
      if (!model) throw new Error("empty model");
      /* 外框包围盒归一化到 ~1m，底边落地，放在面板正前方地面 */
      const box = new THREE.Box3().setFromObject(model);
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      const maxDim = Math.max(size.x, size.y, size.z) || 1;
      const holder = new THREE.Group();
      model.position.copy(center).negate();
      holder.add(model);
      holder.scale.setScalar(1.0 / maxDim);
      const entry = strip.find((e) => e.id === id);
      const phi = entry && entry._phi != null ? entry._phi : ANCHOR;
      const rr = R - 1.7;
      holder.position.set(rr * Math.sin(phi), 0, rr * Math.cos(phi));
      holder.rotation.y = phi + Math.PI;
      scene.add(holder);
      while (chatModels.size >= MODEL_CAP) removeChatModel(modelOrder[0]);
      chatModels.set(id, holder);
      modelOrder.push(id);
      statusEl.textContent = "";
    } catch (err) {
      if (!disposed) statusEl.textContent = t("xrModelLoadFail");
    } finally {
      if (objUrl) URL.revokeObjectURL(objUrl);
      placingModels.delete(id);
    }
  }

  function toggleChatModel(id, msg) {
    if (chatModels.has(id)) {
      removeChatModel(id);
      statusEl.textContent = "";
      return;
    }
    placeChatModel(id, msg);
  }

  function handlePick(pickRay) {
    /* 饼图扇区点击 → 名称/数值/百分比浮签（需求 3.2） */
    const sector = native.pickSector(pickRay);
    if (sector) { native.showSectorTip(sector); return; }
    /* 图片平面点击 → 聚焦放大 / 返回（需求 3.6 指向放大） */
    const imgId = native.pickImage(pickRay);
    if (imgId != null) {
      if (focus && focus.kind === "image" && focus.id === imgId) exitFocus();
      else enterFocus(imgId, "image");
      return;
    }
    const panelId = panels.raycast(pickRay);
    if (!panelId) return;
    /* 3D 模型附件面板：点击放置/收起（此类面板短、无翻段交互，不与 cycleSegment 冲突） */
    const m = ctx.msgById(panelId);
    if (m && m.msgType === "attachment" && isModelFilename(m.attachmentName)) {
      toggleChatModel(String(panelId), m);
      return;
    }
    if (focus && focus.id === panelId) { exitFocus(); return; } /* 聚焦中再按一次 → 返回（需求 7.2） */
    /* 语音面板：点击复用 2D 播放链（需求 5.1，零 2D 逻辑改动），音频经 spatialHook 空间化 */
    if (m && m.msgType === "voice") {
      try { ctx.playVoice(m); } catch (err) {}
      return;
    }
    const now = performance.now();
    if (lastClick.id === panelId && now - lastClick.t < 350) {
      lastClick = { id: null, t: 0 };
      enterFocus(panelId, "panel"); /* 双击 → 聚焦阅读（桌面输入映射，需求 7.2） */
      return;
    }
    lastClick = { id: panelId, t: now };
    panels.cycleSegment(panelId); /* 多段长文点击续读；聚焦模式见 handlePanelClick 双击分支 */
  }

  function handlePanelClick(e) {
    const rect = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    handlePick(raycaster);
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
    if (e.key === "Escape") {
      if (focus) { exitFocus(); return; } /* 先退聚焦，再退 3D（需求 7.2） */
      doExit();
      return;
    }
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
    refreshHud3D(); /* 3D HUD 同步（非沉浸式时 no-op） */
  }
  function toggleFollow() {
    follow = !follow;
    refreshFollowBtn();
  }
  followBtn.addEventListener("click", toggleFollow);

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

  /* ---------- WebXR 沉浸式会话（任务 10）：renderer.xr + 手柄射线拾取 + 摇杆平移/转向。
     输入抽象层三动作：确认（trigger→射线拾取，桌面=鼠标点击）、移动（左摇杆平移，
     桌面=滚轮/WASD）、旋转（右摇杆转向，桌面=拖拽环视）。无手柄时头向环视天然可用。
     3D HUD 两按钮（返回 2D / 跟随开关）挂在用户前方，手柄射线可点；桌面 DOM HUD
     在头显内不可见。真机行为留任务 12 用户抽查。 */

  let xrInImmersive = false;
  const _xrV1 = new THREE.Vector3();
  const _xrV2 = new THREE.Vector3();
  const _xrQ1 = new THREE.Quaternion();
  const xrRay = new THREE.Raycaster();

  const hud3D = new THREE.Group();
  hud3D.visible = false;
  scene.add(hud3D);
  function drawHudButton(mesh, label) {
    const c = mesh.userData.hudCanvas;
    const g = c.g;
    g.clearRect(0, 0, 512, 128);
    g.beginPath();
    g.roundRect(8, 16, 496, 96, 48);
    g.fillStyle = "rgba(16,22,34,0.88)";
    g.fill();
    g.lineWidth = 4;
    g.strokeStyle = "rgba(91,140,255,0.9)";
    g.stroke();
    g.fillStyle = "#dfe8f4";
    g.font = "bold 50px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(label, 256, 68);
    mesh.material.map.needsUpdate = true;
  }
  function buildHudButton(label) {
    const c = document.createElement("canvas");
    c.width = 512;
    c.height = 128;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(0.46, 0.115),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false })
    );
    mesh.material.map.colorSpace = THREE.SRGBColorSpace;
    mesh.userData.hudCanvas = { c, g: c.getContext("2d") };
    drawHudButton(mesh, label);
    return mesh;
  }
  const hudBackBtn = buildHudButton(t("xrHudBack"));
  const hudFollowBtn = buildHudButton(t("xrFollowOn"));
  hudBackBtn.position.y = 0.1;
  hudFollowBtn.position.y = -0.1;
  hud3D.add(hudBackBtn, hudFollowBtn);
  function refreshHud3D() {
    drawHudButton(hudFollowBtn, follow ? t("xrFollowOn") : t("xrFollowOff"));
  }

  /* 手柄：targetRaySpace（射线）挂 rig——rig 即用户载体，传送/转向随体 */
  const xrControllers = [];
  for (let i = 0; i < 2; i++) {
    const c = renderer.xr.getController(i);
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
      new THREE.LineBasicMaterial({ color: 0x6ea8ff, transparent: true, opacity: 0.85 })
    );
    line.scale.z = 4;
    line.visible = false;
    c.add(line);
    c.userData.xrLine = line;
    c.addEventListener("selectstart", () => onXRSelect(c));
    rig.add(c);
    xrControllers.push(c);
  }
  renderer.xr.addEventListener("sessionstart", () => {
    xrInImmersive = true;
    for (const c of xrControllers) c.userData.xrLine.visible = true;
    refreshVRBtn();
    refreshHud3D();
  });
  renderer.xr.addEventListener("sessionend", () => {
    xrInImmersive = false;
    for (const c of xrControllers) c.userData.xrLine.visible = false;
    refreshVRBtn();
  });

  async function enterImmersive() {
    if (disposed || xrInImmersive) return;
    if (!navigator.xr || !navigator.xr.requestSession) { statusEl.textContent = t("xrVRFail"); return; }
    try {
      const session = await navigator.xr.requestSession("immersive-vr", { optionalFeatures: ["local-floor", "bounded-floor"] });
      await renderer.xr.setSession(session);
      refreshVRBtn();
    } catch (err) {
      statusEl.textContent = t("xrVRFail");
    }
  }
  function exitImmersive() {
    const s = renderer.xr.getSession();
    if (s) { try { s.end().catch(() => {}); } catch (err) {} }
  }

  /* 确认动作（手柄）：射线先试 3D HUD 按钮，再走与桌面一致的拾取链 */
  function onXRSelect(c) {
    if (disposed || !renderer.xr.isPresenting) return;
    c.getWorldQuaternion(_xrQ1);
    xrRay.ray.origin.setFromMatrixPosition(c.matrixWorld);
    xrRay.ray.direction.set(0, 0, -1).applyQuaternion(_xrQ1).normalize();
    const hudHits = xrRay.intersectObjects(hud3D.children, false);
    if (hudHits.length) {
      if (hudHits[0].object === hudBackBtn) { doExit(); return; }
      if (hudHits[0].object === hudFollowBtn) { toggleFollow(); return; }
      return;
    }
    handlePick(xrRay);
  }

  /* 移动/旋转（手柄摇杆，xr-standard：axes[2]=X axes[3]=Y，死区 0.15） */
  function updateXRInput(dt) {
    const session = renderer.xr.getSession();
    if (!session || disposed) return;
    for (const src of session.inputSources) {
      const gp = src && src.gamepad;
      if (!gp || !gp.axes || gp.axes.length < 4) continue;
      const ax = gp.axes[2] || 0;
      const ay = gp.axes[3] || 0;
      if (src.handedness === "right") {
        if (Math.abs(ax) > 0.15) rig.rotation.y -= ax * dt * 2.4; /* 旋转动作 */
      } else if (Math.abs(ax) > 0.15 || Math.abs(ay) > 0.15) {
        /* 移动动作：以头向水平 yaw 为基准推杆平移 */
        camera.getWorldDirection(_xrV1);
        _xrV1.y = 0;
        if (_xrV1.lengthSq() < 1e-6) _xrV1.set(0, 0, -1); else _xrV1.normalize();
        _xrV2.crossVectors(_xrV1, camera.up).normalize();
        const spd = 1.7 * dt;
        rig.position.addScaledVector(_xrV1, -ay * spd).addScaledVector(_xrV2, ax * spd);
        const hr = Math.hypot(rig.position.x, rig.position.z);
        if (hr > MAX_RADIUS) {
          rig.position.x *= MAX_RADIUS / hr;
          rig.position.z *= MAX_RADIUS / hr;
        }
        rig.position.y = 0;
      }
    }
  }

  /* 3D HUD 跟随视点（仅沉浸式可见）：置于头前 1.15m 水平方向，直立面向用户 */
  function updateXRHud() {
    const presenting = renderer.xr.isPresenting;
    hud3D.visible = presenting;
    if (!presenting || disposed) return;
    camera.getWorldPosition(_xrV1);
    camera.getWorldDirection(_xrV2);
    _xrV2.y = 0;
    if (_xrV2.lengthSq() < 1e-6) _xrV2.set(0, 0, -1); else _xrV2.normalize();
    hud3D.position.copy(_xrV1).addScaledVector(_xrV2, 1.15);
    hud3D.position.y = _xrV1.y - 0.12;
    hud3D.lookAt(_xrV1);
  }

  /* 按钮初始文字：3D HUD（canvas）与 DOM HUD 都就绪后再刷（避免声明顺序依赖） */
  refreshFollowBtn();
  refreshVRBtn();

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
      if (info && info.closed) { doExit(); return; }
      avatars.applyRoom(info); /* 形象随在线成员增量同步（需求 4.1） */
    } catch (err) {}
  });

  /* ---------- 主循环 ---------- */

  let lastT = 0;

  function frame(tNow) {
    if (disposed) return;
    /* 页面隐藏时浏览器本就停发 rAF（需求 7.3 的暂停由浏览器保证）；
       这里不再叠加 running 门控，避免可见性误报导致黑屏。
       沉浸式下 setAnimationLoop 由 XR 帧驱动（同一路径，任务 10）。 */
    const dt = Math.min(0.05, (tNow - lastT) / 1000 || 0.016);
    lastT = tNow;
    updateXRInput(dt);  /* 手柄摇杆平移/转向（无会话 no-op） */
    updateXRHud();      /* 3D HUD 跟随视点（无会话隐藏） */
    updateControls(dt);
    if (follow && hArc > 0.001) {
      hArc *= Math.exp(-dt * 4);
      if (hArc < 0.005) hArc = 0;
      syncSlider();
    }
    layout(dt);
    try { avatars.update(dt); } catch (err) {} /* 形象呼吸/浮动/表情推进（需求 4.4） */
    updateSpatialVoices(); /* 语音声源跟随站位/面板位 + 口型推进（任务 9） */
    /* 接近已加载的最旧一端 → 向前分页回填（一次性拉全，需求 2.7） */
    const endArc = (strip.length - 1) * PITCH;
    if (strip.length && !historyEnd && hArc + WIN_OLD * R > endArc - 1.2 && (endArc < WIN_OLD * R - 1 || hArc > 0)) {
      backfill();
    }
    renderer.render(scene, camera);
  }
  lastT = performance.now();
  renderer.setAnimationLoop(frame);

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
    stats: () => ({ ...panels.stats(), children: panels.group.children.length, native: native.stats() }),
    strip: () => strip.length,
    lastLayout: null,
    scene: () => scene.children.map((o) => o.type),
    childPos: (i) => {
      const m = panels.group.children[i || 0];
      return m ? [+m.position.x.toFixed(2), +m.position.y.toFixed(2), +m.position.z.toFixed(2), +m.scale.y.toFixed(2)] : null;
    },
    debugRec: (id) => panels.debugRec(String(id)),
    ids: () => panels.group.children.map((m) => m.userData.panelId),
    nativeGroup: () => native.group,
    avatarGroup: () => avatars.group,
    avatarApi: () => avatars, /* setExpression/wave/positionOf（ARKit52 驱动接口验证用） */
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
    renderer.setAnimationLoop(null);
    exitImmersive(); /* 头显会话随 3D 退出一并结束（需求 7.4） */
    if (resizeTimer) clearTimeout(resizeTimer);
    focus = null;
    for (const id of Array.from(spatialVoices.keys())) dropSpatialVoice(id); /* 空间音频摘除（需求 7.4） */
    try { ctx.setVoiceSpatial(null); } catch (e) {}
    for (const id of Array.from(chatModels.keys())) removeChatModel(id);
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("keyup", onKeyUp);
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVis);
    try { unsubMsg(); } catch (e) {}
    try { unsubRoom(); } catch (e) {}
    native.dispose();
    avatars.dispose();
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
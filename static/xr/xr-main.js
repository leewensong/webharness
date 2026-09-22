/* WebXR 3D 渲染端主模块：场景生命周期、竖列消息墙（2D 直列式布局 + 右侧滚行条）、
   桌面第一人称预览。
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
import { buildRoomScene, sceneKeyOf } from "./xr-rooms.js";

const R = 6;             // 消息列半径（米）
const PANEL_W = 1.12;    // 参考面板世界宽（米）——单面板实际宽 = 气泡 CSS 宽 × PX_PER_M
const REF_CSS = 480;     // 参考 CSS 宽（与 xr-panels 的 refCss 一致）：全局 px→米 比例的分母
const PX_PER_M = PANEL_W / REF_CSS; /* 短消息窄、长消息宽，字号全局一致（不再等宽压扁）；480 对应 15px 字 ≈ 3.5cm，2~3m 外可读 */
const ANCHOR = Math.PI;  // 消息列方位角（相机默认在 +Z 侧面向 −Z 看墙正面）
const FLOOR_Y = 0.42;    // 列底基准：最新面板底边（历史向上堆叠，越旧越高）
const GAP = 0.16;        // 面板纵向间距
const BAND_HI = 6.0;     // 可视带顶（带底 = FLOOR_Y）：面板滑过该带即进入窗口
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
  /* cssText 会整块覆盖 setSize 刚写入的宽高：必须自带 100%×100%，否则 dpr>1 时
     画布按缓冲区像素显示、溢出窗口，整个画面偏移（canvas 是 replaced element，
     inset:0 不会拉伸它） */
  renderer.domElement.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;";
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
    refCss: REF_CSS,
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

  /* ---------- 房间 3D 场景（需求 9 的 map3d） ----------
     有场景时用场景替换展厅的网格与中央环（渐变天空与灯光保留作基调），并把场景提供的
     推荐座位交给形象系统；无场景、未知 id、加载失败一律保持展厅原样，绝不影响 2D（需求 1.5）。
     只在描述符真的变了才重建——否则每次房间轮询都会重搭一遍几何。 */
  let roomSystem = null;
  let roomAppliedKey;   /* undefined = 还没应用过 */

  function applyRoomScene(desc) {
    const key = sceneKeyOf(desc);
    if (key === roomAppliedKey) return;
    roomAppliedKey = key;
    let next = null;
    try {
      next = buildRoomScene(desc, { token: ctx.token && ctx.token() });
    } catch (err) {
      console.warn("[xr] 房间场景构建失败，保持展厅", err);
      next = null;
    }
    if (roomSystem) { scene.remove(roomSystem.group); roomSystem.dispose(); }
    roomSystem = next && next.sceneId ? next : null;
    if (next && !roomSystem) next.dispose();   /* 空/未知场景：别留下垃圾组 */
    const on = !!roomSystem;
    grid.visible = !on;
    centerRing.visible = !on;
    ground.position.y = on ? -0.01 : 0;        /* 让位给场景地板，避免 z-fighting */
    if (on) scene.add(roomSystem.group);
    try { avatars.setSeats(on ? roomSystem.seats : []); } catch (err) {}
  }

  try { applyRoomScene((ctx.roomInfo && ctx.roomInfo() && ctx.roomInfo().scene) || null); } catch (err) {}
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
    #xrRoot .xr-status { position: absolute; bottom: 92px; left: 50%; transform: translateX(-50%); color: #8fa3bd; font-size: 13px; }
    #xrRoot .xr-vscroll { position: absolute; top: 84px; bottom: 100px; right: 12px; width: 10px;
      background: rgba(16, 22, 34, 0.55); border-radius: 6px; pointer-events: auto; }
    #xrRoot .xr-vthumb { position: absolute; left: 0; width: 100%; border-radius: 6px;
      background: #46608c; cursor: pointer; }
    #xrRoot .xr-vthumb:hover { background: #5b8cff; }
    #xrRoot .xr-send { position: absolute; bottom: 4px; left: 50%; transform: translateX(-50%);
      display: flex; gap: 8px; pointer-events: auto; }
    #xrRoot .xr-send-input { width: min(430px, 56vw); padding: 9px 14px; font-size: 13px;
      border: 1px solid #2c3d58; border-radius: 999px; background: rgba(16, 22, 34, 0.82);
      color: #dfe8f4; outline: none; }
    #xrRoot .xr-send-input:focus { border-color: #5b8cff; }
    /* dom-overlay 沉浸式：桌面键鼠操作提示不适用，隐藏（其余 HUD 复用） */
    #xrRoot .xr-hud.xr-immersive .xr-hint { display: none; }`;
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
    <div class="xr-vscroll" title=""><div class="xr-vthumb"></div></div>
    <div class="xr-send">
      <input class="xr-send-input" type="text" maxlength="4000" />
      <button type="button" class="xr-btn" data-act="mic"></button>
      <button type="button" class="xr-btn" data-act="send"></button>
    </div>`;
  ctx.root.appendChild(hud);
  const exitBtn = hud.querySelector('[data-act="exit"]');
  const vrBtn = hud.querySelector('[data-act="vr"]');
  const followBtn = hud.querySelector('[data-act="follow"]');
  const nativeBtn = hud.querySelector('[data-act="native"]');
  const titleEl = hud.querySelector(".xr-title");
  const statusEl = hud.querySelector(".xr-status");
  const vscroll = hud.querySelector(".xr-vscroll");
  const vthumb = hud.querySelector(".xr-vthumb");
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

  /* ---------- 3D 内发送（需求 6.3 最小入口）：纯文本走 2D 的 POST（ctx.sendText），
     发出后经 msgEvents.add 自动回流 3D 墙。私聊/引用/附件等复杂编辑引导回 2D。
     输入框内按键 stopPropagation，避免触发 WASD 移动 / Esc 退出的全局快捷键。 */
  const sendInput = hud.querySelector(".xr-send-input");
  const sendBtn = hud.querySelector('[data-act="send"]');
  sendInput.placeholder = t("xrSendPlaceholder");
  sendBtn.textContent = t("xrSend");
  async function xrSend() {
    const text = sendInput.value.trim();
    if (!text) return;
    if (!ctx.sendText) { statusEl.textContent = t("xrSendFail"); return; }
    sendInput.value = "";
    try {
      await ctx.sendText(text);
      statusEl.textContent = "";
    } catch (err) {
      sendInput.value = text; /* 发送失败还原输入 */
      statusEl.textContent = err && err.message ? err.message : t("xrSendFail");
    }
  }
  sendBtn.addEventListener("click", xrSend);
  sendInput.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); xrSend(); }
    else if (e.key === "Escape") sendInput.blur();
  });
  sendInput.addEventListener("keyup", (e) => e.stopPropagation());

  /* ---------- 语音识别输入（桌面与沉浸式 dom-overlay 通用）：SpeechRecognition 实时转写
     进输入框，再点一次停止，文本留在框内由用户确认发送。错误在状态栏提示。
     getUserMedia 仅做权限门，成功即停轨（识别器自行采集音频，无需 MediaRecorder）。 */
  const micBtn = hud.querySelector('[data-act="mic"]');
  const hasASR = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  let asrSR = null;
  let asrFinal = "";
  let asrBase = "";
  let asrOn = false;
  let asrRestarts = 0;
  function micLabel() { micBtn.textContent = asrOn ? t("xrMicStop") : t("xrMic"); }
  micLabel();
  function stopAsr() {
    asrOn = false;
    micLabel();
    const sr = asrSR;
    asrSR = null;
    if (sr) { try { sr.onend = null; sr.onresult = null; sr.onerror = null; sr.abort(); } catch (e) {} }
  }
  async function startAsr() {
    if (!hasASR) { statusEl.textContent = t("xrAsrUnsupported"); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((tk) => tk.stop());
    } catch (e) {
      statusEl.textContent = t("asrNotAllowed");
      return;
    }
    const SRClass = window.SpeechRecognition || window.webkitSpeechRecognition;
    const sr = new SRClass();
    asrSR = sr;
    asrFinal = "";
    asrBase = sendInput.value;
    asrRestarts = 0;
    sr.lang = (navigator.language || "zh-CN").replace("_", "-");
    sr.interimResults = true;
    sr.continuous = true;
    sr.onresult = (e) => {
      let final = "", interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) final += r[0].transcript; else interim += r[0].transcript;
      }
      if (final) asrFinal += final;
      if (!asrOn) return;
      sendInput.value = [asrBase, (asrFinal + interim).trim()].filter(Boolean).join(" ");
    };
    sr.onerror = (e) => {
      if (e.error === "aborted" || e.error === "no-speech") return;
      const map = {
        "audio-capture": t("asrAudio"),
        "not-allowed": t("asrNotAllowed"),
        "service-not-allowed": t("asrService"),
        network: t("asrNetwork"),
      };
      statusEl.textContent = map[e.error] || t("asrError") + e.error;
    };
    sr.onend = () => {
      if (!asrOn || asrSR !== sr) return;
      if (asrRestarts < 5) { asrRestarts += 1; try { sr.start(); } catch (e) { stopAsr(); } }
      else stopAsr();
    };
    try {
      sr.start();
      asrOn = true;
      statusEl.textContent = "";
      micLabel();
    } catch (e) {
      asrSR = null;
      statusEl.textContent = t("asrStartFail") + (e && e.message ? e.message : "");
    }
  }
  micBtn.addEventListener("click", () => { if (asrOn) stopAsr(); else startAsr(); });
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
  let scrollDrag = false;

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

  /* ---------- 布局（2D 直列式）：面板按各自高度自下而上堆叠成单列，最新在列底
     （底边 FLOOR_Y），历史越旧越高。a_i = 面板 i 与最新端之间的堆叠高度（米）；
     hArc 为滚动量——整列向下滑过固定可视带 [FLOOR_Y, BAND_HI]，与 2D 滚动同构：
     往回翻 = 记录向下移出带底，更早的内容从带顶进入。面板世界高优先取栅格化后的
     实际值（与网格完全一致），未栅格化时用 2D 量测估算兜底 ---------- */

  function slotH(e) {
    return panels.heightOf(e.id)
      || THREE.MathUtils.clamp((e.heightCss || 200) * PX_PER_M, 0.1, 2.3);
  }

  /* 消息 id → 稳定横向偏移（米）：单列布局下所有面板同方位，图表/图片/3D 模型
     等原生对象若仍取面板方位会全部叠在同一点，按 id 散开在前方地面 */
  function hashSpread(id) {
    let h = 0;
    const s = String(id);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return ((h % 9) - 4) * 0.82;
  }

  let totalH = 0; /* 整列堆叠总高（最新端→最旧端） */

  function maxScroll() {
    return Math.max(0, totalH - (BAND_HI - FLOOR_Y));
  }

  function scrollBy(d) {
    if (follow) { follow = false; refreshFollowBtn(); }
    hArc = THREE.MathUtils.clamp(hArc + d, 0, maxScroll());
    syncThumb();
  }

  function layout(dt) {
    /* 撤回等导致 totalH 缩水时收敛滚动量（用上一帧 totalH 即可，下一帧自然校正） */
    if (hArc > maxScroll()) hArc = maxScroll();
    const n = strip.length;
    const entries = [];
    let acc = 0;
    for (let i = n - 1; i >= 0; i--) {
      const e = strip[i];
      const h = slotH(e);
      e._a = acc;
      e._h = h;
      acc += h + GAP;
      const yBot = FLOOR_Y + e._a - hArc;
      /* 窗口 = 槽位与可视带（带底 0.2 贴地）相交的面板 */
      if (yBot < BAND_HI && yBot + h > 0.2) {
        e._phi = ANCHOR;
        e._spread = hashSpread(e.id);
        entries.push(e);
      }
    }
    totalH = acc;
    window.__xrDebug && (window.__xrDebug.lastLayout = { n, entries: entries.length, hArc, follow, totalH });
    /* 聚焦接管：面板聚焦 → 原生物让位；图片聚焦 → 仅目标平面移到视点前放大 */
    native.setFocused(focus ? focus.id : null);
    native.setOverride(focus && focus.kind === "image" ? focusPose(1.15) : null);
    panels.sync({
      entries,
      keepIds: strip.map((e) => e.id),
      place: (e, hWorld, wWorld) => {
        if (focus && focus.kind === "panel" && e.id === focus.id) return focusPose(Math.max(wWorld, hWorld));
        return {
          /* 面板宽度随 2D 气泡宽度变化 → 左边缘对齐同一列轴，列右侧参差即 2D 观感本身 */
          x: -PANEL_W / 2 + wWorld / 2,
          y: FLOOR_Y + e._a - hArc + hWorld / 2,
          z: -R,
          rotY: 0,
        };
      },
      dt,
    });
    native.sync(entries, (id) => panels.positionOf(id), dt);
    syncThumb();
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
      const spread = (entry && entry._spread) || 0;
      holder.position.set(rr * Math.sin(phi) + spread * 0.7, 0, rr * Math.cos(phi));
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
    if (keys.has("arrowleft")) scrollBy(dt * 2.2);    /* ← 翻向更早（列上滑过带顶）；→ 回向最新 */
    if (keys.has("arrowright")) scrollBy(-dt * 2.2);
    camera.rotation.set(look.pitch, look.yaw, 0);
  }

  /* ---------- 跟随与右侧滚行条（2D 滚动条同构：拇指在底部 = 最新/跟随，
     向上拖 = 看更早历史；拇指高度按可视带占整列比例，整列装得下时隐藏） ---------- */

  function refreshFollowBtn() {
    followBtn.textContent = follow ? t("xrFollowOn") : t("xrFollowOff");
  }
  function toggleFollow() {
    follow = !follow;
    refreshFollowBtn();
  }
  followBtn.addEventListener("click", toggleFollow);

  function syncThumb() {
    const ma = maxScroll();
    const trackH = vscroll.clientHeight;
    if (ma <= 0 || trackH <= 0) { vthumb.style.display = "none"; return; }
    vthumb.style.display = "";
    const frac = THREE.MathUtils.clamp((BAND_HI - FLOOR_Y) / totalH, 0.08, 1);
    const th = Math.max(24, Math.round(trackH * frac));
    vthumb.style.height = th + "px";
    const f = 1 - hArc / ma; /* 1 = 最新（拇指沉底），0 = 最旧（拇指到顶） */
    vthumb.style.top = Math.round((trackH - th) * f) + "px";
  }
  function thumbFromEvent(e) {
    const ma = maxScroll();
    if (ma <= 0) return;
    const rect = vscroll.getBoundingClientRect();
    const th = vthumb.offsetHeight || 24;
    const f = THREE.MathUtils.clamp((e.clientY - rect.top - th / 2) / Math.max(1, rect.height - th), 0, 1);
    hArc = (1 - f) * ma;
    /* 拖回最底（最新）自然恢复跟随 */
    if (f >= 0.995) { follow = true; }
    else if (follow) { follow = false; }
    refreshFollowBtn();
  }
  vthumb.addEventListener("pointerdown", (e) => {
    scrollDrag = true;
    try { vthumb.setPointerCapture(e.pointerId); } catch (err) {}
    thumbFromEvent(e);
    e.preventDefault();
  });
  vscroll.addEventListener("pointerdown", (e) => {
    if (e.target === vthumb) return; /* 点击轨道空白 → 跳转到该位置并继续拖动 */
    scrollDrag = true;
    thumbFromEvent(e);
  });
  window.addEventListener("pointermove", (e) => { if (scrollDrag) thumbFromEvent(e); });
  const releaseScroll = () => { scrollDrag = false; };
  window.addEventListener("pointerup", releaseScroll);
  window.addEventListener("pointercancel", releaseScroll);

  /* ---------- WebXR 沉浸式会话：renderer.xr + 手柄射线拾取 + 摇杆平移/转向。
     输入抽象层三动作：确认（trigger→射线拾取，桌面=鼠标点击）、移动（左摇杆平移，
     桌面=滚轮/WASD）、旋转（右摇杆转向，桌面=拖拽环视）。无手柄时头向环视天然可用。
     无 3D HUD：设置/返回/发送走 dom-overlay——会话以 optional feature 请求 dom-overlay
     并把 DOM HUD（.xr-hud）作为 overlay 根，支持的头显（如 Quest Browser）内直接
     可见可点，聚焦输入框弹系统虚拟键盘；不支持的浏览器照常进入，仅看不到 overlay。
     真机行为留用户抽查。 */

  let xrInImmersive = false;
  const _xrV1 = new THREE.Vector3();
  const _xrV2 = new THREE.Vector3();
  const _xrQ1 = new THREE.Quaternion();
  const xrRay = new THREE.Raycaster();

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
    /* 侧握键按住说话（松开发送）——沉浸式下的快捷语音入口 */
    c.addEventListener("squeezestart", () => startVRRec());
    c.addEventListener("squeezeend", () => stopVRRec());
    rig.add(c);
    xrControllers.push(c);
  }
  renderer.xr.addEventListener("sessionstart", () => {
    xrInImmersive = true;
    for (const c of xrControllers) c.userData.xrLine.visible = true;
    const s = renderer.xr.getSession();
    if (s && s.domOverlayState) hud.classList.add("xr-immersive");
    refreshVRBtn();
    vrBar.visible = true;
    refreshVrBar();
    vrHintText(t("xrVRHint"), 9000); /* 入场提示几秒后自动淡出 */
  });
  renderer.xr.addEventListener("sessionend", () => {
    xrInImmersive = false;
    for (const c of xrControllers) c.userData.xrLine.visible = false;
    hud.classList.remove("xr-immersive");
    refreshVRBtn();
    killVRRec(); /* 会话结束即停录音（防麦克风指示灯残留） */
    vrBar.visible = false;
    vrHint.visible = false;
  });

  async function enterImmersive() {
    if (disposed || xrInImmersive) return;
    if (!navigator.xr || !navigator.xr.requestSession) { statusEl.textContent = t("xrVRFail"); return; }
    try {
      /* dom-overlay 为 optional：不支持时请求仍成功，只是没有 domOverlayState */
      const session = await navigator.xr.requestSession("immersive-vr", {
        optionalFeatures: ["local-floor", "bounded-floor", "dom-overlay"],
        domOverlay: { root: hud },
      });
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

  /* ---------- 世界内 VR 控制条 + 语音消息（沉浸式专用，不跟头） ----------
     Quest 等浏览器不给 dom-overlay，头显里就没有任何 DOM 入口；这里把「必要设置」
     做成一排钉在消息列底部前方的小面板（世界内固定，不是浮在眼前），手柄射线可点：
     语音（点一下开始/再点结束，等价于按住侧握键）· 跟随最新 · 返回 2D。
     语音消息只发音频、不依赖 ASR——人类能听，Agent 侧自己调 ASR（用户确认可行）。 */

  const vrBar = new THREE.Group();
  vrBar.visible = false;
  scene.add(vrBar);

  function drawBarButton(mesh, label) {
    const { c, g } = mesh.userData.label;
    g.clearRect(0, 0, c.width, c.height);
    g.beginPath();
    g.roundRect(4, 8, c.width - 8, c.height - 16, 30);
    g.fillStyle = "rgba(14,20,32,0.9)";
    g.fill();
    g.lineWidth = 3;
    g.strokeStyle = "rgba(91,140,255,0.9)";
    g.stroke();
    g.fillStyle = "#dfe8f4";
    g.font = "bold 40px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(label, c.width / 2, c.height / 2 + 2);
    mesh.material.map.needsUpdate = true;
  }
  function buildBarButton(label) {
    const c = document.createElement("canvas");
    c.width = 384;
    c.height = 96;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(0.5, 0.125),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false })
    );
    mesh.material.map.colorSpace = THREE.SRGBColorSpace;
    mesh.userData.label = { c, g: c.getContext("2d") };
    drawBarButton(mesh, label);
    return mesh;
  }
  const vrVoiceBtn = buildBarButton(t("xrVrVoice"));
  const vrFollowBtn = buildBarButton(t("xrFollowOn"));
  const vrExitBtn = buildBarButton(t("xrExit"));
  vrVoiceBtn.position.x = -0.56;
  vrExitBtn.position.x = 0.56;
  vrBar.add(vrVoiceBtn, vrFollowBtn, vrExitBtn);

  function buildVrHint() {
    const c = document.createElement("canvas");
    c.width = 1024;
    c.height = 128;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(1.7, 0.212),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false })
    );
    mesh.material.map.colorSpace = THREE.SRGBColorSpace;
    mesh.userData.label = { c, g: c.getContext("2d") };
    mesh.position.y = 0.2;
    return mesh;
  }
  const vrHint = buildVrHint();
  vrHint.visible = false;
  vrBar.add(vrHint);
  /* 钉在消息列底部前方（世界内固定，离墙 0.85m 便于手柄射线瞄准；不跟头） */
  vrBar.position.set(0, FLOOR_Y - 0.26, -R + 0.85);
  vrBar.rotation.x = -0.3; /* 略上仰，便于低头看 */

  function drawVrHint(text) {
    const { c, g } = vrHint.userData.label;
    g.clearRect(0, 0, c.width, c.height);
    g.beginPath();
    g.roundRect(4, 12, c.width - 8, c.height - 24, 34);
    g.fillStyle = "rgba(10,15,24,0.86)";
    g.fill();
    g.lineWidth = 3;
    g.strokeStyle = "rgba(91,140,255,0.7)";
    g.stroke();
    g.fillStyle = "#dfe8f4";
    g.font = "36px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(text, c.width / 2, c.height / 2 + 2);
    vrHint.material.map.needsUpdate = true;
  }
  let vrHintTimer = null;
  let vrHintMsg = ""; /* 最近一次世界内提示文本（调试/验证用） */
  function vrHintText(text, holdMs) {
    vrHintMsg = text;
    drawVrHint(text);
    vrHint.visible = true;
    if (vrHintTimer) clearTimeout(vrHintTimer);
    if (holdMs) vrHintTimer = setTimeout(() => { if (!vrRec) vrHint.visible = false; }, holdMs);
  }
  function refreshVrBar() {
    drawBarButton(vrFollowBtn, follow ? t("xrFollowOn") : t("xrFollowOff"));
    drawBarButton(vrVoiceBtn, vrRec ? t("xrMicStop") : t("xrVrVoice"));
  }

  const VR_REC_MAX_MS = 60000;
  let vrRec = null; /* { mr, chunks, stream, startMs, timer, autoStop } */
  function vrPickMime() {
    const cands = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    for (const m of cands) {
      try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m; } catch (e) {}
    }
    return "";
  }
  function killVRRec() {
    if (!vrRec) return;
    const cur = vrRec;
    vrRec = null;
    if (cur.timer) clearInterval(cur.timer);
    if (cur.autoStop) clearTimeout(cur.autoStop);
    try { cur.mr.onstop = null; if (cur.mr.state !== "inactive") cur.mr.stop(); } catch (e) {}
    if (cur.stream) { try { cur.stream.getTracks().forEach((tk) => tk.stop()); } catch (e) {} }
    refreshVrBar();
  }
  async function startVRRec() {
    if (disposed || vrRec) return;
    if (!ctx.sendVoiceMsg) { vrHintText(t("xrVoiceUnsupported"), 4000); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { vrHintText(t("noRecApi"), 4000); return; }
    let stream = null;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { vrHintText(t("asrNotAllowed"), 4000); return; }
    if (disposed || vrRec) { try { stream.getTracks().forEach((tk) => tk.stop()); } catch (e) {} return; }
    const mime = vrPickMime();
    let mr = null;
    try { mr = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
    catch (e) {
      try { stream.getTracks().forEach((tk) => tk.stop()); } catch (e2) {}
      vrHintText(t("noRecApi"), 4000);
      return;
    }
    vrRec = { mr, chunks: [], stream, startMs: Date.now(), timer: null, autoStop: null };
    mr.ondataavailable = (e) => { if (e.data && e.data.size && vrRec) vrRec.chunks.push(e.data); };
    mr.onstop = () => onVRRecStop();
    try { mr.start(250); } catch (e) { killVRRec(); vrHintText(t("noRecApi"), 4000); return; }
    refreshVrBar();
    const tick = () => {
      if (!vrRec) return;
      vrHintText("● " + t("xrRecording") + " " + ((Date.now() - vrRec.startMs) / 1000).toFixed(1) + "s", 0);
    };
    tick();
    vrRec.timer = setInterval(tick, 250);
    vrRec.autoStop = setTimeout(() => stopVRRec(), VR_REC_MAX_MS);
  }
  function stopVRRec() {
    if (!vrRec) return;
    try { vrRec.mr.stop(); } catch (e) { killVRRec(); }
  }
  function vrToggleRec() { if (vrRec) stopVRRec(); else startVRRec(); }
  async function onVRRecStop() {
    if (!vrRec) return;
    const cur = vrRec;
    const durMs = Date.now() - cur.startMs;
    const mime = (cur.mr && cur.mr.mimeType) || "audio/webm";
    const chunks = cur.chunks;
    killVRRec(); /* 停轨、复位按钮态（此后 vrRec 为 null，提示面板不再被录音态占住） */
    if (durMs < 1000 || !chunks.length) { vrHintText(t("recTooShort"), 3500); return; }
    const blob = new Blob(chunks, { type: mime.split(";")[0].trim() || "audio/webm" });
    try {
      await ctx.sendVoiceMsg(blob, durMs);
      vrHintText(t("xrVoiceSent"), 3000);
    } catch (e) {
      vrHintText((e && e.message) || t("xrSendFail"), 4500);
    }
  }
  refreshVrBar(); /* 初始文字（此时 vrRec 为 null） */

  /* 确认动作（手柄）：先试世界内控制条，再走与桌面一致的拾取链 */
  function onXRSelect(c) {
    if (disposed || !renderer.xr.isPresenting) return;
    c.getWorldQuaternion(_xrQ1);
    xrRay.ray.origin.setFromMatrixPosition(c.matrixWorld);
    xrRay.ray.direction.set(0, 0, -1).applyQuaternion(_xrQ1).normalize();
    if (vrBar.visible) {
      const barHits = xrRay.intersectObjects(vrBar.children, false);
      if (barHits.length) {
        const obj = barHits[0].object;
        if (obj === vrVoiceBtn) { vrToggleRec(); return; }
        if (obj === vrFollowBtn) { toggleFollow(); return; }
        if (obj === vrExitBtn) { doExit(); return; }
        return;
      }
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
        /* 右摇杆 Y = 翻历史（无 dom-overlay 时的浏览手段；推上=看更早，与桌面 ←/↑ 同向） */
        if (Math.abs(ay) > 0.15) scrollBy(-ay * dt * 2.2);
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

  /* DOM HUD 按钮初始文字（避免与 refreshNativeBtn 等声明顺序依赖） */
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
      applyRoomScene((info && info.scene) || null); /* 房主改场景后房内热切换 */
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
    updateControls(dt);
    if (follow && hArc > 0.001) {
      hArc *= Math.exp(-dt * 4);
      if (hArc < 0.005) hArc = 0;
      syncThumb();
    }
    layout(dt);
    try { avatars.update(dt); } catch (err) {} /* 形象呼吸/浮动/表情推进（需求 4.4） */
    updateSpatialVoices(); /* 语音声源跟随站位/面板位 + 口型推进（任务 9） */
    /* 接近已加载的最旧一端 → 向前分页回填（一次性拉全，需求 2.7） */
    const bandH = BAND_HI - FLOOR_Y;
    if (strip.length && !historyEnd && hArc + bandH > totalH - 1.2 && (totalH < bandH + 1 || hArc > 0)) {
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
    scroll: () => ({ hArc: +hArc.toFixed(2), max: +maxScroll().toFixed(2), totalH: +totalH.toFixed(2), follow }),
    scrollBy: (d) => scrollBy(d), /* 与摇杆/←→ 同一条滚动路径（无头显时验证用） */
    nativeGroup: () => native.group,
    avatarGroup: () => avatars.group,
    avatarApi: () => avatars, /* setExpression/wave/positionOf（ARKit52 驱动接口验证用） */
    /* VR 控制条与语音（无头显时验证用：显示控制条 + 直接驱动同一批处理器） */
    showVRBar: (on) => { vrBar.visible = !!on; },
    vrBarGroup: () => vrBar,
    vrBarClick: (act) => {
      if (act === "voice") vrToggleRec();
      else if (act === "follow") toggleFollow();
      else if (act === "exit") doExit();
    },
    vrRecStart: () => startVRRec(),
    vrRecStop: () => stopVRRec(),
    vrRecState: () => (vrRec ? { recording: true, ms: Date.now() - vrRec.startMs } : { recording: false }),
    vrHint: () => vrHintMsg, /* 世界内提示最近一条文本（画布内容不可读，验证用） */
    childScale: (i) => {
      const m = panels.group.children[i || 0];
      return m ? [+m.scale.x.toFixed(3), +m.scale.y.toFixed(3)] : null;
    },
    room: () => (roomSystem ? { id: roomSystem.sceneId, seats: roomSystem.seats.length } : null),
    roomGroup: () => (roomSystem ? roomSystem.group : null),
    seatOf: (name) => avatars.positionOf(name),
    mem: () => ({ geometries: renderer.info.memory.geometries, textures: renderer.info.memory.textures }),
  };

  /* ---------- 退出与释放（需求 7.4） ---------- */

  /* 退出时全量释放。统一走 disposeObjectTree——此处原实现只释放 material.map，
     漏了 normalMap/emissiveMap 等贴图，反复进出 3D 会慢慢涨内存。 */
  function disposeScene() {
    disposeObjectTree(scene);
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
    stopAsr(); /* 语音识别随退出终止，避免麦克风指示灯残留 */
    killVRRec(); /* VR 侧握键录音同理 */
    if (resizeTimer) clearTimeout(resizeTimer);
    focus = null;
    for (const id of Array.from(spatialVoices.keys())) dropSpatialVoice(id); /* 空间音频摘除（需求 7.4） */
    try { ctx.setVoiceSpatial(null); } catch (e) {}
    for (const id of Array.from(chatModels.keys())) removeChatModel(id);
    if (roomSystem) { try { roomSystem.dispose(); } catch (e) {} roomSystem = null; }
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
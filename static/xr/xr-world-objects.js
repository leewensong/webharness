/* 共同文件的非模型世界对象（需求 13/14）：图片盒子 + 媒体播放器。
   与 xr-files.js 的世界摆放层同一约定：返回的 object 以几何中心为原点，box 为本地包围盒
   （最长边 1），holder 的位姿/缩放由摆放层统一套用。播放器交互参考 FPP：屏幕/球面 + 控制条，
   支持 平面 / VR180 / VR360 投影、2D / 左右(SBS) / 上下(OU) 立体、以及更换片源 URL；
   播放状态与投影/立体设置都是本地状态，不写服务器、不跨端同步。

   立体按眼分层：three 的 WebXRManager 让左眼相机看不到 layer 2、右眼相机看不到 layer 1
   （three.module.js 注释：1 = left, 2 = right），所以左眼网格放 layer 1、右眼放 layer 2，
   非沉浸式时退回单画面（mono，layer 0）。 */

import * as THREE from "three";

const TEX_MAX = 2048;
const FRAME_COLOR = 0x2a3447;

/* ---------- 图片盒子 ---------- */

function fitBitmap(bmp) {
  const k = Math.min(1, TEX_MAX / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(bmp.width * k));
  c.height = Math.max(1, Math.round(bmp.height * k));
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return c;
}

/* 图片 → 薄盒（相框）：正反两面贴图、侧边中性色；长宽按原图比例、最长边 1 */
export async function buildPicture(blob) {
  const bmp = await createImageBitmap(blob);
  let canvas;
  try { canvas = fitBitmap(bmp); } finally { try { bmp.close(); } catch (e) {} }
  const aspect = canvas.width / canvas.height;
  const w = aspect >= 1 ? 1 : aspect;
  const h = aspect >= 1 ? 1 / aspect : 1;
  const d = 0.03;
  const front = new THREE.CanvasTexture(canvas);
  front.colorSpace = THREE.SRGBColorSpace;
  front.anisotropy = 4;
  const back = front.clone();
  back.wrapS = THREE.RepeatWrapping;
  back.repeat.x = -1;
  back.needsUpdate = true;
  const edge = new THREE.MeshStandardMaterial({ color: FRAME_COLOR, roughness: 0.7 });
  const mats = [
    edge, edge, edge, edge,
    new THREE.MeshBasicMaterial({ map: front, toneMapped: false }),
    new THREE.MeshBasicMaterial({ map: back, toneMapped: false }),
  ];
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mats);
  return { object: mesh, box: new THREE.Box3(new THREE.Vector3(-w / 2, -h / 2, -d / 2), new THREE.Vector3(w / 2, h / 2, d / 2)) };
}

/* ---------- 媒体播放器 ---------- */

const FW = 1.0, FH = 0.76, FD = 0.04;          /* 外框 */
const SW = 0.94, SH = 0.46;                     /* 平面屏幕 16:9 */
const CW = 0.94, CH = 0.20;                     /* 控制条（两行：按钮 + 进度） */
const SCREEN_Y = FH / 2 - 0.02 - SH / 2;
const CTRL_Y = -FH / 2 + 0.02 + CH / 2;
const DOME_R = 2.4;                             /* 球面半径（本地单位；scale≈1 时约 2.4m） */
const CTRL_PX_W = 1024, CTRL_PX_H = Math.round(CTRL_PX_W * CH / CW);
const BTN_ROW_H = 100, BTN_GAP = 6, BTN_W = 112, BTN_X0 = 12;
const BAR_X0 = 12, BAR_X1 = 900, TIME_X = 916;

const PROJECTIONS = ["flat", "180", "360"];
const STEREOS = ["mono", "sbs", "ou"];

/* uvRect = [offsetX, offsetY, repeatX, repeatY]；按投影/立体换 */
function uvRectFor(stereo, eye) {
  if (stereo === "sbs") return eye === "left" ? [0, 0, 0.5, 1] : eye === "right" ? [0.5, 0, 0.5, 1] : [0, 0, 1, 1];
  if (stereo === "ou") return eye === "left" ? [0, 0.5, 1, 0.5] : eye === "right" ? [0, 0, 1, 0.5] : [0, 0, 1, 1];
  return [0, 0, 1, 1];
}

function fmtTime(s) {
  if (!isFinite(s) || s < 0) return "--:--";
  s = Math.floor(s);
  const m = Math.floor(s / 60), r = s % 60;
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

function isHlsUrl(url) {
  return /mpegurl/i.test(url || "") || /\.m3u8(\?|#|$)/i.test(url || "");
}

let hlsLoad = null;
function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLoad) {
    hlsLoad = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/hls/hls.light.min.js";
      s.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error("hls.js missing")));
      s.onerror = () => { hlsLoad = null; reject(new Error("hls.js load failed")); };
      document.head.appendChild(s);
    });
  }
  return hlsLoad;
}

/* 视频纹理材质：单一 VideoTexture + 每个网格自己的 uvRect 裁切（左右/上下半屏）。
   颜色走 three 的 colorspace_fragment，与内置材质一致。 */
function videoMaterial(texture, rect) {
  return new THREE.ShaderMaterial({
    uniforms: {
      map: { value: texture },
      uvRect: { value: new THREE.Vector4(rect[0], rect[1], rect[2], rect[3]) },
      opacity: { value: 1 },
    },
    vertexShader: /* glsl */`
      varying vec2 vUv;
      void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: /* glsl */`
      uniform sampler2D map;
      uniform vec4 uvRect;
      uniform float opacity;
      varying vec2 vUv;
      void main() {
        vec2 uv = uvRect.xy + vUv * uvRect.zw;
        gl_FragColor = vec4(texture2D(map, uv).rgb, opacity);
        #include <colorspace_fragment>
      }`,
    toneMapped: false,
  });
}

/* opts: { file, fetchBlob(url), fetchText(url), token(), hud?, t?, flash?, camera?, isPresenting?(),
   storageKey? }。返回 { object, box, pickControls(uv), tick(dt), dispose(), _dbg }。
   媒体缺省静音、不自动播放；投影/立体/自定义片源按 storageKey 存 localStorage（本地，不共享）。 */
export async function createMediaPlayer(opts) {
  const file = opts.file;
  const t = opts.t || ((k) => k);
  const flash = opts.flash || (() => {});
  const audioOnly = file.kind === "audio";
  const video = document.createElement("video");
  video.playsInline = true;
  video.muted = true;
  video.preload = "auto";
  video.crossOrigin = "anonymous";

  const storageKey = opts.storageKey || ("wh.xrplayer." + (file.id || "x"));
  const mode = { proj: "flat", stereo: "mono", src: "" }; /* src="" = 文件自带片源 */
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
    if (PROJECTIONS.includes(saved.proj)) mode.proj = saved.proj;
    if (STEREOS.includes(saved.stereo)) mode.stereo = saved.stereo;
    if (typeof saved.src === "string") mode.src = saved.src;
  } catch (e) {}
  function saveMode() {
    try { localStorage.setItem(storageKey, JSON.stringify(mode)); } catch (e) {}
  }

  let objUrl = null, hls = null;
  function cleanupSource() {
    if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
    try { video.pause(); } catch (e) {}
    try { video.removeAttribute("src"); video.load(); } catch (e) {}
    if (objUrl) { URL.revokeObjectURL(objUrl); objUrl = null; }
  }

  /* 片源 = 文件内容（默认）或自定义 URL；HLS 优先原生，否则 hls.js */
  async function attachSource(src) {
    cleanupSource();
    const url = src || file.contentUrl;
    if (isHlsUrl(url)) {
      const absUrl = new URL(url, location.href).href;
      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        /* 原生 HLS 带不了 Authorization：文件源鉴权取回播放列表；外链直接给地址 */
        if (url === file.contentUrl) {
          const text = await opts.fetchText(url);
          objUrl = URL.createObjectURL(new Blob([text], { type: "application/vnd.apple.mpegurl" }));
          video.src = objUrl;
        } else {
          video.src = absUrl;
        }
      } else {
        const Hls = await loadHls();
        if (!Hls.isSupported()) throw new Error("HLS not supported");
        hls = new Hls({
          xhrSetup: (xhr, reqUrl) => {
            const tk = opts.token && opts.token();
            if (tk && new URL(reqUrl, location.href).origin === location.origin && /\/api\//.test(reqUrl)) {
              xhr.setRequestHeader("Authorization", "Bearer " + tk);
            }
          },
        });
        hls.loadSource(absUrl);
        hls.attachMedia(video);
      }
    } else if (url === file.contentUrl || url.startsWith("/")) {
      const blob = await opts.fetchBlob(url);
      objUrl = URL.createObjectURL(blob);
      video.src = objUrl;
    } else {
      video.src = url;
    }
    drawControls();
  }

  const root = new THREE.Group();
  const frameMat = new THREE.MeshStandardMaterial({ color: FRAME_COLOR, roughness: 0.6, metalness: 0.1 });
  const frame = new THREE.Mesh(new THREE.BoxGeometry(FW, FH, FD), frameMat);
  root.add(frame);

  /* 纹理：视频 → VideoTexture（单一，按 uvRect 裁切给左右眼）；音频 → 封面卡 */
  let screenTex;
  if (audioOnly) {
    const c = document.createElement("canvas");
    c.width = 960; c.height = 540;
    const g = c.getContext("2d");
    const grad = g.createLinearGradient(0, 0, 960, 540);
    grad.addColorStop(0, "#1b2740"); grad.addColorStop(1, "#33264a");
    g.fillStyle = grad; g.fillRect(0, 0, 960, 540);
    g.textAlign = "center";
    g.font = "160px system-ui, sans-serif";
    g.fillText("🎵", 480, 280);
    g.fillStyle = "#e6edf7";
    g.font = "34px system-ui, sans-serif";
    let name = file.name || "";
    while (g.measureText(name).width > 880 && name.length > 2) name = name.slice(0, -2);
    g.fillText(name, 480, 420);
    screenTex = new THREE.CanvasTexture(c);
    screenTex.colorSpace = THREE.SRGBColorSpace;
  } else {
    screenTex = new THREE.VideoTexture(video);
    screenTex.colorSpace = THREE.SRGBColorSpace;
    screenTex.minFilter = THREE.LinearFilter;
    screenTex.magFilter = THREE.LinearFilter;
  }

  function makeScreen(eye) {
    const mat = audioOnly
      ? new THREE.MeshBasicMaterial({ map: screenTex, toneMapped: false })
      : videoMaterial(screenTex, uvRectFor(mode.stereo, eye));
    const m = new THREE.Mesh(new THREE.PlaneGeometry(SW, SH), mat);
    m.position.set(0, SCREEN_Y, FD / 2 + 0.001);
    if (eye === "left") m.layers.set(1);
    else if (eye === "right") m.layers.set(2);
    return m;
  }
  const screenMono = makeScreen("mono");
  const screenL = makeScreen("left");
  const screenR = makeScreen("right");
  root.add(screenMono, screenL, screenR);

  /* 球面投影：VR180 取前半球，VR360 全球；观看者走进球心时球心跟随头部 */
  const domeGroup = new THREE.Group();
  domeGroup.position.set(0, SCREEN_Y, 0);
  root.add(domeGroup);
  const domeGeo360 = new THREE.SphereGeometry(DOME_R, 64, 40);
  const domeGeo180 = new THREE.SphereGeometry(DOME_R, 48, 32, 0, Math.PI);
  const domes = {};
  for (const eye of ["mono", "left", "right"]) {
    const make = (geo) => {
      const mat = audioOnly
        ? new THREE.MeshBasicMaterial({ map: screenTex, side: THREE.BackSide, toneMapped: false, depthWrite: false })
        : videoMaterial(screenTex, uvRectFor(mode.stereo, eye));
      mat.side = THREE.BackSide;
      mat.depthWrite = false;
      const m = new THREE.Mesh(geo, mat);
      m.raycast = () => {}; /* 球面不参与拾取，避免点到球面就进入摆放调整 */
      if (eye === "left") m.layers.set(1);
      else if (eye === "right") m.layers.set(2);
      return m;
    };
    domes[eye] = { 180: make(domeGeo180), 360: make(domeGeo360) };
    domeGroup.add(domes[eye]["180"], domes[eye]["360"]);
  }

  function applyModes() {
    /* 平面与球面的裁切都随立体模式变化（单一入口，按钮与调试路径共用） */
    for (const eye of ["mono", "left", "right"]) {
      if (audioOnly) continue;
      const rect = uvRectFor(mode.stereo, eye);
      const planes = { mono: screenMono, left: screenL, right: screenR };
      if (planes[eye].material.uniforms) {
        planes[eye].material.uniforms.uvRect.value.set(rect[0], rect[1], rect[2], rect[3]);
      }
      for (const k of ["180", "360"]) {
        const m = domes[eye][k];
        if (m.material.uniforms) m.material.uniforms.uvRect.value.set(rect[0], rect[1], rect[2], rect[3]);
      }
    }
    refreshVisibility();
  }

  function refreshVisibility() {
    const presenting = !!(opts.isPresenting && opts.isPresenting());
    const stereo = mode.stereo !== "mono";
    const flat = mode.proj === "flat";
    const eyeSplit = presenting && stereo;
    screenMono.visible = flat && !eyeSplit;
    screenL.visible = flat && eyeSplit;
    screenR.visible = flat && eyeSplit;
    const projKey = mode.proj === "180" ? "180" : "360";
    for (const eye of ["mono", "left", "right"]) {
      const want = !flat && (eye === "mono" ? !eyeSplit : eyeSplit);
      domes[eye]["180"].visible = want && projKey === "180";
      domes[eye]["360"].visible = want && projKey === "360";
    }
  }

  /* 进球面中心时球心跟随头部（FPP 式全景），离开则回到播放器位置 */
  const _cam = new THREE.Vector3(), _rootPos = new THREE.Vector3(), _q = new THREE.Quaternion(), _e = new THREE.Euler();
  function updateDomeFollow() {
    if (mode.proj === "flat" || !opts.camera) return;
    const camera = opts.camera;
    const scale = root.parent ? root.parent.scale.x : 1;
    camera.getWorldPosition(_cam);
    root.getWorldPosition(_rootPos);
    if (_cam.distanceTo(_rootPos) < DOME_R * scale * 0.92) {
      domeGroup.position.copy(root.worldToLocal(_cam.clone()));
      camera.getWorldQuaternion(_q);
      _e.setFromQuaternion(_q, "YXZ");
      const camYaw = _e.y;
      root.getWorldQuaternion(_q);
      _e.setFromQuaternion(_q, "YXZ");
      domeGroup.rotation.y = camYaw - _e.y;
    } else {
      domeGroup.position.set(0, SCREEN_Y, 0);
      domeGroup.rotation.y = 0;
    }
  }

  /* ---------- 控制条 ---------- */

  const cc = document.createElement("canvas");
  cc.width = CTRL_PX_W; cc.height = CTRL_PX_H;
  const cg = cc.getContext("2d");
  const ctrlTex = new THREE.CanvasTexture(cc);
  ctrlTex.colorSpace = THREE.SRGBColorSpace;
  const ctrlMat = new THREE.MeshBasicMaterial({ map: ctrlTex, toneMapped: false });
  const controls = new THREE.Mesh(new THREE.PlaneGeometry(CW, CH), ctrlMat);
  controls.position.set(0, CTRL_Y, FD / 2 + 0.001);
  controls.userData.mediaPart = "controls";
  root.add(controls);

  let failed = false;
  video.addEventListener("error", () => { failed = true; drawControls(); });
  video.addEventListener("loadedmetadata", () => drawControls());

  function btnList() {
    const icons = ["back", "play", "fwd", "mute", "loop"];
    const list = icons.map((a) => ({ act: a, label: {
      back: "⏪10", play: video.paused ? "▶" : "⏸", fwd: "10⏩",
      mute: video.muted ? "🔇" : "🔊", loop: "🔁",
    }[a], on: (a === "loop" && video.loop) || (a === "mute" && !video.muted) }));
    if (!audioOnly) {
      list.push({ act: "proj", label: { flat: t("xrPlayerProjFlat"), 180: t("xrPlayerProj180"), 360: t("xrPlayerProj360") }[mode.proj], on: mode.proj !== "flat" });
      list.push({ act: "stereo", label: { mono: t("xrPlayerStereoMono"), sbs: t("xrPlayerStereoSbs"), ou: t("xrPlayerStereoOu") }[mode.stereo], on: mode.stereo !== "mono" });
    }
    list.push({ act: "src", label: t("xrPlayerSrc"), on: !!mode.src });
    return list;
  }

  function drawControls() {
    const g = cg, H = CTRL_PX_H;
    g.clearRect(0, 0, CTRL_PX_W, H);
    g.fillStyle = "#121a28";
    g.fillRect(0, 0, CTRL_PX_W, H);
    btnList().forEach((b, i) => {
      const x = BTN_X0 + i * (BTN_W + BTN_GAP);
      g.fillStyle = b.on ? "rgba(91,140,255,0.8)" : "rgba(46,64,96,0.85)";
      g.beginPath();
      if (g.roundRect) g.roundRect(x, 8, BTN_W, BTN_ROW_H - 14, 14); else g.rect(x, 8, BTN_W, BTN_ROW_H - 14);
      g.fill();
      g.fillStyle = "#e6edf7";
      g.font = /^[\x00-\x7F]+$/.test(b.label) ? "30px system-ui, sans-serif" : "34px system-ui, sans-serif";
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText(b.label, x + BTN_W / 2, 8 + (BTN_ROW_H - 14) / 2 + 2);
    });
    const dur = video.duration, cur = video.currentTime;
    const frac = isFinite(dur) && dur > 0 ? Math.min(1, cur / dur) : 0;
    const by = BTN_ROW_H + (H - BTN_ROW_H) / 2;
    g.fillStyle = "rgba(255,255,255,0.18)";
    g.fillRect(BAR_X0, by - 6, BAR_X1 - BAR_X0, 12);
    g.fillStyle = "#5b8cff";
    g.fillRect(BAR_X0, by - 6, (BAR_X1 - BAR_X0) * frac, 12);
    g.beginPath();
    g.arc(BAR_X0 + (BAR_X1 - BAR_X0) * frac, by, 14, 0, Math.PI * 2);
    g.fill();
    g.fillStyle = failed ? "#e08a8a" : "#c6d2e3";
    g.font = "28px system-ui, sans-serif";
    g.textAlign = "right";
    g.fillText(failed ? "✕ error" : `${fmtTime(cur)} / ${fmtTime(dur)}`, CTRL_PX_W - 12, by + 1);
    g.textAlign = "left";
    g.textBaseline = "alphabetic";
    ctrlTex.needsUpdate = true;
  }

  function act(name) {
    try {
      if (name === "play") { if (video.paused) video.play().catch(() => {}); else video.pause(); }
      else if (name === "back") video.currentTime = Math.max(0, video.currentTime - 10);
      else if (name === "fwd") video.currentTime = Math.min(isFinite(video.duration) ? video.duration : video.currentTime + 10, video.currentTime + 10);
      else if (name === "mute") video.muted = !video.muted;
      else if (name === "loop") video.loop = !video.loop;
      else if (name === "proj") {
        if (audioOnly) return;
        mode.proj = PROJECTIONS[(PROJECTIONS.indexOf(mode.proj) + 1) % PROJECTIONS.length];
        saveMode(); applyModes();
      }
      else if (name === "stereo") {
        if (audioOnly) return;
        mode.stereo = STEREOS[(STEREOS.indexOf(mode.stereo) + 1) % STEREOS.length];
        saveMode(); applyModes();
      }
      else if (name === "src") { openSourceEditor(); return; }
    } catch (e) {}
    drawControls();
  }

  /* 控制条 uv → 按钮 / 进度条；命中返回 true（已消费，不进入摆放调整） */
  function pickControls(uv) {
    const px = uv.x * CTRL_PX_W, py = (1 - uv.y) * CTRL_PX_H;
    const list = btnList();
    if (py <= BTN_ROW_H) {
      for (let i = 0; i < list.length; i++) {
        const x = BTN_X0 + i * (BTN_W + BTN_GAP);
        if (px >= x && px <= x + BTN_W) { act(list[i].act); return true; }
      }
      return true;
    }
    if (px >= BAR_X0 - 10 && px <= BAR_X1 + 10 && isFinite(video.duration) && video.duration > 0) {
      video.currentTime = THREE.MathUtils.clamp((px - BAR_X0) / (BAR_X1 - BAR_X0), 0, 1) * video.duration;
      drawControls();
    }
    return true; /* 控制条空白处也吃掉点击，避免误入调整 */
  }

  /* ---------- 更换片源（HUD DOM 输入，沉浸式走系统键盘） ---------- */

  let srcRowEl = null;
  function buildSrcRow() {
    if (!opts.hud) return null;
    const row = document.createElement("div");
    row.className = "xr-player-src hidden";
    row.innerHTML = `
      <div class="xr-ps-title"></div>
      <input type="url" inputmode="url" spellcheck="false" />
      <div class="xr-ps-btns">
        <button type="button" class="xr-btn" data-a="apply"></button>
        <button type="button" class="xr-btn" data-a="reset"></button>
        <button type="button" class="xr-btn" data-a="cancel"></button>
        <span class="xr-ps-hint"></span>
      </div>`;
    const style = document.createElement("style");
    style.textContent = `
      #xrRoot .xr-player-src { position: absolute; bottom: 58px; left: 50%; transform: translateX(-50%);
        width: min(620px, 78vw); display: flex; flex-direction: column; gap: 6px; z-index: 4; }
      #xrRoot .xr-player-src.hidden { display: none; }
      #xrRoot .xr-player-src .xr-ps-title { color: #dfe8f4; font-size: 13px; }
      #xrRoot .xr-player-src input { width: 100%; border: 1px solid #2c3d58; border-radius: 10px;
        background: rgba(16,22,34,0.92); color: #dfe8f4; font: 13px/1.5 "SF Mono", ui-monospace, monospace;
        padding: 8px 10px; outline: none; }
      #xrRoot .xr-player-src input:focus { border-color: #5b8cff; }
      #xrRoot .xr-player-src .xr-ps-btns { display: flex; gap: 8px; align-items: center; }
      #xrRoot .xr-player-src .xr-ps-hint { color: #8fa3bd; font-size: 12px; flex: 1; text-align: right; }`;
    row.prepend(style);
    row.querySelector(".xr-ps-title").textContent = t("xrPlayerSrcTitle");
    const input = row.querySelector("input");
    input.placeholder = t("xrPlayerSrcPlaceholder");
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); closeSourceEditor(); }
      if (e.key === "Enter") { e.preventDefault(); applySourceEditor(); }
    });
    input.addEventListener("keyup", (e) => e.stopPropagation());
    row.querySelector('[data-a="apply"]').textContent = t("xrPlayerSrcApply");
    row.querySelector('[data-a="reset"]').textContent = t("xrPlayerSrcReset");
    row.querySelector('[data-a="cancel"]').textContent = t("xrPlayerSrcCancel");
    row.querySelector('[data-a="apply"]').addEventListener("click", () => applySourceEditor());
    row.querySelector('[data-a="reset"]').addEventListener("click", () => { input.value = ""; applySourceEditor(); });
    row.querySelector('[data-a="cancel"]').addEventListener("click", () => closeSourceEditor());
    opts.hud.appendChild(row);
    return row;
  }

  function openSourceEditor() {
    if (!srcRowEl) srcRowEl = buildSrcRow();
    if (!srcRowEl) { flash(t("xrPlayerSrcNeeds2d")); return; }
    srcRowEl.querySelector("input").value = mode.src || "";
    srcRowEl.classList.remove("hidden");
    try { srcRowEl.querySelector("input").focus(); } catch (e) {}
  }
  function closeSourceEditor() {
    if (srcRowEl) srcRowEl.classList.add("hidden");
  }
  async function applySourceEditor() {
    if (!srcRowEl) return;
    const hint = srcRowEl.querySelector(".xr-ps-hint");
    const raw = srcRowEl.querySelector("input").value.trim();
    if (raw && !/^https?:\/\//i.test(raw) && !raw.startsWith("/")) {
      hint.textContent = t("xrPlayerSrcInvalid");
      return;
    }
    hint.textContent = "";
    try {
      mode.src = raw;
      saveMode();
      await attachSource(mode.src);
      flash(raw ? t("xrPlayerSrcApplied") : t("xrPlayerSrcResetDone"));
      closeSourceEditor();
    } catch (err) {
      hint.textContent = (err && err.message) || t("xrFileLoadFail");
    }
    drawControls();
  }

  /* ---------- 生命周期 ---------- */

  let acc = 0;
  function tick(dt) {
    updateDomeFollow();
    refreshVisibility();
    if (video.paused) return;
    acc += dt;
    if (acc >= 0.25) { acc = 0; drawControls(); }
  }

  function pause() { try { video.pause(); } catch (e) {} drawControls(); }

  function dispose() {
    try { video.pause(); } catch (e) {}
    if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
    try { video.removeAttribute("src"); video.load(); } catch (e) {}
    if (objUrl) { URL.revokeObjectURL(objUrl); objUrl = null; }
    if (srcRowEl) { try { srcRowEl.remove(); } catch (e) {} srcRowEl = null; }
    screenTex.dispose();
    ctrlTex.dispose();
  }

  await attachSource(mode.src);
  applyModes();

  return {
    object: root,
    box: new THREE.Box3(new THREE.Vector3(-FW / 2, -FH / 2, -FD / 2), new THREE.Vector3(FW / 2, FH / 2, FD / 2)),
    controls, pickControls, tick, pause, dispose,
    _dbg: {
      video, act, mode,
      meshes: { screenMono, screenL, screenR, domeGroup, domes },
      setProjection: (p) => { if (PROJECTIONS.includes(p)) { mode.proj = p; saveMode(); applyModes(); drawControls(); } },
      setStereo: (s) => { if (STEREOS.includes(s)) { mode.stereo = s; saveMode(); applyModes(); drawControls(); } },
      setSource: async (src) => { mode.src = src || ""; saveMode(); await attachSource(mode.src); drawControls(); },
      refreshVisibility,
    },
  };
}

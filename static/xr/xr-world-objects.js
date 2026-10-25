/* 共同文件的非模型世界对象（需求 13）：图片盒子 + 媒体播放器。
   与 xr-files.js 的世界摆放层同一约定：返回的 object 以几何中心为原点，box 为本地包围盒
   （最长边 1），holder 的位姿/缩放由摆放层统一套用。播放器交互参考 FPP：屏幕 + 控制条
   （⏪10 ▶/⏸ 10⏩ 🔇 🔁 + 进度条），播放状态纯本地、不跨端同步。 */

import * as THREE from "three";

const TEX_MAX = 2048;
const FRAME_COLOR = 0x2a3447;

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

const FW = 1.0, FH = 0.72, FD = 0.04;          /* 外框 */
const SW = 0.94, SH = 0.53;                     /* 屏幕 16:9 */
const CW = 0.94, CH = 0.12;                     /* 控制条 */
const SCREEN_Y = FH / 2 - 0.03 - SH / 2;
const CTRL_Y = -FH / 2 + 0.025 + CH / 2;
const CTRL_PX_W = 1024, CTRL_PX_H = Math.round(CTRL_PX_W * CH / CW);
const BTN = ["back", "play", "fwd", "mute", "loop"];
const BTN_W = 104, BTN_GAP = 8, BTN_X0 = 12;
const BAR_X0 = BTN_X0 + BTN.length * (BTN_W + BTN_GAP) + 10;
const BAR_X1 = CTRL_PX_W - 150;

function fmtTime(s) {
  if (!isFinite(s) || s < 0) return "--:--";
  s = Math.floor(s);
  const m = Math.floor(s / 60), r = s % 60;
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

function isHls(file) {
  return /mpegurl/i.test(file.mime || "") || /\.m3u8$/i.test(file.name || "");
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

/* opts: { file, fetchBlob(url), fetchText(url), token() }。返回 { object, box, part(hitObject),
   pickControls(uv) → bool, tick(dt), dispose() }。媒体缺省静音、不自动播放。 */
export async function createMediaPlayer(opts) {
  const file = opts.file;
  const audioOnly = file.kind === "audio";
  const video = document.createElement("video");
  video.playsInline = true;
  video.muted = true;
  video.preload = "auto";
  video.crossOrigin = "anonymous";
  let objUrl = null, hls = null;

  if (isHls(file)) {
    const absUrl = new URL(file.contentUrl, location.href).href;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      /* 原生 HLS 带不了 Authorization：鉴权取回播放列表，分片按列表里的地址直接访问 */
      const text = await opts.fetchText(file.contentUrl);
      objUrl = URL.createObjectURL(new Blob([text], { type: "application/vnd.apple.mpegurl" }));
      video.src = objUrl;
    } else {
      const Hls = await loadHls();
      if (!Hls.isSupported()) throw new Error("HLS not supported");
      hls = new Hls({
        xhrSetup: (xhr, url) => {
          const tk = opts.token && opts.token();
          if (tk && new URL(url, location.href).origin === location.origin && /\/api\//.test(url)) {
            xhr.setRequestHeader("Authorization", "Bearer " + tk);
          }
        },
      });
      hls.loadSource(absUrl);
      hls.attachMedia(video);
    }
  } else {
    const blob = await opts.fetchBlob(file.contentUrl);
    objUrl = URL.createObjectURL(blob);
    video.src = objUrl;
  }

  const root = new THREE.Group();
  const frameMat = new THREE.MeshStandardMaterial({ color: FRAME_COLOR, roughness: 0.6, metalness: 0.1 });
  const frame = new THREE.Mesh(new THREE.BoxGeometry(FW, FH, FD), frameMat);
  root.add(frame);

  /* 屏幕：视频 → VideoTexture；音频 → 封面卡 */
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
  } else {
    screenTex = new THREE.VideoTexture(video);
  }
  screenTex.colorSpace = THREE.SRGBColorSpace;
  const screenMat = new THREE.MeshBasicMaterial({ map: screenTex, toneMapped: false });
  const screen = new THREE.Mesh(new THREE.PlaneGeometry(SW, SH), screenMat);
  screen.position.set(0, SCREEN_Y, FD / 2 + 0.001);
  root.add(screen);

  /* 控制条 */
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

  function drawControls() {
    const g = cg, H = CTRL_PX_H;
    g.clearRect(0, 0, CTRL_PX_W, H);
    g.fillStyle = "#121a28";
    g.fillRect(0, 0, CTRL_PX_W, H);
    const labels = {
      back: "⏪10", play: video.paused ? "▶" : "⏸", fwd: "10⏩",
      mute: video.muted ? "🔇" : "🔊", loop: "🔁",
    };
    BTN.forEach((b, i) => {
      const x = BTN_X0 + i * (BTN_W + BTN_GAP);
      const on = (b === "loop" && video.loop) || (b === "mute" && !video.muted);
      g.fillStyle = on ? "rgba(91,140,255,0.8)" : "rgba(46,64,96,0.85)";
      g.beginPath();
      if (g.roundRect) g.roundRect(x, 14, BTN_W, H - 28, 14); else g.rect(x, 14, BTN_W, H - 28);
      g.fill();
      g.fillStyle = "#e6edf7";
      g.font = "38px system-ui, sans-serif";
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText(labels[b], x + BTN_W / 2, H / 2 + 2);
    });
    const dur = video.duration, cur = video.currentTime;
    const frac = isFinite(dur) && dur > 0 ? Math.min(1, cur / dur) : 0;
    const by = H / 2;
    g.fillStyle = "rgba(255,255,255,0.18)";
    g.fillRect(BAR_X0, by - 6, BAR_X1 - BAR_X0, 12);
    g.fillStyle = "#5b8cff";
    g.fillRect(BAR_X0, by - 6, (BAR_X1 - BAR_X0) * frac, 12);
    g.beginPath();
    g.arc(BAR_X0 + (BAR_X1 - BAR_X0) * frac, by, 14, 0, Math.PI * 2);
    g.fill();
    g.fillStyle = failed ? "#e08a8a" : "#c6d2e3";
    g.font = "28px system-ui, sans-serif";
    g.textAlign = "left";
    g.fillText(failed ? "✕ error" : `${fmtTime(cur)} / ${fmtTime(dur)}`, BAR_X1 + 14, by + 1);
    g.textBaseline = "alphabetic";
    ctrlTex.needsUpdate = true;
  }
  drawControls();

  function act(name) {
    try {
      if (name === "play") { if (video.paused) video.play().catch(() => {}); else video.pause(); }
      else if (name === "back") video.currentTime = Math.max(0, video.currentTime - 10);
      else if (name === "fwd") video.currentTime = Math.min(isFinite(video.duration) ? video.duration : video.currentTime + 10, video.currentTime + 10);
      else if (name === "mute") video.muted = !video.muted;
      else if (name === "loop") video.loop = !video.loop;
    } catch (e) {}
    drawControls();
  }

  /* 控制条 uv → 按钮 / 进度条；命中返回 true（已消费，不进入摆放调整） */
  function pickControls(uv) {
    const px = uv.x * CTRL_PX_W, py = (1 - uv.y) * CTRL_PX_H;
    for (let i = 0; i < BTN.length; i++) {
      const x = BTN_X0 + i * (BTN_W + BTN_GAP);
      if (px >= x && px <= x + BTN_W && py >= 8 && py <= CTRL_PX_H - 8) { act(BTN[i]); return true; }
    }
    if (px >= BAR_X0 - 10 && px <= BAR_X1 + 10 && isFinite(video.duration) && video.duration > 0) {
      video.currentTime = THREE.MathUtils.clamp((px - BAR_X0) / (BAR_X1 - BAR_X0), 0, 1) * video.duration;
      drawControls();
      return true;
    }
    return true; /* 控制条空白处也吃掉点击，避免误入调整 */
  }

  let acc = 0;
  function tick(dt) {
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
    screenTex.dispose();
    ctrlTex.dispose();
  }

  return {
    object: root,
    box: new THREE.Box3(new THREE.Vector3(-FW / 2, -FH / 2, -FD / 2), new THREE.Vector3(FW / 2, FH / 2, FD / 2)),
    controls, pickControls, tick, pause, dispose,
    _dbg: { video, act },
  };
}

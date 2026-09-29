/* 房间共同文件 3D 端（需求 8 / 需求 10）：列表面板 + 分类型预览 + 世界摆放 + 文本编辑。
   面板为单槽位：列表 ↔ 预览共用一块 CanvasTexture 平面（世界内固定，不跟头）；
   世界摆放独立于面板常驻同步。轮询常驻低频（摆放同步 + 面板内容刷新共用一条数据流）。
   位姿约定：holder 内 model.position = -Box3中心（居中），pose.scale 作用于 holder，
   pose.position 即 holder 原点（摆放客户端算好底边落地）。所有客户端按同一约定渲染。
   架构不变量：权限只读 2D 合成结果（ctx.canEdit），不建第二套判断；3D 故障不影响 2D。 */

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const EDITABLE_KINDS = new Set(["markdown", "text", "svg"]);
const POLL_MS = 2500;
const SAVE_THROTTLE_MS = 500;
const MAX_WORLD_SCALE = 20;
const MEASURE_FONT = "13px 'SF Pro Text','PingFang SC','Noto Sans SC',sans-serif";

/* 列表面板（CSS px；纹理 ×2 设备像素） */
const LIST_W = 400, ROW_H = 52, LIST_HEAD = 46, LIST_ROWS = 8, LIST_PAD = 12;
/* 预览面板 */
const PV_W = 520, PV_HEAD = 46, PV_BAR = 60, PV_MAX_CONTENT = 900;
const KIND_ICON = {
  markdown: "📝", mermaid: "📊", text: "📄", svg: "🖼",
  image: "🖼", audio: "🎵", video: "🎬", model: "🧊", other: "📎",
};

export function createXRFiles(opts) {
  const t = opts.t || ((k) => k);
  const tf = opts.tf || ((k, v) => t(k));
  const scene = opts.scene;
  const camera = opts.camera;
  const api = opts.api;
  const token = opts.token || (() => null);
  const roomName = opts.roomName || (() => "");
  const canEdit = opts.canEdit || (() => false);
  const rasterizeDom = opts.rasterizeDom || null;
  const renderFileMarkdown = opts.renderFileMarkdown || null;
  const placeChatModel = opts.placeChatModel || null;
  const removeChatModel = opts.removeChatModel || null;
  const statusEl = opts.statusEl;
  const pxPerM = opts.pxPerM || 1.12 / 356;
  const disposeObjectTree = opts.disposeObjectTree || (() => {});
  const hud = opts.hud;

  const group = new THREE.Group();
  const worldGroup = new THREE.Group();
  group.add(worldGroup);
  let disposed = false;

  /* ---------- 数据 ---------- */

  let filesCache = [];
  let revision = -1;
  let pollTimer = null;
  const base = () => `/api/rooms/${encodeURIComponent(roomName())}/files`;

  async function fetchBlob(url) {
    const r = await fetch(url, { headers: token() ? { Authorization: "Bearer " + token() } : {} });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.blob();
  }
  async function fetchText(url) {
    const r = await fetch(url, { headers: token() ? { Authorization: "Bearer " + token() } : {} });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.text();
  }

  function relTime(sql) {
    if (!sql) return "";
    const d = new Date(String(sql).replace(" ", "T") + "Z");
    if (isNaN(d)) return "";
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return tf("relJustNow");
    if (s < 3600) return tf("relMinutesAgo", { n: Math.floor(s / 60) });
    if (s < 86400) return tf("relHoursAgo", { n: Math.floor(s / 3600) });
    return tf("relDaysAgo", { n: Math.floor(s / 86400) });
  }

  function fmtKB(n) {
    return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
  }

  /* ---------- 面板（单槽位：列表 ↔ 预览） ---------- */

  const canvas = document.createElement("canvas");
  const g2 = canvas.getContext("2d");
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const mat = new THREE.MeshBasicMaterial({ map: tex, toneMapped: false });
  const uiMesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mat);
  group.add(uiMesh);
  uiMesh.visible = false;

  /* 视频内容平面（VideoTexture 没法画进 CanvasTexture，单独一层浮在内容区） */
  const videoMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.MeshBasicMaterial({ color: 0x0b0f17, toneMapped: false })
  );
  group.add(videoMesh);
  videoMesh.visible = false;

  let view = "list";        // list | preview
  let open = false;         // 面板开关（勿与 window.open 撞名——必须显式声明）
  let curFile = null;
  let pv = null;            // 预览内容状态（分类型）
  let hotspots = [];        // [{x,y,w,h,act}] CSS px
  let listScroll = 0;
  let anchor = null;        // 打开时刻的位姿（世界内固定）

  function cssW() { return view === "list" ? LIST_W : PV_W; }
  function cssH() {
    if (view === "list") {
      const rows = Math.max(3, Math.min(filesCache.length, LIST_ROWS));
      return LIST_HEAD + rows * ROW_H + LIST_PAD * 2;
    }
    return PV_HEAD + (pv && pv.state === "ok" ? Math.min(pv.contentH, PV_MAX_CONTENT) : 240) + PV_BAR;
  }

  function contentRect() {
    const hCss = cssH();
    return { x: 0, y: PV_HEAD, w: cssW(), h: hCss - PV_HEAD - PV_BAR };
  }

  function placePanel() {
    const wCss = cssW(), hCss = cssH();
    const wWorld = wCss * pxPerM, hWorld = hCss * pxPerM;
    if (canvas.width !== wCss * 2 || canvas.height !== hCss * 2) {
      canvas.width = wCss * 2; canvas.height = hCss * 2;
    }
    uiMesh.scale.set(wWorld, hWorld, 1);
    if (anchor) {
      uiMesh.position.set(anchor.x, Math.max(hWorld / 2 + 0.25, anchor.y), anchor.z);
      uiMesh.rotation.y = anchor.rotY;
    }
    syncVideoMesh();
  }

  function syncVideoMesh() {
    if (!pv || pv.type !== "texture" || !pv.videoTex) { videoMesh.visible = false; return; }
    const r = contentRect();
    const wWorld = (r.w - 24) * pxPerM;
    const hWorldMax = (r.h - 12) * pxPerM;
    const vw = pv.video.videoWidth || 16, vh = pv.video.videoHeight || 9;
    let w = wWorld, h = w * (vh / vw);
    if (h > hWorldMax) { h = hWorldMax; w = h * (vw / vh); }
    videoMesh.scale.set(w, h, 1);
    videoMesh.position.set(uiMesh.position.x, uiMesh.position.y + (r.y + r.h / 2 - cssH() / 2) * pxPerM, uiMesh.position.z + 0.012);
    videoMesh.rotation.y = uiMesh.rotation.y;
    videoMesh.visible = true;
  }

  function computeAnchor() {
    const p = camera.getWorldPosition(new THREE.Vector3());
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.getWorldQuaternion(new THREE.Quaternion()));
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1);
    fwd.normalize();
    const d = 1.45;
    anchor = {
      x: p.x + fwd.x * d,
      z: p.z + fwd.z * d,
      y: p.y - 0.55,
      rotY: Math.atan2(p.x - (p.x + fwd.x * d), p.z - (p.z + fwd.z * d)),
    };
  }

  function btnRect(i, n) {
    const bw = 100, gap = 10, h = 44;
    const x = PV_W - LIST_PAD - (n - i) * bw - (n - 1 - i) * gap;
    return { x, y: cssH() - PV_BAR + 8, w: bw, h };
  }

  /* ---------- 绘制 ---------- */

  function roundRect(g, x, y, w, h, r) {
    g.beginPath();
    g.moveTo(x + r, y);
    g.lineTo(x + w - r, y);
    g.quadraticCurveTo(x + w, y, x + w, y + r);
    g.lineTo(x + w, y + h - r);
    g.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    g.lineTo(x + r, y + h);
    g.quadraticCurveTo(x, y + h, x, y + h - r);
    g.lineTo(x, y + r);
    g.quadraticCurveTo(x, y, x + r, y);
    g.closePath();
  }

  function drawPanel() {
    if (disposed || !open) return;
    const wCss = cssW(), hCss = cssH();
    if (canvas.width !== wCss * 2 || canvas.height !== hCss * 2) placePanel();
    g2.save();
    g2.scale(2, 2);
    g2.clearRect(0, 0, wCss, hCss);
    g2.fillStyle = "rgba(10,15,24,0.92)";
    roundRect(g2, 0.5, 0.5, wCss - 1, hCss - 1, 14);
    g2.fill();
    g2.lineWidth = 2;
    g2.strokeStyle = "rgba(120,160,220,0.45)";
    g2.stroke();
    hotspots = [];
    if (view === "list") drawList(wCss, hCss);
    else drawPreview(wCss, hCss);
    g2.restore();
    if (mat.map !== tex) { mat.map = tex; mat.needsUpdate = true; }
    tex.needsUpdate = true;
    syncVideoMesh();
  }

  function headerBtn(g, x, label) {
    g.fillStyle = "rgba(46,64,96,0.65)";
    roundRect(g, x, 8, 54, 30, 15);
    g.fill();
    g.fillStyle = "#dfe8f4";
    g.font = "14px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(label, x + 27, 24);
    g.textAlign = "left";
    g.textBaseline = "alphabetic";
    return { x, y: 8, w: 54, h: 30 };
  }

  function drawList(wCss, hCss) {
    const hs = headerBtn(g2, 12, "←");
    hs.act = "close";
    hotspots.push(hs);
    g2.fillStyle = "#dfe8f4";
    g2.font = "bold 16px system-ui, sans-serif";
    g2.fillText(t("xrFilesTitle"), 78, 24);
    g2.fillStyle = "#8fa3bd";
    g2.font = "12px system-ui, sans-serif";
    const n = filesCache.length;
    if (n) g2.fillText(tf("xrFilesCount", { n }), 78, 40);
    if (!n) {
      g2.fillStyle = "#7c90aa";
      g2.font = "13px system-ui, sans-serif";
      g2.textAlign = "center";
      g2.fillText(t("xrFilesEmpty"), wCss / 2, hCss / 2 + 4);
      g2.textAlign = "left";
      return;
    }
    const maxScroll = Math.max(0, n * ROW_H - (hCss - LIST_HEAD - LIST_PAD * 2));
    listScroll = Math.min(listScroll, maxScroll);
    g2.save();
    g2.beginPath();
    g2.rect(0, LIST_HEAD, wCss, hCss - LIST_HEAD - LIST_PAD);
    g2.clip();
    filesCache.forEach((f, i) => {
      const y = LIST_HEAD + i * ROW_H - listScroll;
      if (y + ROW_H < LIST_HEAD || y > hCss - LIST_PAD) return;
      if (i % 2 === 0) {
        g2.fillStyle = "rgba(255,255,255,0.045)";
        g2.fillRect(6, y, wCss - 12, ROW_H - 4);
      }
      g2.font = "19px system-ui, sans-serif";
      g2.fillText(KIND_ICON[f.kind] || "📎", 15, y + 18);
      g2.fillStyle = "#eef3f9";
      g2.font = "14px system-ui, sans-serif";
      let name = f.name || "";
      while (g2.measureText(name).width > wCss - (f.world && f.world.visible ? 210 : 130) && name.length > 2) name = name.slice(0, -2);
      g2.fillText(name, 46, y + 18);
      g2.fillStyle = "#7c90aa";
      g2.font = "11px system-ui, sans-serif";
      g2.fillText(`${f.updatedBy || ""} · ${relTime(f.updatedAt)} · ${fmtKB(f.size)}`, 46, y + 38);
      if (f.kind === "model" && f.world && f.world.visible) {
        g2.fillStyle = "rgba(120,180,255,0.9)";
        g2.font = "11px system-ui, sans-serif";
        g2.textAlign = "right";
        g2.fillText("🧊 " + t("filePlaced"), wCss - 16, y + 18);
        g2.textAlign = "left";
      }
    });
    g2.restore();
    hotspots.push({ x: 0, y: LIST_HEAD, w: wCss, h: hCss - LIST_HEAD - LIST_PAD, act: "row" });
  }

  function drawButton(g, r, label, active, dim) {
    g.fillStyle = dim ? "rgba(46,64,96,0.35)" : active ? "rgba(91,140,255,0.85)" : "rgba(46,64,96,0.65)";
    roundRect(g, r.x, r.y, r.w, r.h, 12);
    g.fill();
    g.lineWidth = 1.5;
    g.strokeStyle = dim ? "rgba(120,160,220,0.2)" : "rgba(91,140,255,0.6)";
    g.stroke();
    g.fillStyle = dim ? "#61708a" : "#dfe8f4";
    g.font = "13px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(label, r.x + r.w / 2, r.y + r.h / 2 + 1);
    g.textAlign = "left";
    g.textBaseline = "alphabetic";
  }

  function previewButtons() {
    const btns = [];
    const editable = curFile && EDITABLE_KINDS.has(curFile.kind) && canEdit();
    if (curFile && curFile.kind === "model") {
      if (canEdit()) btns.push({ act: "togglePlace", label: curFile.world && curFile.world.visible ? t("xrFileUnplace") : t("xrFilePlace") });
      if (!curFile.world || !curFile.world.visible) btns.push({ act: "temp", label: t("xrFileTemp") });
    }
    if (editable) btns.push({ act: "edit", label: t("xrFileEdit") });
    btns.push({ act: "close", label: t("xrFileClose") });
    return btns;
  }

  function drawButtons() {
    const btns = previewButtons();
    btns.forEach((b, i) => {
      const r = btnRect(i, btns.length);
      drawButton(g2, r, b.label, false, false);
      hotspots.push({ ...r, act: b.act });
    });
  }

  function drawPreview(wCss, hCss) {
    const hs = headerBtn(g2, 12, "←");
    hs.act = "back";
    hotspots.push(hs);
    g2.fillStyle = "#dfe8f4";
    g2.font = "bold 15px system-ui, sans-serif";
    let title = curFile ? curFile.name : "";
    while (g2.measureText(title).width > wCss - 150 && title.length > 2) title = title.slice(0, -2);
    g2.fillText(title, 78, 26);
    if (curFile) {
      g2.fillStyle = "#7c90aa";
      g2.font = "11px system-ui, sans-serif";
      g2.fillText(`${curFile.updatedBy || ""} · ${relTime(curFile.updatedAt)} · ${fmtKB(curFile.size)}`, 78, 41);
    }
    const r = contentRect();
    if (!pv || pv.state === "loading") {
      g2.fillStyle = "#8fa3bd";
      g2.font = "15px system-ui, sans-serif";
      g2.textAlign = "center";
      g2.fillText(t("xrFileLoading"), wCss / 2, r.y + r.h / 2);
      g2.textAlign = "left";
    } else if (pv.state === "failed") {
      g2.fillStyle = "#c98a8a";
      g2.font = "15px system-ui, sans-serif";
      g2.textAlign = "center";
      g2.fillText(t("xrFileLoadFail"), wCss / 2, r.y + r.h / 2);
      g2.textAlign = "left";
    } else if (pv.type === "texture") {
      /* 视频在独立平面上；画布只留按钮，无内容热点 */
      if (mat.map !== tex) { mat.map = tex; mat.needsUpdate = true; }
    } else if (pv.type === "img") {
      const dw = Math.min(wCss - 32, pv.wCss);
      const scale = dw / pv.wCss;
      const fullDest = pv.fullH * scale;
      pv.scroll = Math.min(pv.scroll || 0, Math.max(0, fullDest - r.h));
      g2.fillStyle = pv.bg || "#10151d";
      g2.fillRect(8, r.y, wCss - 16, r.h);
      g2.save();
      g2.beginPath();
      g2.rect(8, r.y, wCss - 16, r.h);
      g2.clip();
      const sy = (pv.scroll || 0) / scale;
      const sh = Math.min(pv.fullH - sy, r.h / scale);
      g2.drawImage(pv.img, 0, sy, pv.wCss, sh, 8 + (wCss - 16 - dw) / 2, r.y, dw, sh * scale);
      g2.restore();
      if (fullDest > r.h) hotspots.push({ ...r, act: "scroll" });
    } else if (pv.type === "lines") {
      const lh = 22;
      const maxLines = Math.floor((r.h - 8) / lh);
      pv.scrollLine = Math.min(pv.scrollLine || 0, Math.max(0, pv.lines.length - maxLines));
      g2.fillStyle = pv.bg || "#10151d";
      g2.fillRect(8, r.y, wCss - 16, r.h);
      g2.save();
      g2.beginPath();
      g2.rect(8, r.y, wCss - 16, r.h);
      g2.clip();
      g2.font = MEASURE_FONT;
      g2.fillStyle = "#eef3f9";
      for (let i = 0; i < maxLines; i++) {
        const ln = pv.lines[(pv.scrollLine || 0) + i];
        if (ln === undefined) break;
        g2.fillText(ln, 16, r.y + 22 + i * lh);
      }
      g2.restore();
      if (pv.lines.length > maxLines) hotspots.push({ ...r, act: "scroll" });
    } else if (pv.type === "info") {
      g2.fillStyle = "#8fa3bd";
      g2.font = "14px system-ui, sans-serif";
      g2.textAlign = "center";
      const msg = curFile && (curFile.kind === "audio" || curFile.kind === "video" || curFile.kind === "other")
        ? t("xrFileView2d")
        : `${KIND_ICON[curFile.kind] || "📎"}  ${curFile.name}`;
      g2.fillText(msg, wCss / 2, r.y + r.h / 2);
      g2.textAlign = "left";
    }
    if (pv && pv.type !== "texture") drawButtons();
    else drawButtons();
  }

  /* ---------- 打开 / 关闭 ---------- */

  function openPanel() {
    if (disposed || open) return;
    open = true;
    view = "list";
    curFile = null;
    pv = null;
    listScroll = 0;
    computeAnchor();
    placePanel();
    uiMesh.visible = true;
    drawPanel();
  }
  function closePanel() {
    if (!open) return;
    open = false;
    uiMesh.visible = false;
    closePreviewContent();
    placePanel();
    drawPanel();
  }
  function isOpen() { return open; }

  function closePreviewContent() {
    if (pv && pv.video) {
      try { pv.video.pause(); pv.video.removeAttribute("src"); pv.video.load(); } catch (e) {}
    }
    if (pv && pv.videoTex) { pv.videoTex.dispose(); }
    if (pv && pv.objUrl) { URL.revokeObjectURL(pv.objUrl); }
    if (curFile && curFile.kind === "model" && removeChatModel) removeChatModel("file:" + curFile.id);
    if (pv && pv.mdEl) { try { pv.mdEl.remove(); } catch (e) {} }
    videoMesh.visible = false;
    pv = null;
    curFile = null;
  }

  function openPreview(file) {
    if (disposed) return;
    if (!open) { open = true; computeAnchor(); uiMesh.visible = true; }
    closePreviewContent();
    view = "preview";
    curFile = file;
    pv = { state: "loading", type: "none", scroll: 0, scrollLine: 0, contentH: 240 };
    placePanel();
    drawPanel();
    loadPreviewContent(file);
  }
  function backToList() {
    closePreviewContent();
    view = "list";
    placePanel();
    drawPanel();
  }

  /* ---------- 分类型预览（任务 15） ---------- */

  function wrapText(text, maxW) {
    const mc = wrapText._c || (wrapText._c = document.createElement("canvas").getContext("2d"));
    mc.font = MEASURE_FONT;
    const lines = [];
    for (const para of String(text).replace(/\t/g, "    ").split("\n")) {
      let cur = "";
      for (const ch of para) {
        if (mc.measureText(cur + ch).width > maxW) { lines.push(cur); cur = ch; }
        else cur += ch;
      }
      lines.push(cur);
      if (lines.length > 4000) { lines.push("…"); break; }
    }
    return lines;
  }

  async function loadPreviewContent(file) {
    try {
      if (file.kind === "markdown") {
        if (!renderFileMarkdown || !rasterizeDom) throw new Error("no bridge");
        const el = await renderFileMarkdown(await fetchText(file.contentUrl));
        if (disposed || curFile !== file) { try { el.remove(); } catch (e) {} return; }
        try {
          const out = await rasterizeDom(el);
          if (disposed || curFile !== file) return;
          if (!out) throw new Error("raster fail");
          const dw = Math.min(PV_W - 32, out.wCss);
          pv = {
            state: "ok", type: "img", img: out.img, wCss: out.wCss, fullH: out.hCss, bg: out.bg,
            scroll: 0, contentH: out.hCss * (dw / out.wCss),
          };
        } finally {
          try { el.remove(); } catch (e) {}
        }
      } else if (file.kind === "image" || file.kind === "svg") {
        /* svg 同样走位图（createImageBitmap 不执行脚本） */
        const blob = await fetchBlob(file.contentUrl);
        const bmp = await createImageBitmap(blob);
        if (disposed || curFile !== file) { try { bmp.close(); } catch (e) {} return; }
        const wCss = PV_W - 32;
        const off = document.createElement("canvas");
        off.width = Math.min(wCss, bmp.width);
        off.height = Math.max(1, Math.round(off.width * (bmp.height / bmp.width)));
        off.getContext("2d").drawImage(bmp, 0, 0, off.width, off.height);
        try { bmp.close(); } catch (e) {}
        pv = {
          state: "ok", type: "img", img: off, wCss: off.width, fullH: off.height, bg: "#0b0f17",
          scroll: 0, contentH: off.height * (Math.min(wCss, off.width) / off.width),
        };
      } else if (file.kind === "text") {
        const text = await fetchText(file.contentUrl);
        if (disposed || curFile !== file) return;
        const lines = wrapText(text, PV_W - 40);
        pv = { state: "ok", type: "lines", lines, scrollLine: 0, contentH: Math.min(lines.length * 22, 700), bg: "#10151d" };
      } else if (file.kind === "video") {
        const blob = await fetchBlob(file.contentUrl);
        if (disposed || curFile !== file) return;
        const objUrl = URL.createObjectURL(blob);
        const video = document.createElement("video");
        video.src = objUrl;
        video.loop = true;
        video.muted = true;
        video.playsInline = true;
        const vtex = new THREE.VideoTexture(video);
        vtex.colorSpace = THREE.SRGBColorSpace;
        pv = { state: "ok", type: "texture", video, videoTex: vtex, objUrl, playing: false, contentH: 480 };
        syncVideoMesh();
        video.addEventListener("loadeddata", () => { if (!disposed && pv && pv.video === video) { pv.videoTex.needsUpdate = true; syncVideoMesh(); } });
      } else {
        pv = { state: "ok", type: "info", contentH: 240 };
      }
    } catch (err) {
      if (!disposed && curFile === file) pv = { state: "failed", type: "none", contentH: 240 };
    }
    if (disposed) return;
    if (pv && pv.type === "texture") pv.contentH = 480;
    placePanel();
    drawPanel();
  }

  function toggleVideo() {
    if (!pv || pv.type !== "texture") return;
    if (pv.playing) { try { pv.video.pause(); } catch (e) {} pv.playing = false; }
    else { try { pv.video.play(); } catch (e) {} pv.playing = true; }
  }

  /* ---------- 编辑（任务 17）：HUD DOM textarea（dom-overlay/系统键盘） ---------- */

  let edit = null;
  let editRowEl = null;

  function buildEditRow() {
    if (!hud) return null;
    const row = document.createElement("div");
    row.className = "xr-file-edit hidden";
    row.innerHTML = `
      <textarea spellcheck="false"></textarea>
      <div class="xr-fe-btns">
        <button type="button" class="xr-btn" data-a="save"></button>
        <button type="button" class="xr-btn" data-a="cancel"></button>
        <span class="xr-fe-hint"></span>
      </div>`;
    const style = document.createElement("style");
    style.textContent = `
      #xrRoot .xr-file-edit { position: absolute; bottom: 58px; left: 50%; transform: translateX(-50%);
        width: min(560px, 72vw); display: flex; flex-direction: column; gap: 6px; z-index: 3; }
      #xrRoot .xr-file-edit.hidden { display: none; }
      #xrRoot .xr-file-edit textarea { width: 100%; height: 180px; resize: vertical;
        border: 1px solid #2c3d58; border-radius: 10px; background: rgba(16,22,34,0.9);
        color: #dfe8f4; font: 13px/1.5 "SF Mono", ui-monospace, monospace; padding: 8px 10px; outline: none; }
      #xrRoot .xr-file-edit textarea:focus { border-color: #5b8cff; }
      #xrRoot .xr-file-edit .xr-fe-btns { display: flex; gap: 8px; align-items: center; }
      #xrRoot .xr-file-edit .xr-fe-hint { color: #8fa3bd; font-size: 12px; flex: 1; text-align: right; }`;
    row.prepend(style);
    const area = row.querySelector("textarea");
    area.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); endEdit(); }
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); saveEdit(); }
    });
    area.addEventListener("keyup", (e) => e.stopPropagation());
    row.querySelector('[data-a="save"]').textContent = t("xrFileSave");
    row.querySelector('[data-a="cancel"]').textContent = t("xrFileCancel");
    row.querySelector('[data-a="save"]').addEventListener("click", saveEdit);
    row.querySelector('[data-a="cancel"]').addEventListener("click", endEdit);
    hud.appendChild(row);
    return row;
  }

  async function startEdit() {
    if (!curFile || !EDITABLE_KINDS.has(curFile.kind) || !canEdit() || disposed) return;
    let content = "";
    let meta = curFile;
    try {
      const single = await api(`${base()}/${curFile.id}`);
      meta = single.file;
      content = await fetchText(meta.contentUrl);
    } catch (err) {
      if (statusEl) statusEl.textContent = (err && err.message) || t("xrFileLoadFail");
      return;
    }
    if (disposed) return;
    if (!editRowEl) editRowEl = buildEditRow();
    if (!editRowEl) {
      if (statusEl) statusEl.textContent = t("xrFileView2d");
      return;
    }
    edit = { file: meta, base: meta.updatedAt };
    const area = editRowEl.querySelector("textarea");
    area.value = content;
    editRowEl.querySelector(".xr-fe-hint").textContent =
      /[一-鿿]/.test(content) || content.length > 400 ? t("xrFileEdit2dHint") : "";
    editRowEl.classList.remove("hidden");
    try { area.focus(); } catch (e) {}
  }
  function endEdit() {
    if (editRowEl) editRowEl.classList.add("hidden");
    edit = null;
  }
  async function saveEdit() {
    if (!edit || !editRowEl) return;
    const text = editRowEl.querySelector("textarea").value;
    try {
      const payload = await api(`${base()}/${edit.file.id}`, {
        method: "PUT",
        body: JSON.stringify({ content: text, baseUpdatedAt: edit.base }),
      });
      edit = { file: payload.file, base: payload.file.updatedAt };
      if (statusEl) statusEl.textContent = t("xrFileSaved");
      pollOnce();
    } catch (err) {
      const msg = err && err.message ? err.message : "";
      if (err && err.status === 409) {
        if (statusEl) statusEl.textContent = t("xrFileConflict");
        try {
          const single = await api(`${base()}/${edit.file.id}`);
          const latest = await fetchText(single.file.contentUrl);
          editRowEl.querySelector("textarea").value = latest;
          edit.base = single.file.updatedAt;
        } catch (e) {}
      } else if (statusEl) {
        statusEl.textContent = msg || t("xrSendFail");
      }
    }
  }

  /* ---------- 世界摆放（任务 16） ---------- */

  const gltfLoader = new GLTFLoader();
  const worldRecs = new Map(); // fileId → { holder, file, state, box, pendingPlace }

  function applyPose(rec) {
    if (adjust && adjust.fileId === rec.file.id) return; /* 调整中本地位姿优先 */
    const pose = (rec.file.world && rec.file.world.pose) || {};
    const pos = pose.position || [0, 0.5, 0];
    const rot = pose.rotation || [0, 0, 0];
    const sc = pose.scale || [1, 1, 1];
    rec.holder.position.set(pos[0], pos[1], pos[2]);
    rec.holder.rotation.set(rot[0] || 0, rot[1] || 0, rot[2] || 0);
    const s = Math.max(0.01, Math.abs(sc[0]));
    rec.holder.scale.set(s, s, s);
  }

  function removeWorld(id) {
    const rec = worldRecs.get(id);
    if (!rec) return;
    worldGroup.remove(rec.holder);
    disposeObjectTree(rec.holder);
    worldRecs.delete(id);
    if (adjust && adjust.fileId === id) exitAdjust(false);
  }

  function newRec(file) {
    const holder = new THREE.Group();
    const wire = new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshBasicMaterial({ color: 0x3d5a8a, wireframe: true })
    );
    wire.position.y = 0.5;
    holder.add(wire);
    holder.userData.fileId = file.id;
    worldGroup.add(holder);
    const rec = { holder, file, state: "loading", box: null, pendingPlace: false };
    worldRecs.set(file.id, rec);
    return rec;
  }

  function syncWorld() {
    const want = filesCache.filter((f) => f.kind === "model" && f.world && f.world.visible);
    const wantIds = new Set(want.map((f) => f.id));
    for (const id of Array.from(worldRecs.keys())) if (!wantIds.has(id)) removeWorld(id);
    for (const f of want) {
      let rec = worldRecs.get(f.id);
      if (!rec) {
        rec = newRec(f);
        applyPose(rec);
        loadWorldModel(rec);
      } else {
        rec.file = f;
        if (rec.state === "ready") applyPose(rec);
      }
    }
  }

  function loadWorldModel(rec) {
    (async () => {
      try {
        const blob = await fetchBlob(rec.file.contentUrl);
        if (disposed || !worldRecs.has(rec.file.id)) return;
        const buf = await blob.arrayBuffer();
        const gltf = await new Promise((resolve, reject) => {
          try { gltfLoader.parse(buf, "", resolve, reject); }
          catch (e) { reject(e); }
        });
        if (disposed || !worldRecs.has(rec.file.id)) return;
        const model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
        if (!model) throw new Error("empty model");
        const box = new THREE.Box3().setFromObject(model);
        const center = box.getCenter(new THREE.Vector3());
        model.position.copy(center).negate();
        rec.holder.remove(rec.holder.children[0]); /* 摘掉线框占位 */
        rec.holder.add(model);
        rec.box = box;
        rec.state = "ready";
        if (rec.pendingPlace) doInitialPlace(rec);
        else applyPose(rec);
      } catch (err) {
        if (!disposed && worldRecs.has(rec.file.id)) {
          rec.state = "failed";
          if (statusEl) statusEl.textContent = t("xrFileLoadFail");
          if (rec.pendingPlace) { rec.pendingPlace = false; removeWorld(rec.file.id); }
        }
      }
    })();
  }

  /* 摆入（任务 16）：模型就绪后客户端算初始位姿——摆放者面前空位 + 朝向摆放者 +
     Box3 ~1m 归一化 scale + 底边落地——一次 PUT；服务端 400/403 即时撤占位 */
  async function placeFile(file) {
    if (!canEdit()) { if (statusEl) statusEl.textContent = t("xrFileNoPerm"); return; }
    let rec = worldRecs.get(file.id);
    if (!rec) {
      rec = newRec(file);
      rec.pendingPlace = true;
      loadWorldModel(rec);
      return;
    }
    if (rec.state !== "ready") { rec.pendingPlace = true; return; }
    if (rec.placing) return;
    rec.pendingPlace = false;
    doInitialPlace(rec);
  }

  async function doInitialPlace(rec) {
    if (rec.placing || rec.state !== "ready") return;
    rec.placing = true;
    const p = camera.getWorldPosition(new THREE.Vector3());
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.getWorldQuaternion(new THREE.Quaternion()));
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1);
    fwd.normalize();
    const spot = { x: p.x + fwd.x * 1.6, z: p.z + fwd.z * 1.6 };
    const rotY = Math.atan2(p.x - spot.x, p.z - spot.z);
    const size = rec.box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const s = THREE.MathUtils.clamp(1.0 / maxDim, 0.02, MAX_WORLD_SCALE);
    try {
      const payload = await api(`${base()}/${rec.file.id}/placement`, {
        method: "PUT",
        body: JSON.stringify({
          visible: true,
          position: [+spot.x.toFixed(4), +((size.y / 2) * s).toFixed(4), +spot.z.toFixed(4)],
          rotation: [0, +rotY.toFixed(4), 0],
          scale: [s, s, s],
        }),
      });
      if (disposed) return;
      applyFilePayload(payload.file, payload.revision);
    } catch (err) {
      removeWorld(rec.file.id);
      if (statusEl) statusEl.textContent = (err && err.message) || t("xrSendFail");
    } finally {
      rec.placing = false;
    }
  }

  function applyFilePayload(f2, rev) {
    const i = filesCache.findIndex((x) => x.id === f2.id);
    if (i >= 0) filesCache[i] = f2;
    if (rev != null) revision = rev;
    const rec = worldRecs.get(f2.id);
    if (rec) rec.file = f2;
    syncWorld();
    if (view === "preview" && curFile && curFile.id === f2.id) { curFile = f2; drawPanel(); }
    else if (view === "list") drawPanel();
  }

  async function unplaceFile(file) {
    if (!canEdit()) { if (statusEl) statusEl.textContent = t("xrFileNoPerm"); return; }
    try {
      const payload = await api(`${base()}/${file.id}/placement`, { method: "PUT", body: JSON.stringify({ visible: false }) });
      if (disposed) return;
      applyFilePayload(payload.file, payload.revision);
    } catch (err) {
      if (statusEl) statusEl.textContent = (err && err.message) || t("xrSendFail");
    }
  }

  /* 拖拽节流保存：500ms 合并；松手立即保存 */
  const saveTimers = new Map();
  function poseOf(fileId) {
    const rec = worldRecs.get(fileId);
    if (!rec) return null;
    return {
      position: [+rec.holder.position.x.toFixed(4), +rec.holder.position.y.toFixed(4), +rec.holder.position.z.toFixed(4)],
      rotation: [0, +rec.holder.rotation.y.toFixed(4), 0],
      scale: [+rec.holder.scale.x.toFixed(4), +rec.holder.scale.y.toFixed(4), +rec.holder.scale.z.toFixed(4)],
    };
  }
  async function putPose(fileId) {
    const pose = poseOf(fileId);
    if (!pose) return;
    try {
      await api(`${base()}/${fileId}/placement`, {
        method: "PUT",
        body: JSON.stringify({ visible: true, ...pose }),
      });
    } catch (err) {
      if (statusEl) statusEl.textContent = (err && err.message) || t("xrSendFail");
    }
  }
  function scheduleSave(fileId) {
    if (saveTimers.has(fileId)) return;
    saveTimers.set(fileId, setTimeout(() => {
      saveTimers.delete(fileId);
      putPose(fileId);
    }, SAVE_THROTTLE_MS));
  }
  function flushSave(fileId) {
    if (saveTimers.has(fileId)) { clearTimeout(saveTimers.get(fileId)); saveTimers.delete(fileId); }
    putPose(fileId);
  }

  /* ---------- 调整模式（移动 / 旋转 / 缩放 / 收起 / 完成） ---------- */

  let adjust = null; // { fileId, submode }

  const adjBarMesh = (() => {
    const c = document.createElement("canvas");
    c.width = 1000; c.height = 128;
    const gg = c.getContext("2d");
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false, toneMapped: false })
    );
    mesh.material.map.colorSpace = THREE.SRGBColorSpace;
    mesh.userData = { c, g: gg };
    mesh.visible = false;
    group.add(mesh);
    return mesh;
  })();

  const ADJ_BTNS = [
    ["move", "xrAdjMove"], ["rotate", "xrAdjRotate"], ["scale", "xrAdjScale"],
    ["unplace", "xrFileUnplace"], ["done", "xrAdjDone"],
  ];
  const adjBtnRect = (i) => ({ x: 16 + i * 196, y: 16, w: 180, h: 96 });

  function drawAdjustBar() {
    const { c, g } = adjBarMesh.userData;
    g.clearRect(0, 0, c.width, c.height);
    g.fillStyle = "rgba(10,15,24,0.9)";
    roundRect(g, 2, 2, c.width - 4, c.height - 4, 26);
    g.fill();
    g.lineWidth = 4;
    g.strokeStyle = "rgba(120,160,220,0.5)";
    g.stroke();
    ADJ_BTNS.forEach(([act, key], i) => {
      const r = adjBtnRect(i);
      const dim = act === "unplace" && !canEdit();
      g.fillStyle = dim ? "rgba(46,64,96,0.35)" : adjust && adjust.submode === act ? "rgba(91,140,255,0.9)" : "rgba(46,64,96,0.65)";
      roundRect(g, r.x, r.y, r.w, r.h, 20);
      g.fill();
      g.fillStyle = dim ? "#61708a" : "#dfe8f4";
      g.font = "bold 40px system-ui, sans-serif";
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText(t(key), r.x + r.w / 2, r.y + r.h / 2);
    });
    g.textAlign = "left";
    g.textBaseline = "alphabetic";
    adjBarMesh.material.map.needsUpdate = true;
  }

  function enterAdjust(fileId) {
    if (!canEdit()) { if (statusEl) statusEl.textContent = t("xrFileNoPerm"); return; }
    const rec = worldRecs.get(fileId);
    if (!rec) return;
    adjust = { fileId, submode: "move" };
    drawAdjustBar();
    adjBarMesh.visible = true;
    const wp = rec.holder.getWorldPosition(new THREE.Vector3());
    const size = rec.box ? rec.box.getSize(new THREE.Vector3()) : new THREE.Vector3(1, 1, 1);
    const topY = wp.y + Math.max(0.35, (size.y * rec.holder.scale.y) / 2 + 0.3);
    adjBarMesh.position.set(wp.x, topY, wp.z);
    adjBarMesh.rotation.y = Math.atan2(camera.getWorldPosition(new THREE.Vector3()).x - wp.x, camera.getWorldPosition(new THREE.Vector3()).z - wp.z);
    adjBarMesh.scale.set(1.05, 0.134, 1);
    if (statusEl) statusEl.textContent = t("xrAdjHint");
  }
  function exitAdjust(save) {
    if (adjust && save !== false) flushSave(adjust.fileId);
    adjust = null;
    adjBarMesh.visible = false;
    if (statusEl && statusEl.textContent === t("xrAdjHint")) statusEl.textContent = "";
  }
  function adjusting() { return !!adjust; }

  function adjBarPick(ray) {
    if (!adjBarMesh.visible) return null;
    const hits = ray.intersectObject(adjBarMesh, false);
    if (!hits.length) return null;
    const uv = hits[0].uv;
    const px = uv.x * 1000, py = (1 - uv.y) * 128;
    for (let i = 0; i < ADJ_BTNS.length; i++) {
      const r = adjBtnRect(i);
      if (px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h) return ADJ_BTNS[i][0];
    }
    return "bar";
  }

  /* ---------- 射线交互（桌面 + 手柄统一入口） ---------- */

  function uiHit(ray) {
    if (!open || !uiMesh.visible) return null;
    const hits = ray.intersectObject(uiMesh, false);
    if (!hits.length) return null;
    const uv = hits[0].uv;
    return { px: uv.x * cssW(), py: (1 - uv.y) * cssH() };
  }

  function worldPick(ray) {
    const hits = ray.intersectObjects(worldGroup.children, true);
    for (const h of hits) {
      let o = h.object;
      while (o && o.parent !== worldGroup) o = o.parent;
      if (o && o.userData.fileId != null) return { fileId: o.userData.fileId };
    }
    return null;
  }

  /* 返回 true = 已消费（点击落在文件 UI 或世界模型上） */
  function handlePick(ray) {
    if (disposed) return false;
    if (adjBarMesh.visible) {
      const act = adjBarPick(ray);
      if (act) {
        if (act === "move" || act === "rotate" || act === "scale") { adjust.submode = act; drawAdjustBar(); }
        else if (act === "done") exitAdjust();
        else if (act === "unplace" && adjust) { const f = filesCache.find((x) => x.id === adjust.fileId); exitAdjust(false); if (f) unplaceFile(f); }
        return true;
      }
    }
    const hit = uiHit(ray);
    if (hit) {
      const spot = hotspots.find((h) => hit.px >= h.x && hit.px <= h.x + h.w && hit.py >= h.y && hit.py <= h.y + h.h);
      if (spot) {
        if (view === "list") {
          if (spot.act === "close") closePanel();
          else if (spot.act === "row") {
            const idx = Math.floor((hit.py - LIST_HEAD + listScroll) / ROW_H);
            const f = filesCache[idx];
            if (f) openPreview(f);
          }
        } else if (spot.act === "back") backToList();
        else if (spot.act === "close") { endEdit(); closePanel(); }
        else if (spot.act === "edit") startEdit();
        else if (spot.act === "togglePlace") { if (curFile) (curFile.world && curFile.world.visible ? unplaceFile(curFile) : placeFile(curFile)); }
        else if (spot.act === "temp") {
          if (curFile && placeChatModel) placeChatModel("file:" + curFile.id, { downloadUrl: curFile.contentUrl });
        } else if (spot.act === "play") toggleVideo();
      }
      return true;
    }
    if (adjBarMesh.visible) return false; /* 调整中：点模型外的空白仍留给消息墙，但不再选新模型 */
    const w = worldPick(ray);
    if (w) {
      if (adjust && adjust.fileId === w.fileId) return true; /* 已选中：拖拽由 beginDrag 接管 */
      enterAdjust(w.fileId);
      return true;
    }
    return false;
  }

  /* 按下 → 可能成为拖拽（模型移动 / 面板滚动）；返回拖拽对象或 null */
  function beginDrag(ray) {
    if (disposed) return null;
    if (adjust) {
      const w = worldPick(ray);
      if (w && w.fileId === adjust.fileId) {
        const rec = worldRecs.get(adjust.fileId);
        const drag = {
          kind: "xrfile-model", fileId: adjust.fileId, submode: adjust.submode,
          pos0: rec.holder.position.clone(), rot0: rec.holder.rotation.y,
        };
        if (drag.submode === "move") {
          const gp = new THREE.Vector3();
          const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
          if (ray.ray.intersectPlane(plane, gp)) { drag.gx0 = gp.x; drag.gz0 = gp.z; }
        } else if (drag.submode === "rotate") {
          const c = rec.holder.position;
          drag.a0 = Math.atan2((ray.ray.origin.x - c.x), (ray.ray.origin.z - c.z));
          const n = new THREE.Vector3().subVectors(camera.getWorldPosition(new THREE.Vector3()), c).setY(0);
          if (n.lengthSq() < 1e-6) n.set(0, 0, 1); else n.normalize();
          drag.plane = new THREE.Plane().setFromNormalAndCoplanarPoint(n, c.clone());
        } else if (drag.submode === "scale") {
          const c = rec.holder.position;
          const n = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.getWorldQuaternion(new THREE.Quaternion()));
          drag.plane = new THREE.Plane().setFromNormalAndCoplanarPoint(n, c.clone());
          const p = new THREE.Vector3();
          drag.scY0 = ray.ray.intersectPlane(drag.plane, p) ? p.y : c.y;
          drag.sc0 = rec.holder.scale.x;   /* 起始缩放：每帧都乘 holder 会指数失控 */
        }
        return drag;
      }
      return null;
    }
    const hit = uiHit(ray);
    if (hit) {
      const spot = hotspots.find((h) => (h.act === "scroll" || (view === "list" && h.act === "row")) &&
        hit.px >= h.x && hit.px <= h.x + h.w && hit.py >= h.y && hit.py <= h.y + h.h);
      if (spot) {
        return {
          kind: "xrfile-panel", py: hit.py,
          listScroll0: listScroll,
          pvScroll0: pv ? pv.scroll || 0 : 0,
          pvLine0: pv ? pv.scrollLine || 0 : 0,
        };
      }
    }
    return null;
  }

  function dragMove(drag, ray) {
    if (disposed || !drag) return;
    if (drag.kind === "xrfile-panel") {
      const hit = uiHit(ray);
      if (!hit) return;
      const dy = hit.py - drag.py;
      drag.moved = Math.max(drag.moved || 0, Math.abs(dy));
      if (view === "list") {
        const maxScroll = Math.max(0, filesCache.length * ROW_H - (cssH() - LIST_HEAD - LIST_PAD * 2));
        listScroll = Math.min(Math.max(0, drag.listScroll0 - dy), maxScroll);
        drawPanel();
      } else if (pv && pv.type === "img") {
        const r = contentRect();
        const dw = Math.min(cssW() - 32, pv.wCss);
        const scale = dw / pv.wCss;
        const maxDest = Math.max(0, pv.fullH * scale - r.h);
        pv.scroll = Math.min(Math.max(0, drag.pvScroll0 - dy), maxDest);
        drawPanel();
      } else if (pv && pv.type === "lines") {
        const r = contentRect();
        const maxLines = Math.floor((r.h - 8) / 22);
        pv.scrollLine = Math.min(Math.max(0, Math.round(drag.pvLine0 - dy / 22)), Math.max(0, pv.lines.length - maxLines));
        drawPanel();
      }
      return;
    }
    if (drag.kind !== "xrfile-model") return;
    const rec = worldRecs.get(drag.fileId);
    if (!rec) return;
    if (drag.submode === "move") {
      const p = new THREE.Vector3();
      if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), p)) {
        if (drag.gx0 != null) {
          rec.holder.position.x = drag.pos0.x + (p.x - drag.gx0);
          rec.holder.position.z = drag.pos0.z + (p.z - drag.gz0);
        }
        scheduleSave(drag.fileId);
      }
    } else if (drag.submode === "rotate") {
      const p = new THREE.Vector3();
      if (drag.plane && ray.ray.intersectPlane(drag.plane, p)) {
        const c = rec.holder.position;
        if (Math.hypot(p.x - c.x, p.z - c.z) > 0.15) {
          const a = Math.atan2(p.x - c.x, p.z - c.z);
          rec.holder.rotation.y = drag.rot0 + (a - drag.a0);
          scheduleSave(drag.fileId);
        }
      }
    } else if (drag.submode === "scale") {
      const p = new THREE.Vector3();
      if (drag.plane && ray.ray.intersectPlane(drag.plane, p)) {
        const s = THREE.MathUtils.clamp(drag.sc0 * Math.exp((p.y - drag.scY0) * 1.6), 0.05, MAX_WORLD_SCALE);
        rec.holder.scale.setScalar(s);
        scheduleSave(drag.fileId);
      }
    }
  }
  function dragEnd(drag) {
    if (!drag) return;
    if (drag.kind === "xrfile-model") flushSave(drag.fileId);
  }

  /* 手柄摇杆（调整中）：左手柄平移、右手柄旋转/缩放；返回 true 表示已消费 */
  function xrJoystick(hand, ax, ay, dt) {
    if (disposed || !adjust) return false;
    const rec = worldRecs.get(adjust.fileId);
    if (!rec) return false;
    const dead = (v) => (Math.abs(v) > 0.15 ? v : 0);
    if (hand === "left") {
      const lax = dead(ax), lay = dead(ay);
      if (adjust.submode === "move" && (lax || lay)) {
        const fwd = new THREE.Vector3();
        camera.getWorldDirection(fwd);
        fwd.y = 0;
        if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1); else fwd.normalize();
        const right = new THREE.Vector3().crossVectors(fwd, camera.up).normalize();
        rec.holder.position.addScaledVector(fwd, -lay * dt * 0.6).addScaledVector(right, lax * dt * 0.6);
        scheduleSave(adjust.fileId);
      }
      return true;
    }
    const rax = dead(ax), rayY = dead(ay);
    if (adjust.submode === "rotate" && rax) {
      rec.holder.rotation.y -= rax * dt * 0.9;
      scheduleSave(adjust.fileId);
    } else if (adjust.submode === "scale" && rayY) {
      const s = THREE.MathUtils.clamp(rec.holder.scale.x * (1 - rayY * dt * 0.7), 0.05, MAX_WORLD_SCALE);
      rec.holder.scale.setScalar(s);
      scheduleSave(adjust.fileId);
    }
    return true;
  }

  function scrollPanelContentBy(dy) {
    if (!open) return false;
    if (view === "list") {
      const maxScroll = Math.max(0, filesCache.length * ROW_H - (cssH() - LIST_HEAD - LIST_PAD * 2));
      const before = listScroll;
      listScroll = Math.min(Math.max(0, listScroll + dy), maxScroll);
      if (listScroll !== before) { drawPanel(); return true; }
      return false;
    }
    if (pv && pv.type === "img") {
      const r = contentRect();
      const dw = Math.min(cssW() - 32, pv.wCss);
      const scale = dw / pv.wCss;
      const maxDest = Math.max(0, pv.fullH * scale - r.h);
      const before = pv.scroll || 0;
      pv.scroll = Math.min(Math.max(0, before + dy), maxDest);
      if (pv.scroll !== before) { drawPanel(); return true; }
      return false;
    }
    if (pv && pv.type === "lines") {
      const r = contentRect();
      const maxLines = Math.floor((r.h - 8) / 22);
      const before = pv.scrollLine || 0;
      pv.scrollLine = Math.min(Math.max(0, before + Math.round(dy / 22)), Math.max(0, pv.lines.length - maxLines));
      if (pv.scrollLine !== before) { drawPanel(); return true; }
      return false;
    }
    return false;
  }

  function hoverPanel(ray) {
    if (!open) return false;
    return !!uiHit(ray);
  }

  /* Esc 分级退出：编辑 → 调整 → 面板；都没命中返回 false（交给 3D 全局逻辑） */
  function escStack() {
    if (edit) { endEdit(); return true; }
    if (adjust) { exitAdjust(); return true; }
    if (open) { closePanel(); return true; }
    return false;
  }

  /* ---------- 轮询（常驻：世界摆放同步 + 面板内容刷新） ---------- */

  let polling = false;
  async function pollOnce() {
    if (disposed || polling || !roomName()) return;
    polling = true;
    try {
      const data = await api(base());
      if (disposed) return;
      const changed = data.revision !== revision;
      revision = data.revision;
      filesCache = data.files || [];
      if (changed) {
        syncWorld();
        if (view === "preview" && curFile && !filesCache.some((f) => f.id === curFile.id)) backToList();
      }
      if (open && (changed || view === "list")) drawPanel();
    } catch (err) { /* 静默重试；房间归档等由 2D 端主导退出 */ }
    finally { polling = false; }
  }
  pollTimer = setInterval(pollOnce, POLL_MS);
  pollOnce();

  function tick() { /* 预留：动画钩子 */ }

  function dispose() {
    if (disposed) return;
    disposed = true;
    if (pollTimer) clearInterval(pollTimer);
    for (const tm of Array.from(saveTimers.values())) clearTimeout(tm);
    saveTimers.clear();
    for (const id of Array.from(worldRecs.keys())) removeWorld(id);
    closePreviewContent();
    endEdit();
    adjBarMesh.material.map.dispose();
    adjBarMesh.material.dispose();
    mat.dispose();
    tex.dispose();
    videoMesh.material.dispose();
  }

  return {
    group, openPanel, closePanel, isOpen,
    handlePick, beginDrag, dragMove, dragEnd, xrJoystick,
    scrollPanelContentBy, hoverPanel, adjusting, exitAdjust, escStack,
    tick, dispose,
    /* __xrDebug 验证面：构造射线 / 读内部状态（不进正式交互路径） */
    _dbg: {
      state: () => ({
        view, open,
        curFileId: curFile && curFile.id,
        pvType: pv && pv.type, pvState: pv && pv.state,
        listScroll, hotspots: hotspots.map((h) => ({ ...h })),
        world: worldRecs.size,
        adjust: adjust ? adjust.submode : null,
        panelW: cssW(), panelH: cssH(),
      }),
      openById: (id) => {
        const f = filesCache.find((x) => String(x.id) === String(id));
        if (f) openPreview(f);
        return !!f;
      },
      rayAt: (px, py) => {
        /* PlaneGeometry(1,1) 的几何本地坐标就是 -0.5..0.5（缩放在 object 矩阵上），
           本地点直接用 CSS 比例，别乘世界尺寸 */
        const local = new THREE.Vector3(px / cssW() - 0.5, 0.5 - py / cssH(), 0.01);
        const target = uiMesh.localToWorld(local);
        const origin = camera.getWorldPosition(new THREE.Vector3());
        return new THREE.Raycaster(origin, target.sub(origin).normalize());
      },
      rayTo: (x, y, z) => {
        const origin = camera.getWorldPosition(new THREE.Vector3());
        const target = new THREE.Vector3(x, y, z);
        return new THREE.Raycaster(origin, target.sub(origin).normalize());
      },
      worldPos: (id) => {
        const r = worldRecs.get(Number(id) || id);
        return r ? r.holder.getWorldPosition(new THREE.Vector3()).toArray().map((v) => +v.toFixed(3)) : null;
      },
      canvasInfo: () => ({ w: canvas.width, h: canvas.height, head: canvas.toDataURL("image/png").slice(0, 64) }),
      forceDraw: () => drawPanel(),
      canEdit: () => canEdit(),
      tryAdjust: (id) => { enterAdjust(Number(id) || id); return adjust ? adjust.submode : null; },
    },
  };
}
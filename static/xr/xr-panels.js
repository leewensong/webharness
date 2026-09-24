/* 面板模式管线：把 2D 管线渲染好的气泡 DOM 栅格化为 CanvasTexture 贴到 3D 面板。
   聊天记录只存「数据 + 形式/意图」，样式归渲染端——此处直接消费 2D 管线的 DOM 产物
   与文档样式表，3D 端不做第二套内容解析（需求 2.3 的 100% 覆盖来源）。
   管线：live 气泡克隆（canvas→dataURL、blob:→data: 内联）
        → SVG foreignObject（嵌入文档 <style> 文本 + 重建 <body>，使 body/:root 规则生效）
        → data:URL Image → canvas 裁段 → THREE.CanvasTexture（LRU ≤ maxTextures）。
   兜底：栅格化失败 → 纯文本 Canvas 面板，保证可读（需求 2.3）。 */

import * as THREE from "three";

const NS_SVG = "http://www.w3.org/2000/svg";
const NS_XHTML = "http://www.w3.org/1999/xhtml";

const TEX_SCALE = 2;          // 栅格放大倍率（中文可读性，需求 2.6）
const SEG_H = 1024;           // 单段纹理 CSS 高（×2 = 2048 设备像素）
const RASTER_MAX_H = 8192;    // 单条消息最大栅格高（CSS px），超出截断为 8 段
const SVG_VIEWPORT_W = 1280;  // SVG 图像视口宽：>768 避免 2D 移动端媒体查询在栅格中生效
const RASTER_CONCURRENCY = 2; // 同时在途栅格数（错峰，避免进 3D 瞬间卡顿）
const STREAM_THROTTLE_MS = 300;

export function createPanelSystem(opts) {
  const panelWidth = opts.panelWidth || 1.12;
  const maxH = opts.maxH || 2.3;
  const maxW = opts.maxW || 2.4;
  const refCss = opts.refCss || 356;       /* 参考 CSS 宽：panelWidth/refCss = 全局 px→米 比例 */
  const pxPerM = panelWidth / refCss;      /* 所有面板共用：字号随内容一致，宽度随气泡真实宽度 */
  const maxTextures = opts.maxTextures || 24;
  const t = opts.t || ((k) => k);

  const group = new THREE.Group();
  const sharedGeo = new THREE.PlaneGeometry(1, 1);
  const records = new Map();   // id → rec
  const texCache = new Map();  // "id:seg" → { tex, last }
  let texClock = 0;
  let disposed = false;
  let placeholderMat = null;

  /* ---------- 文档样式与环境 ---------- */

  let styleTextCache = null;
  function docStyles() {
    if (styleTextCache === null) {
      /* 项目无 @font-face（纯系统字体栈），直接嵌 <style> 文本即可；样式归文档，
         面板快照因此天然与 2D 外观一致。 */
      styleTextCache = Array.from(document.querySelectorAll("style"))
        .map((s) => s.textContent).join("\n");
    }
    return styleTextCache;
  }

  function bodyBg() {
    try { return getComputedStyle(document.body).backgroundColor || "#10151d"; }
    catch (e) { return "#10151d"; }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* 等待异步内容就绪：图片 hydration（blob: 加载）、mermaid 渲染（mermaid.run 异步）。
     echarts 初始化同步，无需等待。上限 3s，超时按当前状态栅格化。 */
  async function waitForQuiet(el) {
    const deadline = performance.now() + 3000;
    while (performance.now() < deadline) {
      const imgsPending = Array.from(el.querySelectorAll("img")).some((img) => !img.complete);
      const mermaidPending = Array.from(el.querySelectorAll(".mermaid-box")).some((b) => !b.querySelector("svg"));
      if (!imgsPending && !mermaidPending) break;
      await sleep(120);
    }
    try {
      if (document.fonts && document.fonts.status !== "loaded") {
        await Promise.race([document.fonts.ready, sleep(300)]);
      }
    } catch (e) { /* 字体 API 异常不阻塞栅格化 */ }
  }

  function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = reject;
      fr.readAsDataURL(blob);
    });
  }

  /* 克隆气泡并内联资源。SVG-as-image 是独立文档：blob: 取不回、canvas 是空白副本，
     必须在序列化前全部转成 data:。clone 锁定显式宽度：.bubble 的 max-width 是百分比，
     搬进别的容器会二次缩窄导致换行漂移。 */
  async function prepareClone(el) {
    const clone = el.cloneNode(true);
    clone.classList.remove("streaming", "flash");
    clone.style.maxWidth = "none";
    clone.style.width = el.offsetWidth + "px";
    clone.style.marginLeft = "0";
    clone.style.marginRight = "0";

    /* 交互控件不进快照 */
    clone.querySelectorAll(".msg-menu-btn, .speak-btn").forEach((n) => n.remove());

    /* canvas（echarts）→ dataURL <img>，按文档序与原节点一一对应 */
    const liveCanvases = el.querySelectorAll("canvas");
    const cloneCanvases = clone.querySelectorAll("canvas");
    for (let i = 0; i < cloneCanvases.length && i < liveCanvases.length; i++) {
      const pic = document.createElement("img");
      try { pic.src = liveCanvases[i].toDataURL("image/png"); } catch (e) { continue; }
      pic.style.cssText = liveCanvases[i].style.cssText;
      pic.style.display = "block";
      const cc = cloneCanvases[i];
      if (cc.parentNode) cc.replaceWith(pic);
    }

    /* 图片按原始渲染尺寸锁定，blob:/相对路径统一转 data: */
    const liveImgs = Array.from(el.querySelectorAll("img"));
    const cloneImgs = Array.from(clone.querySelectorAll("img"));
    for (let i = 0; i < cloneImgs.length; i++) {
      const li = liveImgs[i];
      const ci = cloneImgs[i];
      if (!li) break;
      const r = li.getBoundingClientRect();
      if (r.width > 0) {
        ci.style.width = r.width + "px";
        ci.style.height = r.height + "px";
        try { ci.style.objectFit = getComputedStyle(li).objectFit || ""; } catch (e) {}
      }
      let src = ci.getAttribute("src") || "";
      if (!src) {
        /* 未 hydration 的 data-src（竞态）：2D 里失败时也是隐藏，快照同样隐藏 */
        ci.style.visibility = "hidden";
        continue;
      }
      try {
        if (src.startsWith("blob:")) {
          ci.src = await blobToDataURL(await (await fetch(src)).blob());
        } else if (!src.startsWith("data:")) {
          const resp = await fetch(src, { mode: "cors" });
          if (resp.ok) ci.src = await blobToDataURL(await resp.blob());
          else ci.removeAttribute("src");
        }
      } catch (e) { ci.removeAttribute("src"); }
    }
    return clone;
  }

  /* 单条消息栅格化 → { img, wCss, hCss, bg, segments }；失败返回 null（调用方降级） */
  async function rasterize(entry) {
    const el = entry.el;
    if (!el || !el.offsetWidth || !el.offsetHeight) return null;
    await waitForQuiet(el);
    if (disposed) return null;
    const clone = await prepareClone(el);
    if (disposed) return null;
    const wCss = el.offsetWidth;
    const hCss = Math.min(el.offsetHeight, RASTER_MAX_H);

    /* 重建 <body>：嵌入的 <style> 里 body/:root 规则（字体、颜色、背景、CSS 变量）才会生效。
       SVG 视口固定 1280 宽（桌面断点），气泡容器与 2D 的 #log 同 id/class。 */
    const styleEl = document.createElementNS(NS_XHTML, "style");
    styleEl.textContent = docStyles();
    const holder = document.createElementNS(NS_XHTML, "div");
    holder.setAttribute("id", "log");
    holder.setAttribute("class", "log");
    holder.style.cssText = `display:block;width:${wCss}px;margin:0;padding:0;background:transparent;`;
    holder.appendChild(clone);
    const bodyEl = document.createElementNS(NS_XHTML, "body");
    bodyEl.setAttribute("xmlns", NS_XHTML);
    bodyEl.style.margin = "0";
    bodyEl.appendChild(styleEl);
    bodyEl.appendChild(holder);
    const fo = document.createElementNS(NS_SVG, "foreignObject");
    fo.setAttribute("width", SVG_VIEWPORT_W);
    fo.setAttribute("height", hCss);
    fo.appendChild(bodyEl);
    const svg = document.createElementNS(NS_SVG, "svg");
    svg.setAttribute("xmlns", NS_SVG);
    svg.setAttribute("width", SVG_VIEWPORT_W);
    svg.setAttribute("height", hCss);
    svg.appendChild(fo);
    const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(svg));

    const img = new Image();
    await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = url; });
    if (disposed) return null;
    return {
      img,
      wCss,
      hCss,
      bg: bodyBg(),
      segments: Math.max(1, Math.ceil(hCss / SEG_H)),
      truncated: el.offsetHeight > RASTER_MAX_H,
    };
  }

  /* ---------- 纯文本兜底面板 ---------- */

  function fallbackText(msg) {
    const m = msg || {};
    let body = String(m.content || "");
    body = body.replace(/```[a-zA-Z0-9_-]*\n?[\s\S]*?```/g, "［代码块］")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "🖼")
      .replace(/[#*_>`~]/g, "");
    const head = `${m.username || "?"} · ${m.createdAt || ""}`;
    const tag = m.whisper ? " 🔒" : "";
    return { head, body, tag };
  }

  function buildTextFallback(rec) {
    const { head, body } = fallbackText(rec.entry.msg);
    const wCss = 640, scale = 2, pad = 26;
    const measure = document.createElement("canvas").getContext("2d");
    measure.font = "15px 'SF Pro Text','PingFang SC','Noto Sans SC',sans-serif";
    const lines = [];
    for (const para of body.split("\n")) {
      if (!para) { lines.push(""); continue; }
      let cur = "";
      for (const ch of para) {
        if (measure.measureText(cur + ch).width > (wCss - pad * 2) / scale) { lines.push(cur); cur = ch; }
        else cur += ch;
      }
      lines.push(cur);
      if (lines.length >= 34) { lines.push("…"); break; }
    }
    const hCss = Math.min(1024, lines.length * 24 + 64);
    const canvas = document.createElement("canvas");
    canvas.width = wCss * scale; canvas.height = Math.round(hCss * scale);
    const g = canvas.getContext("2d");
    g.scale(scale, scale);
    g.fillStyle = "#131a26";
    g.fillRect(0, 0, wCss, hCss);
    g.fillStyle = "#3a4c66";
    g.fillRect(0, 0, wCss, 2);
    g.font = "12px 'SF Pro Text','PingFang SC',sans-serif";
    g.fillStyle = "#8b9bb0";
    g.fillText(head + " " + t("xrPanelDegraded"), pad, 30);
    g.font = "15px 'SF Pro Text','PingFang SC','Noto Sans SC',sans-serif";
    g.fillStyle = "#eef3f9";
    lines.forEach((ln, i) => g.fillText(ln, pad, 58 + i * 24));
    rec.fallbackCanvas = canvas;
    rec.wCss = wCss; rec.hCss = hCss;
  }

  /* ---------- 纹理 ---------- */

  function placeholderMaterial() {
    if (!placeholderMat) {
      const c = document.createElement("canvas");
      c.width = c.height = 128;
      const g = c.getContext("2d");
      g.fillStyle = "#131a26"; g.fillRect(0, 0, 128, 128);
      g.strokeStyle = "#2a3a55"; g.lineWidth = 4; g.strokeRect(2, 2, 124, 124);
      g.fillStyle = "#54677f";
      g.beginPath();
      for (const dx of [-22, 0, 22]) { g.arc(64 + dx, 64, 5, 0, Math.PI * 2); g.fill(); }
      const tex = new THREE.CanvasTexture(c);
      tex.colorSpace = THREE.SRGBColorSpace;
      placeholderMat = new THREE.MeshBasicMaterial({ map: tex, toneMapped: false });
    }
    return placeholderMat;
  }

  function makeSegmentTexture(rec, seg) {
    const segH = Math.min(SEG_H, rec.hCss - seg * SEG_H);
    const scale = rec.fallbackCanvas ? TEX_SCALE : Math.min(TEX_SCALE, 2048 / rec.wCss);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(rec.wCss * scale);
    canvas.height = Math.round(Math.max(1, segH) * scale);
    const g = canvas.getContext("2d");
    g.scale(scale, scale);
    if (rec.fallbackCanvas) {
      /* 兜底画布本身已按 2× 绘制，等尺寸贴上 */
      g.drawImage(rec.fallbackCanvas, 0, 0, rec.wCss, rec.hCss);
    } else {
      g.fillStyle = rec.bg || "#10151d";
      g.fillRect(0, 0, rec.wCss, segH);
      /* 截取 SVG 全图的对应段（CSS 坐标，g.scale 已放大） */
      g.drawImage(rec.img, 0, seg * SEG_H, rec.wCss, segH, 0, 0, rec.wCss, segH);
      if (rec.segments > 1) {
        g.fillStyle = "rgba(9,13,20,0.78)";
        g.fillRect(0, segH - 36, rec.wCss, 36);
        g.fillStyle = "#cdd9ea";
        g.font = "13px 'SF Pro Text','PingFang SC',sans-serif";
        g.fillText(`${t("xrSegmentHint")} (${seg + 1}/${rec.segments})`, 14, segH - 12);
      }
    }
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    return tex;
  }

  function applySegment(rec) {
    const key = rec.id + ":" + rec.seg;
    let hit = texCache.get(key);
    if (!hit) {
      hit = { tex: makeSegmentTexture(rec, rec.seg) };
      texCache.set(key, hit);
    }
    hit.last = ++texClock;
    if (rec.material) { rec.material.dispose(); }
    rec.material = new THREE.MeshBasicMaterial({ map: hit.tex, toneMapped: false });
    rec.mesh.material = rec.material;
    evictLRU();
  }

  /* ---------- 记录与网格 ---------- */

  function newRec(entry) {
    const rec = {
      id: entry.id,
      entry,
      mesh: null,
      material: null,
      rasterState: "idle",   // idle | pending | done | failed
      img: null, wCss: 0, hCss: 0, segments: 1, seg: 0, bg: "",
      fallbackCanvas: null,
      bornAt: performance.now(),
      lastRasterAt: 0,
      invalidateTimer: null,
      cur: null,             // 平滑用当前位置 {x,y,z,rotY}
    };
    rec.segHeightCss = () => Math.min(SEG_H, rec.hCss - rec.seg * SEG_H) || SEG_H;
    rec.mesh = new THREE.Mesh(sharedGeo, placeholderMaterial());
    rec.mesh.userData.panelId = rec.id;
    group.add(rec.mesh);
    return rec;
  }

  /* 栅格化并发闸（≤2 在途） */
  let activeRasters = 0;
  const rasterQueue = [];
  function acquireRasterSlot() {
    if (activeRasters < RASTER_CONCURRENCY) { activeRasters++; return Promise.resolve(); }
    return new Promise((resolve) => rasterQueue.push(() => { activeRasters++; resolve(); }));
  }
  function releaseRasterSlot() {
    activeRasters--;
    const next = rasterQueue.shift();
    if (next) next();
  }

  async function kickRaster(rec) {
    if (rec.rasterState !== "idle" || disposed) return;
    rec.rasterState = "pending";
    await acquireRasterSlot();
    try {
      if (disposed) return;
      rec.lastRasterAt = performance.now();
      const out = await rasterize(rec.entry);
      if (disposed) return;
      if (!out || !out.img) {
        rec.rasterState = "failed";
        buildTextFallback(rec);
        rec.rasterState = "done";
        rec.seg = 0;
        applySegment(rec);
        return;
      }
      rec.img = out.img; rec.wCss = out.wCss; rec.hCss = out.hCss;
      rec.segments = out.segments; rec.bg = out.bg; rec.fallbackCanvas = null;
      rec.seg = 0;
      rec.rasterState = "done";
      applySegment(rec);
    } catch (err) {
      if (!disposed) {
        rec.rasterState = "failed";
        buildTextFallback(rec);
        rec.rasterState = "done";
        rec.seg = 0;
        applySegment(rec);
      }
    } finally {
      releaseRasterSlot();
    }
  }

  /* 消息更新（流式追加等）：同一面板重建节流 ≥300ms；force 跳过节流（流结束终栅） */
  function invalidate(id, force) {
    const rec = records.get(id);
    if (!rec || disposed) return;
    const reraster = () => {
      if (disposed) return;
      if (rec.invalidateTimer) { clearTimeout(rec.invalidateTimer); rec.invalidateTimer = null; }
      rec.rasterState = "idle";
      rec.seg = 0;
      kickRaster(rec);
    };
    if (rec.invalidateTimer) { clearTimeout(rec.invalidateTimer); rec.invalidateTimer = null; }
    const since = performance.now() - rec.lastRasterAt;
    if (force || since >= STREAM_THROTTLE_MS) reraster();
    else rec.invalidateTimer = setTimeout(reraster, STREAM_THROTTLE_MS - since + 10);
  }

  /* 视口宽度显著变化（进 3D 时窗口过窄、之后拉宽等）：全部重栅格化，
     避免把瞬态布局宽度永久锁进纹理。逐条走 invalidate 的节流闸。 */
  function invalidateAll() {
    for (const [id] of records) invalidate(id, false);
  }

  function disposeRec(rec) {
    if (rec.invalidateTimer) clearTimeout(rec.invalidateTimer);
    if (rec.mesh) { group.remove(rec.mesh); rec.mesh.material = null; }
    if (rec.material) { rec.material.dispose(); }
    for (const [key, hit] of texCache) {
      if (key.startsWith(rec.id + ":")) { hit.tex.dispose(); texCache.delete(key); }
    }
  }

  /* 点击翻段：同一节点换裁剪，不重栅格化 */
  function cycleSegment(id) {
    const rec = records.get(id);
    if (!rec || rec.rasterState !== "done" || !rec.img || rec.segments <= 1) return false;
    rec.seg = (rec.seg + 1) % rec.segments;
    applySegment(rec);
    return true;
  }

  function raycast(raycaster) {
    const hits = raycaster.intersectObjects(group.children, false);
    for (const h of hits) {
      if (h.object.visible) return h.object.userData.panelId;
    }
    return null;
  }

  /* 面板当前平滑位置（世界坐标）——原生图表等附属物跟随用；无记录返回 null */
  function positionOf(id) {
    const rec = records.get(id);
    return rec && rec.mesh ? rec.mesh.position : null;
  }

  /* 面板当前世界高（网格 scale.y，含未栅格化占位高）——直列布局堆叠用；无记录返回 null */
  function heightOf(id) {
    const rec = records.get(id);
    return rec && rec.mesh ? rec.mesh.scale.y : null;
  }

  /* 面板当前世界宽（网格 scale.x）——面板宽度随 2D 气泡宽度变化，布局对齐用；无记录返回 null */
  function widthOf(id) {
    const rec = records.get(id);
    return rec && rec.mesh ? rec.mesh.scale.x : null;
  }

  function evictLRU() {
    while (texCache.size > maxTextures) {
      let worstKey = null, worstScore = Infinity;
      for (const [key, hit] of texCache) {
        const rec = records.get(key.split(":")[0]);
        const visible = rec && rec.mesh && rec.mesh.visible;
        const score = (visible ? 1e9 : 0) + hit.last;
        if (score < worstScore) { worstScore = score; worstKey = key; }
      }
      if (!worstKey) break;
      const hit = texCache.get(worstKey);
      hit.tex.dispose();
      texCache.delete(worstKey);
      const rec = records.get(worstKey.split(":")[0]);
      if (rec && rec.material && rec.material.map === hit.tex) {
        rec.material.dispose();
        rec.material = null;
        rec.mesh.material = placeholderMaterial();
      }
    }
  }

  /* 每帧同步：entries=窗口内条目，keepIds=全部条目 id（窗口外保留面板隐藏），
     place(entry, hWorld, wWorld) → {x,y,z,rotY}；dt 用于平滑（新消息滑入/滚动跟手）。 */
  function sync({ entries, keepIds, place, dt }) {
    if (disposed) return;
    const keep = keepIds || entries.map((e) => e.id);
    const keepSet = new Set(keep);
    for (const [id, rec] of records) {
      if (!keepSet.has(id)) { disposeRec(rec); records.delete(id); }
    }
    const seen = new Set();
    for (const e of entries) {
      seen.add(e.id);
      let rec = records.get(e.id);
      if (!rec) {
        rec = newRec(e);
        records.set(e.id, rec);
        kickRaster(rec);
      }
      if (rec.entry !== e) rec.entry = e;
      /* 被 LRU 挤掉纹理后面板重新可见：重栅格化，否则永远停留在共享占位符 */
      if (rec.rasterState === "done" && !rec.material) {
        rec.rasterState = "idle";
        kickRaster(rec);
      }
      let wWorld, hWorld;
      if (rec.rasterState === "done" && rec.wCss) {
        wWorld = rec.wCss * pxPerM;
        hWorld = rec.segHeightCss() * pxPerM;
        /* 超上限时整块等比收缩（w、h 乘同一个 k），绝不单维压扁内容 */
        const k = Math.min(1, maxW / wWorld, maxH / hWorld);
        wWorld *= k; hWorld *= k;
      } else {
        wWorld = panelWidth;
        hWorld = Math.min(maxH, panelWidth * 0.8);
      }
      rec.mesh.scale.set(wWorld, hWorld, 1);
      const p = place(e, hWorld, wWorld);
      if (!rec.cur) {
        rec.cur = { x: p.x, y: p.y, z: p.z, rotY: p.rotY };
      } else {
        const k = 1 - Math.exp(-(dt || 0.016) * 9);
        rec.cur.x += (p.x - rec.cur.x) * k;
        rec.cur.y += (p.y - rec.cur.y) * k;
        rec.cur.z += (p.z - rec.cur.z) * k;
        let dr = p.rotY - rec.cur.rotY;
        while (dr > Math.PI) dr -= Math.PI * 2;
        while (dr < -Math.PI) dr += Math.PI * 2;
        rec.cur.rotY += dr * k;
      }
      rec.mesh.position.set(rec.cur.x, rec.cur.y, rec.cur.z);
      rec.mesh.rotation.y = rec.cur.rotY;
      rec.mesh.visible = true;
      /* 淡入（仅真纹理材质；共享占位材质不做透明，避免互相污染） */
      if (rec.material) {
        const age = (performance.now() - rec.bornAt) / 350;
        if (age < 1) {
          rec.material.transparent = true;
          rec.material.opacity = Math.max(0.05, age);
        } else if (rec.material.transparent) {
          rec.material.transparent = false;
          rec.material.opacity = 1;
        }
      }
    }
    for (const [id, rec] of records) {
      if (!seen.has(id) && rec.mesh) rec.mesh.visible = false;
    }
  }

  function remove(id) {
    const rec = records.get(id);
    if (rec) { disposeRec(rec); records.delete(id); }
  }

  function removeAll() {
    for (const [, rec] of records) disposeRec(rec);
    records.clear();
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    removeAll();
    if (placeholderMat) { placeholderMat.dispose(); placeholderMat = null; }
    sharedGeo.dispose();
  }

  function stats() {
    /* segs：多段长文的 [当前段, 总段数]（验证/排查翻段用） */
    const segs = {};
    for (const [id, r] of records) if (r.segments > 1) segs[id] = [r.seg, r.segments];
    return { panels: records.size, textures: texCache.size, segs };
  }

  /* 排查用：单条记录的栅格化参数 + 当前纹理缩略图（dataURL，配合 __xrDebug 验证） */
  function debugRec(id) {
    const r = records.get(id);
    if (!r) return null;
    let texUrl = null;
    try {
      const src = r.material && r.material.map && r.material.map.image;
      if (src && src.width) {
        const c = document.createElement("canvas");
        const s = Math.min(1, 96 / Math.max(src.width, src.height));
        c.width = Math.max(1, Math.round(src.width * s));
        c.height = Math.max(1, Math.round(src.height * s));
        c.getContext("2d").drawImage(src, 0, 0, c.width, c.height);
        texUrl = c.toDataURL("image/png");
      }
    } catch (e) { texUrl = "err:" + e.message; }
    return {
      state: r.rasterState, seg: r.seg, segments: r.segments,
      wCss: r.wCss, hCss: r.hCss,
      entryW: r.entry.el.offsetWidth, entryH: r.entry.el.offsetHeight,
      entryConnected: r.entry.el.isConnected,
      imgW: r.img && r.img.naturalWidth, imgH: r.img && r.img.naturalHeight,
      fallback: !!r.fallbackCanvas,
      texUrl,
    };
  }

  return { group, sync, invalidate, invalidateAll, remove, removeAll, cycleSegment, raycast, positionOf, heightOf, widthOf, dispose, stats, debugRec };
}
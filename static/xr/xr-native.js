/* 原生 3D 模式（数据驱动）：不经过 DOM，直接用消息里的数据重建 3D 图形——
   ```chart JSON（与 2D 同源）→ bar/line/pie 几何体；```a2ui 组件树 → 原生 3D
   小部件（解析/绑定/状态机经桥复用 2D 端实现，见下方 a2ui 段注释）。
   面板模式保留为兜底开关（xr-main 里的「原生图表」HUD 按钮）。
   每条含图表/a2ui 的消息 → 一个 Group，跟随其面板位置滑入滑出（面板前方空地，
   落地放置）；点击饼图扇区显示名称/数值/百分比浮签（需求 3.1–3.5）。 */

import * as THREE from "three";

const CHART_COLORS = ["#5b8cff", "#7c5cff", "#3ecf8e", "#ffb86b", "#ff6b7a", "#4dd0e1", "#c792ea", "#a3e635"];
const CHART_RADIUS = 4.1;      // 图表放置半径（墙 R=6，面板前 ~1.9m 空地）
const BAR_MAX_H = 1.05;
const PIE_R = 0.55;            // 饼图外半径（米）
const PIE_INNER = 0.24;        // 环内半径（与 2D ["35%","65%"] 同比例）
const PIE_THICK = 0.14;
const MAX_CHARTS = 40;         // 驻留上限（超出移除最旧的不可见图表）
const FONT = "'SF Pro Text','PingFang SC','Noto Sans SC',sans-serif";
const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i;
const IMG_CENTER_Y = 1.15;     // 图片平面正常悬挂高度（中心，米）
const IMG_TARGET_H = 1.15;     // 图片加载后的目标高度
const IMG_MAX_W = 2.0;
const IMG_MAX_H = 1.6;
const IMG_FRAME = 0.035;       // 白框边宽

export function isImageFilename(name) { return IMAGE_RE.test(String(name || "")); }

const fmtNum = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return Math.abs(n) >= 1000 ? n.toLocaleString("en-US") : String(Math.round(n * 100) / 100);
};

/* ---------- spec 提取（与 2D renderChartBlocks 同一份数据：```chart JSON） ---------- */

export function extractChartSpec(content) {
  const text = String(content || "");
  if (!text.includes("```chart")) return null;
  const re = /```chart[^\n]*\n([\s\S]*?)```/g;
  let m;
  while ((m = re.exec(text))) {
    try {
      const spec = JSON.parse(m[1]);
      const norm = normalizeSpec(spec);
      if (norm) return norm;
    } catch (e) { /* 下一块/放弃 */ }
  }
  return null;
}

function normalizeSpec(spec) {
  if (!spec || typeof spec !== "object") return null;
  if (spec.type === "pie") {
    if (!Array.isArray(spec.data) || !spec.data.length) return null;
    const data = spec.data
      .map((d) => (typeof d === "object" && d ? { name: String(d.name ?? d.label ?? ""), value: Number(d.value ?? d) } : { name: String(d), value: Number(d) }))
      .filter((d) => isFinite(d.value));
    return data.length ? { type: "pie", title: spec.title ? String(spec.title) : "", data } : null;
  }
  if (spec.type === "bar" || spec.type === "line") {
    if (!Array.isArray(spec.data) || !spec.data.length) return null;
    const values = spec.data.map((v) => Number(v)).filter((v) => isFinite(v));
    if (!values.length) return null;
    const cats = values.map((_, i) => (Array.isArray(spec.categories) && spec.categories[i] != null ? String(spec.categories[i]) : String(i + 1)));
    return { type: spec.type, title: spec.title ? String(spec.title) : "", categories: cats, data: values };
  }
  return null;
}

/* ---------- 文字纹理（标题/数值标签/图例）：单行 Canvas → Sprite ---------- */

function makeTextSprite(text, opts) {
  const o = opts || {};
  const px = o.px || 44;
  const color = o.color || "#eef3f9";
  const weight = o.weight || 600;
  const pad = o.pad != null ? o.pad : Math.round(px * 0.25);
  const scalePerPx = o.scalePerPx != null ? o.scalePerPx : 0.0016; /* 44px 字 → ~7cm 高，3m 外可读 */
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = `${weight} ${px}px ${FONT}`;
  const tw = Math.max(2, Math.ceil(measure.measureText(text).width));
  const canvas = document.createElement("canvas");
  canvas.width = tw + pad * 2;
  canvas.height = px + pad * 2;
  const g = canvas.getContext("2d");
  if (o.bg) {
    g.fillStyle = o.bg;
    g.beginPath();
    const r = Math.min(canvas.height / 2, 14);
    g.moveTo(r, 0);
    g.arcTo(canvas.width, 0, canvas.width, canvas.height, r);
    g.arcTo(canvas.width, canvas.height, 0, canvas.height, r);
    g.arcTo(0, canvas.height, 0, 0, r);
    g.arcTo(0, 0, canvas.width, 0, r);
    g.fill();
  }
  g.font = `${weight} ${px}px ${FONT}`;
  g.fillStyle = color;
  g.textBaseline = "middle";
  g.fillText(text, pad, canvas.height / 2);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(canvas.width * scalePerPx, canvas.height * scalePerPx, 1);
  sprite.renderOrder = 5;
  return sprite;
}

function disposeSprite(sp) {
  if (!sp) return;
  if (sp.material) {
    if (sp.material.map) sp.material.map.dispose();
    sp.material.dispose();
  }
}

/* ---------- 图表构建（每次内容变化重建；尺寸以米计，落地 y=0） ---------- */

function buildBarChart(spec) {
  const g = new THREE.Group();
  const n = spec.data.length;
  const pitch = THREE.MathUtils.clamp(1.5 / Math.max(n, 1), 0.13, 0.42);
  const vMax = Math.max(...spec.data.map(Math.abs), 1e-9);
  const color = new THREE.Color(CHART_COLORS[0]);
  const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.1 });
  const geo = new THREE.CylinderGeometry(1, 1, 1, 12); /* 12 段圆柱（性能预算） */
  for (let i = 0; i < n; i++) {
    const h = Math.max(0.015, (Math.abs(spec.data[i]) / vMax) * BAR_MAX_H);
    const bar = new THREE.Mesh(geo, mat);
    bar.scale.set(pitch * 0.32, h, pitch * 0.32);
    const x = (i - (n - 1) / 2) * pitch;
    bar.position.set(x, h / 2, 0);
    g.add(bar);
    const lab = makeTextSprite(fmtNum(spec.data[i]), { px: 40, color: "#cdd9ea" });
    lab.position.set(x, h + 0.09, 0);
    g.add(lab);
    const cat = makeTextSprite(truncate(spec.categories[i], 10), { px: 34, color: "#8b9bb0" });
    cat.position.set(x, -0.07, pitch * 1.1);
    g.add(cat);
  }
  /* 地面基线 */
  const lineW = Math.max(1.0, n * pitch);
  const base = new THREE.Mesh(new THREE.BoxGeometry(lineW, 0.012, 0.012), new THREE.MeshBasicMaterial({ color: 0x273140 }));
  base.position.y = 0.006;
  g.add(base);
  return g;
}

function buildLineChart(spec) {
  const g = new THREE.Group();
  const n = spec.data.length;
  const pitch = THREE.MathUtils.clamp(1.5 / Math.max(n - 1, 1), 0.14, 0.4);
  const vMin = Math.min(0, ...spec.data);
  const vMax = Math.max(...spec.data, vMin + 1e-9);
  const span = vMax - vMin;
  const toY = (v) => 0.05 + ((v - vMin) / span) * BAR_MAX_H;
  const pts = [];
  const dotGeo = new THREE.SphereGeometry(0.028, 12, 8);
  const dotMat = new THREE.MeshStandardMaterial({ color: new THREE.Color(CHART_COLORS[0]), roughness: 0.4 });
  for (let i = 0; i < n; i++) {
    const x = (i - (n - 1) / 2) * pitch;
    const y = toY(spec.data[i]);
    pts.push(new THREE.Vector3(x, y, 0));
    const dot = new THREE.Mesh(dotGeo, dotMat);
    dot.position.set(x, y, 0);
    g.add(dot);
    const lab = makeTextSprite(fmtNum(spec.data[i]), { px: 36, color: "#cdd9ea" });
    lab.position.set(x, y + 0.1, 0);
    g.add(lab);
    if (n <= 14) {
      const cat = makeTextSprite(truncate(spec.categories[i], 10), { px: 32, color: "#8b9bb0" });
      cat.position.set(x, -0.07, 0);
      g.add(cat);
    }
  }
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: new THREE.Color(CHART_COLORS[0]) }));
  g.add(line);
  const base = new THREE.Mesh(new THREE.BoxGeometry(Math.max(1.0, (n - 1) * pitch), 0.012, 0.012), new THREE.MeshBasicMaterial({ color: 0x273140 }));
  base.position.y = 0.006;
  g.add(base);
  return g;
}

/* 扇区网格 + userData 记录扇区数据（点击浮签用，需求 3.2） */
function buildPieChart(spec) {
  const g = new THREE.Group();
  const total = spec.data.reduce((s, d) => s + d.value, 0) || 1;
  let a0 = 0;
  const shapeOf = (sa, ea) => {
    const shape = new THREE.Shape();
    shape.moveTo(PIE_INNER, 0);
    shape.absarc(0, 0, PIE_R, sa, ea, false);
    shape.absarc(0, 0, PIE_INNER, ea, sa, true);
    return shape;
  };
  spec.data.forEach((d, i) => {
    const frac = d.value / total;
    const ea = a0 + frac * Math.PI * 2;
    const geo = new THREE.ExtrudeGeometry(shapeOf(a0, ea), { depth: PIE_THICK, bevelEnabled: false, curveSegments: 24 });
    geo.rotateX(-Math.PI / 2); /* 形状 XY → 水平 XZ，挤出方向变 +Y，几何占 y∈[0, PIE_THICK] */
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: new THREE.Color(CHART_COLORS[i % CHART_COLORS.length]), roughness: 0.5 }));
    mesh.userData.pieSector = { name: d.name || `#${i + 1}`, value: d.value, pct: frac * 100 };
    g.add(mesh);
    /* 图例名放该扇区外缘中角（形状点 (cosθ,−sinθ) 旋转后映射到 XZ，与扇区对齐） */
    const mid = (a0 + ea) / 2;
    const lab = makeTextSprite(truncate(d.name || `#${i + 1}`, 12), { px: 36, color: CHART_COLORS[i % CHART_COLORS.length], weight: 500 });
    lab.position.set(Math.cos(mid) * (PIE_R + 0.16), PIE_THICK + 0.04, -Math.sin(mid) * (PIE_R + 0.16));
    g.add(lab);
    a0 = ea;
  });
  return g;
}

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function addTitle(g, title, topY) {
  if (!title) return;
  const sp = makeTextSprite(truncate(title, 24), { px: 52, color: "#eef3f9", bg: "rgba(16,22,34,0.72)", pad: 18 });
  sp.position.set(0, topY + 0.16, 0);
  g.add(sp);
}

function buildChartObject(spec) {
  const root = new THREE.Group();
  let chart;
  if (spec.type === "pie") {
    chart = buildPieChart(spec);
    root.add(chart);
    addTitle(root, spec.title, PIE_THICK + 0.2);
  } else {
    chart = spec.type === "bar" ? buildBarChart(spec) : buildLineChart(spec);
    root.add(chart);
    addTitle(root, spec.title, BAR_MAX_H + 0.2);
  }
  root.userData.chartKind = spec.type;
  return root;
}

/* ---------- a2ui 原生 3D 小部件（需求 3.3/3.4/3.5） ----------
   解析 / JSON Pointer 绑定 / surface 状态机经桥复用 2D 端同一实现（index.html
   a2uiParseMessages / a2uiValue / a2uiApplyMessages），这里只做「组件 → THREE」换形。
   目录 v1 原生支持：Column/Row/Card/Text/MetricCard/Progress/Callout/Timeline/
   PieChart/BarChart/LineChart；Table/Divider 目录内但 3D 不做 → 整条回退面板模式；
   未知组件渲染 3D 占位（2D `[a2ui: 类型名]` 的 3D 版），绝不因新组件崩溃（需求 3.5）。
   构建产物约定：{ obj, w, h }，obj 局部原点在底部中心（y=0 落地），尺寸以米计。 */

const A2UI_GAP = 0.09;    // Row/Column 布局间距
const A2UI_PAD = 0.14;    // Card/Metric/Callout 内边距
const A2UI_V1_NATIVE = new Set(["Column", "Row", "Card", "Text", "MetricCard", "Progress", "Callout", "Timeline", "PieChart", "BarChart", "LineChart"]);
const A2UI_V1_FALLBACK = new Set(["Table", "Divider"]);

export function extractA2uiBlocks(content) {
  const text = String(content || "");
  if (!text.includes("```a2ui")) return null;
  const re = /```a2ui[^\n]*\n([\s\S]*?)```/g;
  const blocks = [];
  let m;
  while ((m = re.exec(text))) blocks.push(m[1]);
  return blocks.length ? blocks : null;
}

/* sprite 锚点在中心 → 包一层组把原点挪到底部中心 */
function leaf(text, opts) {
  const sp = makeTextSprite(text, opts);
  const g = new THREE.Group();
  sp.position.y = sp.scale.y / 2;
  g.add(sp);
  return { obj: g, w: sp.scale.x, h: sp.scale.y };
}

function plateFor(w, h, color, z) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, 0.035), new THREE.MeshStandardMaterial({ color, roughness: 0.75, metalness: 0.08 }));
  mesh.position.set(0, h / 2, z != null ? z : -0.028);
  return mesh;
}

function layoutCol(kids) {
  const g = new THREE.Group();
  const h = kids.reduce((s, k) => s + k.h, 0) + A2UI_GAP * Math.max(0, kids.length - 1);
  const w = Math.max(0.12, ...kids.map((k) => k.w));
  let y = h;
  for (const k of kids) {
    y -= k.h;
    k.obj.position.set(0, y, 0);
    g.add(k.obj);
    y -= A2UI_GAP;
  }
  return { obj: g, w, h };
}

function layoutRow(kids) {
  const g = new THREE.Group();
  const w = kids.reduce((s, k) => s + k.w, 0) + A2UI_GAP * Math.max(0, kids.length - 1);
  const h = Math.max(0.12, ...kids.map((k) => k.h));
  let x = -w / 2;
  for (const k of kids) {
    k.obj.position.set(x + k.w / 2, (h - k.h) / 2, 0);
    g.add(k.obj);
    x += k.w + A2UI_GAP;
  }
  return { obj: g, w, h };
}

function buildText(comp, ctx) {
  const px = { h1: 64, h2: 52, h3: 44, caption: 30 }[comp.variant] || 42;
  return leaf(String(ctx.value(comp.text, ctx.model) ?? ""), { px, weight: comp.variant === "h1" ? 700 : 600, color: comp.variant === "caption" ? "#8b9bb0" : "#eef3f9" });
}

function buildMetricCard(comp, ctx) {
  const rows = [];
  const label = String(ctx.value(comp.label, ctx.model) ?? "");
  if (label) rows.push(leaf(label, { px: 30, color: "#8b9bb0" }));
  const head = [leaf(String(ctx.value(comp.value, ctx.model) ?? ""), { px: 58, weight: 700 })];
  if (comp.change != null) {
    const trend = ["up", "down", "flat"].includes(comp.trend) ? comp.trend : "flat";
    const pre = trend === "up" ? "▲ " : trend === "down" ? "▼ " : "";
    const color = trend === "up" ? "#3ecf8e" : trend === "down" ? "#ff6b7a" : "#8b9bb0";
    head.push(leaf(pre + String(ctx.value(comp.change, ctx.model) ?? ""), { px: 30, color }));
  }
  rows.push(layoutRow(head));
  if (comp.caption != null) {
    const cap = String(ctx.value(comp.caption, ctx.model) ?? "");
    if (cap) rows.push(leaf(cap, { px: 26, color: "#8b9bb0" }));
  }
  const body = layoutCol(rows);
  const w = body.w + 0.2, h = body.h + 0.18;
  const g = new THREE.Group();
  body.obj.position.set(0, 0.09, 0);
  g.add(plateFor(w, h, 0x1b2433), body.obj);
  return { obj: g, w, h };
}

const A2UI_TONE = { success: 0x3ecf8e, warning: 0xffb86b, danger: 0xff6b7a, default: 0x5b8cff };

function buildProgress(comp, ctx) {
  const value = Number(ctx.value(comp.value, ctx.model));
  if (!Number.isFinite(value)) return null;
  const maxRaw = Number(ctx.value(comp.max, ctx.model));
  const max = Number.isFinite(maxRaw) && maxRaw > 0 ? maxRaw : 100;
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const tone = ["success", "warning", "danger"].includes(comp.tone) ? comp.tone : "default";
  const label = comp.label != null ? String(ctx.value(comp.label, ctx.model) ?? "") : "";
  const head = layoutRow([leaf(label || " ", { px: 30 }), leaf(Math.round(pct) + "%", { px: 30, color: "#8b9bb0" })]);
  const trackW = Math.max(0.9, head.w);
  const track = new THREE.Mesh(new THREE.BoxGeometry(trackW, 0.05, 0.03), new THREE.MeshStandardMaterial({ color: 0x202b3a, roughness: 0.8 }));
  track.position.set(0, 0.025, 0);
  const fill = new THREE.Mesh(new THREE.BoxGeometry(Math.max(trackW * pct / 100, 0.02), 0.058, 0.038), new THREE.MeshStandardMaterial({ color: A2UI_TONE[tone], roughness: 0.5 }));
  fill.position.set(-trackW / 2 + (trackW * pct) / 100, 0.025, 0.004);
  const g = new THREE.Group();
  head.obj.position.set(0, 0.09, 0);
  g.add(head.obj, track);
  return { obj: g, w: Math.max(trackW, head.w), h: head.h + 0.09 };
}

const A2UI_SEV = { info: 0x5b8cff, success: 0x3ecf8e, warning: 0xffb86b, danger: 0xff6b7a };

function buildCallout(comp, ctx) {
  const sev = ["info", "success", "warning", "danger"].includes(comp.severity) ? comp.severity : "info";
  const rows = [];
  if (comp.title != null) {
    const ti = String(ctx.value(comp.title, ctx.model) ?? "");
    if (ti) rows.push(leaf(ti, { px: 36, weight: 700, color: "#" + new THREE.Color(A2UI_SEV[sev]).getHexString() }));
  }
  const txt = String(ctx.value(comp.text, ctx.model) ?? "");
  if (txt) rows.push(leaf(txt, { px: 32, weight: 500, color: "#dbe4f0" }));
  if (!rows.length) return null;
  const body = layoutCol(rows);
  const w = body.w + A2UI_PAD * 2 + 0.05, h = body.h + A2UI_PAD * 2;
  const edge = new THREE.Mesh(new THREE.BoxGeometry(0.05, h, 0.042), new THREE.MeshStandardMaterial({ color: A2UI_SEV[sev], roughness: 0.5 }));
  edge.position.set(-w / 2 + 0.025, h / 2, 0.0);
  const g = new THREE.Group();
  body.obj.position.set(0.05, A2UI_PAD, 0);
  g.add(plateFor(w, h, 0x161e2c), edge, body.obj);
  return { obj: g, w, h };
}

function buildTimeline(comp, ctx) {
  const items = ctx.value(comp.items, ctx.model);
  if (!Array.isArray(items) || !items.length) return null;
  const rows = [];
  for (const it of items) {
    if (!it || typeof it !== "object") continue;
    const parts = [];
    if (it.time != null) parts.push(leaf(String(it.time), { px: 26, color: "#8b9bb0" }));
    parts.push(leaf(String(it.title ?? ""), { px: 34, weight: 600 }));
    if (it.description != null && String(it.description)) parts.push(leaf(String(it.description), { px: 28, color: "#9fb0c5" }));
    const node = new THREE.Mesh(new THREE.SphereGeometry(0.032, 12, 8), new THREE.MeshStandardMaterial({ color: it.tone === "success" ? 0x3ecf8e : it.tone === "pending" ? 0x5a6478 : 0x5b8cff, roughness: 0.4 }));
    rows.push(layoutRow([{ obj: node, w: 0.08, h: 0.08 }, layoutCol(parts)]));
  }
  if (!rows.length) return null;
  const body = layoutCol(rows);
  const line = new THREE.Mesh(new THREE.BoxGeometry(0.014, Math.max(0.1, body.h - 0.12), 0.012), new THREE.MeshBasicMaterial({ color: 0x2a3648 }));
  line.position.set(-body.w / 2 + 0.04, body.h / 2, 0);
  const g = new THREE.Group();
  g.add(line, body.obj);
  return { obj: g, w: body.w, h: body.h };
}

/* a2ui 内嵌图表：数据语义与 2D a2uiBuildNode 一致（nameKey/valueKey/categories），
   几何直接复用上方的 chart 构建器（需求 3.1/3.2 同一套） */
function buildA2uiChart(comp, ctx) {
  const data = ctx.value(comp.data, ctx.model);
  if (!Array.isArray(data) || !data.length) return null;
  const title = comp.title != null ? String(ctx.value(comp.title, ctx.model) ?? "") : "";
  let spec;
  if (comp.component === "PieChart") {
    const nk = comp.nameKey || "name";
    const vk = comp.valueKey || "value";
    spec = { type: "pie", title, data: data.map((d) => (d && typeof d === "object" ? { name: String(d[nk]), value: Number(d[vk]) } : { name: String(d), value: 0 })) };
  } else {
    const cats = ctx.value(comp.categories, ctx.model);
    spec = { type: comp.component === "BarChart" ? "bar" : "line", title, categories: Array.isArray(cats) ? cats.map(String) : [], data: data.map((v) => Number(v)) };
  }
  const obj = buildChartObject(spec);
  const box = new THREE.Box3().setFromObject(obj);
  const size = box.getSize(new THREE.Vector3());
  return { obj, w: Math.max(size.x, 0.2), h: Math.max(size.y, 0.1) };
}

function buildPlaceholder(comp) {
  return leaf("[a2ui: " + String(comp.component || "?") + "]", { px: 34, color: "#8b9bb0", bg: "rgba(27,36,51,0.85)" });
}

function buildA2uiNode(comp, ctx) {
  if (!comp || typeof comp !== "object") return null;
  if (!A2UI_V1_NATIVE.has(comp.component)) return buildPlaceholder(comp);
  switch (comp.component) {
    case "Column": return layoutCol(kidsOf(comp, ctx));
    case "Row": return layoutRow(kidsOf(comp, ctx));
    case "Card": {
      const kids = kidsOf(comp, ctx);
      if (comp.title != null) {
        const ti = String(ctx.value(comp.title, ctx.model) ?? "");
        if (ti) kids.unshift(leaf(ti, { px: 40, weight: 700 }));
      }
      if (!kids.length) return null;
      const body = layoutCol(kids);
      const w = body.w + A2UI_PAD * 2, h = body.h + A2UI_PAD * 2;
      const g = new THREE.Group();
      body.obj.position.set(0, A2UI_PAD, 0);
      g.add(plateFor(w, h, 0x18202e), body.obj);
      return { obj: g, w, h };
    }
    case "Text": return buildText(comp, ctx);
    case "MetricCard": return buildMetricCard(comp, ctx);
    case "Progress": return buildProgress(comp, ctx);
    case "Callout": return buildCallout(comp, ctx);
    case "Timeline": return buildTimeline(comp, ctx);
    case "PieChart":
    case "BarChart":
    case "LineChart": return buildA2uiChart(comp, ctx);
    default: return buildPlaceholder(comp);
  }
}

function kidsOf(comp, ctx) {
  const ids = Array.isArray(comp.children) ? comp.children : (comp.child ? [comp.child] : []);
  const out = [];
  for (const cid of ids) {
    const c = ctx.components.get(String(cid));
    if (!c) continue;
    const k = buildA2uiNode(c, ctx);
    if (k) out.push(k);
  }
  return out;
}

/* 可达树里出现 Table/Divider → 整条消息回退面板模式（需求 3.3） */
function hasFallbackComp(comp, comps, seen) {
  if (!comp || typeof comp !== "object" || seen.has(comp)) return false;
  seen.add(comp);
  if (A2UI_V1_FALLBACK.has(comp.component)) return true;
  const ids = Array.isArray(comp.children) ? comp.children : (comp.child ? [comp.child] : []);
  for (const cid of ids) {
    if (hasFallbackComp(comps.get(String(cid)), comps, seen)) return true;
  }
  return false;
}

function buildA2uiObject(blocks, bridge) {
  if (!bridge || !bridge.parse || !bridge.apply || !bridge.value) return null;
  const roots = [];
  try {
    for (const block of blocks) {
      for (const s of bridge.apply(bridge.parse(block)).values()) {
        const rootComp = s.components.get("root");
        if (!rootComp || hasFallbackComp(rootComp, s.components, new Set())) continue;
        const node = buildA2uiNode(rootComp, { components: s.components, model: s.model, value: bridge.value });
        if (node) roots.push(node);
      }
    }
  } catch (e) {
    return null; /* 解析失败 → 面板模式显示 2D 错误框（与 2D 一致） */
  }
  if (!roots.length) return null;
  if (roots.length === 1) return roots[0].obj;
  return layoutCol(roots).obj;
}

/* ---------- 图片消息：白框纹理平面（需求 3.6）。本地原点在平面中心 ----------
   （正常悬挂在所属消息面板的中心高度，随卷绕布局爬升；聚焦时由 override 提到视点高度）。
   纹理异步加载：/api/ 附件经 opts.fetchImage 取 blob URL；失败保持深色占位面，
   不重试（面板模式的 2D 图片始终兜底可见）。 */

function buildImageObject(msgId) {
  const g = new THREE.Group();
  const frame = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshStandardMaterial({ color: 0xf2f4f8, roughness: 0.6 }));
  frame.position.z = -0.008;
  const pic = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ color: 0x1a2230 }));
  g.add(frame, pic);
  g.userData.isImage = true;
  g.userData.msgId = msgId;
  g.userData.pic = pic;
  g.userData.frame = frame;
  return g;
}

function applyImageTexture(rec, tex) {
  const pic = rec.obj.userData.pic, frame = rec.obj.userData.frame;
  const iw = (tex.image && tex.image.width) || 1, ih = (tex.image && tex.image.height) || 1;
  let w = IMG_TARGET_H * (iw / ih), h = IMG_TARGET_H;
  if (w > IMG_MAX_W) { w = IMG_MAX_W; h = w * (ih / iw); }
  if (h > IMG_MAX_H) { h = IMG_MAX_H; w = h * (iw / ih); }
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  if (pic.material.map && pic.material.map.dispose) pic.material.map.dispose();
  pic.material.map = tex;
  pic.material.color.set(0xffffff);
  pic.material.needsUpdate = true;
  pic.scale.set(w, h, 1);
  frame.scale.set(w + IMG_FRAME * 2, h + IMG_FRAME * 2, 1);
}

function startImageLoad(rec, msg, fetchImage) {
  const my = (rec.loadSeq = (rec.loadSeq || 0) + 1);
  (async () => {
    let src = String(msg.downloadUrl || "");
    let objUrl = null;
    try {
      if (src.startsWith("/api/")) {
        if (typeof fetchImage !== "function") return; /* 无鉴权取图通道 → 保持占位面 */
        objUrl = src = await fetchImage(src);
      }
      const tex = await new THREE.TextureLoader().loadAsync(src);
      if (rec.disposed || my !== rec.loadSeq) { tex.dispose(); return; }
      applyImageTexture(rec, tex);
    } catch (e) { /* 占位面兜底，不重试 */ }
    finally { if (objUrl) URL.revokeObjectURL(objUrl); }
  })();
}

/* ---------- 系统：生命周期 + 每帧跟随面板 ---------- */

export function createNativeSystem(opts) {
  const a2ui = (opts && opts.a2ui) || null;   // { parse, value, apply } —— 2D 端 a2ui 数据语义桥
  const fetchImage = (opts && opts.fetchImage) || null;  // url → blob URL（/api/ 附件带 token）
  const group = new THREE.Group();
  const charts = new Map();   // msgId → { obj, specKey }
  const order = [];
  const failedA2ui = new Map();   // msgId → specKey（a2ui 回退面板后不再重建）
  let enabled = true;
  let tipSprite = null;
  let tipTimer = 0;
  let focusedId = null;       // 面板聚焦中的消息：原生物让位（需求 7.2）
  let overridePose = null;    // 图片聚焦：{x,y,z,rotY}——聚焦平面平滑到此位姿

  function remove(id) {
    const rec = charts.get(id);
    if (!rec) return;
    rec.disposed = true;
    group.remove(rec.obj);
    rec.obj.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material && o.material.dispose) {
        if (o.material.map && o.material.map.dispose && !o.isSprite) o.material.map.dispose();
        o.material.dispose();
      }
      if (o.isSprite) disposeSprite(o);
    });
    charts.delete(id);
    const i = order.indexOf(id);
    if (i >= 0) order.splice(i, 1);
  }

  function removeAll() {
    for (const id of Array.from(charts.keys())) remove(id);
  }

  function hideTip() {
    if (tipSprite) { group.remove(tipSprite); disposeSprite(tipSprite); tipSprite = null; }
  }

  /* entries：xr-main 布局后的窗口内条目（e._phi 已设置）；panelPos(id) → 面板当前平滑位置或 null */
  function sync(entries, panelPos, dt) {
    if (!enabled) { for (const [, rec] of charts) rec.obj.visible = false; hideTip(); return; }
    const seen = new Set();
    for (const e of entries) {
      const content = e.msg && e.msg.content;
      const spec = extractChartSpec(content);
      const blocks = spec ? null : extractA2uiBlocks(content);
      const m = e.msg;
      const isImg = m && (m.msgType === "image" || isImageFilename(m.attachmentName)) && m.downloadUrl;
      if (!spec && !blocks && !isImg) continue;
      seen.add(e.id);
      let rec = charts.get(e.id);
      const specKey = (spec ? "c" : blocks ? "a" : "i") + JSON.stringify(spec || blocks || m.downloadUrl);
      if (rec && rec.specKey !== specKey) { remove(e.id); rec = null; } /* 流式/更新后重建 */
      if (!rec && failedA2ui.get(e.id) === specKey) continue; /* a2ui 回退面板：不重复构建 */
      if (!rec) {
        let obj;
        try { obj = spec ? buildChartObject(spec) : blocks ? buildA2uiObject(blocks, a2ui) : buildImageObject(e.id); } catch (err) { obj = null; }
        if (!obj) {
          if (blocks) failedA2ui.set(e.id, specKey);
          continue;
        }
        failedA2ui.delete(e.id);
        group.add(obj);
        rec = { obj, specKey, baseY: obj.position.y }; /* baseY：聚焦后回归的悬挂高度 */
        charts.set(e.id, rec);
        order.push(e.id);
        if (blocks == null && spec == null) {
          rec.obj.position.y = IMG_CENTER_Y;
          rec.baseY = IMG_CENTER_Y;
          rec.isImg = true; /* 图片悬挂高度跟随所属面板（卷绕布局下面板随龄爬升） */
          startImageLoad(rec, m, fetchImage);
        }
        /* 驻留上限：移除最旧的不可见图表 */
        while (order.length > MAX_CHARTS) {
          const oldest = order.find((id) => !charts.get(id).obj.visible) || order[0];
          if (oldest === e.id) break;
          remove(oldest);
        }
      }
      if (focusedId === e.id && !overridePose) { rec.obj.visible = false; continue; } /* 面板聚焦 → 原生物让位 */
      const focusImg = focusedId === e.id && overridePose; /* 图片聚焦：仅目标平面接管位姿（需求 3.6） */
      const pp = focusImg ? overridePose : panelPos(e.id);
      rec.obj.visible = !!pp;
      if (pp) {
        const l = 1 - Math.exp(-(dt || 0.016) * 9);
        if (focusImg) {
          /* 图片聚焦：整位姿平滑（含高度与朝向），目标 ~40° 视角（需求 7.2） */
          rec.obj.position.x += (pp.x - rec.obj.position.x) * l;
          rec.obj.position.y += (pp.y - rec.obj.position.y) * l;
          rec.obj.position.z += (pp.z - rec.obj.position.z) * l;
          let dr = pp.rotY - rec.obj.rotation.y;
          while (dr > Math.PI) dr -= Math.PI * 2;
          while (dr < -Math.PI) dr += Math.PI * 2;
          rec.obj.rotation.y += dr * l;
        } else {
          const hr = Math.hypot(pp.x, pp.z) || 1;
          const k = CHART_RADIUS / hr;
          /* 直列布局：面板全部同方位（pp.x≈0），按消息 id 的稳定横向偏移散开，
             避免多张图表/图片叠在同一点（xr-main 的 hashSpread 计算，随条目传入） */
          const tx = pp.x * k + (e._spread || 0) * k, tz = pp.z * k;
          rec.obj.position.x += (tx - rec.obj.position.x) * l;
          rec.obj.position.z += (tz - rec.obj.position.z) * l;
          /* 图片跟随面板中心高度；图表/a2ui 立于地面（baseY），随方位即可对应 */
          rec.obj.position.y += ((rec.isImg ? pp.y : (rec.baseY || 0)) - rec.obj.position.y) * l; /* 退聚焦后回到悬挂高度 */
          rec.obj.rotation.y = Math.atan2(tx, tz) + Math.PI;
        }
        rec.obj.visible = true;
      }
    }
    for (const [id, rec] of charts) {
      if (!seen.has(id)) rec.obj.visible = false;
    }
    for (const id of failedA2ui.keys()) {
      if (!seen.has(id)) failedA2ui.delete(id);
    }
  }

  /* 点击拾取：命中饼图扇区 → 浮签（名称/数值/百分比，需求 3.2） */
  function pickSector(raycaster) {
    if (!enabled) return null;
    const hits = raycaster.intersectObjects(group.children, true);
    for (const h of hits) {
      if (!h.object.visible || !h.object.userData.pieSector) continue;
      let p = h.object;
      while (p.parent && p.parent !== group) p = p.parent;
      if (!p.visible) continue;
      return h.object;
    }
    return null;
  }

  /* 点击拾取：命中图片平面 → 返回 msgId（需求 3.6 指向放大） */
  function pickImage(raycaster) {
    if (!enabled) return null;
    const hits = raycaster.intersectObjects(group.children, true);
    for (const h of hits) {
      if (!h.object.visible) continue;
      let p = h.object;
      while (p.parent && p.parent !== group) p = p.parent;
      if (p.visible && p.userData && p.userData.isImage) return p.userData.msgId;
    }
    return null;
  }

  function showSectorTip(sector) {
    hideTip();
    const s = sector.userData.pieSector;
    const sp = makeTextSprite(`${s.name} · ${fmtNum(s.value)} · ${s.pct.toFixed(1)}%`, { px: 46, color: "#eef3f9", bg: "rgba(16,22,34,0.85)" });
    const box = new THREE.Box3().setFromObject(sector);
    const c = box.getCenter(new THREE.Vector3());
    sp.position.set(c.x, Math.max(box.max.y, PIE_THICK) + 0.22, c.z);
    group.add(sp);
    tipSprite = sp;
    if (tipTimer) clearTimeout(tipTimer);
    tipTimer = setTimeout(hideTip, 5000);
  }

  return { group, sync, pickSector, showSectorTip, pickImage, remove, removeAll, setEnabled: (b) => { enabled = !!b; if (!enabled) hideTip(); }, isEnabled: () => enabled, setFocused: (id) => { focusedId = id || null; }, setOverride: (pose) => { overridePose = pose || null; }, stats: () => ({ charts: charts.size }), dispose: () => { hideTip(); removeAll(); focusedId = null; overridePose = null; } };
}
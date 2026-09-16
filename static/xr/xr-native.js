/* 原生 3D 模式（数据驱动）：不经过 DOM，直接用消息里的 ```chart JSON 重建 3D 图形。
   数据与 2D 同源——同一份 spec（type: bar/line/pie + categories/data），只是渲染端换成
   几何体（需求 3.1/3.2）；面板模式保留为兜底开关（xr-main 里的「原生图表」HUD 按钮）。
   每条含图表的消息 → 一个 chart Group，跟随其面板位置滑入滑出（面板前方空地，落地放置）；
   点击饼图扇区显示名称/数值/百分比浮签（需求 3.2）。 */

import * as THREE from "three";

const CHART_COLORS = ["#5b8cff", "#7c5cff", "#3ecf8e", "#ffb86b", "#ff6b7a", "#4dd0e1", "#c792ea", "#a3e635"];
const CHART_RADIUS = 4.1;      // 图表放置半径（墙 R=6，面板前 ~1.9m 空地）
const BAR_MAX_H = 1.05;
const PIE_R = 0.55;            // 饼图外半径（米）
const PIE_INNER = 0.24;        // 环内半径（与 2D ["35%","65%"] 同比例）
const PIE_THICK = 0.14;
const MAX_CHARTS = 40;         // 驻留上限（超出移除最旧的不可见图表）
const FONT = "'SF Pro Text','PingFang SC','Noto Sans SC',sans-serif";

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

/* ---------- 系统：生命周期 + 每帧跟随面板 ---------- */

export function createNativeSystem(opts) {
  const group = new THREE.Group();
  const charts = new Map();   // msgId → { obj, specKey }
  const order = [];
  let enabled = true;
  let tipSprite = null;
  let tipTimer = 0;

  function remove(id) {
    const rec = charts.get(id);
    if (!rec) return;
    group.remove(rec.obj);
    rec.obj.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material && o.material.dispose) o.material.dispose();
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
      const spec = extractChartSpec(e.msg && e.msg.content);
      if (!spec) continue;
      seen.add(e.id);
      let rec = charts.get(e.id);
      const specKey = JSON.stringify(spec);
      if (rec && rec.specKey !== specKey) { remove(e.id); rec = null; } /* 流式/更新后重建 */
      if (!rec) {
        let obj;
        try { obj = buildChartObject(spec); } catch (err) { continue; }
        group.add(obj);
        rec = { obj, specKey };
        charts.set(e.id, rec);
        order.push(e.id);
        /* 驻留上限：移除最旧的不可见图表 */
        while (order.length > MAX_CHARTS) {
          const oldest = order.find((id) => !charts.get(id).obj.visible) || order[0];
          if (oldest === e.id) break;
          remove(oldest);
        }
      }
      const pp = panelPos(e.id);
      rec.obj.visible = !!pp;
      if (pp) {
        const hr = Math.hypot(pp.x, pp.z) || 1;
        const k = CHART_RADIUS / hr;
        const tx = pp.x * k, tz = pp.z * k;
        const l = 1 - Math.exp(-(dt || 0.016) * 9);
        rec.obj.position.x += (tx - rec.obj.position.x) * l;
        rec.obj.position.z += (tz - rec.obj.position.z) * l;
        rec.obj.rotation.y = Math.atan2(tx, tz) + Math.PI;
        rec.obj.visible = true;
      }
    }
    for (const [id, rec] of charts) {
      if (!seen.has(id)) rec.obj.visible = false;
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

  return { group, sync, pickSector, showSectorTip, remove, removeAll, setEnabled: (b) => { enabled = !!b; if (!enabled) hideTip(); }, isEnabled: () => enabled, stats: () => ({ charts: charts.size }), dispose: () => { hideTip(); removeAll(); } };
}
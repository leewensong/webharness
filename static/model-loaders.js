/* 多格式 3D 模型解析（需求 13.5）：glTF 家族之外的 FBX/OBJ/STL/PLY/3MF/USD(Z)。
   2D 缩略图（model-preview.js）与 XR 世界摆放（xr-files.js）共用；各格式 Loader 按需
   动态 import，不进首屏。后处理参考 webxr-3d-viewer 的 normalizeModel：几何包成 Mesh、
   无面 PLY 渲染成点云、补法线/材质、隐藏模型自带灯光。只处理单文件自包含资源——外挂
   .mtl/贴图找不到时照常显示几何。 */

const FORMAT_BY_EXT = {
  glb: "gltf", gltf: "gltf", vrm: "gltf",
  fbx: "fbx", obj: "obj", stl: "stl", ply: "ply", "3mf": "3mf",
  usdz: "usd", usd: "usd", usda: "usd", usdc: "usd",
};
const FORMAT_BY_MIME = {
  "model/gltf-binary": "gltf", "model/gltf+json": "gltf", "model/vrm": "gltf",
  "model/x-fbx": "fbx", "model/obj": "obj", "model/stl": "stl", "model/x-ply": "ply",
  "model/3mf": "3mf", "model/vnd.usdz+zip": "usd", "model/vnd.usd": "usd",
};

export const MODEL_EXT_RE = /\.(glb|gltf|vrm|fbx|obj|stl|ply|3mf|usdz|usda|usdc|usd)$/i;

/* mime 优先（服务端权威分类），其次文件名扩展名；不认识返回 null */
export function modelFormatOf(name, mime) {
  const m = String(mime || "").split(";")[0].trim().toLowerCase();
  if (FORMAT_BY_MIME[m]) return FORMAT_BY_MIME[m];
  const match = /\.([a-z0-9]+)$/i.exec(String(name || ""));
  return match ? FORMAT_BY_EXT[match[1].toLowerCase()] || null : null;
}

export function isGltfFamily(name, mime) {
  return modelFormatOf(name, mime) === "gltf";
}

/* 同一 Loader 类只 import 一次 */
const loaderCache = new Map();
function importLoader(format) {
  if (!loaderCache.has(format)) {
    const p = {
      gltf: () => import("three/addons/loaders/GLTFLoader.js").then((m) => m.GLTFLoader),
      fbx: () => import("three/addons/loaders/FBXLoader.js").then((m) => m.FBXLoader),
      obj: () => import("three/addons/loaders/OBJLoader.js").then((m) => m.OBJLoader),
      stl: () => import("three/addons/loaders/STLLoader.js").then((m) => m.STLLoader),
      ply: () => import("three/addons/loaders/PLYLoader.js").then((m) => m.PLYLoader),
      "3mf": () => import("three/addons/loaders/3MFLoader.js").then((m) => m.ThreeMFLoader),
      usd: () => import("three/addons/loaders/USDLoader.js").then((m) => m.USDLoader),
    }[format];
    if (!p) return Promise.reject(new Error("unsupported model format: " + format));
    const job = p();
    job.catch(() => loaderCache.delete(format)); /* 加载失败不缓存，下次重试 */
    loaderCache.set(format, job);
  }
  return loaderCache.get(format);
}

/* 外挂资源（.mtl / 贴图 / .bin）一律拦截为空：共同文件是单文件，不能让 Loader 去请求
   房间内容端点之外的相对路径（会 404 刷日志，甚至打到别的同名文件）。 */
function isolatedManager(THREE) {
  const manager = new THREE.LoadingManager();
  manager.setURLModifier((url) => (/^(blob:|data:)/.test(url) ? url : "data:,"));
  return manager;
}

function finishObject(THREE, obj) {
  obj.traverse((child) => {
    if (child.isLight) child.visible = false;
    if (!child.isMesh && !child.isPoints) return;
    if (child.isMesh && child.geometry && !child.geometry.attributes.normal) child.geometry.computeVertexNormals();
    if (!child.material) child.material = new THREE.MeshStandardMaterial({ color: 0x9aa4b2, roughness: 0.6 });
  });
  return obj;
}

function geometryToObject(THREE, geom) {
  const hasColor = !!geom.attributes.color;
  const vertexCount = geom.attributes.position ? geom.attributes.position.count : 0;
  /* PLY 点云：无 index 且没有组成三角形的面数据（点数不是 3 的倍数或显式无面）→ Points */
  if (!geom.index && geom.userData && geom.userData.isPointCloud) {
    return new THREE.Points(geom, new THREE.PointsMaterial({ size: 0.01, sizeAttenuation: true, vertexColors: hasColor, color: hasColor ? 0xffffff : 0xb8c4d6 }));
  }
  if (!geom.index && vertexCount % 3 !== 0) {
    return new THREE.Points(geom, new THREE.PointsMaterial({ size: 0.01, sizeAttenuation: true, vertexColors: hasColor, color: hasColor ? 0xffffff : 0xb8c4d6 }));
  }
  if (!geom.attributes.normal) geom.computeVertexNormals();
  return new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
    color: hasColor ? 0xffffff : 0x9aa4b2, vertexColors: hasColor, roughness: 0.6, metalness: 0.05,
  }));
}

/* PLY 头里 element face 0（或缺失）= 点云；只读前 4KB 头部文本 */
function plyIsPointCloud(buffer) {
  const head = new TextDecoder().decode(new Uint8Array(buffer, 0, Math.min(4096, buffer.byteLength)));
  const end = head.indexOf("end_header");
  if (end < 0) return false;
  const m = /element\s+face\s+(\d+)/.exec(head.slice(0, end));
  return !m || Number(m[1]) === 0;
}

/* format + ArrayBuffer → { object, animations }；失败抛错由调用方降级 */
export async function parseModel(format, buffer) {
  const THREE = await import("three");
  const Loader = await importLoader(format);
  const loader = new Loader(isolatedManager(THREE));
  if (format === "gltf") {
    const gltf = await new Promise((resolve, reject) => {
      try { loader.parse(buffer, "", resolve, reject); } catch (e) { reject(e); }
    });
    const object = gltf.scene || (gltf.scenes && gltf.scenes[0]);
    if (!object) throw new Error("empty model");
    return { object, animations: gltf.animations || [] };
  }
  if (format === "usd") {
    const group = await new Promise((resolve, reject) => {
      try { loader.parse(buffer, "", resolve, reject); } catch (e) { reject(e); }
    });
    return { object: finishObject(THREE, group), animations: group.animations || [] };
  }
  if (format === "obj") {
    const object = loader.parse(new TextDecoder().decode(buffer));
    return { object: finishObject(THREE, object), animations: [] };
  }
  if (format === "fbx") {
    const object = loader.parse(buffer, "");
    return { object: finishObject(THREE, object), animations: object.animations || [] };
  }
  if (format === "3mf") {
    const inner = loader.parse(buffer);
    /* 3MF 规范是 Z-up：外包一层转成 three 的 Y-up */
    inner.rotation.x = -Math.PI / 2;
    const object = new THREE.Group();
    object.add(inner);
    return { object: finishObject(THREE, object), animations: [] };
  }
  /* STL / PLY 返回 BufferGeometry */
  const geom = loader.parse(buffer);
  if (format === "ply" && plyIsPointCloud(buffer)) geom.userData.isPointCloud = true;
  /* STL 也多为 Z-up 的 CAD 导出，但没有可靠标记，保持原样，交给摆放后旋转 */
  return { object: finishObject(THREE, geometryToObject(THREE, geom)), animations: [] };
}

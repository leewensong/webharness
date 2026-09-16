/* 3D 模型附件预览（glb/gltf/vrm）：按模型外框包围盒自动取景，渲染一张预览缩略图。
   2D 侧独立小模块——只在消息里真的出现 3D 附件时才被动态 import（2D 首屏零 3D 依赖）。
   失败/超时一律返回 null，由调用方降级为静态图标；任何异常不得影响消息渲染。 */

const MODEL_EXT_RE = /\.(glb|gltf|vrm)$/i;

export function isModelFilename(name) {
  return MODEL_EXT_RE.test(String(name || ""));
}

/* 成功结果按 URL 缓存（dataURL）；失败不缓存，下次渲染重试 */
const cache = new Map();
const inflight = new Map();
/* 串行队列：栅格化是重活，错峰执行 */
let queue = Promise.resolve();
let sharedRenderer = null;

const TIMEOUT_MS = 8000;
const SIZE = 256;

async function loadDeps() {
  const THREE = await import("three");
  const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
  return { THREE, GLTFLoader };
}

function getRenderer(THREE) {
  if (!sharedRenderer) {
    sharedRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    sharedRenderer.setSize(SIZE, SIZE, false);
    sharedRenderer.setClearColor(0x000000, 0);
  }
  return sharedRenderer;
}

function disposeObject(THREE, obj) {
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

/* objUrl: blob: URL（调用方已处理好鉴权/跨域取回）→ 预览 dataURL 或 null。
   超时放弃后调用方不等待本 Promise，模型资源在 finally 中释放。 */
async function renderFromObjectUrl(objUrl) {
  const { THREE, GLTFLoader } = await loadDeps();
  const loader = new GLTFLoader();
  const gltf = await loader.loadAsync(objUrl);
  const model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
  if (!model) throw new Error("empty model");

  const box = new THREE.Box3().setFromObject(model);
  if (box.isEmpty()) throw new Error("empty bounds");
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;

  try {
    const scene = new THREE.Scene();
    scene.add(model);
    scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x2a3242, 1.5));
    const dir = new THREE.DirectionalLight(0xffffff, 1.6);
    dir.position.set(1.5, 3, 2);
    scene.add(dir);

    const camera = new THREE.PerspectiveCamera(40, 1, maxDim / 100, maxDim * 10);
    const viewDir = new THREE.Vector3(1, 0.55, 1).normalize();
    const dist = (maxDim / 2) / Math.tan((camera.fov * Math.PI) / 360) * 1.25;
    camera.position.copy(center).addScaledVector(viewDir, dist);
    camera.lookAt(center);

    const renderer = getRenderer(THREE);
    renderer.render(scene, camera);
    return renderer.domElement.toDataURL("image/png");
  } finally {
    disposeObject(THREE, model);
  }
}

/* url → dataURL（成功）或 null（失败/超时）。getBlob: 可选的自定义取回（服务器附件需带 token）。 */
export function renderModelPreview(url, opts) {
  if (!url) return Promise.resolve(null);
  if (cache.has(url)) return Promise.resolve(cache.get(url));
  if (inflight.has(url)) return inflight.get(url);

  const getBlob = (opts && opts.getBlob) || ((u) => fetch(u).then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.blob(); }));
  const job = queue.then(async () => {
    let blobUrl = null;
    try {
      const blob = await getBlob(url);
      blobUrl = URL.createObjectURL(blob);
      const work = renderFromObjectUrl(blobUrl).catch(() => null);
      const dataUrl = await Promise.race([
        work,
        new Promise((resolve) => setTimeout(() => resolve(null), TIMEOUT_MS)),
      ]);
      if (dataUrl) cache.set(url, dataUrl);
      return dataUrl;
    } catch (err) {
      return null;
    } finally {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
      inflight.delete(url);
    }
  });
  inflight.set(url, job);
  /* 队列吞掉错误，保证后续任务继续跑 */
  queue = job.catch(() => {});
  return job;
}
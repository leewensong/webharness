/* xr-avatars.js — 成员 3D 形象（需求 4）：GLB/GLTF/VRM 加载 + 缺省胶囊化身 + 名牌。
   数据来自 roomEvents.onlineUsers（2D 桥，3D 只订阅不写）；站位按 hash(username)
   稳定散列到环形槽位；模型加载串行化（同时在途 ≤2）；任何加载失败静默回退缺省
   化身（需求 4.5）。退出 3D 时 dispose 全量释放。 */

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const RING_R = 2.9;        // 站位环半径（墙 R=6 与中央视角之间）
const SLOTS = 16;          // 站位槽位数（hash 取模，稳定不跳）
const AVATAR_H = 1.55;     // 模型归一化目标高度（米）
const CAP_H = 1.46;        // 缺省胶囊总高
const PLATE_W = 1.1;       // 名牌精灵宽（米）

function hashStr(s) {
  let h = 2166136261;
  const str = String(s || "");
  for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  return h >>> 0;
}

function hueOf(username) {
  return hashStr(username) % 360;
}

/* 名牌：用户名 + 在线绿点 + 房主 ★ 标识（需求 4.6）。canvas → Sprite 常驻头顶。 */
function buildNamePlate(username, isOwner, onlineTag) {
  const c = document.createElement("canvas");
  c.width = 512;
  c.height = 128;
  const g = c.getContext("2d");
  g.clearRect(0, 0, 512, 128);
  const rounded = () => {
    g.beginPath();
    g.roundRect(6, 14, 500, 100, 26);
  };
  rounded();
  g.fillStyle = "rgba(10,14,22,0.72)";
  g.fill();
  g.lineWidth = 3;
  g.strokeStyle = isOwner ? "rgba(255,204,102,0.9)" : "rgba(90,110,150,0.55)";
  g.stroke();
  /* 在线绿点 */
  g.fillStyle = "#3ecf8e";
  g.beginPath();
  g.arc(48, 64, 14, 0, Math.PI * 2);
  g.fill();
  /* 房主 ★ + 用户名 */
  g.textBaseline = "middle";
  let x = 78;
  if (isOwner) {
    g.fillStyle = "#ffcc66";
    g.font = "bold 52px system-ui, sans-serif";
    g.fillText("★", x, 66);
    x += 58;
  }
  g.fillStyle = isOwner ? "#ffe2a8" : "#e8ecf4";
  g.font = "bold 50px system-ui, sans-serif";
  const tag = isOwner && onlineTag ? ` ${onlineTag}` : "";
  let label = username + tag;
  while (g.measureText(label).width > 512 - x - 24 && label.length > 2) label = label.slice(0, -2) + "…";
  g.fillText(label, x, 66);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  sp.scale.set(PLATE_W, PLATE_W * 0.25, 1);
  return sp;
}

export function createAvatarSystem(opts) {
  const t = opts.t || ((k) => k);
  const group = new THREE.Group();
  const avatars = new Map();      // username → rec { root, plate, baseY, capMesh?, disposed }
  const queue = [];               // 待加载的 { username, url }
  let inFlight = 0;
  const MAX_CONCURRENT = 2;

  const gltfLoader = new GLTFLoader();
  gltfLoader.register((parser) => new VRMLoaderPlugin(parser));

  function disposeRec(rec) {
    rec.disposed = true;
    group.remove(rec.root);
    rec.root.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) {
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        for (const mt of mats) {
          if (mt.map && mt.map.dispose) mt.map.dispose();
          mt.dispose();
        }
      }
    });
  }

  function buildCapsule(rec, username, isOwner) {
    const hue = hueOf(username);
    const color = new THREE.Color().setHSL(hue / 360, 0.52, 0.55);
    const mesh = new THREE.Mesh(
      new THREE.CapsuleGeometry(0.28, 0.9, 4, 16),
      new THREE.MeshStandardMaterial({ color, roughness: 0.75 }),
    );
    mesh.position.y = CAP_H / 2;
    rec.root.add(mesh);
    rec.capMesh = mesh;
    rec.baseY = 0;
  }

  /* 模型归一化：高度 → AVATAR_H，脚底落地（y=0）。 */
  function normalizeModel(root) {
    let box = new THREE.Box3().setFromObject(root);
    const h = box.max.y - box.min.y;
    if (h > 0.01) {
      const s = AVATAR_H / h;
      root.scale.multiplyScalar(s);
      box = new THREE.Box3().setFromObject(root);
    }
    root.position.y -= box.min.y;
  }

  async function loadOne(job) {
    const rec = avatars.get(job.username);
    if (!rec || rec.disposed || rec.modelApplied) return;
    let objUrl = null;
    try {
      let src = job.url;
      if (src.startsWith("/api/")) {
        const resp = await fetch(src, { headers: opts.token ? { Authorization: "Bearer " + opts.token() } : {} });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        objUrl = URL.createObjectURL(await resp.blob());
        src = objUrl;
      }
      const gltf = await gltfLoader.loadAsync(src);
      if (rec.disposed) return;
      const vrm = gltf.userData && gltf.userData.vrm;
      const model = (vrm && vrm.scene) || gltf.scene || (gltf.scenes && gltf.scenes[0]);
      if (!model) throw new Error("empty scene");
      if (vrm) VRMUtils.rotateVRM0(vrm); /* VRM0 面向 +Z，转到 three.js -Z 约定 */
      normalizeModel(model);
      if (rec.capMesh) {
        rec.root.remove(rec.capMesh);
        rec.capMesh.geometry.dispose();
        rec.capMesh.material.dispose();
        rec.capMesh = null;
      }
      rec.root.add(model);
      rec.modelApplied = true;
      rec.plate.position.y = AVATAR_H + 0.32;
    } catch (err) {
      /* 静默回退：缺省胶囊保持原位（需求 4.5） */
    } finally {
      if (objUrl) URL.revokeObjectURL(objUrl);
    }
  }

  function pump() {
    while (inFlight < MAX_CONCURRENT && queue.length) {
      const job = queue.shift();
      inFlight++;
      loadOne(job).finally(() => { inFlight--; pump(); });
    }
  }

  function placeAvatar(rec, username) {
    const ang = ((hashStr(username) % SLOTS) / SLOTS) * Math.PI * 2;
    rec.root.position.set(RING_R * Math.sin(ang), 0, RING_R * Math.cos(ang));
    rec.root.rotation.y = Math.atan2(-rec.root.position.x, -rec.root.position.z); /* 面向中央 */
  }

  /* roomEvents.onlineUsers → 同步成员形象（增/换名牌/移除离线）。 */
  function applyRoom(info) {
    if (!info || info.closed) return;
    const users = (info.onlineUsers || []).filter((u) => u && u.username && u.username !== (opts.username && opts.username()));
    const seen = new Set();
    for (const u of users) {
      seen.add(u.username);
      let rec = avatars.get(u.username);
      if (!rec) {
        const root = new THREE.Group();
        rec = { root, plate: null, capMesh: null, baseY: 0, disposed: false, modelApplied: false, isOwner: !!u.isRoomOwner };
        buildCapsule(rec, u.username, rec.isOwner);
        rec.plate = buildNamePlate(u.username, rec.isOwner, t("xrOwnerTag"));
        rec.plate.position.y = CAP_H + 0.32;
        root.add(rec.plate);
        placeAvatar(rec, u.username); /* 站位按用户名散列（需求 4.7，进房不乱跳） */
        group.add(root);
        avatars.set(u.username, rec);
        if (u.model3dUrl) { queue.push({ username: u.username, url: u.model3dUrl }); pump(); }
      } else if (!!u.isRoomOwner !== rec.isOwner) {
        /* 房主标识变化（罕见）：原位重建名牌 */
        rec.root.remove(rec.plate);
        rec.plate.material.map.dispose();
        rec.plate.material.dispose();
        rec.isOwner = !!u.isRoomOwner;
        rec.plate = buildNamePlate(u.username, rec.isOwner, t("xrOwnerTag"));
        rec.plate.position.y = rec.modelApplied ? AVATAR_H + 0.32 : CAP_H + 0.32;
        rec.root.add(rec.plate);
      }
    }
    for (const [name, rec] of Array.from(avatars)) {
      if (!seen.has(name)) { disposeRec(rec); avatars.delete(name); }
    }
  }

  /* 语音（任务 9）取声源坐标：发送者站位，无形象返回 null。 */
  function positionOf(username) {
    const rec = avatars.get(String(username));
    return rec && !rec.disposed ? rec.root.position : null;
  }

  function dispose() {
    for (const [, rec] of avatars) disposeRec(rec);
    avatars.clear();
    queue.length = 0;
    inFlight = 0;
  }

  return { group, applyRoom, positionOf, dispose };
}
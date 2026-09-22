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
/* 内置缺省形象：账号的 model3dUrl 存 `builtin:<id>`，这里映射到静态 VRM。
   约定与服务器 app/main.py 的 BUILTIN_AVATARS[].file 一致（那边是权威目录）；
   认不出的 id 会走加载失败路径，静默回退胶囊，不影响 2D。 */
const BUILTIN_AVATAR_PREFIX = "builtin:";
const BUILTIN_AVATAR_DIR = "/static/avatars/";

/* ---------- ARKit 52 表情接口（需求 4.3） ----------
   驱动优先级：模型自带 ARKit 命名 morph target（部分 GLB 直接支持）→ 逐 mesh 驱动；
   VRM 模型经 ARKit→VRM1 预设表情映射表（口型/表情常见项），未映射项静默忽略。 */
export const ARKIT52 = [
  "eyeBlinkLeft", "eyeBlinkRight", "eyeLookDownLeft", "eyeLookDownRight",
  "eyeLookInLeft", "eyeLookInRight", "eyeLookOutLeft", "eyeLookOutRight",
  "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight",
  "eyeWideLeft", "eyeWideRight", "browDownLeft", "browDownRight", "browInnerUp",
  "browOuterUpLeft", "browOuterUpRight", "noseSneerLeft", "noseSneerRight",
  "cheekPuff", "cheekSquintLeft", "cheekSquintRight", "jawOpen", "jawLeft",
  "jawRight", "jawForward", "mouthLeft", "mouthRight", "mouthFrownLeft",
  "mouthFrownRight", "mouthSmileLeft", "mouthSmileRight", "mouthDimpleLeft",
  "mouthDimpleRight", "mouthPucker", "mouthStretchLeft", "mouthStretchRight",
  "mouthPressLeft", "mouthPressRight", "mouthRollLower", "mouthRollUpper",
  "mouthShrugLower", "mouthShrugUpper", "mouthClose", "mouthFunnel",
  "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft",
  "mouthUpperUpRight", "tongueOut",
];
const ARKIT_SET = new Set(ARKIT52);

const ARKIT_TO_VRM = {
  jawOpen: [["aa", 1]],
  mouthFunnel: [["oh", 1]],
  mouthPucker: [["ou", 1]],
  mouthStretchLeft: [["ih", 1]],
  mouthStretchRight: [["ih", 1]],
  eyeBlinkLeft: [["blinkLeft", 1]],
  eyeBlinkRight: [["blinkRight", 1]],
  mouthSmileLeft: [["happy", 0.6]],
  mouthSmileRight: [["happy", 0.6]],
  browDownLeft: [["angry", 0.8]],
  browDownRight: [["angry", 0.8]],
  mouthFrownLeft: [["sad", 0.7]],
  mouthFrownRight: [["sad", 0.7]],
  browInnerUp: [["surprised", 0.7]],
  eyeWideLeft: [["surprised", 0.5]],
  eyeWideRight: [["surprised", 0.5]],
};

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
      /* 内置缺省形象 → 静态 VRM；映射后不以 /api/ 开头，直接进 GLTFLoader。
         VRM0 的朝向纠正、归一化、表情与骨骼动画都在下面同一条路径里照常生效。 */
      if (src.startsWith(BUILTIN_AVATAR_PREFIX)) {
        const id = src.slice(BUILTIN_AVATAR_PREFIX.length).trim();
        if (!/^[a-z0-9-]+$/.test(id)) throw new Error("bad builtin avatar id");
        src = BUILTIN_AVATAR_DIR + id + ".vrm";
      }
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
      /* 能力捕获（需求 4.3/4.4）：VRM 表情管理器 + Humanoid 待机动画骨骼；
         非 VRM 模型扫描 ARKit 命名的 morph target 直接驱动 */
      rec.vrm = vrm || null;
      rec.humanoidOn = !!(vrm && vrm.humanoid && rec.humanoidFlag);
      if (rec.humanoidOn) {
        const hb = vrm.humanoid;
        rec.breathBone = hb.getNormalizedBoneNode("chest") || hb.getNormalizedBoneNode("spine");
        rec.armBone = hb.getNormalizedBoneNode("rightUpperArm");
        rec.foreArm = hb.getNormalizedBoneNode("rightLowerArm");
      }
      if (!vrm) {
        const morphs = new Map();
        model.traverse((o) => {
          if (o.isMesh && o.morphTargetDictionary) {
            for (const [name, idx] of Object.entries(o.morphTargetDictionary)) {
              if (ARKIT_SET.has(name)) {
                if (!morphs.has(name)) morphs.set(name, []);
                morphs.get(name).push({ mesh: o, idx });
              }
            }
          }
        });
        rec.arkitMorphs = morphs;
      }
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

  /* ---------- 座位（房间 3D 场景提供的推荐位置） ----------
     seats 为空 = 无场景，沿用原来的 hash 环站位，行为与改动前完全一致。
     分配用「成员名字典序排序后各自 hash 起位、线性探测取空座」：纯 hash 取模会撞座
     （10 座 4 人时约 50% 概率重叠），排序 + 探测既不重叠，又在成员集合不变时稳定。 */
  let seats = [];
  let seatAssignment = new Map();   // username → 座位下标

  function assignSeats(names) {
    seatAssignment = new Map();
    if (!seats.length) return;
    const taken = new Set();
    for (const name of Array.from(names).sort()) {
      let idx = hashStr(name) % seats.length;
      for (let step = 0; step < seats.length && taken.has(idx); step++) idx = (idx + 1) % seats.length;
      taken.add(idx);
      seatAssignment.set(name, idx);
    }
  }

  /* 场景座位表（推荐位置语义；用户/Agent 将来可自行改位，见需求 9 的 xr_state）。
     已在场的成员立即重新就座——所以调用时机不影响结果，但先于 applyRoom 更省一次重排。 */
  function setSeats(list) {
    seats = Array.isArray(list)
      ? list.filter((s) => s && Number.isFinite(s.x) && Number.isFinite(s.z))
      : [];
    assignSeats(avatars.keys());
    for (const [name, rec] of avatars) if (!rec.disposed) placeAvatar(rec, name);
  }

  function placeAvatar(rec, username) {
    const idx = seatAssignment.get(username);
    const seat = idx === undefined ? null : seats[idx];
    if (seat) {
      rec.root.position.set(seat.x, 0, seat.z);
      rec.root.rotation.y = seat.ry != null ? seat.ry : Math.atan2(-seat.x, -seat.z);
      return;
    }
    const ang = ((hashStr(username) % SLOTS) / SLOTS) * Math.PI * 2;
    rec.root.position.set(RING_R * Math.sin(ang), 0, RING_R * Math.cos(ang));
    rec.root.rotation.y = Math.atan2(-rec.root.position.x, -rec.root.position.z); /* 面向中央 */
  }

  /* roomEvents.onlineUsers → 同步成员形象（增/换名牌/移除离线）。 */
  function applyRoom(info) {
    if (!info || info.closed) return;
    const users = (info.onlineUsers || []).filter((u) => u && u.username && u.username !== (opts.username && opts.username()));
    const seen = new Set();
    assignSeats(users.map((u) => u.username));   /* 先排座，再按座建 rec */
    for (const u of users) {
      seen.add(u.username);
      let rec = avatars.get(u.username);
      if (!rec) {
        const root = new THREE.Group();
        rec = {
          root, plate: null, capMesh: null, baseY: 0, disposed: false, modelApplied: false,
          isOwner: !!u.isRoomOwner, humanoidFlag: !!u.model3dHumanoid, arkitFlag: !!u.model3dArkit,
          vrm: null, arkitMorphs: null, humanoidOn: false, breathBone: null, armBone: null,
          foreArm: null, waveT: null,
        };
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
      /* 能力标志可能随后台设置更新；已加载模型即时生效 */
      if (rec.humanoidFlag !== !!u.model3dHumanoid) {
        rec.humanoidFlag = !!u.model3dHumanoid;
        rec.humanoidOn = !!(rec.vrm && rec.vrm.humanoid && rec.humanoidFlag);
      }
      rec.arkitFlag = !!u.model3dArkit;
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

  /* ---------- ARKit 52 驱动接口（需求 4.3）：口型/表情统一入口。
     VRM → 映射表转预设表情；GLB（带 ARKit morph）→ 直接驱动 morph target。
     任务 9 语音口型、任务 10 手柄触发都走这里。 */
  function setExpression(username, name, weight) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed || !ARKIT_SET.has(String(name))) return false;
    const w = THREE.MathUtils.clamp(Number(weight) || 0, 0, 1);
    if (rec.vrm && rec.vrm.expressionManager) {
      const map = ARKIT_TO_VRM[String(name)];
      if (!map) return false;
      for (const [preset, scale] of map) rec.vrm.expressionManager.setValue(preset, w * scale);
      return true;
    }
    const list = rec.arkitMorphs && rec.arkitMorphs.get(String(name));
    if (list) {
      for (const { mesh, idx } of list) mesh.morphTargetInfluences[idx] = w;
      return true;
    }
    return false;
  }

  /* 挥手（需求 4.4 内置动画之二）：进入 2.4s 挥手窗口，update() 逐帧推进。 */
  function wave(username) {
    const rec = avatars.get(String(username));
    if (rec && !rec.disposed && rec.humanoidOn) rec.waveT = 0;
  }

  /* 逐帧：未勾选 Humanoid（或缺省胶囊）→ 轻微上下浮动；勾选的 VRM 做呼吸 +
     挥手窗口，并推进 three-vrm 表情/弹簧骨骼。 */
  function update(dt) {
    const now = performance.now() / 1000;
    for (const [, rec] of avatars) {
      if (rec.disposed) continue;
      if (!rec.humanoidOn) {
        rec.root.position.y = Math.abs(Math.sin(now * 1.5)) * 0.03;
      } else if (rec.vrm) {
        if (rec.breathBone) rec.breathBone.rotation.x = Math.sin(now * 1.1) * 0.02; /* 待机呼吸 */
        if (rec.waveT != null) {
          rec.waveT += dt;
          const k = rec.waveT;
          if (k >= 2.4) {
            rec.waveT = null;
            if (rec.armBone) rec.armBone.rotation.z = 0;
            if (rec.foreArm) rec.foreArm.rotation.z = 0;
          } else {
            const raise = Math.min(1, k / 0.4) * (1 - Math.max(0, (k - 1.8) / 0.6));
            if (rec.armBone) rec.armBone.rotation.z = -2.1 * raise;
            if (rec.foreArm) rec.foreArm.rotation.z = (-0.5 + Math.sin(k * 12) * 0.5) * raise;
          }
        }
        rec.vrm.update(dt);
      }
    }
  }

  function dispose() {
    for (const [, rec] of avatars) disposeRec(rec);
    avatars.clear();
    queue.length = 0;
    inFlight = 0;
  }

  return { group, applyRoom, positionOf, setExpression, wave, update, dispose, setSeats };
}
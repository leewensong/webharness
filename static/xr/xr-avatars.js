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
/* 人类客户端上报的 yaw 采用相机朝向，但人类 Avatar 的模型前向与此约定相差
   180°。Agent 已在自己的上报侧补过这个偏移，因此不能对所有账号统一加 π。 */
const HUMAN_FACING_OFFSET = Math.PI;

function facingYawFor(rec, yaw) {
  return Number(yaw || 0) + (rec && rec.isAgent ? 0 : HUMAN_FACING_OFFSET);
}

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

/* 关节表（顺序即二进制帧里的关节序号）：VRM humanoid 标准骨骼，与 three-vrm 的枚举同序。
   必须与 app/main.py 的 PRESENCE_BONES 完全一致——服务端按序号发，这里按序号还原。 */
export const PRESENCE_BONES = [
  "hips", "spine", "chest", "upperChest", "neck", "head", "leftEye", "rightEye", "jaw",
  "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes",
  "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes",
  "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
  "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
  "leftThumbMetacarpal", "leftThumbProximal", "leftThumbDistal",
  "leftIndexProximal", "leftIndexIntermediate", "leftIndexDistal",
  "leftMiddleProximal", "leftMiddleIntermediate", "leftMiddleDistal",
  "leftRingProximal", "leftRingIntermediate", "leftRingDistal",
  "leftLittleProximal", "leftLittleIntermediate", "leftLittleDistal",
  "rightThumbMetacarpal", "rightThumbProximal", "rightThumbDistal",
  "rightIndexProximal", "rightIndexIntermediate", "rightIndexDistal",
  "rightMiddleProximal", "rightMiddleIntermediate", "rightMiddleDistal",
  "rightRingProximal", "rightRingIntermediate", "rightRingDistal",
  "rightLittleProximal", "rightLittleIntermediate", "rightLittleDistal",
];
const _handQ = new THREE.Quaternion();   /* 手部四元数插值临时值 */
const _ikJointPos = new THREE.Vector3();
const _ikEndPos = new THREE.Vector3();
const _ikToEnd = new THREE.Vector3();
const _ikToGoal = new THREE.Vector3();
const _ikGoal = new THREE.Vector3();
const _ikParentQ = new THREE.Quaternion();
const _ikDeltaQ = new THREE.Quaternion();
const _ikLocalDeltaQ = new THREE.Quaternion();
const _ikWorldQ = new THREE.Quaternion();
const _ikTmpQ = new THREE.Quaternion();
const _headPitchQ = new THREE.Quaternion();
const _headAxisX = new THREE.Vector3(1, 0, 0);
const _ikHandTarget = new THREE.Vector3();

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
    for (const m of rec.handMeshes || []) {
      group.remove(m);          /* 手部标记挂在 group 下（世界坐标），不在 rec.root 里 */
      m.visible = false;
    }
    rec.handMeshes = [];
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

  /* 创建人类双臂的两段 IK 链。目标是 world-space 的手掌/手腕位置；
     upperArm + lowerArm 用 CCD 逼近目标，hand 再跟随手柄/手势的世界旋转。 */
  function buildArmIK(rec) {
    if (!rec || !rec.vrm || !rec.vrm.humanoid) return null;
    if (rec.armIK) return rec.armIK;
    const hb = rec.vrm.humanoid;
    const out = {};
    rec.appliedModel && rec.appliedModel.updateMatrixWorld(true);
    for (const side of ["left", "right"]) {
      const upper = hb.getNormalizedBoneNode(`${side}UpperArm`);
      const lower = hb.getNormalizedBoneNode(`${side}LowerArm`);
      const hand = hb.getNormalizedBoneNode(`${side}Hand`);
      if (!upper || !lower || !hand) continue;
      const up = upper.getWorldPosition(new THREE.Vector3());
      const elbow = lower.getWorldPosition(new THREE.Vector3());
      const wrist = hand.getWorldPosition(new THREE.Vector3());
      out[side] = {
        upper, lower, hand,
        upperLen: Math.max(0.05, up.distanceTo(elbow)),
        lowerLen: Math.max(0.05, elbow.distanceTo(wrist)),
        target: new THREE.Vector3(), goal: new THREE.Vector3(),
        targetQ: new THREE.Quaternion(),
        restUpper: upper.quaternion.clone(),
        restLower: lower.quaternion.clone(),
        restHand: hand.quaternion.clone(),
        initialized: false, active: false, qInitialized: false,
      };
    }
    rec.armIK = out;
    return out;
  }

  function buildHeadTracking(rec) {
    if (!rec || !rec.vrm || !rec.vrm.humanoid) return null;
    if (rec.headTracking) return rec.headTracking;
    const hb = rec.vrm.humanoid;
    const neck = hb.getNormalizedBoneNode("neck");
    const head = hb.getNormalizedBoneNode("head");
    if (!neck && !head) return null;
    rec.headTracking = {
      neck, head,
      restNeck: neck ? neck.quaternion.clone() : null,
      restHead: head ? head.quaternion.clone() : null,
      pitch: 0,
      initialized: false,
    };
    /* XR 的 p 是真实头部高度，而模型已经归一化到 AVATAR_H；不把真实头高
       直接当作模型脚底高度，否则手目标会整体高出模型半米左右。 */
    rec.headHeight = null;
    const headNode = head || neck;
    if (headNode) {
      rec.root.updateMatrixWorld(true);
      const rootPos = rec.root.getWorldPosition(new THREE.Vector3());
      const headPos = headNode.getWorldPosition(new THREE.Vector3());
      const h = headPos.y - rootPos.y;
      if (Number.isFinite(h) && h > 0.5) rec.headHeight = h;
    }
    return rec.headTracking;
  }

  /* 摘掉已套用的模型（换装/重建时用）：VRM 走 deepDispose，其余按材质遍历释放 */
  function removeAppliedModel(rec) {
    if (!rec.appliedModel) return;
    if (rec.vrm && VRMUtils.deepDispose) {
      VRMUtils.deepDispose(rec.appliedModel);
    } else {
      rec.appliedModel.traverse((o) => {
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
    rec.root.remove(rec.appliedModel);
    rec.appliedModel = null;
    rec.vrm = null;
    rec.arkitMorphs = null;
    rec.humanoidOn = false;
    rec.breathBone = rec.armBone = rec.foreArm = null;
    rec.waveT = null;
    rec.remoteBoneTargets.clear();
    rec.handTargetsBySide = { left: null, right: null };
    rec.armIK = null;
    rec.headTracking = null;
    rec.headHeight = null;
    rec.ikBones.clear();
    rec.animationQueue.length = 0;
    rec.animationClip = null;
    rec.animationTime = 0;
    rec.animationBoneNames.clear();
    rec.animationFaceNames.clear();
    rec.agentBones.clear();
    rec.plate.position.y = CAP_H + 0.32;
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
      if (rec.modelUrl !== job.url) return; /* 加载期间又换了形象：这份过期，丢弃 */
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
      rec.appliedModel = model;
      rec.modelApplied = true;
      rec.plate.position.y = AVATAR_H + 0.32;
      /* 能力捕获（需求 4.3/4.4）：VRM 表情管理器 + Humanoid 待机动画骨骼；
         非 VRM 模型扫描 ARKit 命名的 morph target 直接驱动 */
      rec.vrm = vrm || null;
      buildArmIK(rec);
      buildHeadTracking(rec);
      rec.humanoidOn = !!(vrm && vrm.humanoid && rec.humanoidFlag);
      if (rec.humanoidOn) {
        const hb = vrm.humanoid;
        rec.breathBone = hb.getNormalizedBoneNode("chest") || hb.getNormalizedBoneNode("spine");
        rec.armBone = hb.getNormalizedBoneNode("rightUpperArm");
        rec.foreArm = hb.getNormalizedBoneNode("rightLowerArm");
        /* 走路循环要用的骨骼（见 update）：腿与另一侧手臂。素材里没有动画剪辑，
           全靠这里的程序化驱动——所以「内嵌行走动画」在这里等价于摆动这些骨骼。 */
        rec.leftArm = hb.getNormalizedBoneNode("leftUpperArm");
        rec.legL = hb.getNormalizedBoneNode("leftUpperLeg");
        rec.legR = hb.getNormalizedBoneNode("rightUpperLeg");
        rec.shinL = hb.getNormalizedBoneNode("leftLowerLeg");
        rec.shinR = hb.getNormalizedBoneNode("rightLowerLeg");
        buildArmIK(rec);
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
    if (rec.hasPose) return;   /* 已有 6DoF 位姿：座位只是默认位，不再抢位 */
    const idx = seatAssignment.get(username);
    const seat = idx === undefined ? null : seats[idx];
    if (seat) {
      rec.root.position.set(seat.x, 0, seat.z);
      rec.root.rotation.y = facingYawFor(rec, seat.ry != null ? seat.ry : Math.atan2(-seat.x, -seat.z));
      return;
    }
    const ang = ((hashStr(username) % SLOTS) / SLOTS) * Math.PI * 2;
    rec.root.position.set(RING_R * Math.sin(ang), 0, RING_R * Math.cos(ang));
    rec.root.rotation.y = facingYawFor(rec, Math.atan2(-rec.root.position.x, -rec.root.position.z)); /* 面向中央 */
  }

  /* roomEvents.onlineUsers → 同步成员形象（增/换名牌/移除离线）。 */
  function applyRoom(info) {
    if (!info || info.closed) return;
    const users = (info.onlineUsers || []).filter((u) => u && u.username && u.username !== (opts.username && opts.username()));
    const seen = new Set();
    assignSeats(users.map((u) => u.username));   /* 先排座，再按座建 rec */
    for (const u of users) {
      if (leftSet.has(u.username)) continue; /* 已显式离开 3D：等他再次上报位姿才出现 */
      seen.add(u.username);
      let rec = avatars.get(u.username);
      if (!rec) {
        const root = new THREE.Group();
        rec = {
          username: u.username,
          root, plate: null, capMesh: null, baseY: 0, disposed: false, modelApplied: false,
          modelUrl: u.model3dUrl || null, appliedModel: null,
          isOwner: !!u.isRoomOwner, isAgent: u.kind === "agent",
          humanoidFlag: !!u.model3dHumanoid, arkitFlag: !!u.model3dArkit,
          vrm: null, arkitMorphs: null, humanoidOn: false, breathBone: null, armBone: null,
          foreArm: null, waveT: null,
          poseTarget: null, hasPose: false, handTargets: null, handMeshes: [],
          handTargetsBySide: { left: null, right: null },
          armIK: null, headTracking: null, headHeight: null, poseHeadY: null,
          headPitchTarget: 0, headPoseSeen: false, ikBones: new Set(),
          /* Agent 逐拍骨骼：保留最新目标，渲染帧里 quaternion.slerp 趋近。 */
          remoteBoneTargets: new Map(),
          /* Agent 关键帧动画：当前 clip + 等待播放的 clip 队列。 */
          animationQueue: [], animationClip: null, animationTime: 0,
          animationBoneNames: new Set(), animationFaceNames: new Set(),
          agentBones: new Set(),
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
      /* 旧服务端没有 kind 时按人类处理；有 kind 时保留 Agent 的既有朝向约定。 */
      rec.isAgent = u.kind === "agent";
      /* 能力标志可能随后台设置更新；已加载模型即时生效 */
      if (rec.humanoidFlag !== !!u.model3dHumanoid) {
        rec.humanoidFlag = !!u.model3dHumanoid;
        rec.humanoidOn = !!(rec.vrm && rec.vrm.humanoid && rec.humanoidFlag);
      }
      rec.arkitFlag = !!u.model3dArkit;
      /* 形象文件中途变更（需求 4.1 增量同步）：摘掉旧模型并重新排队加载，
         不在场的重进 3D 才会重建，这里保证在场的也能即时换装 */
      if (rec.modelUrl !== (u.model3dUrl || null)) {
        rec.modelUrl = u.model3dUrl || null;
        removeAppliedModel(rec);
        rec.modelApplied = false;
        if (rec.modelUrl) { queue.push({ username: u.username, url: rec.modelUrl }); pump(); }
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

  /* ---------- ARKit 52 驱动接口（需求 4.3）：口型/表情统一入口。
     VRM → 映射表转预设表情；GLB（带 ARKit morph）→ 直接驱动 morph target。
     任务 9 语音口型、任务 10 手柄触发都走这里。 */
  function applyExpressionRec(rec, name, weight) {
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

  function setExpression(username, name, weight) {
    return applyExpressionRec(avatars.get(String(username)), name, weight);
  }

  /* 挥手（需求 4.4 内置动画之二）：进入 2.4s 挥手窗口，update() 逐帧推进。 */
  function wave(username) {
    const rec = avatars.get(String(username));
    if (rec && !rec.disposed && rec.humanoidOn) rec.waveT = 0;
  }

  /* ---------- 远端位姿（房间内 3D 位姿同步）----------
     服务端 room_presence 下发的是「目标位姿」：这里保存目标并逐帧插值趋近，避免瞬移。
     有 6DoF 位姿的成员不再按座位/环形摆放（座位只是他还没上报位姿时的默认位）；
     显式离开 3D（presence/leave）的成员先隐藏，等他再次上报位姿才重新出现。 */
  const leftSet = new Set();
  const HAND_GEO = new THREE.BoxGeometry(0.075, 0.05, 0.14);
  const HAND_MAT = new THREE.MeshBasicMaterial({ color: 0x9fc4ff, transparent: true, opacity: 0.85 });

  function ensureHands(rec) {
    if (rec.handMeshes.length) return rec.handMeshes;
    for (let i = 0; i < 2; i++) {
      const m = new THREE.Mesh(HAND_GEO, HAND_MAT);
      m.visible = false;
      group.add(m);
      rec.handMeshes.push(m);
    }
    return rec.handMeshes;
  }

  function handTargetWorld(rec, hand, out = _ikHandTarget) {
    if (!hand || !Array.isArray(hand.p) || hand.p.length !== 3) return null;
    out.set(hand.p[0], hand.p[1], hand.p[2]);
    /* Keep the Avatar's feet/body scale while preserving the user's relative
       hand height beneath the tracked head. */
    if (Number.isFinite(rec.headHeight) && Number.isFinite(rec.poseHeadY)) {
      out.y += rec.headHeight - rec.poseHeadY;
    }
    return out;
  }

  /* 目标位姿 = 服务端最新一份；返回是否命中在场成员 */
  function setRemotePose(username, pose) {
    const name = String(username);
    const rec = avatars.get(name);
    leftSet.delete(name);          /* 再次上报 = 又回到 3D 里了 */
    if (!rec || rec.disposed) return false;
    if (pose && Array.isArray(pose.p) && pose.p.length === 3 && pose.p.every(Number.isFinite)) {
      const [x, y, z] = pose.p;
      const ry = Number.isFinite(pose.yaw) ? facingYawFor(rec, pose.yaw) : rec.root.rotation.y;
      rec.poseTarget = { x, y, z, ry };
      if (!rec.hasPose) {          /* 首次：直接就位，避免从座位慢慢飘过去 */
        rec.hasPose = true;
        rec.root.position.x = x;
        rec.root.position.z = z;
        rec.root.rotation.y = ry;
      }
    }
    if (pose && Number.isFinite(pose.pitch)) {
      rec.headPitchTarget = THREE.MathUtils.clamp(Number(pose.pitch), -Math.PI / 2, Math.PI / 2);
      rec.headPoseSeen = true;
    }
    if (pose && pose.p && Number.isFinite(pose.p[1])) rec.poseHeadY = pose.p[1];
    const hands = Array.isArray(pose && pose.hands) ? pose.hands.filter((h) => h && Array.isArray(h.p)).slice(0, 2) : [];
    rec.handTargets = hands;
    rec.handTargetsBySide = { left: null, right: null };
    hands.forEach((h, i) => {
      /* 新客户端带 handedness；旧客户端没有时，优先用头部 yaw 下的身体横向
         位置判断左右，避免两个 controller 的输入顺序把手臂交叉/反向。 */
      let side = h.handedness === "left" || h.handedness === "right" ? h.handedness : null;
      if (!side && pose && Array.isArray(pose.p) && Number.isFinite(pose.yaw)) {
        const rx = Math.cos(pose.yaw), rz = -Math.sin(pose.yaw);
        const lateral = (h.p[0] - pose.p[0]) * rx + (h.p[2] - pose.p[2]) * rz;
        if (Math.abs(lateral) > 0.015) side = lateral < 0 ? "left" : "right";
      }
      if (!side) side = i === 0 ? "left" : "right";
      if (!rec.handTargetsBySide[side]) rec.handTargetsBySide[side] = h;
    });
    if (hands.length) {
      enableHumanoidProcedurals(rec); /* 人类手势也要能驱动未勾选程序动画的 VRM */
      buildArmIK(rec);
    }
    const meshes = hands.length ? ensureHands(rec) : rec.handMeshes;
    meshes.forEach((m, i) => { m.visible = i < hands.length; });
    return true;
  }

  /* ---------- 远端长序列动画 ----------
     Agent 不必每 100ms 重发骨骼：一次 presence 可以携带一个稀疏关键帧序列，
     渲染端在 XR 帧里按自己的刷新率采样。animation action:
       replace = 立即中断当前与排队动画并播放本段；
       append  = 当前段结束后排到队尾；
       stop    = 清空当前与排队动画，交还本地程序动画（若仍有 live bones 则继续跟随）。 */
  function refreshAgentBones(rec) {
    const keys = new Set(rec.remoteBoneTargets ? rec.remoteBoneTargets.keys() : []);
    for (const name of rec.animationBoneNames || []) keys.add(name);
    rec.agentBones = keys;
  }

  function compileAnimation(raw) {
    if (!raw || (raw.action !== "replace" && raw.action !== "append")) return null;
    const frames = Array.isArray(raw.keyframes) ? raw.keyframes : [];
    if (!frames.length) return null;
    const boneTracks = new Map();
    const faceTracks = new Map();
    let duration = 0;
    for (const frame of frames) {
      const at = Number(frame && frame.t);
      if (!Number.isFinite(at) || at < 0) continue;
      duration = Math.max(duration, at);
      for (const [name, q] of Object.entries((frame && frame.bones) || {})) {
        if (!Array.isArray(q) || q.length !== 4) continue;
        const node = new THREE.Quaternion(q[0], q[1], q[2], q[3]).normalize();
        if (!boneTracks.has(name)) boneTracks.set(name, []);
        boneTracks.get(name).push({ t: at, q: node, tmp: new THREE.Quaternion() });
      }
      for (const [name, weight] of Object.entries((frame && frame.face) || {})) {
        const w = THREE.MathUtils.clamp(Number(weight) || 0, 0, 1);
        if (!faceTracks.has(name)) faceTracks.set(name, []);
        faceTracks.get(name).push({ t: at, w });
      }
    }
    if (!boneTracks.size && !faceTracks.size) return null;
    return {
      id: raw.id || null,
      loop: !!raw.loop,
      duration,
      boneTracks,
      faceTracks,
      boneNames: new Set(boneTracks.keys()),
      faceNames: new Set(faceTracks.keys()),
    };
  }

  function sampleQuatTrack(track, time, out) {
    if (!track || !track.length) return null;
    /* 稀疏轨道在自己的第一关键帧之前保持进入动画时的姿势，而不是提前跳到
       第一关键帧；这样「每帧只写变化的骨骼」才有正确语义。 */
    if (time < track[0].t) return null;
    if (time === track[0].t) return out.copy(track[0].q);
    const last = track.length - 1;
    if (time >= track[last].t) return out.copy(track[last].q);
    let lo = 0, hi = last;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (track[mid].t <= time) lo = mid;
      else hi = mid;
    }
    const a = track[lo], b = track[hi];
    const span = b.t - a.t;
    const alpha = span > 1e-6 ? (time - a.t) / span : 1;
    return out.copy(a.q).slerp(b.q, THREE.MathUtils.clamp(alpha, 0, 1));
  }

  function sampleNumberTrack(track, time) {
    if (!track || !track.length) return 0;
    if (time < track[0].t) return null;
    if (time === track[0].t) return track[0].w;
    const last = track.length - 1;
    if (time >= track[last].t) return track[last].w;
    let lo = 0, hi = last;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (track[mid].t <= time) lo = mid;
      else hi = mid;
    }
    const a = track[lo], b = track[hi];
    const span = b.t - a.t;
    const alpha = span > 1e-6 ? (time - a.t) / span : 1;
    return THREE.MathUtils.lerp(a.w, b.w, THREE.MathUtils.clamp(alpha, 0, 1));
  }

  function startNextAnimation(rec) {
    if (rec.animationClip || !rec.animationQueue.length) return;
    rec.animationClip = rec.animationQueue.shift();
    rec.animationTime = 0;
    rec.animationBoneNames = new Set(rec.animationClip.boneNames);
    rec.animationFaceNames = new Set(rec.animationClip.faceNames);
    enableHumanoidProcedurals(rec);
    refreshAgentBones(rec);
  }

  function clearAnimation(rec) {
    rec.animationQueue.length = 0;
    rec.animationClip = null;
    rec.animationTime = 0;
    rec.animationBoneNames.clear();
    rec.animationFaceNames.clear();
    refreshAgentBones(rec);
  }

  function applyAnimationSample(rec, time) {
    const clip = rec.animationClip;
    if (!clip || !rec.vrm || !rec.vrm.humanoid) return;
    const hb = rec.vrm.humanoid;
    for (const [name, track] of clip.boneTracks) {
      const node = hb.getNormalizedBoneNode(name);
      if (!node) continue;
      if (sampleQuatTrack(track, time, track[0].tmp)) node.quaternion.copy(track[0].tmp);
    }
    for (const [name, track] of clip.faceTracks) {
      const weight = sampleNumberTrack(track, time);
      if (weight != null) applyExpressionRec(rec, name, weight);
    }
  }

  function finishAnimationClip(rec) {
    rec.animationClip = null;
    rec.animationTime = 0;
    rec.animationBoneNames.clear();
    rec.animationFaceNames.clear();
    refreshAgentBones(rec);
    startNextAnimation(rec);
  }

  function advanceAnimation(rec, dt) {
    if (!rec.animationClip) startNextAnimation(rec);
    let remaining = Math.max(0, Number(dt) || 0);
    for (let guard = 0; guard < 32; guard++) {
      const clip = rec.animationClip;
      if (!clip) return;
      if (clip.duration <= 1e-6) {
        applyAnimationSample(rec, 0);
        if (clip.loop) return; /* 单帧 loop 动画 = 持续保持该姿势 */
        finishAnimationClip(rec);
        continue;
      }
      const available = Math.max(0, clip.duration - rec.animationTime);
      if (clip.loop) {
        rec.animationTime = (rec.animationTime + remaining) % clip.duration;
        applyAnimationSample(rec, rec.animationTime);
        return;
      }
      if (remaining <= available) {
        rec.animationTime += remaining;
        applyAnimationSample(rec, rec.animationTime);
        return;
      }
      rec.animationTime = clip.duration;
      applyAnimationSample(rec, rec.animationTime);
      remaining -= available;
      finishAnimationClip(rec);
      if (remaining <= 1e-6 && rec.animationClip) {
        applyAnimationSample(rec, rec.animationTime);
        return;
      }
    }
  }

  function setAnimationCommand(username, command) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed || !command || typeof command !== "object") return false;
    const action = command.action;
    if (action === "stop") {
      clearAnimation(rec);
      return true;
    }
    const clip = compileAnimation(command);
    if (!clip) return false;
    if (action === "replace") clearAnimation(rec);
    rec.animationQueue.push(clip);
    startNextAnimation(rec);
    return true;
  }

  /* 显式离开 3D：立即移除形象，并记住该成员（房间轮询再看到他时不重建） */
  function removeRemote(username) {
    const name = String(username);
    leftSet.add(name);
    const rec = avatars.get(name);
    if (rec) { disposeRec(rec); avatars.delete(name); }
  }

  function clearLeft(username) {
    if (username === undefined) leftSet.clear();
    else leftSet.delete(String(username));
  }

  /* 逐帧：未勾选 Humanoid（或缺省胶囊）→ 轻微上下浮动；勾选的 VRM 做呼吸 +
     挥手窗口，并推进 three-vrm 表情/弹簧骨骼。 */
  function update(dt) {
    const now = performance.now() / 1000;
    for (const [, rec] of avatars) {
      if (rec.disposed) continue;
      /* 远端位姿插值（指数趋近，~6/s：0.5s 一次目标也能平滑移动）+ 估算水平速度
         （走路循环靠它驱动——不额外传输任何骨骼数据） */
      if (rec.hasPose && rec.poseTarget) {
        const px = rec.root.position.x;
        const pz = rec.root.position.z;
        const k = 1 - Math.exp(-dt * 6);
        rec.root.position.x += (rec.poseTarget.x - rec.root.position.x) * k;
        rec.root.position.z += (rec.poseTarget.z - rec.root.position.z) * k;
        let dr = rec.poseTarget.ry - rec.root.rotation.y;
        while (dr > Math.PI) dr -= Math.PI * 2;
        while (dr < -Math.PI) dr += Math.PI * 2;
        rec.root.rotation.y += dr * k;
        if (dt > 0) {
          const dx = rec.root.position.x - px;
          const dz = rec.root.position.z - pz;
          const inst = Math.hypot(dx, dz) / dt;
          rec.speed = (rec.speed || 0) * 0.8 + inst * 0.2;   /* 平滑，避免单帧抖动误判为走动 */
          if (inst > 0.05) {
            /* 把位移分解到形象本地坐标：前进分量决定步幅，横向分量收小步幅并加一点侧倾。
               人只传头/手，腿是固定算法，所以「朝向与移动方向不一致」（横移/倒退）时
               要看得出来——否则横着飘却迈正步。世界前向 = (-sin ry, -cos ry)。 */
            const sinY = Math.sin(rec.root.rotation.y);
            const cosY = Math.cos(rec.root.rotation.y);
            const fwd = (dx * -sinY + dz * -cosY) / inst;
            const lat = (dx * -cosY + dz * sinY) / inst;
            rec.moveFwd = (rec.moveFwd || 0) * 0.8 + fwd * 0.2;
            rec.moveLat = (rec.moveLat || 0) * 0.8 + lat * 0.2;
          } else {
            rec.moveFwd = (rec.moveFwd || 0) * 0.9;
            rec.moveLat = (rec.moveLat || 0) * 0.9;
          }
        }
      } else {
        rec.speed = (rec.speed || 0) * 0.9;
      }
      /* 双手 6DoF 标记（头显用户才有；直接世界坐标，挂在 group 下） */
      if (rec.handMeshes.length) {
        const hs = rec.handTargets || [];
        for (let i = 0; i < rec.handMeshes.length; i++) {
          const m = rec.handMeshes[i];
          const h = hs[i];
          if (!h || !m.visible) { m.visible = false; continue; }
          const target = handTargetWorld(rec, h);
          if (!target) { m.visible = false; continue; }
          const k = 1 - Math.exp(-dt * 10);
          m.position.x += (target.x - m.position.x) * k;
          m.position.y += (target.y - m.position.y) * k;
          m.position.z += (target.z - m.position.z) * k;
          if (Array.isArray(h.q) && h.q.length === 4) {
            _handQ.set(h.q[0], h.q[1], h.q[2], h.q[3]);
            m.quaternion.slerp(_handQ, k);
          }
        }
      }
      /* 逐拍 bones 是目标值，先在渲染帧里平滑趋近；当前长序列占用的关节跳过，
         由 advanceAnimation() 在同一帧按关键帧时间轴写入。 */
      updateRemoteBoneTargets(rec, dt);
      if (!rec.humanoidOn) {
        /* 未开启 humanoid 程序动画也要推进 VRM：Agent 远端骨骼/表情只有走
           vrm.update 才会写进渲染骨骼（账号 humanoid 开关只管程序动画本身） */
        rec.root.position.y = Math.abs(Math.sin(now * 1.5)) * 0.03;
      } else if (rec.vrm) {
        if (rec.breathBone) rec.breathBone.rotation.x = Math.sin(now * 1.1) * 0.02; /* 待机呼吸 */
        /* 走路循环：素材里没有动画剪辑，所以「行走动画」= 摆动腿与另一侧手臂。
           速度来自收到位姿的位移，摆幅与频率随速度增大；停下后把腿复位，
           否则会卡在跨步姿势。挥手时不动手臂，免得两路驱动打架。 */
        const speed = rec.speed || 0;
        const wantWalk = speed > 0.15;
        rec.walkMix = (rec.walkMix || 0) + ((wantWalk ? 1 : 0) - (rec.walkMix || 0)) * (1 - Math.exp(-dt * 5));
        /* Agent 接管过的关节不许我们碰——包括下面「停下复位」那条路径，
           否则一停步就会把 Agent 摆好的姿势清零（rotation 与 quaternion 是联动的）。 */
        const ours = (bone) => !(rec.agentBones && rec.agentBones.has(bone))
          && !(rec.ikBones && rec.ikBones.has(bone))
          && !(rec.animationBoneNames && rec.animationBoneNames.has(bone));
        if (rec.walkMix > 0.02) {
          const fwd = Math.abs(rec.moveFwd || 0);
          const lat = rec.moveLat || 0;
          /* 步幅按「沿朝向前进」的程度缩放：横移时收小（腿是固定算法，横着走不该迈正步） */
          const amp = 0.55 * rec.walkMix * Math.min(1, speed / 1.2) * (0.35 + 0.65 * fwd);
          rec.walkPhase = (rec.walkPhase || 0) + dt * (2.0 + speed * 2.4) * Math.PI;
          const sw = Math.sin(rec.walkPhase);
          if (rec.legL && ours("leftUpperLeg")) rec.legL.rotation.x = sw * amp;
          if (rec.legR && ours("rightUpperLeg")) rec.legR.rotation.x = -sw * amp;
          if (rec.shinL && ours("leftLowerLeg")) rec.shinL.rotation.x = Math.max(0, -sw) * amp * 0.9;
          if (rec.shinR && ours("rightLowerLeg")) rec.shinR.rotation.x = Math.max(0, sw) * amp * 0.9;
          if (rec.waveT == null) {
            if (rec.leftArm && ours("leftUpperArm")) rec.leftArm.rotation.x = -sw * amp * 0.7;
            if (rec.armBone && ours("rightUpperArm")) rec.armBone.rotation.x = sw * amp * 0.7;
          }
          /* 横移时上身轻微侧倾，让动作不像纯滑行（呼吸用的是 .x，这里用 .z 不打架） */
          if (rec.breathBone) rec.breathBone.rotation.z = -lat * 0.12 * rec.walkMix;
          rec.root.position.y = Math.abs(sw) * 0.022 * rec.walkMix;  /* 轻微上下起伏 */
        } else {
          for (const [bone, node] of [["leftUpperLeg", rec.legL], ["rightUpperLeg", rec.legR],
                                      ["leftLowerLeg", rec.shinL], ["rightLowerLeg", rec.shinR]]) {
            if (node && ours(bone)) node.rotation.x = 0;
          }
          if (rec.waveT == null) {
            if (rec.leftArm && ours("leftUpperArm")) rec.leftArm.rotation.x = 0;
            if (rec.armBone && ours("rightUpperArm")) rec.armBone.rotation.x = 0;
          }
          if (rec.breathBone) rec.breathBone.rotation.z = 0;
          rec.root.position.y = 0;
        }
        if (rec.waveT != null) {
          rec.waveT += dt;
          const k = rec.waveT;
          if (k >= 2.4) {
            rec.waveT = null;
            if (rec.armBone && ours("rightUpperArm")) rec.armBone.rotation.z = 0;
            if (rec.foreArm && ours("rightLowerArm")) rec.foreArm.rotation.z = 0;
          } else {
            const raise = Math.min(1, k / 0.4) * (1 - Math.max(0, (k - 1.8) / 0.6));
            if (rec.armBone && ours("rightUpperArm")) rec.armBone.rotation.z = -2.1 * raise;
            if (rec.foreArm && ours("rightLowerArm")) rec.foreArm.rotation.z = (-0.5 + Math.sin(k * 12) * 0.5) * raise;
          }
        }
      }
      if (rec.vrm) {
        advanceAnimation(rec, dt);
        applyHandIK(rec, dt);
        applyHeadTracking(rec, dt);
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

  /* Agent 接管即授权：账号没勾 humanoid 的 VRM，一旦收到远端骨骼就补齐
     程序动画所需的骨骼捕获（走路循环与呼吸共用 ours() 让位给 Agent 关节）。 */
  function enableHumanoidProcedurals(rec) {
    if (rec.humanoidOn || !rec.vrm || !rec.vrm.humanoid) return;
    const hb = rec.vrm.humanoid;
    rec.breathBone = hb.getNormalizedBoneNode("chest") || hb.getNormalizedBoneNode("spine");
    rec.armBone = hb.getNormalizedBoneNode("rightUpperArm");
    rec.foreArm = hb.getNormalizedBoneNode("rightLowerArm");
    rec.leftArm = hb.getNormalizedBoneNode("leftUpperArm");
    rec.legL = hb.getNormalizedBoneNode("leftUpperLeg");
    rec.legR = hb.getNormalizedBoneNode("rightUpperLeg");
    rec.shinL = hb.getNormalizedBoneNode("leftLowerLeg");
    rec.shinR = hb.getNormalizedBoneNode("rightLowerLeg");
    rec.humanoidOn = true;
    buildArmIK(rec);
  }

  /* Agent 上报的骨骼（level 3）：**只覆盖它报过的关节**，未报的继续走本地程序化动画
     （所以 Agent 可以只做上半身 IK，腿仍由我们的走路算法负责）。 */
  function setRemoteBones(username, bones) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed || !rec.vrm || !rec.vrm.humanoid) return false;
    if (!bones) return false;
    enableHumanoidProcedurals(rec);
    const hb = rec.vrm.humanoid;
    let applied = 0;
    for (const [name, q] of Object.entries(bones)) {
      if (!Array.isArray(q) || q.length !== 4) continue;
      const node = hb.getNormalizedBoneNode(name);
      if (!node) continue;                       /* 该模型没有这根骨头：静默跳过 */
      const target = rec.remoteBoneTargets.get(name) || node.quaternion.clone();
      target.set(q[0], q[1], q[2], q[3]).normalize();
      rec.remoteBoneTargets.set(name, target);
      applied++;
    }
    refreshAgentBones(rec);
    return applied > 0;
  }

  function updateRemoteBoneTargets(rec, dt) {
    if (!rec.vrm || !rec.vrm.humanoid || !rec.remoteBoneTargets.size) return;
    const k = 1 - Math.exp(-Math.max(0, Number(dt) || 0) * 14);
    const hb = rec.vrm.humanoid;
    for (const [name, target] of rec.remoteBoneTargets) {
      if (rec.animationBoneNames.has(name)) continue; /* 长序列动画在当前片段期间优先 */
      const node = hb.getNormalizedBoneNode(name);
      if (node) node.quaternion.slerp(target, k);
    }
  }

  function setBoneWorldQuaternion(node, worldQ) {
    if (!node.parent) {
      node.quaternion.copy(worldQ).normalize();
      return;
    }
    node.parent.getWorldQuaternion(_ikParentQ);
    _ikTmpQ.copy(_ikParentQ).invert().multiply(worldQ);
    node.quaternion.copy(_ikTmpQ).normalize();
  }

  function rotateIKJointToward(rec, joint, end, goal) {
    joint.getWorldPosition(_ikJointPos);
    end.getWorldPosition(_ikEndPos);
    _ikToEnd.subVectors(_ikEndPos, _ikJointPos);
    _ikToGoal.subVectors(goal, _ikJointPos);
    if (_ikToEnd.lengthSq() < 1e-8 || _ikToGoal.lengthSq() < 1e-8) return;
    _ikToEnd.normalize();
    _ikToGoal.normalize();
    _ikDeltaQ.setFromUnitVectors(_ikToEnd, _ikToGoal);
    if (joint.parent) {
      joint.parent.getWorldQuaternion(_ikParentQ);
      _ikLocalDeltaQ.copy(_ikParentQ).invert().multiply(_ikDeltaQ).multiply(_ikParentQ);
      joint.quaternion.premultiply(_ikLocalDeltaQ).normalize();
    } else {
      joint.quaternion.premultiply(_ikDeltaQ).normalize();
    }
    if (rec.appliedModel) rec.appliedModel.updateMatrixWorld(true);
  }

  function solveArmIK(rec, chain) {
    const shoulder = chain.upper.getWorldPosition(_ikJointPos);
    _ikGoal.copy(chain.target);
    _ikToGoal.subVectors(_ikGoal, shoulder);
    const dist = _ikToGoal.length();
    if (dist < 1e-6) return;
    const minReach = Math.max(0.04, Math.abs(chain.upperLen - chain.lowerLen) + 0.01);
    const maxReach = Math.max(minReach, chain.upperLen + chain.lowerLen - 0.01);
    const clamped = THREE.MathUtils.clamp(dist, minReach, maxReach);
    _ikGoal.copy(shoulder).addScaledVector(_ikToGoal, clamped / dist);
    /* CCD：先转肘部，再转肩部；几次小迭代足够覆盖手柄/手腕的正常活动范围。 */
    for (let i = 0; i < 5; i++) {
      rotateIKJointToward(rec, chain.lower, chain.hand, _ikGoal);
      rotateIKJointToward(rec, chain.upper, chain.hand, _ikGoal);
    }
  }

  function releaseArmIK(rec, chain, dt) {
    if (!chain || !chain.active) return;
    const k = 1 - Math.exp(-Math.max(0, Number(dt) || 0) * 12);
    chain.upper.quaternion.slerp(chain.restUpper, k);
    chain.lower.quaternion.slerp(chain.restLower, k);
    chain.hand.quaternion.slerp(chain.restHand, k);
    if (k > 0.98) {
      chain.active = false;
      chain.initialized = false;
      chain.qInitialized = false;
    }
  }

  function applyHandIK(rec, dt) {
    /* Agent 的 bones/animation 仍走原有协议；这里只把人类的真实手柄/手势目标
       反解到 Avatar 的上臂、前臂和手腕，避免两套驱动互相抢骨骼。 */
    if (rec.isAgent || !rec.vrm || !rec.vrm.humanoid) return;
    const chains = rec.armIK || buildArmIK(rec);
    if (!chains) return;
    const hands = rec.handTargetsBySide || { left: null, right: null };
    const k = 1 - Math.exp(-Math.max(0, Number(dt) || 0) * 18);
    for (const side of ["left", "right"]) {
      const chain = chains[side];
      if (!chain) continue;
      const names = [`${side}UpperArm`, `${side}LowerArm`, `${side}Hand`];
      const animationOwnsArm = names.some((name) => rec.animationBoneNames && rec.animationBoneNames.has(name));
      const h = hands[side];
      const target = handTargetWorld(rec, h);
      if (!target || animationOwnsArm || !target.toArray().every(Number.isFinite)) {
        releaseArmIK(rec, chain, dt);
        for (const name of names) rec.ikBones.delete(name);
        continue;
      }
      if (!chain.initialized) {
        chain.target.copy(target);
        chain.initialized = true;
      } else {
        chain.target.lerp(target, k);
      }
      if (Array.isArray(h.q) && h.q.length === 4 && h.q.every(Number.isFinite)) {
        _ikWorldQ.set(h.q[0], h.q[1], h.q[2], h.q[3]).normalize();
        if (!chain.active || !chain.qInitialized) chain.targetQ.copy(_ikWorldQ);
        else chain.targetQ.slerp(_ikWorldQ, k);
        chain.qInitialized = true;
      }
      solveArmIK(rec, chain);
      if (chain.qInitialized) setBoneWorldQuaternion(chain.hand, chain.targetQ);
      chain.active = true;
      for (const name of names) rec.ikBones.add(name);
    }
  }

  function applyHeadTracking(rec, dt) {
    if (rec.isAgent || !rec.headPoseSeen || !rec.vrm || !rec.vrm.humanoid) return;
    const tr = rec.headTracking || buildHeadTracking(rec);
    if (!tr) return;
    if ((rec.animationBoneNames && (rec.animationBoneNames.has("neck") || rec.animationBoneNames.has("head")))
        || (rec.agentBones && (rec.agentBones.has("neck") || rec.agentBones.has("head")))) return;
    const k = 1 - Math.exp(-Math.max(0, Number(dt) || 0) * 12);
    tr.pitch = tr.initialized ? THREE.MathUtils.lerp(tr.pitch, rec.headPitchTarget, k) : rec.headPitchTarget;
    tr.initialized = true;
    const neckAngle = tr.pitch * 0.35;
    const headAngle = tr.pitch * 0.65;
    if (tr.neck && tr.restNeck) {
      _headPitchQ.setFromAxisAngle(_headAxisX, neckAngle);
      tr.neck.quaternion.copy(tr.restNeck).multiply(_headPitchQ);
    }
    if (tr.head && tr.restHead) {
      _headPitchQ.setFromAxisAngle(_headAxisX, headAngle);
      tr.head.quaternion.copy(tr.restHead).multiply(_headPitchQ);
    }
  }

  /* 走路循环的调试快照（验证/排查用）：速度、混合权重、腿的当前摆角 */
  function debugWalk(username) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed) return null;
    return {
      humanoid: !!rec.humanoidOn,
      speed: +(rec.speed || 0).toFixed(3),
      mix: +(rec.walkMix || 0).toFixed(3),
      phase: +(rec.walkPhase || 0).toFixed(2),
      legL: rec.legL ? +rec.legL.rotation.x.toFixed(3) : null,
      legR: rec.legR ? +rec.legR.rotation.x.toFixed(3) : null,
      fwd: +(rec.moveFwd || 0).toFixed(2),
      lat: +(rec.moveLat || 0).toFixed(2),
      y: +rec.root.position.y.toFixed(3),
    };
  }

  /* 单根骨骼的调试快照：当前四元数 + 是否已被 Agent 接管（排查 level 3 用） */
  function debugBone(username, bone) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed || !rec.vrm || !rec.vrm.humanoid) return null;
    const node = rec.vrm.humanoid.getNormalizedBoneNode(bone);
    if (!node) return null;
    return {
      q: node.quaternion.toArray().map((v) => +v.toFixed(3)),
      agent: !!(rec.agentBones && rec.agentBones.has(bone)),
    };
  }

  function debugAnimation(username) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed) return null;
    return {
      current: rec.animationClip ? (rec.animationClip.id || null) : null,
      time: +(rec.animationTime || 0).toFixed(3),
      queued: rec.animationQueue.length,
      bones: Array.from(rec.animationBoneNames),
    };
  }

  function debugAvatarPose(username) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed) return null;
    return { yaw: rec.root.rotation.y, isAgent: !!rec.isAgent };
  }

  function debugAvatarIK(username) {
    const rec = avatars.get(String(username));
    if (!rec || rec.disposed) return null;
    const chainInfo = (chain) => {
      if (!chain) return null;
      const p = chain.hand.getWorldPosition(new THREE.Vector3());
      return {
        active: !!chain.active,
        q: chain.hand.quaternion.toArray(),
        hand: p.toArray(),
        target: chain.target.toArray(),
        error: p.distanceTo(chain.target),
      };
    };
    return {
      left: chainInfo(rec.armIK && rec.armIK.left),
      right: chainInfo(rec.armIK && rec.armIK.right),
      head: rec.headTracking ? { pitch: rec.headTracking.pitch, target: rec.headPitchTarget } : null,
      calibration: { headHeight: rec.headHeight, poseHeadY: rec.poseHeadY },
      bones: Array.from(rec.ikBones || []),
    };
  }

  return {
    group, applyRoom, positionOf, setExpression, wave, update, dispose, setSeats,
    setRemotePose, removeRemote, clearLeft, debugWalk, setRemoteBones, debugBone,
    setAnimationCommand, debugAnimation, debugAvatarPose, debugAvatarIK,
  };
}

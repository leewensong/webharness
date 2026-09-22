/* xr-rooms.js — 房间 3D 场景（需求 9 的 map3d）。
   两种内置标准场景用 three.js 图元程序化搭建（会议室 10 座 / 狼人杀 12 座），
   用户上传的 GLB 与外链 URL 走 GLTFLoader。

   关键设计约束（改这里之前先读）：
   1. 家具活动区固定 6m×3m，居中于原点；相机水平活动半径 5.1m（xr-main 的 MAX_RADIUS），
      所以站在原点附近就能绕桌走动。
   2. **房间外壳必须够深**：消息墙是单列垂直面板，位于 z = −R = −6（xr-main 的 ANCHOR），
      所以外壳 z 向到 −6.6 才装得下它。外壳太浅会让墙切穿消息墙。
   3. **不建天花板、不建 z=−6 那面墙**：面板带最高到 BAND_HI = 6.0m，任何 3m 高的天花板
      都会切穿它；后侧由消息墙本身充当视觉墙。
   4. **只用 MeshStandardMaterial / MeshBasicMaterial，不加载外部贴图**，这样 dispose 是平凡的。
   5. BUILTIN_SCENES 的键必须与服务器 app/main.py 的 BUILTIN_ROOM_SCENES 一致。
      服务器是权威清单；这里不认识的 id 一律当作「无场景」回退展厅，绝不抛错。

   座位是**推荐位置**（不是强制分配）：形像按用户名 hash 就座（xr-avatars.setSeats），
   将来用户与 Agent 可自行决定位置（需求 9 的 xr_state，本期只预留）。 */

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

export const AREA = { w: 6, d: 3, h: 3.0 };      // 家具活动区（宽 × 深）
export const SHELL = { w: 14, d: 12.6, zMin: -6.6, zMax: 6.0, wallH: 3.0 };
/* 家具区整体沿 z 向房间深处偏移：相机默认在 z=2.6 朝 −z 看，家具居中于原点时几乎顶到
   眼前、显得局促。往消息墙方向挪一点，人站在家具后方、房间才显得宽敞。 */
export const ZONE_Z = -1.3;

const TABLE_H = 0.75;        // 桌面高度
const CHAIR_SEAT_H = 0.45;   // 椅面高度

/* 长条桌两侧各 N 个座位：北侧(z<0)朝 +z，南侧朝 −z，两侧相对而坐。
   ry 沿用 xr-avatars 的 atan2(-x,-z) 约定（面向房间中心），与环上站位一致。 */
function sideSeats(count, spanX, zOff) {
  const seats = [];
  const step = count > 1 ? spanX / (count - 1) : 0;
  const x0 = -spanX / 2;
  for (let i = 0; i < count; i++) {
    const x = Math.round((x0 + step * i) * 100) / 100;
    seats.push({ x, y: 0, z: ZONE_Z - zOff, ry: 0 });        // 北侧
  }
  for (let i = 0; i < count; i++) {
    const x = Math.round((x0 + step * i) * 100) / 100;
    seats.push({ x, y: 0, z: ZONE_Z + zOff, ry: Math.PI });  // 南侧
  }
  return seats;
}

/* ---------- 图元小件 ---------- */

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.85, metalness: 0.05, ...opts });
}

function box(w, h, d, material, x, y, z) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  mesh.position.set(x, y, z);
  return mesh;
}

/* 椅子：座面 + 靠背（背在远离桌子的一侧，由 ry 决定朝向） */
function buildChair(material) {
  const g = new THREE.Group();
  g.add(box(0.44, 0.05, 0.44, material, 0, CHAIR_SEAT_H, 0));
  g.add(box(0.44, 0.42, 0.05, material, 0, CHAIR_SEAT_H + 0.23, -0.2));
  const legGeo = new THREE.BoxGeometry(0.04, CHAIR_SEAT_H, 0.04);
  for (const [dx, dz] of [[-0.18, -0.18], [0.18, -0.18], [-0.18, 0.18], [0.18, 0.18]]) {
    const leg = new THREE.Mesh(legGeo, material);
    leg.position.set(dx, CHAIR_SEAT_H / 2, dz);
    g.add(leg);
  }
  return g;
}

/* 长条桌：桌面 + 四条腿（居中于原点，沿 x 长） */
function buildTable(material, legMaterial, w, d) {
  const g = new THREE.Group();
  g.add(box(w, 0.07, d, material, 0, TABLE_H, 0));
  const legGeo = new THREE.BoxGeometry(0.08, TABLE_H - 0.07, 0.08);
  const lx = w / 2 - 0.3;
  const lz = d / 2 - 0.2;
  for (const [dx, dz] of [[-lx, -lz], [lx, -lz], [-lx, lz], [lx, lz]]) {
    const leg = new THREE.Mesh(legGeo, legMaterial);
    leg.position.set(dx, (TABLE_H - 0.07) / 2, dz);
    g.add(leg);
  }
  return g;
}

/* 房间外壳：地板 + 两侧矮墙 + 前侧矮墙。不建天花板，也不建消息墙那面（见文件头约束 3）。 */
function buildShell(group, floorMat, wallMat) {
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(SHELL.w, SHELL.d), floorMat);
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(0, 0, (SHELL.zMin + SHELL.zMax) / 2);
  group.add(floor);

  const halfW = SHELL.w / 2;
  const depth = SHELL.zMax - SHELL.zMin;
  const midZ = (SHELL.zMin + SHELL.zMax) / 2;

  for (const sx of [-1, 1]) {
    group.add(box(0.12, SHELL.wallH, depth, wallMat, sx * halfW, SHELL.wallH / 2, midZ));
  }
  group.add(box(SHELL.w, SHELL.wallH, 0.12, wallMat, 0, SHELL.wallH / 2, SHELL.zMax));
}

/* 家具区子组：内部坐标以家具区中心为原点，整组沿 z 偏移 ZONE_Z。
   座位坐标（seats）已经是含 ZONE_Z 的绝对坐标，所以椅子不放进这个子组，免得二次偏移。 */
function buildZone(group) {
  const zone = new THREE.Group();
  zone.position.z = ZONE_Z;
  group.add(zone);
  return zone;
}

/* 家具活动区地面（6×3，比外壳地板略亮，标出「房间里这块是活动区」） */
function buildAreaFloor(group, material) {
  const patch = new THREE.Mesh(new THREE.PlaneGeometry(AREA.w, AREA.d), material);
  patch.rotation.x = -Math.PI / 2;
  patch.position.y = 0.012;
  group.add(patch);
}

/* ---------- 内置场景 ---------- */

/* 座位表（推荐位置）：会议室两侧各 5，狼人杀两侧各 6 */
const MEETING_SEATS = sideSeats(5, 4.0, 0.95);
const WEREWOLF_SEATS = sideSeats(6, 4.5, 1.0);

function buildMeeting(group) {
  const wallMat = mat(0x232a38);
  const floorMat = mat(0x171d28);
  const areaMat = mat(0x242c3b);
  const tableMat = mat(0x424b5e, { roughness: 0.6 });
  const legMat = mat(0x2b3242);
  const chairMat = mat(0x2f3748);

  buildShell(group, floorMat, wallMat);
  const zone = buildZone(group);
  buildAreaFloor(zone, areaMat);
  zone.add(buildTable(tableMat, legMat, 4.2, 1.1));

  for (const seat of MEETING_SEATS) {
    const chair = buildChair(chairMat);
    chair.position.set(seat.x, seat.y, seat.z);
    chair.rotation.y = seat.ry;
    group.add(chair);
  }

  /* 两端：一端投影幕（自发光），一端白板 */
  const screenMat = new THREE.MeshStandardMaterial({
    color: 0xdfe7f5, emissive: 0x2c3850, roughness: 1.0,
  });
  const boardMat = new THREE.MeshStandardMaterial({ color: 0xe8eef7, roughness: 0.9 });
  zone.add(box(0.06, 1.65, 1.9, screenMat, -2.95, 1.35, 0));
  zone.add(box(0.06, 1.65, 1.9, boardMat, 2.95, 1.35, 0));
}

function buildWerewolf(group) {
  const wallMat = mat(0x2a2333);
  const floorMat = mat(0x1b1622);
  const areaMat = mat(0x2b2436);
  const tableMat = mat(0x4a4260, { roughness: 0.6 });
  const legMat = mat(0x322b42);
  const chairMat = mat(0x38304a);

  buildShell(group, floorMat, wallMat);
  const zone = buildZone(group);
  buildAreaFloor(zone, areaMat);
  zone.add(buildTable(tableMat, legMat, 5.4, 1.2));

  for (const seat of WEREWOLF_SEATS) {
    const chair = buildChair(chairMat);
    chair.position.set(seat.x, seat.y, seat.z);
    chair.rotation.y = seat.ry;
    group.add(chair);
  }

  /* 端头法官席：一张小台 + 暖光标记 */
  zone.add(box(1.0, 0.07, 0.6, tableMat, -2.6, 0.95, 0));
  const lampMat = new THREE.MeshBasicMaterial({ color: 0xffd9a0 });
  zone.add(box(0.5, 0.05, 0.06, lampMat, -2.6, 1.02, 0));
}

export const BUILTIN_SCENES = {
  meeting: {
    id: "meeting",
    seatCount: MEETING_SEATS.length,
    seats: MEETING_SEATS,
    build: buildMeeting,
  },
  werewolf: {
    id: "werewolf",
    seatCount: WEREWOLF_SEATS.length,
    seats: WEREWOLF_SEATS,
    build: buildWerewolf,
  },
};

/* ---------- 用户场景（GLB / 外链） ---------- */

/* 归一化：水平 footprint 与高度**双约束**缩放到能放进活动区并落地。
   （xr-avatars 的形象只按高度归一化，这里不行——一个 20m 宽的大厅会压不进去。） */
function normalizeScene(model) {
  let bbox = new THREE.Box3().setFromObject(model);
  const size = bbox.getSize(new THREE.Vector3());
  const s = Math.min(
    AREA.w / (size.x || 1),
    AREA.d / (size.z || 1),
    AREA.h / (size.y || 1),
  );
  if (Number.isFinite(s) && s > 0) {
    model.scale.multiplyScalar(s);
    bbox = new THREE.Box3().setFromObject(model);
  }
  const center = bbox.getCenter(new THREE.Vector3());
  model.position.x -= center.x;
  model.position.z -= center.z;
  model.position.y -= bbox.min.y;
}

function disposeTree(root) {
  root.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose();
    if (!obj.material) return;
    const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
    for (const mt of mats) {
      for (const key of ["map", "normalMap", "roughnessMap", "metalnessMap", "emissiveMap", "aoMap"]) {
        if (mt[key] && mt[key].dispose) mt[key].dispose();
      }
      mt.dispose();
    }
  });
}

/* 描述符 → 稳定标识。xr-main 用它判断「场景是否真的变了」，避免每次房间轮询都重建几何。 */
export function sceneKeyOf(desc) {
  if (!desc || !desc.kind) return null;
  if (desc.kind === "builtin") return `builtin:${desc.id}`;
  if (desc.kind === "file" || desc.kind === "url") return `${desc.kind}:${desc.url || ""}`;
  return null;
}

/* 构建房间场景。返回：
     group     THREE.Group（已含图元或加载好的 GLB；无场景时是空 Group）
     seats     [{x,y,z,ry}]，内置场景有；GLB/外链为空（形象回退 hash 环）
     sceneId   "builtin:meeting" / "file:<url>" / null
     ready     Promise，GLB 加载完（失败也 resolve，静默回退）
     dispose() 释放本模块创建的全部资源并摘除 group
   未知 kind / 未知内置 id / 加载失败 → 一律当作无场景，绝不影响 2D（需求 1.5、7.4）。 */
export function buildRoomScene(desc, opts = {}) {
  const token = opts.token;
  const group = new THREE.Group();
  const key = sceneKeyOf(desc);
  let seats = [];
  let ready = Promise.resolve();
  let disposed = false;

  function dispose() {
    disposed = true;
    disposeTree(group);
    if (group.parent) group.parent.remove(group);
  }

  const builtin = desc && desc.kind === "builtin" ? BUILTIN_SCENES[desc.id] : null;
  if (builtin) {
    try {
      builtin.build(group);
      seats = builtin.seats.map((s) => ({ ...s }));
    } catch (err) {
      console.warn("[xr-rooms] 内置场景搭建失败，回退展厅", err);
      dispose();
      group.clear();
      return { group, seats: [], sceneId: null, ready, dispose };
    }
  } else if (desc && (desc.kind === "file" || desc.kind === "url") && desc.url) {
    ready = loadExternalScene(group, desc.url, token, () => disposed, disposeTree);
  } else {
    if (desc && desc.kind === "builtin") {
      console.warn("[xr-rooms] 未知的内置场景 id，回退展厅:", desc.id);
    }
    return { group, seats, sceneId: null, ready, dispose };
  }

  return { group, seats, sceneId: key, ready, dispose };
}

/* 加载用户场景。isDisposed() 在加载期间可能变真（用户很快退出 3D）：那样就不能再挂进
   已释放的 group，直接把刚加载的模型释放掉，否则会漏到场景外。 */
async function loadExternalScene(group, url, token, isDisposed, disposeFn) {
  let objectUrl = null;
  let model = null;
  try {
    let src = url;
    if (src.startsWith("/api/")) {
      /* 房间场景走鉴权接口：带 token 取 blob 再交给 loader（与形象加载同一套路） */
      const resp = await fetch(src, { headers: token ? { Authorization: "Bearer " + token } : {} });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      objectUrl = URL.createObjectURL(await resp.blob());
      src = objectUrl;
    }
    const gltf = await new GLTFLoader().loadAsync(src);
    model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
    if (!model) throw new Error("empty scene");
    /* 源场景常自带灯光，会与既有的半球光/平行光叠加过曝 */
    model.traverse((o) => { if (o.isLight) o.visible = false; });
    normalizeScene(model);
    if (isDisposed()) { disposeFn(model); return; }
    group.add(model);
  } catch (err) {
    console.warn("[xr-rooms] 房间场景加载失败，回退展厅：", url, err);
    if (model) disposeFn(model);
  } finally {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
  }
}

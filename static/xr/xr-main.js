/* WebXR 3D 渲染端主模块：场景生命周期、竖列消息墙（2D 直列式布局 + 右侧滚行条）、
   桌面第一人称预览。
   架构不变量：3D 只订阅 2D 端的 msgEvents/roomEvents（单一数据流），不建第二套轮询；
   历史回填是沿墙向前的一次性 beforeId 分页拉取（需求 2.7）。退出时资源全量 dispose，
   任何 3D 故障不得影响 2D 正常聊天（需求 1.5、7.4）。 */

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { isModelFilename } from "../model-preview.js";
import { mergeXRI18n } from "./xr-i18n.js";
import { createPanelSystem } from "./xr-panels.js";
import { createNativeSystem } from "./xr-native.js";
import { createAvatarSystem, ARKIT52, PRESENCE_BONES } from "./xr-avatars.js";
import { HAND_ORIENTATION, readXRHands } from "./xr-hand-pose.js";
import { createXRFiles } from "./xr-files.js";
import { createXRWorldUI } from "./xr-world-ui.js";
import { buildRoomScene, sceneKeyOf } from "./xr-rooms.js";

const R = 6;             // 消息列半径（米）
const PANEL_W = 1.12;    // 参考面板世界宽（米）——单面板实际宽 = 气泡 CSS 宽 × PX_PER_M
const REF_CSS = 356;     // 参考 CSS 宽（与 xr-panels 的 refCss 一致）：全局 px→米 比例的分母
const PX_PER_M = PANEL_W / REF_CSS; /* 短消息窄、长消息宽，字号全局一致（不再等宽压扁）；356 对应 15px 字 ≈ 4.7cm */
const ANCHOR = Math.PI;  // 消息列方位角（相机默认在 +Z 侧面向 −Z 看墙正面）
const FLOOR_Y = 0.42;    // 列底基准：最新面板底边（历史向上堆叠，越旧越高）
const GAP = 0.02;        // 面板纵向间距（贴紧：像 2D 里连续的气泡列）
const BAND_HI = 2.42;    // 可视带顶（带底 = FLOOR_Y）：带高 2.0m ≈ 同时 4~5 条
const LOG_W = 1.9;       // scroll panel 记录区内容宽上限（米）：再宽的面板等比收缩
const LOG_PAD = 0.14;    // 记录区内边距（边界相对内容外扩）
const LOG_L = -PANEL_W / 2 - LOG_PAD; /* 记录区内容左缘（面板左对齐于此） */
const EYE_Y = 1.55;
const MAX_RADIUS = 5.1;  // 相机水平活动半径（离墙 0.9m）

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function createXR(ctx) {
  if (!ctx.root) throw new Error("xrRoot missing");
  if (ctx.I18N) mergeXRI18n(ctx.I18N);
  const t = ctx.t || ((k) => k);
  const tf = ctx.tf || ((k, v) => t(k));

  if (!document.createElement("canvas").getContext("webgl2")) {
    throw new Error(t("xrFallback"));
  }

  let disposed = false;
  let worldUi = null;

  /* ---------- 渲染器与场景 ---------- */

  /* alpha + 透明清屏是 immersive-ar 透传真实环境的必要条件；桌面模式仍由
     渐变天空提供完整背景。 */
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.setSize(window.innerWidth, window.innerHeight);
  /* cssText 会整块覆盖 setSize 刚写入的宽高：必须自带 100%×100%，否则 dpr>1 时
     画布按缓冲区像素显示、溢出窗口，整个画面偏移（canvas 是 replaced element，
     inset:0 不会拉伸它） */
  renderer.domElement.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;";
  renderer.xr.enabled = true; /* 沉浸式会话接入（任务 10）；无会话时为普通桌面渲染 */
  renderer.xr.setReferenceSpaceType("local-floor");
  renderer.localClippingEnabled = true; /* scroll panel 视口裁剪（面板材质用 clippingPlanes） */
  ctx.root.classList.remove("hidden");
  ctx.root.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 0.05, 200);
  camera.rotation.order = "YXZ";
  camera.position.set(0, EYE_Y, 2.6);
  /* rig：用户在场景中的「载体」。桌面模式 rig 恒等（相机行为与此前完全一致）；
     沉浸式下 XR 位姿写入 camera（相对 rig），传送/平移/转向作用于 rig。 */
  const rig = new THREE.Group();
  rig.add(camera);
  scene.add(rig);
  /* 语音空间音频监听者（需求 5.1）：挂相机随视点移动 */
  const audioListener = new THREE.AudioListener();
  camera.add(audioListener);

  /* 渐变天空（安静展厅，需求 2.x 视觉基调） */
  const skyMat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      top: { value: new THREE.Color(0x0b1226) },
      horizon: { value: new THREE.Color(0x233457) },
      bottom: { value: new THREE.Color(0x0a0e16) },
    },
    vertexShader: "varying vec3 vDir; void main(){ vDir = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: `varying vec3 vDir; uniform vec3 top; uniform vec3 horizon; uniform vec3 bottom;
      void main(){
        float h = vDir.y;
        vec3 c = h >= 0.0 ? mix(horizon, top, pow(min(h * 1.4, 1.0), 0.75)) : mix(horizon, bottom, min(-h * 2.2, 1.0));
        gl_FragColor = vec4(c, 1.0);
      }`,
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(70, 32, 16), skyMat);
  scene.add(sky);

  const hemi = new THREE.HemisphereLight(0x93a9d4, 0x2c3746, 1.5);
  scene.add(hemi);
  const dirLight = new THREE.DirectionalLight(0xbccbe8, 0.55);
  dirLight.position.set(3, 8, 2);
  scene.add(dirLight);

  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(18, 64),
    new THREE.MeshBasicMaterial({ color: 0x0d1420 })
  );
  ground.rotation.x = -Math.PI / 2;
  scene.add(ground);
  const grid = new THREE.GridHelper(36, 36, 0x24344f, 0x131b2a);
  grid.position.y = 0.02;
  scene.add(grid);
  const centerRing = new THREE.Mesh(
    new THREE.RingGeometry(0.5, 0.56, 48),
    new THREE.MeshBasicMaterial({ color: 0x2e4a78, side: THREE.DoubleSide })
  );
  centerRing.rotation.x = -Math.PI / 2;
  centerRing.position.y = 0.03;
  scene.add(centerRing);

  /* ---------- 面板系统 ---------- */

  /* scroll panel 视口裁剪：4 个裁剪面把面板裁到面板框内——x 为框的左右外缘，
     y 为可视带上下缘（像真正的滚动视口，超出的部分被切掉而不是飘在面板外）。
     常量每帧在 updateScrollBar 里按当前框宽刷新；聚焦的面板例外（setNoClip）。 */
  const clipL = new THREE.Plane(new THREE.Vector3(1, 0, 0), -(LOG_L - LOG_PAD));
  const clipR = new THREE.Plane(new THREE.Vector3(-1, 0, 0), LOG_L + LOG_W + LOG_PAD * 2);
  const clipB = new THREE.Plane(new THREE.Vector3(0, 1, 0), -FLOOR_Y);
  const clipT = new THREE.Plane(new THREE.Vector3(0, -1, 0), BAND_HI);
  const panelClip = [clipL, clipR, clipB, clipT];

  const panels = createPanelSystem({
    t,
    panelWidth: PANEL_W,
    refCss: REF_CSS,
    maxW: LOG_W, /* 记录区宽即面板宽上限：再宽的面板也不会伸到右侧滚行条底下 */
    maxH: 2.35,
    maxTextures: 24,
    clipPlanes: panelClip,
  });
  scene.add(panels.group);

  /* 原生 3D 图表（```chart 数据驱动，需求 3.1/3.2；面板模式保留为兜底开关） */
  const native = createNativeSystem({ a2ui: ctx.a2ui, fetchImage: fetchImageObjectURL });
  scene.add(native.group);

  /* ---------- 成员形象（需求 4）：xr-avatars 加载 GLB/VRM + 缺省胶囊，站位按用户名散列。
     初始成员取 roomInfo()（进入 3D 时已错过的最后一次 roomEvents 快照），之后随
     roomEvents 增量同步。加载失败静默回退胶囊（需求 4.5）。 */
  const avatars = createAvatarSystem({ t, tf, username: ctx.username, token: ctx.token });
  scene.add(avatars.group);

  /* ---------- 房间 3D 场景（需求 9 的 map3d） ----------
     有场景时用场景替换展厅的网格与中央环（渐变天空与灯光保留作基调），并把场景提供的
     推荐座位交给形象系统；无场景、未知 id、加载失败一律保持展厅原样，绝不影响 2D（需求 1.5）。
     只在描述符真的变了才重建——否则每次房间轮询都会重搭一遍几何。 */
  let roomSystem = null;
  let xrPassthrough = false;
  let roomAppliedKey;   /* undefined = 还没应用过 */

  function refreshShowroomVisuals() {
    const hasRoomScene = !!roomSystem;
    /* AR 下隐藏虚拟环境（包括房间场景的墙壁/地板），否则会盖住真实环境。
       消息、成员形象、共同文件模型属于独立组，继续显示；座位/共享世界坐标不变。 */
    sky.visible = !xrPassthrough;
    ground.visible = !xrPassthrough;
    grid.visible = !xrPassthrough && !hasRoomScene;
    centerRing.visible = !xrPassthrough && !hasRoomScene;
    if (roomSystem) roomSystem.group.visible = !xrPassthrough;
  }

  function applyRoomScene(desc) {
    const key = sceneKeyOf(desc);
    if (key === roomAppliedKey) return;
    roomAppliedKey = key;
    let next = null;
    try {
      next = buildRoomScene(desc, { token: ctx.token && ctx.token() });
    } catch (err) {
      console.warn("[xr] 房间场景构建失败，保持展厅", err);
      next = null;
    }
    if (roomSystem) { scene.remove(roomSystem.group); roomSystem.dispose(); }
    roomSystem = next && next.sceneId ? next : null;
    if (next && !roomSystem) next.dispose();   /* 空/未知场景：别留下垃圾组 */
    const on = !!roomSystem;
    ground.position.y = on ? -0.01 : 0;        /* 让位给场景地板，避免 z-fighting */
    refreshShowroomVisuals();
    if (on) scene.add(roomSystem.group);
    try { avatars.setSeats(on ? roomSystem.seats : []); } catch (err) {}
  }

  try { applyRoomScene((ctx.roomInfo && ctx.roomInfo() && ctx.roomInfo().scene) || null); } catch (err) {}
  try { avatars.applyRoom(ctx.roomInfo && ctx.roomInfo()); } catch (err) {}

  /* ---------- 房间内 3D 位姿同步（人类上报 + 他人位姿渲染） ----------
     人类位姿 = camera 世界位姿（桌面 WASD / 头显 6DoF 都反映在这里）+（沉浸式）两只
     手柄位姿；0.5s 上报一次。Agent 走同一接口自报，服务端按步行速度校验不许瞬移。
     首次拉全量快照并记下服务端 serverTime 当游标，之后每 0.5s 拉增量；服务端返回
     reset=true（游标过期 / 落后太多）时退回全量重取。离开 3D 时发一条 leave，
     其他人立即移除其形象。旧服务端没有这些接口时静默关闭（404 → presenceOff）。 */
  /* 位姿流：二进制脏位增量 + 服务端 hold 节流 + 按距离分级 + 关键帧动画命令。
     上报 10Hz（每 100ms 一次）；拉取的 tick 由服务端的 hold 决定，客户端只留一个下限
     防打点（服务端立刻就有数据时也不至于狂发）。分级阈值与服务端一致。 */
  const PRESENCE_SEND_MS = 100;
  const PRESENCE_POLL_MS = 60;         /* 客户端下限：真正节奏由服务端 hold 决定 */
  const PRESENCE_HOLD_MS = 100;        /* 传给服务端的节流窗口（毫秒）≈ 10Hz */
  const PRESENCE_BIN_MAGIC = 0xb1;     /* 与 app/main.py 的 PRESENCE_BIN_MAGIC 对应 */
  const PRESENCE_BIN_VERSION = 3;      /* 版本 3：hands 带 left/right，供 Avatar IK */
  const P_DIRTY_POS = 1;
  const P_DIRTY_ORIENT = 2;
  const P_DIRTY_HANDS = 4;
  const P_DIRTY_STATE = 8;
  const P_DIRTY_BONES = 16;   /* level 3：Agent 上报的骨骼（只覆盖它报过的关节） */
  const P_DIRTY_FACE = 32;    /* level 3：Agent 上报的 ARKit52 表情权重（只覆盖报过的） */
  const P_DIRTY_ANIMATION = 64; /* 一次性关键帧动画命令：replace/append/stop */
  const PRESENCE_NEAR_M = 5.0;
  const PRESENCE_MID_M = 15.0;
  const PRESENCE_FAR_APPLY_MS = 500;   /* 远处成员本地最多每 500ms 应用一次（2Hz） */
  const PRESENCE_PACK_POS = 100.0;     /* 位置量化：厘米 */
  const PRESENCE_PACK_ANGLE = 65536.0 / (2 * Math.PI);
  let presenceSendAcc = 0;
  let presencePollAcc = 0;
  let presenceCursor = null;   /* 增量日志 id 游标（单调递增；比时间戳稳，不会漏同一毫秒的事件） */
  let presenceBusy = false;    /* 拉取在途（避免请求堆积） */
  let presenceOff = false;     /* 服务端不支持（404）→ 关闭 */
  let presenceWarned = false;  /* 协议不匹配只警告一次（避免每 tick 刷屏） */
  let memberRefreshAt = 0;
  const presenceIdNames = new Map();  /* userId → username（二进制帧只带数字 id） */
  const presenceFarAt = new Map();    /* username → 上次应用时间（远处限频） */
  /* username → 累积出的完整位姿。增量帧只带「变了的那几个字段」，而 setRemotePose
     需要完整位姿（位置/朝向/手/状态），所以必须在这里把脏字段并进上一份完整位姿。 */
  const presencePoses = new Map();

  /* 这里才第一次学 id 映射：presenceIdNames 是 const，**必须等它和本节其它声明都求值完**
     再调用，否则 TDZ 报错会被外面的 try/catch 静默吞掉（表现是远端形象永远不动）。 */
  presenceLearnIds(ctx.roomInfo && ctx.roomInfo());

  /* 从 roomEvents 的在线成员里学习 id→用户名映射（服务端 _online_users 现在带 userId） */
  function presenceLearnIds(info) {
    for (const u of (info && info.onlineUsers) || []) {
      if (u && u.username && typeof u.userId === "number") presenceIdNames.set(u.userId, u.username);
    }
  }

  function presenceRoom() {
    const r = ctx.roomName && ctx.roomName();
    return r ? encodeURIComponent(r) : "";
  }

  function localPosePayload() {
    const p = new THREE.Vector3();
    const q = new THREE.Quaternion();
    camera.getWorldPosition(p);
    camera.getWorldQuaternion(q);
    const e = new THREE.Euler().setFromQuaternion(q, "YXZ");
    let hands = [];
    if (xrInImmersive && renderer.xr.isPresenting) {
      const session = renderer.xr.getSession();
      const sources = session && session.inputSources ? Array.from(session.inputSources) : [];
      hands = readXRHands(xrControllers, xrControllerGrips, xrHandSpaces, sources);
    }
    // Keep the existing binary layout: a small state tag identifies the quaternion
    // frame, preventing the receiver from applying the grip correction twice.
    return { p: [p.x, p.y, p.z], yaw: e.y, pitch: e.x, hands,
      state: hands.length ? { handOrientation: HAND_ORIENTATION } : null };
  }

  async function presenceSend() {
    const room = presenceRoom();
    if (presenceOff || disposed || !room) return;
    try {
      /* 上报的响应**不动游标**：游标是增量日志 id，由增量响应头给出（曾经在这里用
         serverTime 覆盖过，时间戳喂给 sinceId 会 422，整个位姿流静默失效）。 */
      await ctx.api(`/api/rooms/${room}/presence`, {
        method: "POST",
        body: JSON.stringify(localPosePayload()),
      });
    } catch (err) {
      if (err && err.status === 404) presenceOff = true; /* 旧服务端：静默关闭 */
      /* 服务端会把越速位移裁到步行上限（响应 clamped），不报错；这里只需忽略网络抖动 */
    }
  }

  /* 收到位的位姿事件但本地还没有这个成员的形象（房间快照还没轮询到）→ 拉一次房间详情重建 */
  async function ensurePresenceMember(username, pose) {
    const now = performance.now();
    if (now - memberRefreshAt < 3000) return; /* 去抖：3s 内最多拉一次 */
    memberRefreshAt = now;
    const room = presenceRoom();
    if (!room) return;
    try {
      const info = await ctx.api(`/api/rooms/${room}`);
      avatars.applyRoom(info);
      if (pose && pose.animation) avatars.setAnimationCommand(username, pose.animation);
      if (username) avatars.setRemotePose(username, pose);
    } catch (err) { /* 拉不到就等下一次房间轮询 */ }
  }

  function applyRemoteState(username, state) {
    if (!state) return;
    if (typeof state.expression === "string") {
      try { avatars.setExpression(username, state.expression, Number(state.weight) || 1); } catch (err) {}
    }
  }

  /* 二进制帧必须走裸 fetch：ctx.api 会把响应当 JSON 解析 */
  async function presenceFetchBin(url) {
    const res = await fetch(url, {
      headers: ctx.token ? { Authorization: "Bearer " + ctx.token() } : {},
      cache: "no-store",
    });
    if (!res.ok) {
      const err = new Error("HTTP " + res.status);
      err.status = res.status;
      throw err;
    }
    return {
      cursor: res.headers.get("X-Presence-Id") || null,
      reset: res.headers.get("X-Presence-Reset") === "1",
      buf: await res.arrayBuffer(),
    };
  }

  /* 解一帧二进制增量（与 app/main.py 的 _presence_frame_bytes 严格对应）：
     帧头 3B（magic/version/count），条目为 u32 id + u8 kind + u8 脏位 + 按脏位排列的字段。
     kind: 0=位姿 1=离开；version=3 包含 hands 左右标记与 64=关键帧动画 JSON 块。 */
  function presenceDecodeFrame(buf) {
    const dv = new DataView(buf);
    if (dv.byteLength < 3 || dv.getUint8(0) !== PRESENCE_BIN_MAGIC || dv.getUint8(1) !== PRESENCE_BIN_VERSION) return [];
    const count = dv.getUint8(2);
    const out = [];
    let o = 3;
    for (let i = 0; i < count && o + 6 <= dv.byteLength; i++) {
      const userId = dv.getUint32(o, true); o += 4;
      const kind = dv.getUint8(o); o += 1;
      const mask = dv.getUint8(o); o += 1;
      if (kind !== 0) { out.push({ userId, kind }); continue; }
      /* 注意：只放**实际存在**的字段（稀疏位姿）。增量帧只带脏字段，
         缺失字段绝不能填默认值——填了 p:[0,0,0] 会把远端形象瞬移到原点。 */
      const pose = {};
      if (mask & P_DIRTY_POS) {
        pose.p = [
          dv.getInt16(o, true) / PRESENCE_PACK_POS,
          dv.getInt16(o + 2, true) / PRESENCE_PACK_POS,
          dv.getInt16(o + 4, true) / PRESENCE_PACK_POS,
        ];
        o += 6;
      }
      if (mask & P_DIRTY_ORIENT) {
        pose.yaw = dv.getInt16(o, true) / PRESENCE_PACK_ANGLE;
        pose.pitch = dv.getInt16(o + 2, true) / PRESENCE_PACK_ANGLE;
        o += 4;
      }
      if (mask & P_DIRTY_HANDS) {
        const n = dv.getUint8(o); o += 1;
        pose.hands = [];
        for (let h = 0; h < n; h++) {
          const side = dv.getUint8(o); o += 1;
          pose.hands.push({
            handedness: side === 1 ? "left" : side === 2 ? "right" : null,
            p: [
              dv.getInt16(o, true) / PRESENCE_PACK_POS,
              dv.getInt16(o + 2, true) / PRESENCE_PACK_POS,
              dv.getInt16(o + 4, true) / PRESENCE_PACK_POS,
            ],
            q: [
              dv.getInt16(o + 6, true) / 32767, dv.getInt16(o + 8, true) / 32767,
              dv.getInt16(o + 10, true) / 32767, dv.getInt16(o + 12, true) / 32767,
            ],
          });
          o += 14;
        }
      }
      if (mask & P_DIRTY_STATE) {
        const len = dv.getUint8(o); o += 1;
        try {
          pose.state = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, o, len)));
        } catch (err) { pose.state = null; }
        o += len;
      }
      if (mask & P_DIRTY_BONES) {
        const n = dv.getUint8(o); o += 1;
        pose.bones = {};
        for (let b = 0; b < n; b++) {
          const idx = dv.getUint8(o); o += 1;
          pose.bones[PRESENCE_BONES[idx]] = [
            dv.getInt16(o, true) / 32767, dv.getInt16(o + 2, true) / 32767,
            dv.getInt16(o + 4, true) / 32767, dv.getInt16(o + 6, true) / 32767,
          ];
          o += 8;
        }
      }
      if (mask & P_DIRTY_FACE) {
        const n = dv.getUint8(o); o += 1;
        pose.face = {};
        for (let f = 0; f < n; f++) {
          const idx = dv.getUint8(o); const val = dv.getUint8(o + 1); o += 2;
          pose.face[ARKIT52[idx]] = val / 255;
        }
      }
      if (mask & P_DIRTY_ANIMATION) {
        if (o + 4 > dv.byteLength) return [];
        const len = dv.getUint32(o, true); o += 4;
        if (len > dv.byteLength - o) return [];
        try {
          pose.animation = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, o, len)));
        } catch (err) { pose.animation = null; }
        o += len;
      }
      out.push({ userId, kind: 0, pose });
    }
    return out;
  }

  /* 按距离本地限频：近处每帧都应用；远处最多 2Hz（服务端也已按距离裁掉了手与状态） */
  function presenceApplyLod(username, pose) {
    const sp = pose && pose.p;
    if (!sp) return true;
    const me = new THREE.Vector3();
    camera.getWorldPosition(me);
    const dist = Math.hypot(sp[0] - me.x, sp[2] - me.z);
    if (dist < PRESENCE_MID_M) { presenceFarAt.delete(username); return true; }
    const now = performance.now();
    if (now - (presenceFarAt.get(username) || 0) < PRESENCE_FAR_APPLY_MS) return false;
    presenceFarAt.set(username, now);
    return true;
  }

  /* 把增量里的脏字段并入该成员的完整位姿，返回合并后的结果 */
  function presenceMergePose(username, ev) {
    const cur = presencePoses.get(username) || { p: [0, 1.6, 0], yaw: 0, pitch: 0, hands: [], state: null };
    const d = ev.pose || {};
    if (d.p) cur.p = d.p;
    if (d.yaw !== undefined) cur.yaw = d.yaw;
    if (d.pitch !== undefined) cur.pitch = d.pitch;
    if (d.hands !== undefined) cur.hands = d.hands;
    if (d.state !== undefined) cur.state = d.state;
    /* 骨骼/表情也是稀疏的：只把报过的并进去，未报的保持上一次的值 */
    if (d.bones !== undefined) cur.bones = Object.assign({}, cur.bones, d.bones);
    if (d.face !== undefined) cur.face = Object.assign({}, cur.face, d.face);
    if (d.animation !== undefined) cur.animation = d.animation;
    presencePoses.set(username, cur);
    return cur;
  }

  async function presencePoll() {
    const room = presenceRoom();
    if (presenceOff || disposed || presenceBusy || !room) return;
    presenceBusy = true;
    const me = ctx.username && ctx.username();
    try {
      if (!presenceCursor) {
        /* 全量：首次进入 3D，或游标过期/落后太多后重取（JSON，字段齐全） */
        const snap = await ctx.api(`/api/rooms/${room}/presence`);
        presenceCursor = (snap && snap.logId != null) ? snap.logId : null;
        for (const u of snap.users || []) {
          if (!u.username || u.username === me) continue;
          if (u.pose && u.pose.animation) {
            try { avatars.setAnimationCommand(u.username, u.pose.animation); } catch (err) {}
          }
          if (!avatars.setRemotePose(u.username, u.pose)) ensurePresenceMember(u.username, u.pose);
          else applyRemoteState(u.username, u.state);
        }
      } else {
        const { cursor, reset, buf } = await presenceFetchBin(
          `/api/rooms/${room}/presence/delta?sinceId=${encodeURIComponent(presenceCursor)}`
            + `&hold=${PRESENCE_HOLD_MS}&fmt=bin`
        );
        if (reset) { presenceCursor = null; return; }   /* 下一轮走全量重取 */
        if (cursor) presenceCursor = cursor;
        for (const ev of presenceDecodeFrame(buf)) {
          const username = presenceIdNames.get(ev.userId);
          if (!username || username === me) continue;
          if (ev.kind === 1) {
            avatars.removeRemote(username);
            presencePoses.delete(username);
            presenceFarAt.delete(username);
            continue;
          }
          const merged = presenceMergePose(username, ev);
          /* 骨骼/表情（level 3）只在近处才下发（服务端 LOD），且与本地限频无关，先应用它们。
             只应用**本次新增**的那些——之前应用过的关节/表情已经落在模型上，没人会覆盖
             （程序化动画对 Agent 接管的关节会让路）。 */
          if (ev.pose && ev.pose.bones) {
            try { avatars.setRemoteBones(username, ev.pose.bones); } catch (err) {}
          }
          if (ev.pose && ev.pose.face) {
            for (const [ename, weight] of Object.entries(ev.pose.face)) {
              try { avatars.setExpression(username, ename, weight); } catch (err) {}
            }
          }
          if (ev.pose && ev.pose.animation) {
            try { avatars.setAnimationCommand(username, ev.pose.animation); } catch (err) {}
          }
          if (!presenceApplyLod(username, merged)) continue;
          if (!avatars.setRemotePose(username, merged)) ensurePresenceMember(username, merged);
          else applyRemoteState(username, merged.state);
        }
      }
    } catch (err) {
      if (err && err.status === 404) presenceOff = true;
      else if (err && err.status === 422) {
        /* 协议不匹配（例如游标类型不对）：重置回全量自愈，并只警告一次——
           422 既不触发 presenceOff 也不重试的话，位姿流会静默卡死。 */
        if (!presenceWarned) {
          presenceWarned = true;
          console.warn("[xr] presence 增量请求被拒（422），已重置游标改走全量");
        }
        presenceCursor = null;
      }
    } finally {
      presenceBusy = false;
    }
  }

  /* 离开 3D：发一条 leave（别人立即移除我的形象），并清空本地游标 */
  function presenceLeave() {
    const room = presenceRoom();
    presenceCursor = null;
    if (presenceOff || !room) return;
    try {
      ctx.api(`/api/rooms/${room}/presence/leave`, { method: "POST", body: "{}" });
    } catch (err) { /* 退出路径不容错：失败就靠在线窗口自然过期 */ }
  }

  /* ---------- 语音空间音频（需求 5.1/5.2）：2D 播放链创建的 audio 元素经
     setVoiceSpatial 注册的钩子路由进 PositionalAudio(HRTF)；声源绑定发送者形象
     站位（随站位），无形象回退消息面板位。播放结束自动摘除；进 3D 时 2D 端
     已停止播放（enterXR 侧），2D/3D 不并存出声。 */
  const spatialVoices = new Map(); // msgId → { pa, srcNode, el, username, onEnd }
  function dropSpatialVoice(id) {
    const e = spatialVoices.get(String(id));
    if (!e) return;
    spatialVoices.delete(String(id));
    try { e.el.removeEventListener("ended", e.onEnd); } catch (err) {}
    try { e.srcNode.disconnect(); } catch (err) {}
    /* MediaElementSource 路由是永久的：断开声像图后接回 destination，
       兜底 2D 侧对同一元素的续播（平铺出声，静默失败也无碍） */
    try { e.srcNode.connect(audioListener.context.destination); } catch (err) {}
    try { e.pa.disconnect(); } catch (err) {}
    if (e.pa.parent) e.pa.parent.remove(e.pa);
    if (e.username) { try { avatars.setExpression(e.username, "jawOpen", 0); } catch (err) {} }
  }
  function spatialHook(audioEl, msg) {
    if (!audioEl || !msg || msg.id == null) return;
    const id = String(msg.id);
    dropSpatialVoice(id); /* 同消息重复播放：先清旧路由 */
    const ac = audioListener.context;
    if (ac.state === "suspended") { try { ac.resume(); } catch (err) {} }
    const pa = new THREE.PositionalAudio(audioListener);
    pa.setRefDistance(1.6);
    pa.setRolloffFactor(1.4);
    const srcNode = ac.createMediaElementSource(audioEl);
    pa.setNodeSource(srcNode); /* 元素输出 → panner(HRTF) → 监听者 */
    scene.add(pa);
    const entry = { pa, srcNode, el: audioEl, username: msg.username || "", onEnd: null };
    entry.onEnd = () => dropSpatialVoice(id);
    audioEl.addEventListener("ended", entry.onEnd);
    spatialVoices.set(id, entry);
    audioEl.dataset.xrSpatial = "1";
  }
  try { ctx.setVoiceSpatial(spatialHook); } catch (err) {}

  function updateSpatialVoices() {
    const nowS = performance.now() / 1000;
    for (const [, e] of spatialVoices) {
      const sp = e.username ? avatars.positionOf(e.username) : null;
      if (sp) e.pa.position.set(sp.x, sp.y + 1.45, sp.z); /* 形象嘴部高度 */
      else {
        const pp = panels.positionOf(e.msgId);
        if (pp) e.pa.position.copy(pp);
      }
      /* 口型 best-effort（需求 5.2）：有表情能力的形象 jawOpen 振荡，其余静默 */
      if (e.username) {
        try { avatars.setExpression(e.username, "jawOpen", 0.28 + 0.24 * Math.sin(nowS * 13)); } catch (err) {}
      }
    }
  }

  /* ---------- 聚焦模式（需求 7.2 / 3.6）：对准一条消息放大到舒适阅读（~40° 视角）。
     桌面映射：面板双击进入 / 聚焦中单击返回；图片平面单击进入（指向放大）；
     Esc 先退聚焦再退 3D。头显侧手柄射线在任务 10 接同一 API（enterFocus/exitFocus）。 */

  let focus = null;   // { id, kind: "panel" | "image" } | null
  let lastClick = { id: null, t: 0 }; /* 双击检测（面板聚焦入口） */

  function enterFocus(id, kind) {
    if (focus && focus.kind === "panel") panels.setNoClip(focus.id, false);
    focus = { id: String(id), kind };
    /* 聚焦的面板移到视点前（在面板框之外），必须退出视口裁剪才看得全 */
    if (kind === "panel") panels.setNoClip(focus.id, true);
  }
  function exitFocus() {
    if (focus && focus.kind === "panel") panels.setNoClip(focus.id, false);
    focus = null;
    native.setOverride(null);
    native.setFocused(null);
  }

  function focusPose(hWorld) {
    const dist = THREE.MathUtils.clamp((hWorld * 0.5) / Math.tan(THREE.MathUtils.degToRad(20)), 0.5, 2.2);
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-8) fwd.set(0, 0, -1);
    fwd.normalize();
    const x = camera.position.x + fwd.x * dist;
    const z = camera.position.z + fwd.z * dist;
    return { x, y: EYE_Y, z, rotY: Math.atan2(camera.position.x - x, camera.position.z - z) };
  }

  async function fetchImageObjectURL(url) {
    const resp = await fetch(url, { headers: ctx.token ? { Authorization: "Bearer " + ctx.token() } : {} });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return URL.createObjectURL(await resp.blob());
  }

  /* ---------- HUD（xrRoot 内 HTML 覆盖层） ---------- */

  const hudStyle = document.createElement("style");
  hudStyle.textContent = `
    #xrRoot .xr-hud { position: absolute; inset: 0; pointer-events: none; z-index: 2;
      font-family: "SF Pro Text", "PingFang SC", "Noto Sans SC", sans-serif; color: #dfe8f4; }
    #xrRoot .xr-top { position: absolute; top: 14px; left: 16px; display: flex; gap: 10px; align-items: center; pointer-events: auto; }
    #xrRoot .xr-btn { pointer-events: auto; border: 1px solid #2c3d58; background: rgba(16, 22, 34, 0.82);
      color: #dfe8f4; font-size: 13px; padding: 8px 14px; border-radius: 999px; cursor: pointer; }
    #xrRoot .xr-btn:hover { border-color: #5b8cff; }
    #xrRoot .xr-title { color: #8fa3bd; font-size: 13px; }
    #xrRoot .xr-hint { position: absolute; top: 18px; right: 18px; color: #7c90aa; font-size: 12px; max-width: 46vw; text-align: right; }
    #xrRoot .xr-status { position: absolute; bottom: 92px; left: 50%; transform: translateX(-50%); color: #8fa3bd; font-size: 13px; }
    #xrRoot .xr-send { position: absolute; bottom: 4px; left: 50%; transform: translateX(-50%);
      display: flex; gap: 8px; pointer-events: auto; }
    #xrRoot .xr-send-input { width: min(430px, 56vw); padding: 9px 14px; font-size: 13px;
      border: 1px solid #2c3d58; border-radius: 999px; background: rgba(16, 22, 34, 0.82);
      color: #dfe8f4; outline: none; }
    #xrRoot .xr-send-input:focus { border-color: #5b8cff; }
    /* 沉浸式由世界内 CanvasTexture UI 接管顶部和底部操作栏；DOM 仍保留一个
       透明、可聚焦的输入框，用于唤起系统键盘。无论设备是否授予 dom-overlay，
       都隐藏 DOM 视觉层，避免同一组按钮在视野里出现两份。 */
    #xrRoot .xr-hud.xr-immersive .xr-hint,
    #xrRoot .xr-hud.xr-immersive .xr-top { display: none; }
    #xrRoot .xr-hud.xr-immersive .xr-send {
      position: fixed; left: -10000px; bottom: 0; width: 1px; height: 1px;
      opacity: 0; pointer-events: none; transform: none;
    }
    #xrRoot .xr-hud.xr-immersive .xr-send-input { width: 1px; height: 1px; padding: 0; }
    `;
  ctx.root.appendChild(hudStyle);

  const hud = document.createElement("div");
  hud.className = "xr-hud";
  hud.innerHTML = `
    <div class="xr-top">
      <button type="button" class="xr-btn" data-act="exit">‹ ${t("xrExit")}</button>
      <button type="button" class="xr-btn" data-act="immersive"></button>
      <button type="button" class="xr-btn" data-act="switchImmersive"></button>
      <button type="button" class="xr-btn" data-act="follow"></button>
      <button type="button" class="xr-btn" data-act="native"></button>
      <button type="button" class="xr-btn" data-act="files"></button>
      <span class="xr-title"></span>
    </div>
    <div class="xr-hint">${t("xrHintDesktop")}</div>
    <div class="xr-status"></div>
    <div class="xr-send">
      <input class="xr-send-input" type="text" maxlength="4000" />
      <button type="button" class="xr-btn" data-act="mic"></button>
      <button type="button" class="xr-btn" data-act="send"></button>
    </div>`;
  ctx.root.appendChild(hud);
  const exitBtn = hud.querySelector('[data-act="exit"]');
  const immersiveBtn = hud.querySelector('[data-act="immersive"]');
  const switchImmersiveBtn = hud.querySelector('[data-act="switchImmersive"]');
  const followBtn = hud.querySelector('[data-act="follow"]');
  const nativeBtn = hud.querySelector('[data-act="native"]');
  const titleEl = hud.querySelector(".xr-title");
  const statusEl = hud.querySelector(".xr-status");
  exitBtn.addEventListener("click", () => doExit());

  /* ---------- 房间共同文件（需求 8/10）：列表面板 + 分类型预览 + 世界摆放 + 文本编辑。
     自包含系统模块，初始化失败降级为 no-op 存根（3D 故障不影响 2D 的架构不变量）。 */
  let files;
  try {
    files = createXRFiles({
      t, tf,
      scene, camera,
      api: ctx.api,
      token: ctx.token,
      roomName: ctx.roomName,
      canEdit: ctx.filesCanEdit,
      rasterizeDom: panels.rasterizeDom,
      renderFileMarkdown: ctx.renderFileMarkdown,
      placeChatModel, removeChatModel,
      disposeObjectTree,
      statusEl, hud,
      pxPerM: PANEL_W / REF_CSS,
    });
    scene.add(files.group);
  } catch (err) {
    console.warn("[xr] files system init failed", err);
    const noop = () => {};
    files = {
      group: new THREE.Group(), openPanel: noop, closePanel: noop, isOpen: () => false,
      handlePick: () => false, beginDrag: () => null, dragMove: noop, dragEnd: noop,
      xrJoystick: () => false, scrollPanelContentBy: () => false, hoverPanel: () => false,
      adjusting: () => false, exitAdjust: noop, escStack: () => false, tick: noop, dispose: noop,
    };
  }
  const filesBtn = hud.querySelector('[data-act="files"]');
  filesBtn.textContent = t("xrFiles");
  filesBtn.addEventListener("click", () => {
    try { files.isOpen() ? files.closePanel() : files.openPanel(); } catch (err) {}
  });
  function availableImmersiveModes() {
    const modes = ctx.immersiveModes ? ctx.immersiveModes() : null;
    if (modes) return { ar: !!modes.ar, vr: !!modes.vr };
    const mode = ctx.immersiveMode ? ctx.immersiveMode() : null;
    return { ar: mode === "ar", vr: mode === "vr" };
  }
  function canSwitchImmersive() {
    const modes = availableImmersiveModes();
    return modes.ar && modes.vr;
  }
  function currentImmersiveMode() {
    return activeImmersiveMode || retryImmersiveMode
      || (ctx.immersiveMode ? ctx.immersiveMode() : "ar");
  }
  function switchTargetMode() {
    return currentImmersiveMode() === "ar" ? "vr" : "ar";
  }
  /* 沉浸式入口：优先 AR 透视；AR/VR 都支持时，另提供一个切换按钮。 */
  function refreshImmersiveBtn() {
    if (!ctx.immersible || !ctx.immersible()) {
      immersiveBtn.style.display = "none";
      switchImmersiveBtn.style.display = "none";
      return;
    }
    const mode = currentImmersiveMode();
    immersiveBtn.style.display = "";
    immersiveBtn.disabled = xrStarting;
    immersiveBtn.textContent = xrInImmersive
      ? t(mode === "vr" ? "xrExitVR" : "xrExitAR")
      : t(mode === "vr" ? "xrEnterVR" : "xrEnterAR");
    const switchable = xrInImmersive && canSwitchImmersive();
    switchImmersiveBtn.style.display = switchable ? "" : "none";
    switchImmersiveBtn.disabled = xrStarting;
    switchImmersiveBtn.textContent = t(switchTargetMode() === "vr" ? "xrSwitchToVR" : "xrSwitchToAR");
  }
  immersiveBtn.addEventListener("click", () => {
    if (xrInImmersive) exitImmersive();
    else enterImmersive();
  });
  switchImmersiveBtn.addEventListener("click", () => switchImmersiveMode());

  /* ---------- 3D 内发送（需求 6.3 最小入口）：纯文本走 2D 的 POST（ctx.sendText），
     发出后经 msgEvents.add 自动回流 3D 墙。私聊/引用/附件等复杂编辑引导回 2D。
     输入框内按键 stopPropagation，避免触发 WASD 移动 / Esc 退出的全局快捷键。 */
  const sendInput = hud.querySelector(".xr-send-input");
  const sendBtn = hud.querySelector('[data-act="send"]');
  sendInput.placeholder = t("xrSendPlaceholder");
  sendBtn.textContent = t("xrSend");
  async function xrSend() {
    const text = sendInput.value.trim();
    if (!text) return;
    if (!ctx.sendText) { statusEl.textContent = t("xrSendFail"); return; }
    sendInput.value = "";
    if (worldUi) worldUi.refresh();
    try {
      await ctx.sendText(text);
      statusEl.textContent = "";
    } catch (err) {
      sendInput.value = text; /* 发送失败还原输入 */
      if (worldUi) worldUi.refresh();
      statusEl.textContent = err && err.message ? err.message : t("xrSendFail");
    }
  }
  sendBtn.addEventListener("click", xrSend);
  sendInput.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); xrSend(); }
    else if (e.key === "Escape") sendInput.blur();
  });
  sendInput.addEventListener("keyup", (e) => e.stopPropagation());
  sendInput.addEventListener("input", () => { if (worldUi) worldUi.refresh(); });

  /* ---------- 语音识别输入（桌面与沉浸式 dom-overlay 通用）：SpeechRecognition 实时转写
     进输入框，再点一次停止，文本留在框内由用户确认发送。错误在状态栏提示。
     getUserMedia 仅做权限门，成功即停轨（识别器自行采集音频，无需 MediaRecorder）。 */
  const micBtn = hud.querySelector('[data-act="mic"]');
  const hasASR = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  let asrSR = null;
  let asrFinal = "";
  let asrBase = "";
  let asrOn = false;
  let asrRestarts = 0;
  function micLabel() {
    micBtn.textContent = asrOn ? t("xrMicStop") : t("xrMic");
    if (worldUi) worldUi.refresh();
  }
  micLabel();
  function stopAsr() {
    asrOn = false;
    micLabel();
    const sr = asrSR;
    asrSR = null;
    if (sr) { try { sr.onend = null; sr.onresult = null; sr.onerror = null; sr.abort(); } catch (e) {} }
  }
  async function startAsr() {
    if (!hasASR) { statusEl.textContent = t("xrAsrUnsupported"); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((tk) => tk.stop());
    } catch (e) {
      statusEl.textContent = t("asrNotAllowed");
      return;
    }
    const SRClass = window.SpeechRecognition || window.webkitSpeechRecognition;
    const sr = new SRClass();
    asrSR = sr;
    asrFinal = "";
    asrBase = sendInput.value;
    asrRestarts = 0;
    sr.lang = (navigator.language || "zh-CN").replace("_", "-");
    sr.interimResults = true;
    sr.continuous = true;
    sr.onresult = (e) => {
      let final = "", interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) final += r[0].transcript; else interim += r[0].transcript;
      }
      if (final) asrFinal += final;
      if (!asrOn) return;
      sendInput.value = [asrBase, (asrFinal + interim).trim()].filter(Boolean).join(" ");
    };
    sr.onerror = (e) => {
      if (e.error === "aborted" || e.error === "no-speech") return;
      const map = {
        "audio-capture": t("asrAudio"),
        "not-allowed": t("asrNotAllowed"),
        "service-not-allowed": t("asrService"),
        network: t("asrNetwork"),
      };
      statusEl.textContent = map[e.error] || t("asrError") + e.error;
    };
    sr.onend = () => {
      if (!asrOn || asrSR !== sr) return;
      if (asrRestarts < 5) { asrRestarts += 1; try { sr.start(); } catch (e) { stopAsr(); } }
      else stopAsr();
    };
    try {
      sr.start();
      asrOn = true;
      statusEl.textContent = "";
      micLabel();
    } catch (e) {
      asrSR = null;
      statusEl.textContent = t("asrStartFail") + (e && e.message ? e.message : "");
    }
  }
  micBtn.addEventListener("click", () => { if (asrOn) stopAsr(); else startAsr(); });
  function refreshNativeBtn() {
    nativeBtn.textContent = native.isEnabled() ? t("xrNativeOn") : t("xrNativeOff");
  }
  nativeBtn.addEventListener("click", () => {
    native.setEnabled(!native.isEnabled());
    refreshNativeBtn();
    if (worldUi) worldUi.refresh();
  });
  refreshNativeBtn();

  /* 沉浸式世界控制台：DOM overlay 不可用时仍可用手柄射线操作；有 overlay 时
     视觉上也只保留这一套，避免浏览器 UI 与世界 UI 重叠。输入框点击后复用
     DOM input 唤起系统键盘，输入结果实时画回世界面板。 */
  worldUi = createXRWorldUI({
    scene, camera, t,
    roomName: () => ctx.roomName && ctx.roomName(),
    messageCount: () => strip.length,
    hint: () => t("xrVRHint"),
    immersiveMode: () => currentImmersiveMode(),
    canSwitchImmersive: () => xrInImmersive && canSwitchImmersive(),
    isFollowing: () => follow,
    isNative: () => native.isEnabled(),
    inputValue: () => sendInput.value,
    isAsrOn: () => asrOn,
    onExit: () => doExit(),
    onFollow: () => toggleFollow(),
    onNative: () => { native.setEnabled(!native.isEnabled()); refreshNativeBtn(); worldUi.refresh(); },
    onSwitchImmersive: () => switchImmersiveMode(),
    onFiles: () => { try { files.isOpen() ? files.closePanel() : files.openPanel(); } catch (err) {} },
    onMic: () => { if (asrOn) stopAsr(); else startAsr(); worldUi.refresh(); },
    onSend: () => xrSend(),
    focusInput: () => { try { sendInput.focus({ preventScroll: true }); } catch (e) { sendInput.focus(); } },
    logLeft: LOG_L, logWidth: LOG_W, logZ: -R, bandTop: BAND_HI, floorY: FLOOR_Y,
  });

  sendInput.addEventListener("focus", () => { if (worldUi) worldUi.setFocusedInput(true); });
  sendInput.addEventListener("blur", () => { if (worldUi) worldUi.setFocusedInput(false); });

  /* ---------- 消息条带（最旧 → 最新） ---------- */

  let strip = [];
  const byId = new Map();
  let follow = true;
  let hArc = 0;
  let backfilling = false;
  let historyEnd = false;

  function collectFromLog() {
    const out = [];
    ctx.logElement.querySelectorAll(".bubble[data-msg-id]").forEach((el) => {
      const id = el.dataset.msgId;
      const msg = ctx.msgById(id);
      if (!msg) return;
      out.push({ id, msg, el, staged: false, widthCss: el.offsetWidth, heightCss: el.offsetHeight });
    });
    return out;
  }

  function rebuildFromLog() {
    ctx.clearStaged();
    strip = collectFromLog();
    byId.clear();
    for (const e of strip) byId.set(e.id, e.msg);
    panels.removeAll();
    historyEnd = false;
    backfillPages = 0;
    refreshTitle();
  }

  let backfillPages = 0;
  async function backfill() {
    if (backfilling || historyEnd || disposed) return;
    const oldest = strip[0];
    if (!oldest || !oldest.msg || !oldest.msg.id) { historyEnd = true; return; }
    backfilling = true;
    statusEl.textContent = t("xrBackfilling");
    try {
      while (!historyEnd && !disposed && backfillPages < 12) {
        const anchor = strip[0].msg.id;
        const data = await ctx.api(
          `/api/rooms/${encodeURIComponent(ctx.roomName())}/messages?limit=200&beforeId=${encodeURIComponent(anchor)}`
        );
        if (disposed) break;
        const msgs = (data && data.messages) || [];
        const fresh = [];
        for (const m of msgs) {
          const mid = String(m.id);
          if (byId.has(mid)) continue;
          byId.set(mid, m);
          const el = ctx.buildStaged(m);
          fresh.push({ id: mid, msg: m, el, staged: true, widthCss: el.offsetWidth, heightCss: el.offsetHeight });
        }
        if (fresh.length) strip = fresh.concat(strip);
        backfillPages++;
        refreshTitle();
        /* 服务器返回不足一页 → 已到最早 */
        if (msgs.length < 200 || strip[0].msg.id === anchor) {
          historyEnd = true;
          statusEl.textContent = t("xrHistoryEnd");
          break;
        }
        await sleep(30);
      }
    } catch (err) {
      if (!disposed) statusEl.textContent = t("xrBackfillFail");
    } finally {
      backfilling = false;
    }
  }

  function refreshTitle() {
    titleEl.textContent = `${ctx.roomName() || ""} · ${tf("xrMsgCount", { n: strip.length })}`;
    statusEl.textContent = strip.length ? statusEl.textContent : t("xrRoomEmpty");
    if (worldUi) worldUi.refresh();
  }

  /* ---------- 布局（2D 直列式）：面板按各自高度自下而上堆叠成单列，最新在列底
     （底边 FLOOR_Y），历史越旧越高。a_i = 面板 i 与最新端之间的堆叠高度（米）；
     hArc 为滚动量——整列向下滑过固定可视带 [FLOOR_Y, BAND_HI]，与 2D 滚动同构：
     往回翻 = 记录向下移出带底，更早的内容从带顶进入。面板世界高优先取栅格化后的
     实际值（与网格完全一致），未栅格化时用 2D 量测估算兜底 ---------- */

  function slotH(e) {
    return panels.heightOf(e.id)
      || THREE.MathUtils.clamp((e.heightCss || 200) * PX_PER_M, 0.1, 2.3);
  }

  /* 消息 id → 稳定横向偏移（米）：单列布局下所有面板同方位，图表/图片/3D 模型
     等原生对象若仍取面板方位会全部叠在同一点，按 id 散开在前方地面 */
  function hashSpread(id) {
    let h = 0;
    const s = String(id);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return ((h % 9) - 4) * 0.82;
  }

  let totalH = 0; /* 整列堆叠总高（最新端→最旧端） */

  function maxScroll() {
    return Math.max(0, totalH - (BAND_HI - FLOOR_Y));
  }

  function scrollBy(d) {
    if (follow) { follow = false; refreshFollowBtn(); }
    hArc = THREE.MathUtils.clamp(hArc + d, 0, maxScroll());
    updateScrollBar();
  }

  function layout(dt) {
    /* 撤回等导致 totalH 缩水时收敛滚动量（用上一帧 totalH 即可，下一帧自然校正） */
    if (hArc > maxScroll()) hArc = maxScroll();
    const n = strip.length;
    const entries = [];
    let acc = 0;
    for (let i = n - 1; i >= 0; i--) {
      const e = strip[i];
      const h = slotH(e);
      e._a = acc;
      e._h = h;
      acc += h + GAP;
      const yBot = FLOOR_Y + e._a - hArc;
      /* 窗口 = 槽位与可视带（带底 0.2 贴地）相交的面板 */
      if (yBot < BAND_HI && yBot + h > 0.2) {
        e._phi = ANCHOR;
        e._spread = hashSpread(e.id);
        entries.push(e);
      }
    }
    totalH = acc;
    window.__xrDebug && (window.__xrDebug.lastLayout = { n, entries: entries.length, hArc, follow, totalH });
    /* 聚焦接管：面板聚焦 → 原生物让位；图片聚焦 → 仅目标平面移到视点前放大 */
    native.setFocused(focus ? focus.id : null);
    native.setOverride(focus && focus.kind === "image" ? focusPose(1.15) : null);
    panels.sync({
      entries,
      keepIds: strip.map((e) => e.id),
      place: (e, hWorld, wWorld) => {
        if (focus && focus.kind === "panel" && e.id === focus.id) return focusPose(Math.max(wWorld, hWorld));
        return {
          /* 面板宽度随 2D 气泡宽度变化 → 左边缘对齐记录区左缘（右缘参差即 2D 观感本身） */
          x: LOG_L + wWorld / 2,
          y: FLOOR_Y + e._a - hArc + hWorld / 2,
          z: -R,
          rotY: 0,
        };
      },
      dt,
    });
    native.sync(entries, (id) => panels.positionOf(id), dt);
    /* 记录区宽度贴内容：取窗口内最宽面板（未栅格化时退回 2D 量测估算） */
    let mw = 0.9;
    for (const e of entries) {
      const w = panels.widthOf(e.id) || ((e.widthCss || 480) * PX_PER_M);
      if (w > mw) mw = w;
    }
    logContentW += (mw - logContentW) * (1 - Math.exp(-(dt || 0.016) * 4));
    updateScrollBar();
  }

  /* ---------- 控制（桌面第一人称：拖拽环视 / 滚轮走近 / WASD / ←→ 翻历史） ---------- */

  const look = { yaw: 0, pitch: 0 };
  const keys = new Set();
  let dragInfo = null;

  renderer.domElement.addEventListener("pointerdown", (e) => {
    const ray = pointerRay(e);
    const drag = dragBegin(ray, false); /* 落在滚行条/记录上 → 拖动滚动；否则环视 */
    if (drag) { drag.o = ray.ray.origin.clone(); drag.d = ray.ray.direction.clone(); }
    dragInfo = {
      x: e.clientX, y: e.clientY, t: performance.now(), moved: 0, id: e.pointerId,
      mode: drag ? drag.mode : "look", drag,
    };
    try { renderer.domElement.setPointerCapture(e.pointerId); } catch (err) {}
  });
  renderer.domElement.addEventListener("pointermove", (e) => {
    if (!dragInfo || dragInfo.id !== e.pointerId) return;
    const dx = e.clientX - dragInfo.x, dy = e.clientY - dragInfo.y;
    dragInfo.x = e.clientX; dragInfo.y = e.clientY;
    dragInfo.moved += Math.abs(dx) + Math.abs(dy);
    if (dragInfo.mode === "look") {
      look.yaw -= dx * 0.0042;
      look.pitch = THREE.MathUtils.clamp(look.pitch - dy * 0.003, -1.2, 1.2);
      return;
    }
    dragMove(dragInfo.drag, pointerRay(e)); /* 记录/滚行条：跟手滚动 */
  });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (dragInfo && dragInfo.id === e.pointerId) {
      /* 原地按下-抬起（未达 6px / 400ms 门控）仍是点击；按在滚行条上则按下已跳转 */
      if (dragInfo.moved < 6 && performance.now() - dragInfo.t < 400 && dragInfo.mode !== "bar") handlePanelClick(e);
      dragEnd(dragInfo.drag);
      dragInfo = null;
    }
  });
  renderer.domElement.addEventListener("pointercancel", () => { dragEnd(dragInfo && dragInfo.drag); dragInfo = null; });
  /* 悬停（桌面）：鼠标指着记录/滚行条 → 滚行条拇指高亮，也是「可拖动」的提示 */
  renderer.domElement.addEventListener("pointermove", (e) => {
    if (dragInfo) return;
    hoverLog = rayPointingAtLog(pointerRay(e));
    refreshSbActive();
  });
  renderer.domElement.addEventListener("contextmenu", (e) => e.preventDefault());

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();

  /* 鼠标位置的拾取射线（按下判定、拖动跟手都用它） */
  function pointerRay(e) {
    const rect = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    return raycaster;
  }

  /* ---------- 3D 模型附件放置（需求 3.9）：点击 3D 文件卡片所在面板 → 放置/收起模型。
     取回/解析与形象加载同一思路（服务器附件带 token 取 blob）；场景内上限 LRU，
     退出时随 dispose 全量释放。VRM 也由 GLTFLoader 解析（VRM 即 GLB），专属处理 Phase 3。 */

  const MODEL_CAP = 6;
  const chatModels = new Map();  // msgId → THREE.Group（已归一化）
  const modelOrder = [];
  const placingModels = new Map(); // id → 本次加载票据；隐藏/关闭后旧回调作废
  const gltfLoader = new GLTFLoader();

  function disposeObjectTree(obj) {
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

  function removeChatModel(id) {
    const pending = placingModels.delete(id);
    if (pending && !placingModels.size && !disposed) statusEl.textContent = "";
    const obj = chatModels.get(id);
    if (!obj) return pending;
    scene.remove(obj);
    disposeObjectTree(obj);
    chatModels.delete(id);
    const i = modelOrder.indexOf(id);
    if (i >= 0) modelOrder.splice(i, 1);
    return true;
  }

  async function placeChatModel(id, msg) {
    if (disposed) return false;
    if (chatModels.has(id) || placingModels.has(id)) return true;
    const ticket = {};
    placingModels.set(id, ticket);
    statusEl.textContent = t("xrModelLoading");
    let objUrl = null;
    try {
      const url = msg.downloadUrl;
      let src = url;
      if (url && url.startsWith("/api/")) {
        /* 服务器附件端点要求鉴权，GLTFLoader 不带 Authorization：先取 blob 再加载 */
        const resp = await fetch(url, { headers: ctx.token ? { Authorization: "Bearer " + ctx.token() } : {} });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        objUrl = URL.createObjectURL(await resp.blob());
        src = objUrl;
      }
      if (disposed || placingModels.get(id) !== ticket) return false;
      const gltf = await gltfLoader.loadAsync(src);
      const model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
      if (disposed || placingModels.get(id) !== ticket) { if (model) disposeObjectTree(model); return false; }
      if (!model) throw new Error("empty model");
      /* 外框包围盒归一化到 ~1m，底边落地，放在面板正前方地面 */
      const box = new THREE.Box3().setFromObject(model);
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      const maxDim = Math.max(size.x, size.y, size.z) || 1;
      const holder = new THREE.Group();
      model.position.copy(center).negate();
      holder.add(model);
      holder.scale.setScalar(1.0 / maxDim);
      const entry = strip.find((e) => e.id === id);
      const phi = entry && entry._phi != null ? entry._phi : ANCHOR;
      const rr = R - 1.7;
      const spread = (entry && entry._spread) || 0;
      holder.position.set(rr * Math.sin(phi) + spread * 0.7, 0, rr * Math.cos(phi));
      holder.rotation.y = phi + Math.PI;
      scene.add(holder);
      while (chatModels.size >= MODEL_CAP) removeChatModel(modelOrder[0]);
      chatModels.set(id, holder);
      modelOrder.push(id);
      statusEl.textContent = "";
      return true;
    } catch (err) {
      if (!disposed && placingModels.get(id) === ticket) statusEl.textContent = t("xrModelLoadFail");
      return false;
    } finally {
      if (objUrl) URL.revokeObjectURL(objUrl);
      if (placingModels.get(id) === ticket) placingModels.delete(id);
    }
  }

  function toggleChatModel(id, msg) {
    if (chatModels.has(id) || placingModels.has(id)) {
      removeChatModel(id);
      statusEl.textContent = "";
      return;
    }
    placeChatModel(id, msg);
  }

  function handlePick(pickRay) {
    /* 沉浸式顶部/底部控制台优先于消息墙和文件面板。 */
    try { if (worldUi && worldUi.handlePick(pickRay)) return; } catch (err) {}
    /* 共同文件 UI/世界模型优先（未开面板且无摆放时是廉价的 no-op） */
    try { if (files.handlePick(pickRay)) return; } catch (err) {}
    /* 饼图扇区点击 → 名称/数值/百分比浮签（需求 3.2） */
    const sector = native.pickSector(pickRay);
    if (sector) { native.showSectorTip(sector); return; }
    /* 图片平面点击 → 聚焦放大 / 返回（需求 3.6 指向放大） */
    const imgId = native.pickImage(pickRay);
    if (imgId != null) {
      if (focus && focus.kind === "image" && focus.id === imgId) exitFocus();
      else enterFocus(imgId, "image");
      return;
    }
    const panelId = panels.raycast(pickRay);
    if (!panelId) return;
    /* 3D 模型附件面板：点击放置/收起（此类面板短、无翻段交互，不与 cycleSegment 冲突） */
    const m = ctx.msgById(panelId);
    if (m && m.msgType === "attachment" && isModelFilename(m.attachmentName)) {
      toggleChatModel(String(panelId), m);
      return;
    }
    if (focus && focus.id === panelId) { exitFocus(); return; } /* 聚焦中再按一次 → 返回（需求 7.2） */
    /* 语音面板：点击复用 2D 播放链（需求 5.1，零 2D 逻辑改动），音频经 spatialHook 空间化 */
    if (m && m.msgType === "voice") {
      try { ctx.playVoice(m); } catch (err) {}
      return;
    }
    const now = performance.now();
    if (lastClick.id === panelId && now - lastClick.t < 350) {
      lastClick = { id: null, t: 0 };
      enterFocus(panelId, "panel"); /* 双击 → 聚焦阅读（桌面输入映射，需求 7.2） */
      return;
    }
    lastClick = { id: panelId, t: now };
    panels.cycleSegment(panelId); /* 多段长文点击续读；聚焦模式见 handlePanelClick 双击分支 */
  }

  function handlePanelClick(e) {
    const rect = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    handlePick(raycaster);
  }

  renderer.domElement.addEventListener("wheel", (e) => {
    e.preventDefault();
    try {
      if (files.hoverPanel(pointerRay(e))) { files.scrollPanelContentBy(e.deltaY * 0.6); return; }
    } catch (err) {}
    const dir = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
    camera.position.addScaledVector(dir, -e.deltaY * 0.0022);
    clampCamera();
  }, { passive: false });

  function clampCamera() {
    const p = camera.position;
    const hr = Math.hypot(p.x, p.z);
    if (hr > MAX_RADIUS) { p.x *= MAX_RADIUS / hr; p.z *= MAX_RADIUS / hr; }
    p.y = THREE.MathUtils.clamp(p.y, 0.85, 3.4);
  }

  function onKeyDown(e) {
    if (disposed) return;
    if (e.key === "Escape") {
      try { if (files.escStack()) return; } catch (err) {} /* 文件编辑→调整→面板，分级退出 */
      if (focus) { exitFocus(); return; } /* 先退聚焦，再退 3D（需求 7.2） */
      doExit();
      return;
    }
    const k = e.key.toLowerCase();
    if (k === "f") { toggleFollow(); return; }
    if ("wasd".includes(k) || k.startsWith("arrow")) {
      keys.add(k);
      if (k.startsWith("arrow")) e.preventDefault();
    }
  }
  function onKeyUp(e) { keys.delete(e.key.toLowerCase()); }
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);

  function updateControls(dt) {
    let fwd = 0, side = 0;
    if (keys.has("w") || keys.has("arrowup")) fwd += 1;
    if (keys.has("s") || keys.has("arrowdown")) fwd -= 1;
    if (keys.has("d")) side += 1;
    if (keys.has("a")) side -= 1;
    if (fwd || side) {
      const speed = 2.6 * dt;
      const fx = -Math.sin(look.yaw), fz = -Math.cos(look.yaw);
      const rx = Math.cos(look.yaw), rz = -Math.sin(look.yaw);
      camera.position.x += (fx * fwd + rx * side) * speed;
      camera.position.z += (fz * fwd + rz * side) * speed;
      clampCamera();
    }
    if (keys.has("arrowleft")) scrollBy(dt * 2.2);    /* ← 翻向更早（列上滑过带顶）；→ 回向最新 */
    if (keys.has("arrowright")) scrollBy(-dt * 2.2);
    camera.rotation.set(look.pitch, look.yaw, 0);
  }

  /* ---------- 跟随与右侧滚行条（2D 滚动条同构：拇指在底部 = 最新/跟随，
     向上拖 = 看更早历史；拇指高度按可视带占整列比例，整列装得下时隐藏） ---------- */

  function refreshFollowBtn() {
    followBtn.textContent = follow ? t("xrFollowOn") : t("xrFollowOff");
  }
  function toggleFollow() {
    follow = !follow;
    refreshFollowBtn();
    if (worldUi) worldUi.refresh();
  }
  followBtn.addEventListener("click", toggleFollow);

  /* ---------- 世界内 3D 滚行条（替代原 DOM 那条：dom-overlay 在 Quest 上不渲染，
     沉浸式里 DOM 滚行条等于不存在）。轨道钉在记录列右侧，拇指的高度/位置与旧 DOM
     逻辑同构：frac = 带高/总高，t = hArc/maxScroll（0 = 最新、拇指沉底）。
     命中判定走「射线 ∩ z 平面」，不逐帧求交网格——薄片也能稳稳抓住。
     外观：圆角凹槽轨道 + 胶囊拇指（带握纹），指着记录/滚行条或拖动时拇指变亮。 */

  /* 滚行条几何：macOS 式浮层——细胶囊拇指嵌在 scroll panel 内缘（不占面板之外空间），
     轨道极淡；指着/拖动时拇指变亮变实。x 由记录区右缘每帧推出（见 updateScrollBar）。 */
  const SB_INSET = 0.34;        /* 面板内为滚行条预留的右侧空间（内容与条之间留出明显间隙） */
  const SB_Z = -R + 0.3;        /* 浮在面板前：便于拾取，也不与面板同面闪烁 */
  const SB_TRACK_W = 0.11, SB_THUMB_W = 0.085;
  const SB_BOT = FLOOR_Y + 0.12, SB_TOP = BAND_HI - 0.12;
  const SB_TRACK_H = SB_TOP - SB_BOT;
  const SB_THUMB_MIN = 0.12;
  const LOG_DRAG_MIN = 0.04;    /* 记录拖动阈值（米）：小于它仍算点击 */

  function sbRoundRect(g, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    g.beginPath();
    g.moveTo(x + rr, y);
    g.lineTo(x + w - rr, y);
    g.quadraticCurveTo(x + w, y, x + w, y + rr);
    g.lineTo(x + w, y + h - rr);
    g.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
    g.lineTo(x + rr, y + h);
    g.quadraticCurveTo(x, y + h, x, y + h - rr);
    g.lineTo(x, y + rr);
    g.quadraticCurveTo(x, y, x + rr, y);
    g.closePath();
  }
  function sbTexture(w, h, draw) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    draw(c.getContext("2d"), w, h);
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    return tex;
  }
  /* 轨道：极淡的胶囊槽（macOS 风格几乎看不到槽，只留一点底 + 发丝描边） */
  function sbTrackTexture() {
    const W = 96;
    const H = Math.min(2048, Math.round(W * (SB_TRACK_H / SB_TRACK_W)));
    return sbTexture(W, H, (g) => {
      sbRoundRect(g, 1, 1, W - 2, H - 2, W / 2 - 1);
      g.fillStyle = "rgba(8,12,20,0.34)";
      g.fill();
      g.lineWidth = 1.5;
      g.strokeStyle = "rgba(190,210,240,0.14)";
      g.stroke();
    });
  }
  /* 拇指：浅色半透明胶囊（macOS 式，无握纹）；active = 被指着/正被拖动 → 更亮更实 */
  function sbThumbTexture(hWorld, active) {
    const W = 96;
    const H = Math.min(2048, Math.max(36, Math.round(W * (hWorld / SB_THUMB_W))));
    return sbTexture(W, H, (g) => {
      sbRoundRect(g, 1, 1, W - 2, H - 2, W / 2 - 1);
      const grd = g.createLinearGradient(0, 0, W, 0);
      if (active) { grd.addColorStop(0, "rgba(255,255,255,0.95)"); grd.addColorStop(1, "rgba(214,226,244,0.85)"); }
      else { grd.addColorStop(0, "rgba(236,242,252,0.60)"); grd.addColorStop(1, "rgba(200,214,235,0.48)"); }
      g.fillStyle = grd;
      g.fill();
      g.lineWidth = 1.5;
      g.strokeStyle = active ? "rgba(255,255,255,0.9)" : "rgba(255,255,255,0.32)";
      g.stroke();
    });
  }

  /* ---------- scroll panel 的可见边界 ----------
     记录区背后一块圆角面板（半透明底 + 描边 + 顶部淡高光），让「聊天记录装在一个
     scroll panel 里」看得出来。**宽度贴着内容**：面板最宽 + 滚行条内缘空间 + 内边距，
     所以右侧不留大片空白；滚行条嵌在这块面板的内缘（macOS 式浮层）。
     放在面板之后一层（z 更负），透明材质靠深度测试被面板正确遮挡。 */
  const LOG_FRAME_H = (BAND_HI - FLOOR_Y) + LOG_PAD * 2;
  let logContentW = 0.9;   /* 当前窗口内最宽面板（layout 每帧写） */
  let logW = 1.2;          /* 记录区当前宽度（平滑跟随内容宽度） */
  let frameDrawnW = 0;
  function logFrameTexture(worldW) {
    const W = 512;
    const H = Math.min(2048, Math.round(W * (LOG_FRAME_H / worldW)));
    return sbTexture(W, H, (g) => {
      const r = Math.round(W * 0.05);
      sbRoundRect(g, 4, 4, W - 8, H - 8, r);
      g.fillStyle = "rgba(10,15,25,0.80)";
      g.fill();
      g.lineWidth = 4;
      g.strokeStyle = "rgba(140,180,240,0.60)";
      g.stroke();
      const grd = g.createLinearGradient(0, 0, 0, H);
      grd.addColorStop(0, "rgba(160,190,235,0.16)");
      grd.addColorStop(0.25, "rgba(160,190,235,0.04)");
      grd.addColorStop(1, "rgba(0,0,0,0.24)");
      sbRoundRect(g, 4, 4, W - 8, H - 8, r);
      g.fillStyle = grd;
      g.fill();
    });
  }
  const logFrameMat = new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false });
  const logFrame = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), logFrameMat);
  logFrame.position.set(LOG_L, (FLOOR_Y + BAND_HI) / 2, -R - 0.012); /* x 每帧按宽度校正 */
  scene.add(logFrame);
  function drawLogFrame() {
    if (Math.abs(logW - frameDrawnW) < 0.05) return; /* 宽度变化不到 5cm 不重画纹理 */
    frameDrawnW = logW;
    if (logFrameMat.map) logFrameMat.map.dispose();
    logFrameMat.map = logFrameTexture(logW);
    logFrameMat.needsUpdate = true;
  }

  const scrollBar = new THREE.Group();
  scrollBar.position.z = SB_Z;
  const sbTrackMat = new THREE.MeshBasicMaterial({ map: sbTrackTexture(), transparent: true, depthWrite: false });
  const sbTrack = new THREE.Mesh(new THREE.PlaneGeometry(SB_TRACK_W, SB_TRACK_H), sbTrackMat);
  const sbThumbMat = new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false });
  const sbThumb = new THREE.Mesh(new THREE.PlaneGeometry(SB_THUMB_W, 1), sbThumbMat);
  sbThumb.position.z = 0.006; /* 略前一层，避免与轨道同面 z-fighting */
  scrollBar.add(sbTrack, sbThumb);
  scrollBar.visible = false; /* 首帧 updateScrollBar 之前不显示，避免闪一条空轨 */
  scene.add(scrollBar);
  let sbX = 0;               /* 滚行条 x（每帧由记录区右缘推出） */

  let sbThumbH = SB_THUMB_MIN;
  let sbActive = false;      /* 被指着或正被拖动 → 拇指变亮 */
  let sbDrawnH = 0, sbDrawnActive = null;
  function sbDrawThumb() {
    /* 拇指高度只在总高变化时变；变化不到 1.5cm 不重画纹理（省开销） */
    if (Math.abs(sbThumbH - sbDrawnH) < 0.015 && sbActive === sbDrawnActive) return;
    sbDrawnH = sbThumbH;
    sbDrawnActive = sbActive;
    if (sbThumbMat.map) sbThumbMat.map.dispose();
    sbThumbMat.map = sbThumbTexture(sbThumbH, sbActive);
    sbThumbMat.needsUpdate = true;
  }
  function sbSetActive(on) {
    if (on === sbActive) return;
    sbActive = on;
    sbDrawThumb();
  }

  const _sbPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0); /* 法线 +Z；constant 按需改为 -z */
  const _sbHit = new THREE.Vector3();
  /* 射线（Raycaster）与 z = planeZ 平面的交点（无交点返回 null） */
  function rayPlanePoint(caster, planeZ, out) {
    _sbPlane.constant = -planeZ;
    return caster.ray.intersectPlane(_sbPlane, out);
  }

  function updateScrollBar() {
    /* scroll panel：宽度贴着内容（面板最宽 + 滚行条内缘 + 内边距） */
    const targetW = Math.max(0.9, logContentW) + SB_INSET + LOG_PAD * 2;
    /* 内容变宽**立即**跟上（避免滚动中更宽的消息进来时瞬间露在框外）；变窄才平滑收起 */
    if (targetW > logW) logW = targetW;
    else logW += (targetW - logW) * 0.25;
    const frameLeft = LOG_L - LOG_PAD;
    logFrame.scale.set(logW, LOG_FRAME_H, 1);
    logFrame.position.x = frameLeft + logW / 2;
    /* 视口裁剪面跟随框宽（y 面固定为可视带上下缘，面板不会伸出面板外） */
    clipL.constant = -frameLeft;
    clipR.constant = frameLeft + logW;
    drawLogFrame();
    /* 脚边控制条随面板收窄：不伸出面板之外 */
    const barK = THREE.MathUtils.clamp((logW - 0.2) / 1.7, 0.55, 1);
    vrBar.scale.setScalar(barK);
    vrBar.position.x = logFrame.position.x;
    /* 滚行条嵌在面板内缘（macOS 式浮层），随记录区右缘移动 */
    sbX = frameLeft + logW - LOG_PAD - SB_THUMB_W / 2 - 0.02;
    sbTrack.position.set(sbX, (SB_BOT + SB_TOP) / 2, 0);
    const ma = maxScroll();
    if (ma <= 0) { scrollBar.visible = false; return; }
    scrollBar.visible = true;
    const frac = THREE.MathUtils.clamp((BAND_HI - FLOOR_Y) / totalH, 0.08, 1);
    sbThumbH = Math.max(SB_THUMB_MIN, SB_TRACK_H * frac);
    sbThumb.scale.y = sbThumbH;
    /* 世界 y 向上、DOM 的 top 向下：t = hArc/ma（0 = 最新、拇指沉底；1 = 最旧、拇指到顶） */
    sbThumb.position.set(sbX, SB_BOT + (SB_TRACK_H - sbThumbH) * (hArc / ma) + sbThumbH / 2, 0);
    sbDrawThumb();
  }
  /* 世界 y → 轨道自底向上的比例 t（算上拇指半高，与旧 DOM 的 top 映射同构） */
  function sbYToT(y) {
    return THREE.MathUtils.clamp((y - SB_BOT - sbThumbH / 2) / Math.max(0.001, SB_TRACK_H - sbThumbH), 0, 1);
  }
  function sbApplyY(y) {
    const ma = maxScroll();
    if (ma <= 0) return;
    const t = sbYToT(y);
    hArc = t * ma;
    if (t <= 0.005) { follow = true; } /* 拖回最底（最新）自然恢复跟随 */
    else if (follow) { follow = false; }
    refreshFollowBtn();
    updateScrollBar();
  }
  function sbHitTest(ray) {
    if (!scrollBar.visible) return false;
    if (!rayPlanePoint(ray, SB_Z, _sbHit)) return false;
    return Math.abs(_sbHit.x - sbX) <= SB_TRACK_W / 2 + 0.05 &&
      _sbHit.y >= SB_BOT - 0.08 && _sbHit.y <= SB_TOP + 0.08;
  }

  /* 「记录被指着」：右摇杆翻历史只在此时生效（避免与全局摇杆动作冲突），
     同时给滚行条做高亮反馈。桌面用鼠标位置等价判定，便于本地验证与鼠标用户。 */
  let hoverLog = false;   /* 鼠标是否指着记录/滚行条（桌面） */
  function rayPointingAtLog(ray) {
    return sbHitTest(ray) || panels.raycast(ray) != null || native.pickImage(ray) != null;
  }
  function xrPointingAtLog() {
    if (!renderer.xr.isPresenting) return false;
    for (const c of xrControllers) {
      if (rayPointingAtLog(xrRayFrom(c))) return true;
    }
    return false;
  }
  function refreshSbActive() {
    const dragging = (dragInfo && dragInfo.mode === "bar") || (xrDrag && xrDrag.mode === "bar");
    sbSetActive(!!dragging || (renderer.xr.isPresenting ? xrPointingAtLog() : hoverLog));
  }

  /* ---------- 拖动手势（手柄射线与鼠标共用）：三种翻历史里的一种半----------
     记录/滚行条的按下先挂起，移动超过阈值才进入拖动（跟手滚动），
     未越阈值抬起 = 原来的点击（翻段/聚焦/播放语音/放模型）。 */

  const _dragRay = new THREE.Raycaster();
  function dragBegin(ray, immediatePick) {
    if (sbHitTest(ray)) {
      const p = rayPlanePoint(ray, SB_Z, _sbHit);
      if (p) sbApplyY(p.y);
      sbSetActive(true);
      return { mode: "bar", moved: 0 };
    }
    /* 共同文件面板滚动 / 调整中模型拖拽（列表面板开着时优先于记录墙） */
    try {
      const fd = files.beginDrag(ray);
      if (fd) return fd;
    } catch (err) {}
    const onLog = panels.raycast(ray) != null || native.pickImage(ray) != null;
    if (onLog) {
      const p = rayPlanePoint(ray, -R, _sbHit);
      return { mode: "log", moved: 0, y0: p ? p.y : 0, hArc0: hArc };
    }
    /* 图表扇区/模型/空处：手柄按下即响应（与旧行为一致，不参与拖动） */
    if (immediatePick) handlePick(ray);
    return null;
  }
  function dragMove(drag, ray) {
    if (drag.kind && String(drag.kind).startsWith("xrfile")) { try { files.dragMove(drag, ray); } catch (err) {} return; }
    if (drag.mode === "bar") {
      const p = rayPlanePoint(ray, SB_Z, _sbHit);
      if (p) sbApplyY(p.y);
      return;
    }
    if (drag.mode !== "log") return;
    const p = rayPlanePoint(ray, -R, _sbHit);
    if (!p) return;
    const dy = drag.y0 - p.y; /* 手往下拉 → dy > 0 → hArc 增 → 看更早（内容跟手） */
    drag.moved = Math.max(drag.moved, Math.abs(dy));
    if (drag.moved < LOG_DRAG_MIN) return;
    if (follow) { follow = false; refreshFollowBtn(); }
    hArc = THREE.MathUtils.clamp(drag.hArc0 + dy, 0, maxScroll());
    updateScrollBar();
  }
  function dragEnd(drag) {
    if (!drag) return;
    if (drag.kind && String(drag.kind).startsWith("xrfile")) { try { files.dragEnd(drag); } catch (err) {} return; }
    if (drag.mode === "bar") sbSetActive(false);
  }
  /* 记录拖动未越阈值 → 抬起时补一次点击（用按下那一刻的射线） */
  function rayFromStored(drag) {
    _dragRay.ray.origin.copy(drag.o);
    _dragRay.ray.direction.copy(drag.d);
    return _dragRay;
  }

  /* ---------- WebXR 沉浸式会话：renderer.xr + 手柄射线拾取 + 摇杆平移/转向。
     输入抽象层三动作：确认（trigger→射线拾取，桌面=鼠标点击）、移动（左摇杆平移，
     桌面=滚轮/WASD）、旋转（右摇杆转向，桌面=拖拽环视）。无手柄时头向环视天然可用。
     世界内 CanvasTexture 控制台是主入口；dom-overlay 只保留给输入框唤起系统键盘。
     真机行为留用户抽查。 */

  let xrInImmersive = false;
  let xrStarting = false;
  let activeImmersiveMode = null;
  let retryImmersiveMode = null;
  let pendingSwitchMode = null;
  const _xrV1 = new THREE.Vector3();
  const _xrV2 = new THREE.Vector3();
  const _xrQ1 = new THREE.Quaternion();
  const xrRay = new THREE.Raycaster();

  /* 手柄：targetRaySpace（射线）挂 rig——rig 即用户载体，传送/转向随体 */
  const xrControllers = [];
  const xrControllerGrips = [];
  const xrHandSpaces = [];
  for (let i = 0; i < 2; i++) {
    const c = renderer.xr.getController(i);
    const grip = renderer.xr.getControllerGrip(i);
    const hand = renderer.xr.getHand(i);
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
      new THREE.LineBasicMaterial({ color: 0x6ea8ff, transparent: true, opacity: 0.85 })
    );
    line.scale.z = 4;
    line.visible = false;
    c.add(line);
    c.userData.xrLine = line;
    c.userData.handedness = null;
    c.userData.inputSource = null;
    c.addEventListener("connected", (ev) => {
      c.userData.inputSource = ev && ev.data ? ev.data : null;
      c.userData.handedness = ev && ev.data && (ev.data.handedness || null);
    });
    c.addEventListener("disconnected", () => {
      c.userData.inputSource = null;
      c.userData.handedness = null;
    });
    c.addEventListener("selectstart", () => onXRSelect(c));
    c.addEventListener("selectend", () => onXRSelectEnd(c));
    /* 侧握键按住说话（松开发送）——沉浸式下的快捷语音入口 */
    c.addEventListener("squeezestart", () => startVRRec());
    c.addEventListener("squeezeend", () => stopVRRec());
    rig.add(c);
    rig.add(grip);
    rig.add(hand);
    xrControllers.push(c);
    xrControllerGrips.push(grip);
    xrHandSpaces.push(hand);
  }
  renderer.xr.addEventListener("sessionstart", () => {
    xrInImmersive = true;
    xrPassthrough = activeImmersiveMode === "ar";
    ctx.root.classList.toggle("xr-ar", xrPassthrough);
    refreshShowroomVisuals();
    for (const c of xrControllers) c.userData.xrLine.visible = true;
    hud.classList.add("xr-immersive");
    if (worldUi) worldUi.setVisible(true);
    refreshImmersiveBtn();
    /* 世界控制台已经包含返回/跟随/原生图表/文件/输入/语音/发送。
       旧的脚边控制条仅作为世界 UI 初始化失败时的后备，避免沉浸式里重复显示。 */
    vrBar.visible = !worldUi;
    refreshVrBar();
    vrHintText(t("xrVRHint"), 9000); /* 入场提示几秒后自动淡出 */
  });
  renderer.xr.addEventListener("sessionend", () => {
    const nextMode = pendingSwitchMode;
    pendingSwitchMode = null;
    xrInImmersive = false;
    activeImmersiveMode = null;
    xrPassthrough = false;
    ctx.root.classList.remove("xr-ar");
    refreshShowroomVisuals();
    for (const c of xrControllers) c.userData.xrLine.visible = false;
    hud.classList.remove("xr-immersive");
    if (worldUi) worldUi.setVisible(false);
    killVRRec(); /* 会话结束即停录音（防麦克风指示灯残留） */
    vrBar.visible = false;
    vrHint.visible = false;
    if (xrDrag) { dragEnd(xrDrag); xrDrag = null; } /* 会话结束丢弃未完成的拖动 */
    if (nextMode && !disposed) {
      /* WebXR 不支持在同一个 XRSession 内把 immersive-ar 改成 immersive-vr，
         所以先结束旧会话，再创建目标模式的新会话。保留目标模式以便授权失败后重试。 */
      xrStarting = false;
      void enterImmersive(nextMode);
    } else {
      retryImmersiveMode = null;
      xrStarting = false;
      refreshImmersiveBtn();
    }
  });

  async function enterImmersive(requestedMode = null) {
    if (disposed || xrInImmersive || xrStarting) return;
    const mode = requestedMode || retryImmersiveMode
      || (ctx.immersiveMode ? ctx.immersiveMode() : "ar");
    const modes = availableImmersiveModes();
    if (!modes[mode]) {
      statusEl.textContent = t(mode === "vr" ? "xrVRFail" : "xrARFail");
      return;
    }
    if (!mode || !navigator.xr || !navigator.xr.requestSession) {
      statusEl.textContent = t(mode === "vr" ? "xrVRFail" : "xrARFail");
      return;
    }
    xrStarting = true;
    retryImmersiveMode = null;
    activeImmersiveMode = mode;
    refreshImmersiveBtn();
    let session = null;
    try {
      /* 房间坐标约定 y=0 是地面，必须具备 local-floor；dom-overlay 为 optional，
         不支持时仍可通过世界内控制条交互。AR 授权失败不能偷偷退到不透视的 VR。 */
      session = await navigator.xr.requestSession(mode === "ar" ? "immersive-ar" : "immersive-vr", {
        requiredFeatures: ["local-floor"],
        optionalFeatures: ["bounded-floor", "dom-overlay"],
        domOverlay: { root: hud },
      });
      if (disposed) { await session.end(); return; } /* 用户在授权期间已返回 2D */
      await renderer.xr.setSession(session);
      if (disposed) { await session.end(); return; }
      statusEl.textContent = "";
    } catch (err) {
      if (session) { try { await session.end(); } catch (e) {} } /* 初始化失败也释放设备会话 */
      retryImmersiveMode = mode;
      if (!disposed) statusEl.textContent = t(mode === "vr" ? "xrVRFail" : "xrARFail");
    } finally {
      xrStarting = false;
      if (!xrInImmersive) activeImmersiveMode = null;
      if (!disposed) refreshImmersiveBtn();
    }
  }
  function exitImmersive() {
    const s = renderer.xr.getSession();
    if (s) { try { s.end().catch(() => {}); } catch (err) {} }
  }

  function switchImmersiveMode() {
    if (disposed || !xrInImmersive || xrStarting || !canSwitchImmersive()) return false;
    const target = switchTargetMode();
    const session = renderer.xr.getSession();
    if (!session) return false;
    pendingSwitchMode = target;
    retryImmersiveMode = target;
    xrStarting = true;
    refreshImmersiveBtn();
    try {
      const ending = session.end();
      if (ending && typeof ending.catch === "function") {
        ending.catch(() => {
          if (pendingSwitchMode !== target || disposed) return;
          pendingSwitchMode = null;
          xrStarting = false;
          statusEl.textContent = t(target === "vr" ? "xrVRFail" : "xrARFail");
          refreshImmersiveBtn();
          if (worldUi) worldUi.refresh();
        });
      }
    } catch (err) {
      pendingSwitchMode = null;
      xrStarting = false;
      statusEl.textContent = t(target === "vr" ? "xrVRFail" : "xrARFail");
      refreshImmersiveBtn();
      if (worldUi) worldUi.refresh();
    }
    return true;
  }

  /* ---------- 世界内 VR 后备控制条 + 语音消息（沉浸式专用，不跟头） ----------
     正常情况下顶部/底部由 worldUi 接管；这里保留一条精简后备入口，供世界 UI
     初始化失败时使用。手柄侧握键仍可直接录制语音消息，不依赖 ASR。 */

  const vrBar = new THREE.Group();
  vrBar.visible = false;
  scene.add(vrBar);

  function drawBarButton(mesh, label) {
    const { c, g } = mesh.userData.label;
    g.clearRect(0, 0, c.width, c.height);
    g.beginPath();
    g.roundRect(4, 8, c.width - 8, c.height - 16, 30);
    g.fillStyle = "rgba(14,20,32,0.9)";
    g.fill();
    g.lineWidth = 3;
    g.strokeStyle = "rgba(91,140,255,0.9)";
    g.stroke();
    g.fillStyle = "#dfe8f4";
    g.font = "bold 40px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(label, c.width / 2, c.height / 2 + 2);
    mesh.material.map.needsUpdate = true;
  }
  function buildBarButton(label) {
    const c = document.createElement("canvas");
    c.width = 384;
    c.height = 96;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(0.5, 0.125),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false })
    );
    mesh.material.map.colorSpace = THREE.SRGBColorSpace;
    mesh.userData.label = { c, g: c.getContext("2d") };
    drawBarButton(mesh, label);
    return mesh;
  }
  const vrVoiceBtn = buildBarButton(t("xrVrVoice"));
  const vrFollowBtn = buildBarButton(t("xrFollowOn"));
  const vrExitBtn = buildBarButton(t("xrExit"));
  vrVoiceBtn.position.x = -0.56;
  vrExitBtn.position.x = 0.56;
  vrBar.add(vrVoiceBtn, vrFollowBtn, vrExitBtn);

  function buildVrHint() {
    const c = document.createElement("canvas");
    c.width = 1024;
    c.height = 128;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(1.7, 0.212),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false })
    );
    mesh.material.map.colorSpace = THREE.SRGBColorSpace;
    mesh.userData.label = { c, g: c.getContext("2d") };
    mesh.position.y = 0.2;
    return mesh;
  }
  const vrHint = buildVrHint();
  vrHint.visible = false;
  vrBar.add(vrHint);
  /* 钉在消息列底部前方（世界内固定，离墙 0.85m 便于手柄射线瞄准；不跟头） */
  vrBar.position.set(LOG_L + 0.6, FLOOR_Y - 0.26, -R + 0.85); /* x 每帧跟随 scroll panel 居中 */
  vrBar.rotation.x = -0.3; /* 略上仰，便于低头看 */

  function drawVrHint(text) {
    const { c, g } = vrHint.userData.label;
    g.clearRect(0, 0, c.width, c.height);
    g.beginPath();
    g.roundRect(4, 12, c.width - 8, c.height - 24, 34);
    g.fillStyle = "rgba(10,15,24,0.86)";
    g.fill();
    g.lineWidth = 3;
    g.strokeStyle = "rgba(91,140,255,0.7)";
    g.stroke();
    g.fillStyle = "#dfe8f4";
    g.font = "36px system-ui, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(text, c.width / 2, c.height / 2 + 2);
    vrHint.material.map.needsUpdate = true;
  }
  let vrHintTimer = null;
  let vrHintMsg = ""; /* 最近一次世界内提示文本（调试/验证用） */
  function vrHintText(text, holdMs) {
    vrHintMsg = text;
    drawVrHint(text);
    vrHint.visible = true;
    if (vrHintTimer) clearTimeout(vrHintTimer);
    if (holdMs) vrHintTimer = setTimeout(() => { if (!vrRec) vrHint.visible = false; }, holdMs);
  }
  function refreshVrBar() {
    drawBarButton(vrFollowBtn, follow ? t("xrFollowOn") : t("xrFollowOff"));
    drawBarButton(vrVoiceBtn, vrRec ? t("xrMicStop") : t("xrVrVoice"));
  }

  const VR_REC_MAX_MS = 60000;
  let vrRec = null; /* { mr, chunks, stream, startMs, timer, autoStop } */
  function vrPickMime() {
    const cands = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    for (const m of cands) {
      try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m; } catch (e) {}
    }
    return "";
  }
  function killVRRec() {
    if (!vrRec) return;
    const cur = vrRec;
    vrRec = null;
    if (cur.timer) clearInterval(cur.timer);
    if (cur.autoStop) clearTimeout(cur.autoStop);
    try { cur.mr.onstop = null; if (cur.mr.state !== "inactive") cur.mr.stop(); } catch (e) {}
    if (cur.stream) { try { cur.stream.getTracks().forEach((tk) => tk.stop()); } catch (e) {} }
    refreshVrBar();
  }
  async function startVRRec() {
    if (disposed || vrRec) return;
    if (!ctx.sendVoiceMsg) { vrHintText(t("xrVoiceUnsupported"), 4000); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { vrHintText(t("noRecApi"), 4000); return; }
    let stream = null;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { vrHintText(t("asrNotAllowed"), 4000); return; }
    if (disposed || vrRec) { try { stream.getTracks().forEach((tk) => tk.stop()); } catch (e) {} return; }
    const mime = vrPickMime();
    let mr = null;
    try { mr = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
    catch (e) {
      try { stream.getTracks().forEach((tk) => tk.stop()); } catch (e2) {}
      vrHintText(t("noRecApi"), 4000);
      return;
    }
    vrRec = { mr, chunks: [], stream, startMs: Date.now(), timer: null, autoStop: null };
    mr.ondataavailable = (e) => { if (e.data && e.data.size && vrRec) vrRec.chunks.push(e.data); };
    mr.onstop = () => onVRRecStop();
    try { mr.start(250); } catch (e) { killVRRec(); vrHintText(t("noRecApi"), 4000); return; }
    refreshVrBar();
    const tick = () => {
      if (!vrRec) return;
      vrHintText("● " + t("xrRecording") + " " + ((Date.now() - vrRec.startMs) / 1000).toFixed(1) + "s", 0);
    };
    tick();
    vrRec.timer = setInterval(tick, 250);
    vrRec.autoStop = setTimeout(() => stopVRRec(), VR_REC_MAX_MS);
  }
  function stopVRRec() {
    if (!vrRec) return;
    try { vrRec.mr.stop(); } catch (e) { killVRRec(); }
  }
  function vrToggleRec() { if (vrRec) stopVRRec(); else startVRRec(); }
  async function onVRRecStop() {
    if (!vrRec) return;
    const cur = vrRec;
    const durMs = Date.now() - cur.startMs;
    const mime = (cur.mr && cur.mr.mimeType) || "audio/webm";
    const chunks = cur.chunks;
    killVRRec(); /* 停轨、复位按钮态（此后 vrRec 为 null，提示面板不再被录音态占住） */
    if (durMs < 1000 || !chunks.length) { vrHintText(t("recTooShort"), 3500); return; }
    const blob = new Blob(chunks, { type: mime.split(";")[0].trim() || "audio/webm" });
    try {
      await ctx.sendVoiceMsg(blob, durMs);
      vrHintText(t("xrVoiceSent"), 3000);
    } catch (e) {
      vrHintText((e && e.message) || t("xrSendFail"), 4500);
    }
  }
  refreshVrBar(); /* 初始文字（此时 vrRec 为 null） */

  /* 确认动作（手柄）：控制条按钮 → 滚行条/记录的拖动 → 与桌面一致的拾取链。
     按下只挂起记录拖动，点击（未越阈值的抬起）在 onXRSelectEnd 里补。 */
  let xrDrag = null; /* { c, mode, moved, y0, hArc0, o, d } */
  function xrRayFrom(c) {
    c.getWorldQuaternion(_xrQ1);
    xrRay.ray.origin.setFromMatrixPosition(c.matrixWorld);
    xrRay.ray.direction.set(0, 0, -1).applyQuaternion(_xrQ1).normalize();
    return xrRay;
  }
  function onXRSelect(c) {
    if (disposed || !renderer.xr.isPresenting) return;
    const ray = xrRayFrom(c);
    if (vrBar.visible) {
      const barHits = ray.intersectObjects(vrBar.children, false);
      if (barHits.length) {
        const obj = barHits[0].object;
        if (obj === vrVoiceBtn) { vrToggleRec(); return; }
        if (obj === vrFollowBtn) { toggleFollow(); return; }
        if (obj === vrExitBtn) { doExit(); return; }
        return;
      }
    }
    const drag = dragBegin(ray, true); /* 图表/空处在这内部已即时响应 */
    if (!drag) return;
    drag.o = ray.ray.origin.clone();
    drag.d = ray.ray.direction.clone();
    drag.c = c;
    xrDrag = drag;
  }
  function onXRSelectEnd(c) {
    if (!xrDrag || xrDrag.c !== c) return;
    const drag = xrDrag;
    xrDrag = null;
    dragEnd(drag);
    /* 未越阈值的按下-抬起 = 点击（翻段/聚焦/播放语音/放模型/文件面板行） */
    if (drag.mode === "log" && drag.moved < LOG_DRAG_MIN) handlePick(rayFromStored(drag));
    else if (drag.kind === "xrfile-panel" && (drag.moved || 0) < LOG_DRAG_MIN) handlePick(rayFromStored(drag));
  }

  /* ---------- 右摇杆吸附转身（每次 45°）----------
     边缘触发：摇杆推过死区时转一步，回中（带 0.10 迟滞）后才能再转一步——不再是按住
     连续转。转身用 ~180ms 缓出补间，避免瞬跳眩晕；方向与旧的连续旋转一致（推右＝向右转）。 */
  const SNAP_TURN = Math.PI / 4;     /* 每次 45° */
  const SNAP_TURN_SEC = 0.18;        /* 补间时长（秒） */
  let snapArmed = true;              /* 已回中，可触发下一次 */
  let snapFrom = 0, snapTo = 0, snapT = 1;

  function xrSnapTurn(ax) {
    if (Math.abs(ax) > 0.15) {
      if (!snapArmed) return;
      snapArmed = false;
      snapFrom = rig.rotation.y;
      snapTo = rig.rotation.y - Math.sign(ax) * SNAP_TURN;
      snapT = 0;
    } else if (Math.abs(ax) < 0.1) {
      snapArmed = true;
    }
  }
  function stepSnapTurn(dt) {
    if (snapT >= 1) return;
    snapT = Math.min(1, snapT + dt / SNAP_TURN_SEC);
    const e = 1 - Math.pow(1 - snapT, 3); /* ease-out cubic */
    rig.rotation.y = snapFrom + (snapTo - snapFrom) * e;
  }

  /* 移动/旋转（手柄摇杆，xr-standard：axes[2]=X axes[3]=Y，死区 0.15） */
  function updateXRInput(dt) {
    const session = renderer.xr.getSession();
    if (!session || disposed) return;
    for (const src of session.inputSources) {
      const gp = src && src.gamepad;
      if (!gp || !gp.axes || gp.axes.length < 4) continue;
      const ax = gp.axes[2] || 0;
      const ay = gp.axes[3] || 0;
      /* 调整共同文件模型时摇杆被文件系统接管（左手柄平移 / 右手柄旋转缩放） */
      try { if (files.xrJoystick(src.handedness, ax, ay, dt)) continue; } catch (err) {}
      if (src.handedness === "right") {
        xrSnapTurn(ax); /* 左右 = 每次 45° 吸附转身（边缘触发，不是连续旋转） */
        /* 右摇杆 Y = 翻历史：**只在射线指着记录/滚行条时生效**，避免与全局摇杆动作
           （如传送）冲突；推上 = 看更早，与桌面 ←/↑ 同向 */
        if (Math.abs(ay) > 0.15 && xrPointingAtLog()) scrollBy(-ay * dt * 2.2);
      } else if (Math.abs(ax) > 0.15 || Math.abs(ay) > 0.15) {
        /* 移动动作：以头向水平 yaw 为基准推杆平移 */
        camera.getWorldDirection(_xrV1);
        _xrV1.y = 0;
        if (_xrV1.lengthSq() < 1e-6) _xrV1.set(0, 0, -1); else _xrV1.normalize();
        _xrV2.crossVectors(_xrV1, camera.up).normalize();
        const spd = 1.7 * dt;
        rig.position.addScaledVector(_xrV1, -ay * spd).addScaledVector(_xrV2, ax * spd);
        const hr = Math.hypot(rig.position.x, rig.position.z);
        if (hr > MAX_RADIUS) {
          rig.position.x *= MAX_RADIUS / hr;
          rig.position.z *= MAX_RADIUS / hr;
        }
        rig.position.y = 0;
      }
    }
  }

  /* DOM HUD 按钮初始文字（避免与 refreshNativeBtn 等声明顺序依赖） */
  refreshFollowBtn();
  refreshImmersiveBtn();

  /* ---------- 2D 事件订阅（3D 只读，不写共享状态） ---------- */

  const unsubMsg = ctx.msgEvents.subscribe((type, m) => {
    if (disposed) return;
    try {
      if (type === "reset") { rebuildFromLog(); return; }
      if (!m) return;
      const id = String(m.id);
      if (!id || id === "undefined") return;
      if (type === "add") {
        if (!byId.has(id)) {
          const el = ctx.logElement.querySelector(`[data-msg-id="${CSS.escape(id)}"]`);
          if (!el) return;
          byId.set(id, m);
          strip.push({ id, msg: m, el, staged: false, widthCss: el.offsetWidth, heightCss: el.offsetHeight });
        }
      } else if (type === "update") {
        const entry = byId.get(id);
        if (entry) {
          entry.msg = m;
          if (entry.el && entry.el.isConnected) entry.heightCss = entry.el.offsetHeight;
          panels.invalidate(id, !m.streaming);
        }
      } else if (type === "remove") {
        byId.delete(id);
        const i = strip.findIndex((e) => e.id === id);
        if (i >= 0) strip.splice(i, 1);
        panels.remove(id);
      }
      refreshTitle();
    } catch (err) { /* 3D 事件处理故障不得反噬 2D 管线 */ }
  });

  const unsubRoom = ctx.roomEvents.subscribe((info) => {
    if (disposed) return;
    try {
      if (info && info.closed) { doExit(); return; }
      applyRoomScene((info && info.scene) || null); /* 房主改场景后房内热切换 */
      presenceLearnIds(info);  /* 二进制位姿帧只带数字 id，先学会 id→用户名 */
      avatars.applyRoom(info); /* 形象随在线成员增量同步（需求 4.1） */
    } catch (err) {}
  });

  /* ---------- 主循环 ---------- */

  let lastT = 0;
  let avatarUpdateWarned = false;

  function frame(tNow) {
    if (disposed) return;
    /* 页面隐藏时浏览器本就停发 rAF（需求 7.3 的暂停由浏览器保证）；
       这里不再叠加 running 门控，避免可见性误报导致黑屏。
       沉浸式下 setAnimationLoop 由 XR 帧驱动（同一路径，任务 10）。 */
    const dt = Math.min(0.05, (tNow - lastT) / 1000 || 0.016);
    lastT = tNow;
    updateXRInput(dt);  /* 手柄摇杆平移/转身/翻历史（无会话 no-op） */
    stepSnapTurn(dt);   /* 吸附转身的补间推进（松杆后也要走完） */
    if (xrDrag) dragMove(xrDrag, xrRayFrom(xrDrag.c)); /* 按住拖动记录/滚行条：跟手滚动 */
    refreshSbActive();  /* 指着记录/滚行条时拇指高亮（手柄射线每帧移动） */
    updateControls(dt);
    if (follow && hArc > 0.001) {
      hArc *= Math.exp(-dt * 4);
      if (hArc < 0.005) hArc = 0;
      updateScrollBar();
    }
    layout(dt);
    try {
      avatars.update(dt);
    } catch (err) {
      /* 3D 形象故障不能拖垮 2D/XR 主循环，但不能再静默吞掉 IK/骨骼错误；
         第一次记录完整错误，便于从浏览器 console 直接区分数据问题与渲染问题。 */
      if (!avatarUpdateWarned) {
        avatarUpdateWarned = true;
        console.error("[xr] avatar update failed; hand/head IK may be disabled", err);
      }
    }
    /* 位姿同步：0.5s 上报自己的、0.5s 拉别人的增量（页面隐藏时 rAF 停发，自然暂停） */
    presenceSendAcc += dt;
    presencePollAcc += dt;
    if (presenceSendAcc >= PRESENCE_SEND_MS / 1000) { presenceSendAcc = 0; presenceSend(); }
    if (presencePollAcc >= PRESENCE_POLL_MS / 1000) { presencePollAcc = 0; presencePoll(); }
    updateSpatialVoices(); /* 语音声源跟随站位/面板位 + 口型推进（任务 9） */
    try { files.tick(dt); } catch (err) {}
    /* 接近已加载的最旧一端 → 向前分页回填（一次性拉全，需求 2.7） */
    const bandH = BAND_HI - FLOOR_Y;
    if (strip.length && !historyEnd && hArc + bandH > totalH - 1.2 && (totalH < bandH + 1 || hArc > 0)) {
      backfill();
    }
    renderer.render(scene, camera);
  }
  lastT = performance.now();
  renderer.setAnimationLoop(frame);

  let lastViewportW = window.innerWidth;
  let resizeTimer = null;
  function onResize() {
    if (disposed) return;
    renderer.setSize(window.innerWidth, window.innerHeight);
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    /* 宽度显著变化 → 2D 气泡换行宽度随之变化 → 全量重栅格化（去抖） */
    if (Math.abs(window.innerWidth - lastViewportW) >= 80) {
      lastViewportW = window.innerWidth;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if (!disposed) panels.invalidateAll(); }, 300);
    }
  }
  window.addEventListener("resize", onResize);

  function onVis() {
    lastT = performance.now(); /* 回前台时重置时钟，避免 dt 跳变 */
  }
  document.addEventListener("visibilitychange", onVis);

  /* 初始：从 2D 已渲染的 #log 收集当前窗口消息，并按需后台回填全史 */
  rebuildFromLog();
  if (strip.length && !historyEnd) backfill();
  /* 位姿同步起手：先报一次自己的，再拉一次全量（不必等第一个 0.5s 节拍） */
  presenceSend();
  presencePoll();

  /* 桌面调试句柄（验证/排查用；退出 3D 时移除） */
  window.__xrDebug = {
    camera,
    stats: () => ({ ...panels.stats(), children: panels.group.children.length, native: native.stats() }),
    strip: () => strip.length,
    lastLayout: null,
    scene: () => scene.children.map((o) => o.type),
    childPos: (i) => {
      const m = panels.group.children[i || 0];
      return m ? [+m.position.x.toFixed(2), +m.position.y.toFixed(2), +m.position.z.toFixed(2), +m.scale.y.toFixed(2)] : null;
    },
    debugRec: (id) => panels.debugRec(String(id)),
    ids: () => panels.group.children.map((m) => m.userData.panelId),
    scroll: () => ({ hArc: +hArc.toFixed(2), max: +maxScroll().toFixed(2), totalH: +totalH.toFixed(2), follow }),
    scrollBy: (d) => scrollBy(d), /* 与摇杆/←→ 同一条滚动路径（无头显时验证用） */
    scrollBar: () => ({
      visible: scrollBar.visible,
      x: +sbX.toFixed(3),
      thumbY: +sbThumb.position.y.toFixed(3),
      thumbH: +sbThumbH.toFixed(3),
      trackBot: SB_BOT, trackTop: SB_TOP,
      logW: +logW.toFixed(3),
      contentW: +logContentW.toFixed(3),
    }),
    dragState: () => (dragInfo ? { mode: dragInfo.mode, moved: +dragInfo.moved.toFixed(1) } : (xrDrag ? { mode: xrDrag.mode } : null)),
    sbActive: () => sbActive,      /* 滚行条拇指是否高亮（指着/拖动中） */
    hoverLog: () => hoverLog,      /* 桌面：鼠标是否指着记录/滚行条 */
    /* 位姿同步（验证用：手动触发一次上报/拉取，或查看当前游标与自己的位姿） */
    presence: () => ({ off: presenceOff, cursor: presenceCursor, busy: presenceBusy, pose: localPosePayload() }),
    presenceSend: () => presenceSend(),
    presencePoll: () => presencePoll(),
    presenceLeave: () => presenceLeave(),
    /* 吸附转身（无头显时验证用：手动喂摇杆 X 值，同一套边缘触发逻辑） */
    snapTurn: (ax) => { xrSnapTurn(ax); return { armed: snapArmed, t: +snapT.toFixed(2), yaw: +rig.rotation.y.toFixed(3) }; },
    snapState: () => ({ armed: snapArmed, t: +snapT.toFixed(2), yaw: +rig.rotation.y.toFixed(3), deg: +(rig.rotation.y * 180 / Math.PI).toFixed(1) }),
    nativeGroup: () => native.group,
    nativeOn: () => native.isEnabled(),
    avatarGroup: () => avatars.group,
    avatarApi: () => avatars, /* setExpression/wave/positionOf（ARKit52 驱动接口验证用） */
    /* VR 控制条与语音（无头显时验证用：显示控制条 + 直接驱动同一批处理器） */
    showVRBar: (on) => { vrBar.visible = !!on; },
    vrBarGroup: () => vrBar,
    vrBarClick: (act) => {
      if (act === "voice") vrToggleRec();
      else if (act === "follow") toggleFollow();
      else if (act === "exit") doExit();
    },
    vrRecStart: () => startVRRec(),
    vrRecStop: () => stopVRRec(),
    vrRecState: () => (vrRec ? { recording: true, ms: Date.now() - vrRec.startMs } : { recording: false }),
    vrHint: () => vrHintMsg, /* 世界内提示最近一条文本（画布内容不可读，验证用） */
    childScale: (i) => {
      const m = panels.group.children[i || 0];
      return m ? [+m.scale.x.toFixed(3), +m.scale.y.toFixed(3)] : null;
    },
    room: () => (roomSystem ? { id: roomSystem.sceneId, seats: roomSystem.seats.length } : null),
    roomGroup: () => (roomSystem ? roomSystem.group : null),
    seatOf: (name) => avatars.positionOf(name),
    files: () => files, /* 共同文件系统（验证用：isOpen/内部状态） */
    worldUi: () => worldUi ? {
      visible: worldUi.group.visible,
      top: worldUi.top.position.toArray().map((v) => +v.toFixed(3)),
      send: worldUi.send.position.toArray().map((v) => +v.toFixed(3)),
      inputFocused: !!worldUi.state.focusedInput,
      topHotspots: worldUi.state.topHotspots.map((h) => h.act),
      sendHotspots: worldUi.state.sendHotspots.map((h) => h.act),
    } : null,
    worldUiSetVisible: (on) => { if (worldUi) worldUi.setVisible(!!on); },
    worldUiClick: (act) => {
      if (!worldUi) return false;
      const topActs = new Set(["exit", "follow", "native", "switchImmersive", "files"]);
      const mesh = topActs.has(act) ? worldUi.top : worldUi.send;
      const list = topActs.has(act) ? worldUi.state.topHotspots : worldUi.state.sendHotspots;
      const spot = list.find((r) => r.act === act);
      if (!spot) return false;
      const w = mesh.userData.canvas.width, h = mesh.userData.canvas.height;
      const p = mesh.localToWorld(new THREE.Vector3(
        ((spot.x + spot.w / 2) / w - 0.5) * mesh.geometry.parameters.width,
        (0.5 - (spot.y + spot.h / 2) / h) * mesh.geometry.parameters.height,
        0.01
      ));
      const o = camera.getWorldPosition(new THREE.Vector3());
      const ray = new THREE.Raycaster(o, p.sub(o).normalize());
      return worldUi.handlePick(ray);
    },
    mem: () => ({ geometries: renderer.info.memory.geometries, textures: renderer.info.memory.textures }),
    /* 位姿流的内部状态（排查「远端形象不动」时最有用：游标/是否关闭/id 映射/已累积的成员） */
    presence: () => ({
      cursor: presenceCursor,
      off: presenceOff,
      busy: presenceBusy,
      ids: Array.from(presenceIdNames.keys()),
      poses: Array.from(presencePoses.keys()),
    }),
    render: () => { renderer.render(scene, camera); return renderer.info.render.frame; }, /* 验证用：rAF 被遮挡暂停时手动驱动一帧 */
  };

  /* ---------- 退出与释放（需求 7.4） ---------- */

  /* 退出时全量释放。统一走 disposeObjectTree——此处原实现只释放 material.map，
     漏了 normalMap/emissiveMap 等贴图，反复进出 3D 会慢慢涨内存。 */
  function disposeScene() {
    disposeObjectTree(scene);
  }

  function doExit() {
    if (disposed) return;
    dispose();
    if (ctx.onExit) ctx.onExit();
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    renderer.setAnimationLoop(null);
    exitImmersive(); /* 头显会话随 3D 退出一并结束（需求 7.4） */
    stopAsr(); /* 语音识别随退出终止，避免麦克风指示灯残留 */
    killVRRec(); /* VR 侧握键录音同理 */
    xrDrag = null; /* 丢弃未完成的拖动 */
    presenceLeave(); /* 通知房间：我已离开 3D（别人立即移除我的形象） */
    if (resizeTimer) clearTimeout(resizeTimer);
    focus = null;
    for (const id of Array.from(spatialVoices.keys())) dropSpatialVoice(id); /* 空间音频摘除（需求 7.4） */
    try { ctx.setVoiceSpatial(null); } catch (e) {}
    for (const id of Array.from(chatModels.keys())) removeChatModel(id);
    for (const id of Array.from(placingModels.keys())) removeChatModel(id);
    if (worldUi) { worldUi.dispose(); worldUi = null; }
    if (roomSystem) { try { roomSystem.dispose(); } catch (e) {} roomSystem = null; }
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("keyup", onKeyUp);
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVis);
    try { unsubMsg(); } catch (e) {}
    try { unsubRoom(); } catch (e) {}
    try { files.dispose(); } catch (e) {}
    native.dispose();
    avatars.dispose();
    panels.dispose();
    disposeScene();
    renderer.dispose();
    try { renderer.forceContextLoss(); } catch (e) {}
    ctx.clearStaged();
    ctx.root.innerHTML = "";
    ctx.root.classList.remove("xr-ar");
    ctx.root.classList.add("hidden");
    delete window.__xrDebug;
  }

  return { dispose };
}

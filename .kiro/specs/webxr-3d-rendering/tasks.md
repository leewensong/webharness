# 实施计划 — WebXR 3D 房间渲染

> 新会话从这里开始。按阶段推进，每阶段结束可独立在浏览器验证（桌面 3D 预览先行，沉浸式最后接入）。仓库当前版本 2.9.3，本规格落地版本 2.10.0。

## Phase 1：骨架与面板模式（3D 覆盖率即 100%）

- [x] 1. 依赖与加载骨架
  - `static/vendor/three/`：vendored three.module.js（锁定版本，文件头注释版本号）+ addons（GLTFLoader / OrbitControls；three-vrm dist 模块一并放入）
  - `index.html`：import map + 顶栏「3D」按钮 + WebXR 能力检测（`navigator.xr?.isSessionSupported('immersive-vr')`）+ `<div id="xrRoot">` 挂载点 + 动态 import 加载器；2D 首屏不引入任何 3D 代码
  - i18n：顶栏「3D」按钮的键直接进 index.html 字典；其余 3D 键在 `xr-i18n.js`（zh/en，3D 加载时合并进 I18N）
  - _需求：1.1、1.2、1.5、6.4_

- [x] 2. 场景与桌面预览
  - `static/xr/xr-main.js`：场景生命周期（进入/退出/切房间重建）、渐变天空+半球光+地面网格、弧形消息墙布局器、OrbitControls + WASD、「跟随最新」开关
  - 退出时资源全量 dispose；页面隐藏暂停渲染循环
  - _需求：1.3、2.2、6.1、7.3、7.4_

- [x] 3. 面板模式管线
  - `static/xr/xr-panels.js`：栅格化 `#log` 内 live 气泡（进入 3D 时 #log 保持渲染在遮罩下，严禁 display:none——ECharts 会量出 0×0）→ foreignObject → CanvasTexture → 弧面面板（圆角/投影/头像名牌并入快照）；超长内容纵向截断 + 「展开」副面板（同一节点换裁剪重栅格化）；LRU 纹理缓存（≤24 张）与 messageUpdatedAt 失效；流式面板重建节流 ≥300ms
  - 兜底：栅格化失败降级纯文本 Canvas 面板；低端设备（无 WebGL2）回退 2D 并提示
  - 消息流接入：index.html 新增 `msgEvents`（add/update/remove/reset）与 `roomEvents` 桥（约 20 行纯增量），3D 只订阅不写
  - 服务器：`GET messages` 增加只读 `beforeId` 参数（不传即旧行为，Agent API 语义零变）；3D 沿墙向前一次性分页回填全部历史
  - _需求：2.1、2.3、2.4、2.5、2.6、2.7、7.1、7.2、7.5_

## Phase 2：数据驱动 3D（原生图表与 a2ui）

- [x] 4. ```chart 原生 3D
  - `static/xr/xr-native.js`：bar → 12 段圆柱柱列 + 数值标签（标签用单行文字小面板）；line → THREE.Line + 关键点标签；pie → ExtrudeGeometry 扇形 + 指向浮签（名称/值/百分比）；标题/图例小面板
  - 图表数据与 2D 同源（同一 chart JSON），面板模式保留为兜底开关（调试/对比用）
  - _需求：3.1、3.2_

- [x] 5. a2ui 原生 3D 小部件
  - 目录 v1 组件映射：MetricCard/Progress/Callout/Timeline/Text 原生实现；Row/Column/Card → Group 布局容器；Table/未知组件回退面板；catalogId 严格解析 + 未知占位（3D 版 `[a2ui: 类型名]`）
  - JSON Pointer 绑定解析复用 2D 端同一算法：经桥调用 index.html 顶层 `a2uiParseMessages`/`a2uiValue`/`a2uiBuildNode`，不抽文件、不动 2D 加载顺序
  - _需求：3.3、3.4、3.5_

- [x] 6. 图片、3D 模型附件与聚焦模式
  - 图片消息 → 白框纹理平面，指向放大；聚焦模式（对准消息放大至舒适阅读）桌面/头显两套输入映射
  - 3D 模型附件（.glb/.gltf/.vrm）：2D 附件分支升级「3D 文件卡片」——懒加载 GLTFLoader 离屏渲染外框取景缩略图（按 downloadUrl 缓存、在途并发 ≤1、超时/失败降级静态图标），卡片随面板模式自动进 3D；3D 点击此类面板 → 模型放置进场景（Box3 归一化 ~1m、面板前方地面，再点收起；上限 ≤6 LRU；取回/解析与形象加载共用 helper，退出全量 dispose）
  - _需求：3.6、3.8、3.9、6.2_

## Phase 3：形象与语音

- [x] 7. 形象加载
  - `static/xr/xr-avatars.js`：GLTFLoader + three-vrm；服务器文件走 `/api/users/{u}/model3d`，外链直连；加载失败静默回退缺省化身（胶囊体 + 用户名 hash 配色 + 名牌）；站位按 `hash(username)` 稳定散列；房主标识
  - 加载串行化（同时在途 ≤2），VRM/普通 GLB 分支处理
  - _需求：4.1、4.2、4.5、4.6、4.7、7.2_

- [x] 8. ARKit 52 / Humanoid 能力接口
  - `model3dArkit`：morph target 字典暴露（ARKit52 命名）+ ARKit→VRM 表情映射表（口型 aa→A 等）；`model3dHumanoid`：内置待机呼吸/挥手两动画（VRMA 或 glTF 动画剪辑），未勾选退化浮动
  - _需求：4.3、4.4_

- [x] 9. 语音空间音频
  - 3D 内语音播放走 `AudioContext → MediaElementSource → PannerNode(HRTF)`，声源绑定发送者形象/消息位；进 3D 时暂停 2D 端音频；转写字幕随语音面板展示
  - _需求：5.1、5.2、5.3_

## Phase 4：沉浸式与发送入口

- [x] 10. WebXR 会话
  - `renderer.xr` 接入 + 沉浸式进入/退出按钮（能力检测驱动显隐）；手柄射线拾取（播放语音/聚焦/翻页/图例）；摇杆传送或平移；无手柄退化头向环视 + 传送
  - 输入抽象层：旋转/移动/确认三动作，桌面/手柄/头向三份映射
  - _需求：1.2、6.1、6.2_

- [x] 11. 3D 内发送
  - 简洁文本输入面板，回车走现有 POST 消息接口；复杂编辑引导回 2D（面板上放提示）
  - _需求：6.3_

## Phase 5：收尾与发布

- [x] 12. 服务器可选微调与版本
  - `_validate_model3d_bytes` 增加 VRM 识别（魔数同为 glTF，仅细化提示/记录，兼容现状）；版本 2.10.0（app/main.py；`beforeId` 已随 Phase 1 落地）
  - 全量回归：2D 模式逐功能走查（确认 3D 未侵入 2D 管线）；性能预算复核（纹理 LRU、帧率、内存释放）
  - 发布：备份 → build_release.sh → install.sh（生产部署需用户确认后执行）
  - _需求：8.1–8.5、7.1–7.5_
  - 2026-09-17 已完成并发布 2.10.0（备份 /opt/webharness-backup-20260917-042916）；VRM/头显真机抽查待用户进行

- [x] 13. 房间 3D 场景（需求 9；2026-09-20 实施并发布 2.12.0）
  - `rooms` 新增 `map3d`（场景描述符 JSON）、`scene_data`/`scene_mime`/`scene_updated_at`（上传的 GLB，镜像 `users.model3d` 的做法）、`xr_state`（**仅建列**，透传预留，本期不写入不消费）；ALTER TABLE 沿用 db.py 既有迁移模式，且必须放在 `_rebuild_rooms_if_name_globally_unique` 之后
  - **map3d 最终格式**：`{"kind":"builtin","id":"meeting"|"werewolf"}` / `{"kind":"file"}`（上传件的 URL 由服务器按房间名 + `scene_updated_at` 现算，不落库，房间改名不会失效）/ `{"kind":"url","url":"http(s)://…"}`；无场景 = NULL。服务器只做最小 schema 校验（内置 id 必须在册、外链必须 http(s)），不解析场景内容
  - 服务器：`GET /api/room-scenes`（内置目录，常量 `BUILTIN_ROOM_SCENES` 是权威 id 清单）；建房 `scene` 与 `PATCH /api/rooms/{room}` 的 `scene`（`kind:"none"` 显式清空）；`POST/DELETE /api/rooms/{room}/scene`（上传/清除，房主或 roomAgent）、`GET /api/rooms/{room}/scene`（成员下载）；上传上限单独 50MB（`MAX_ROOM_SCENE_BYTES`，附件与形象仍 20MB），且**拒绝 .gltf（JSON）**——它引用外部 .bin/贴图，单文件上传必然加载失败
  - 渲染端：新文件 `static/xr/xr-rooms.js`（内置场景用图元程序化搭建，座位表显式定义；外壳 14×12.6m 裹住消息墙且**不建天花板/不建消息墙那面墙**）；`xr-avatars.js` 的 `setSeats` 按「成员名字典序 + hash + 线性探测」就座（不重叠、成员集合不变则稳定），无场景回退原 hash 环；`xr-main.js` 只在描述符变化时重建场景，并把 `disposeScene` 统一到既有的 `disposeObjectTree`
  - 座位是**推荐位置**：本期只做自动就座；用户/Agent 自行改位的写入接口与同步协议**仍按原计划留待下期**（与 2026-09-17 的决定一致，`xr_state` 仍为纯预留）
  - _需求：9.1–9.5、8.3_
  - 真机（Quest）内场景渲染留用户抽查（本地无设备，桌面路径已回归）

## 验证方式

- 本地 `npm`/脚本无需变更；`.claude/launch.json` 的 `webharness-local` 配置起本地 8768，浏览器验证桌面 3D 预览全流程（进房、消息墙、图表、形象、语音、退出释放）。
- 沉浸式：Chrome 的 WebXR 模拟器扩展 / `--enable-features=OpenXR` 桌面模拟先行，真机（Quest 等）由用户抽查。
- 回归：2D 全功能照常（富文本、语音、私聊、移动端布局），确保 3D 模块按需加载且互不影响。
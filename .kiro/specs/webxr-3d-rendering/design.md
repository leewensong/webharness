# 设计文档 — WebXR 3D 房间渲染

## 架构总览

延续「数据与渲染分离」决策（docs/generative-ui.md）：聊天记录是唯一数据源，2D 网页与 3D 渲染端是它的两个消费者。

```
                    SQLite 聊天记录（content/markdown + chart JSON + a2ui 消息 + 附件）
                                    │  同一套 HTTP API（消息轮询、附件、语音、形象）
                    ┌───────────────┴────────────────┐
             2D 渲染端（现状）                   3D 渲染端（本规格）
        static/index.html 管线               static/xr/* 新增模块
        marked→DOMPurify→diagrams            ┌──────────────┬────────────────┐
                                             │ 面板模式      │ 原生 3D 模式    │
        消息 → DOM（已有，不改动）  ──复用──→ │ DOM→纹理贴面板 │ 数据→3D 几何体   │
```

关键点：**3D 端不做第二套内容解析**。富文本内容先走现有 2D 管线得到 DOM（mermaid/svg/chart/a2ui/markdown 全部已解决），面板模式直接把这份 DOM 栅格化上纹理；只有「数据明确结构化」的类型（chart JSON、a2ui 组件树、图片、语音、形象模型）走原生 3D。这样 3D 覆盖率第一天就是 100%，原生 3D 逐类渐进替换。

## 技术选型

| 事项 | 选择 | 理由与备注 |
| --- | --- | --- |
| 3D 引擎 | **three.js**（vendored ESM） | 事实标准；WebXR 由核心直接支持（`renderer.xr`）；无构建步骤可用（见下） |
| 模块加载 | 原生 ESM + **import map** | 项目无打包器，维持惯例；`static/vendor/three/` 下放 `three.module.js`、`GLTFLoader.js`、`OrbitControls.js` 等，xr 模块用相对路径 import |
| VRM 加载 | **@pixiv/three-vrm**（vendored 其 dist 模块） | VRM 0.x/1.0 标准实现；基于 GLTFLoader 插件机制，与 GLB 共用加载路径 |
| 文本 | **栅格化 DOM → CanvasTexture**（首选） | 现有 2D 管线直接复用，中文排版（换行/混排/emoji）天然正确。troika-three-text 的 CJK SDF 字库体积大、排版需重做，仅在未来需要「可编辑 3D 文本」时再引入 |
| VR UI 面板 | 自研薄层（Plane + CanvasTexture + 圆角/阴影材质） | three-mesh-ui 与 troika 都是为「程序化 UI」设计；我们的面板内容来自既有 DOM，自研层更薄。交互用射线拾取（Raycaster） |
| WebXR 会话 | `renderer.xr` + `VRButton` 逻辑内联 | 不引入 react-three-fiber 等重框架（与无打包约束冲突） |
| 空间音频 | WebAudio `PannerNode`(HRTF) | 语音消息播放时声源绑定到发送者消息位 |

依赖全部 vendored 进 `static/vendor/`（与 echarts/mermaid 同目录体系），首次进入 3D 时按需 `<script type="module">` 加载，2D 首屏不受影响。

## 与 2D 端的集成边界

`index.html` 只增加三样东西：

1. 顶栏「3D」按钮 + WebXR 能力检测（`navigator.xr?.isSessionSupported('immersive-vr')`）。
2. `<script type="importmap">` 与 xr 模块加载器（动态 import，进入 3D 时才拉取）。
3. 一个挂载点 `<div id="xrRoot">`（3D 运行时覆盖视图，退出时移除）。

2D 端在 index.html 内新增一个极薄的桥（纯增量，约 20 行）：`msgEvents.subscribe(fn)`（`add`/`update`/`remove`/`reset` 四类事件，携带完整 message 对象，在 `renderMessages` 内发射）与 `roomEvents.subscribe(fn)`（房间切换/在线成员/房主/归档态，在 `applyRoom` 内发射），另有只读取值器（`store.token`/`store.username`、`t()`/`tf()`、`api()`）。**2D 管线是唯一 DOM 生产者**：3D 直接消费 `#log` 内现成的 live 气泡，不建第二套轮询（增量轮询仍由 2D 既有循环驱动；3D 的历史回填是沿墙向前的**一次性** `beforeId` 分页拉取）。归档视图（viewingArchive）中 3D 入口禁用。

## 渲染管线

### 面板模式（通用兜底）

```
消息 → 2D 管线 renderBody(msg) → DOM（气泡节点，含富文本）
     → 序列化栅格化（SVG foreignObject → Image → offscreen canvas）
     → THREE.CanvasTexture → 弧面/平面 Panel（圆角、投影、头像名牌）
```

- 消费模型：进入 3D 时 `#log` 继续在遮罩下渲染（**严禁 `display:none`**——ECharts 初始化会量出 0×0 空白画布，index.html 已有注释），3D 直接栅格化 `#log` 内现成的 live 气泡；「展开」副面板 = 同一节点换裁剪重栅格化，无需 staging 二次渲染。快照前把气泡内 blob: 图片转 data:；项目无 @font-face（纯系统字体栈），无需嵌字体。
- 栅格化方案用 SVG `foreignObject` 矢量栅格（three.js `HTMLMesh` 同思路），比 html2canvas 依赖少；DOM 内的 canvas（echarts）/内联 svg（mermaid）先转为 dataURL 嵌入再栅格化。浏览器兼容降级：`foreignObject` 栅格失败时回退 `html-to-image` 思路逐段截图或纯文本 Canvas 绘制兜底（保证需求 2.3 的 100% 覆盖）。
- 分辨率自适应：面板宽固定 720px 逻辑像素起，按内容高度撑开；纹理尺寸 ≤2048，超长内容**纵向截断 + 「展开」副面板**（走近/聚焦时续读），避免单条超长消息一张巨纹理。
- 缓存：`Map<messageId, {texture, version}>`；消息 `updatedAt` 变化（流式追加）或撤回时失效。LRU 上限见性能预算。
- 头部（头像+用户名+时间）直接并入 DOM 快照（复用 2D 气泡头部模板），私聊灰底样式随之带入。

### 原生 3D 模式（数据驱动）

按内容类型分派（与 2D 的 renderDiagrams 分派平行）：

| 记录内容 | 3D 实现 | 说明 |
| --- | --- | --- |
| ```chart bar/line | BoxGeometry 柱列 / THREE.Line 折线，地面网格 + 数值标签 | 数据即 JSON，直接建几何体；标签用小面板纹理（复用栅格管线画单行文字） |
| ```chart pie | 挤出扇形（ExtrudeGeometry），悬浮图例 | 指向扇区 → 临时浮签（名称+值+百分比） |
| a2ui MetricCard/Progress/Callout/Timeline | 悬浮指标牌 / 3D 进度条 / 浮标 / 空间时间线（节点+连线） | 组件树遍历逻辑复用 2D 版（a2uiBuildNode 的数据语义），仅渲染端换形；绑定解析（JSON Pointer + dataModel）复用同一算法 |
| a2ui Row/Column/Card | THREE.Group 布局容器（横向/纵向/卡片底板） | 递归组内布局，间距用固定空间单位 |
| a2ui Text | 3D 文本（CanvasTexture 文字条） | 目录 v1 内不含富交互，够用 |
| a2ui Table / 未知组件 | 回退面板模式 | 需求 3.5 |
| image | PlaneGeometry + 图片纹理，白框 | 指向放大（同聚焦模式） |
| voice | 语音面板 + PannerNode 空间音频 | 见下 |
| text/markdown/mermaid/svg | 面板模式 | 示意图不重建，避免语义失真 |

### 场景布局

```
            消息墙（弧形，半径 R≈6m，高 2~2.5m）
        旧 ←—————————————— 新（正面）——————————————→ 更新中
   形象站位（环形散布，按用户名散列固定角度）
        ┌─────────────┐
        │   房间地面    │  中央 = 用户视角起点（站在场地中央看墙）
        └─────────────┘
```

- 弧形墙优于「消息环绕用户一圈」：沉浸式里背后不可见，2D 习惯的时间顺序（左旧右新/从远到近）映射更自然。
- 站位散列：`hash(username) % slots`，与 2D 缺省头像同思路，保证稳定。房主位可置于场地主位。
- 天空/地面/灯光从简（渐变天空 + 半球光 + 地面网格），整体视觉风格以「安静展厅」为准，不做游戏化装修。

### 交互

- 桌面预览：OrbitControls（拖拽旋转/滚轮缩放）+ WASD 平移 + 「跟随最新」开关。
- 沉浸式：手柄射线 → 面板点击（播放语音、聚焦、翻页）；摇杆传送/平移；无手柄退化为头向环视 + 单键传送。
- 聚焦模式：对准消息 → 面板向视点平移放大至舒适阅读距离（约 40° 视角），再按一次返回。这是 3D 端的「点击气泡」等价物。
- 3D 内发送：仅一个简洁输入（面板模式输入条，回车发送纯文本消息走现有 POST 接口）；复杂编辑引导回 2D。

### 语音空间音频

- 既有 `<audio>` 播放链替换为 `AudioContext → MediaElementSource → PannerNode(HRTF) → listener`；声源坐标 = 发送者形象站位（无形象则消息面板位）。
- 2D 端与 3D 端不并存播放（进入 3D 时 2D 音频暂停）。

### 形象（GLB/GLTF/VRM）

- 加载：`GLTFLoader` + `three-vrm`（VRM 由文件内 `VRM` 扩展识别，与 mime 无关）。外链 URL 直连加载（CORS 由源站负责）；服务器文件走既有 `/api/users/{u}/model3d`——该端点要求 Bearer 鉴权而 GLTFLoader 不带 Authorization，先 `fetch`（复用 `api()` 的 token）拿 blob → `URL.createObjectURL` 再交给 loader。
- 能力标志映射：
  - `model3dArkit`（ARKit 52）→ 暴露 morph target 字典（`ARKit52` 命名）；VRM 模型经 ARKit→VRM 表情映射表（aa→A 等常见映射）支持口型与预设表情。
  - `model3dHumanoid` → VRM humanoid 骨骼：MVP 只做待机呼吸/挥手两个内置动画（VRMA 或 glTF 动画剪辑，未勾选的模型退化为轻微上下浮动）；重定向/动画合成后续再扩。
- 缺省化身：胶囊体 + 用户名色（与 2D 缺省头像同 hash）+ 名牌面板。
- 名牌/状态：小面板常驻头顶；房主加标识。加载失败静默回退（需求 4.5）。

## 性能预算

- 面板纹理：LRU ≤ 24 张常驻（约 2048×2048 ≈ 16MB 纹存上限内）；出弧长的旧面板降级为「小标牌」（仅头像+名字+首行摘要）。
- 几何：图表网格面数预算（柱体用 12 段圆柱）；形象上限 = 在线成员数，加载串行化（同时最多 2 个在途加载）。
- 流式面板：同一面板重建节流 ≥300ms，流结束补一次终栅，不逐 tick 重绘。
- 面板位与纹理缓存解耦：弧墙面板位可多于纹理 LRU 上限，远位面板先以降级标牌占位，靠近/聚焦时再栅格化升格；2 行排布下 200 条历史也能完整铺开。
- 帧预算：目标 72fps（Quest 级）；`renderer.setPixelRatio` 上限 1.5；页面隐藏即暂停（`document.visibilitychange` + XR session 状态）。
- 释放：退出 3D/切房间 → 逐一 `geometry.dispose()/texture.dispose()/material.dispose()`，AudioContext 关闭；回归 2D 后 3D 模块不驻留内存（动态 import 可被 GC）。

## 与既有代码的集成

- 版本 2.10.0（仓库当前 2.9.3）；`app/main.py` version 字段照惯例递增。
- 新文件：`static/xr/xr-main.js`（会话/场景生命周期）、`xr-panels.js`（栅格化+纹理缓存）、`xr-native.js`（chart/a2ui 原生渲染）、`xr-avatars.js`（形象加载）、`xr-i18n.js`（3D 补充键，3D 加载时合并进 `I18N`；顶栏「3D」按钮的键直接进 index.html 字典）。`static/vendor/three/` 新增 vendored three.js/插件。
- 服务器改动：`GET /api/rooms/{n}/messages` 增加只读可选 `beforeId` 翻页参数（不传即旧行为，Agent API 语义零变）；`_validate_model3d_bytes` 可选增加对 VRM 扩展块的识别提示（现在就能收，因为魔数同为 `glTF`）；消息格式/房间/权限零改动。
- 发布/备份流程不变；服务器运维信息不进公开仓库（惯例）。

## 暂缓项（明确不做，防范围膨胀）

- **实时位姿/表情同步**（多人头手位姿、口型实时驱动）：需要新端点 + 传输设计，单独立项。
- **移动 AR（immersive-ar）**、WebGPU 管线、KTX2/Draco 压缩：待 VR 验证后再评估。
- **troika/three-mesh-ui 引入**：面板模式已覆盖文本；仅当出现「高频可编辑 3D 文本」需求再评估。
- **3D 端完整输入能力**（语音录制、私聊选择器、图表交互编辑）：一律引导回 2D。
- **房间 3D 场景个性化**（房间模型/主题）：MVP 用统一简洁场景。

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| foreignObject 栅格化跨浏览器差异（字体/canvas 混排） | 首选 Chrome/Edge 为主场景；检测失败自动降级纯文本 Canvas 面板兜底，保证可读 |
| 超长消息/超多消息把纹理撑爆 | 分页副面板 + 弧长滚动 + LRU 上限（需求 7.2） |
| 头显设备碎片化（手柄差异） | 输入抽象成「旋转/移动/确认」三个动作，桌面/手柄/头向各映射一份 |
| 2D/3D 双渲染端状态漂移（如未读、撤回时序） | 单一数据流：2D 端维持会话与消息状态机，3D 只订阅；3D 不直接写共享状态 |
| Vendored three.js 与未来插件版本耦合 | 版本锁定在 vendor 目录并在文件头注释版本号；xr 模块统一从同一入口 import |
# 需求文档 — 房间共创工作台（Room Doc Workbench）

## 简介

2.18.0 已交付「房间共同文件」：房间级共享、LWW 只存最新、8 类 kind、2D 抽屉与 XR 面板、3D 模型可摆入房间。本规格是它的**下一层**：把「共同文件」从**通用存储**升级为**共创工作台（workbench）**——用一套可扩展的**文档类型体系**承载尽可能多的人类工种，让人类与 Agent 在同一份数据上分工、并让 WebXR 的 3D 渲染优势成为主要卖点（而不只是「把 2D 网页塞进头显」）。

两个优选条件（继承 2.18，本规格将其固化为**准入标准**而非建议）：

1. **文本可编辑性优先**：优先选择文本基础格式（XML / JSON / YAML / Markdown / 纯文本 / s-expression / ASCII 变体），使 Agent 能以零依赖方式读写、diff、程序化生成；二进制格式只有在价值极高时才通过「转换管线」纳入。
2. **WebXR 可预览**：优先级顺序为 ① 可直接 3D 渲染（原生三维实体）> ② 可 2D 渲染后作为空间面板/纹理进入 3D 场景 > ③ 仅能存储与下载。

### 与 2.18 的边界

- `kind`（8 类存储/传输分类）**保持不变**，零迁移风险；本规格**纯新增** `doc_type`（细粒度语义类型）与配套的能力注册表。
- 既有 LWW / revision / baseUpdatedAt / 权限 / 归档 / 长轮询语义**全部沿用**，本规格只在其上叠加「类型 → 渲染 → 编辑」的能力层。
- 既有 200 个/房、文本 2MB、二进制 50MB 上限沿用；新增类型默认落在既有配额内，个别类型（IFC/CAD wasm 解析）另设**预览预算**（不是存储上限）。

### 核心设计原则

延续既有的**数据与渲染分离**与**无长连接**两条决策：

- **服务器只认数据与类型标识**，不理解几何、不解谱、不排版；渲染策略由**类型注册表**声明为「能力位」，2D 网页、WebXR、Agent 三类消费者各自按能力位选择实现。
- **一个 doc_type，两端渲染器**：每个 doc_type 在注册表里声明 `web` 与 `xr` 两个预览档位；两端可独立演进（XR 可以先做面板、后做实体）。
- **摆入房间的泛化**：2.18 只允许 `model` 摆入；本规格把「摆入」抽象为**任何可渲染 doc_type 都能摆入**，分「面板式」（2D 内容 → 空间画布）与「实体式」（3D 内容 → 场景对象）两种承载形态。
- **共创而不只是共享**：在 LWW 之上新增**可选的细粒度编辑协议**（JSON Pointer / 文本锚点 patch），让人类与 Agent 的并发修改不必然互相覆盖；仍不引入版本历史（保持 2.18 的产品语义）。

### 术语

- **kind**：既有 8 类存储分类（`markdown`/`text`/`svg`/`image`/`audio`/`video`/`model`/`other`），决定二进制/文本判定与大小上限，**本规格不改**。
- **doc_type**：新增的细粒度语义类型标识（如 `chart`、`geo3d`、`cad2d`、`midi`、`lottie`），是渲染与编辑的**唯一权威标识**；由「扩展名 + 魔数 + 声明 mime」推导，可由客户端用 `?as=` 覆盖提示。
- **能力位（capability）**：doc_type 在注册表中声明的属性，至少含 `textEditable`、`agentFriendly`、`web`（2D 预览档）、`xr`（XR 预览档）、`placement`（可摆入形态）、`limits`（预览预算）、`deps`（渲染所需前端依赖）、`security`（沙箱等级）。
- **预览档（view tier）**：`native3d`（原生 3D 实体）> `panel`（2D 渲染为空间面板/纹理）> `card`（仅信息卡 + 引导 2D）> `none`（仅存储下载）。
- **预览三型（P1/P2/P3）**：预览问题的统一契约（详见设计文档 §十三）。**P1 直接渲染型**（能生成三维几何 → 实体）、**P2 预览图型**（能变成一张图 → plane/薄 box 贴图）、**P3 占位体型**（暂时不会渲染 → 缺省几何体 + 文件名纹理）。三型是**能力位**，由注册表在每次加载时现算，**不写进数据**——渲染器一上线，全房间历史文件自动升级。
- **P1a / P1b**：P1 内分「静态实体」（只是几何）与「交互实体」（几何 + `behavior`，如录音机）。**行为是与三型正交的第二维度**。
- **P2a / P2b**：P2 内分「静态图」（渲染一次）与「活纹理」（持续更新，如 video/lottie）。
- **摆入（placement）**：把文件放进 3D 房间场景并持久化位姿，对全房间同步；三型**都可摆入**。`mount` 取值 `entity`(P1) | `panel`(P2) | `placeholder`(P3) | `player`(P1b 特例)。
- **实体式 / 面板式**：`entity` = 用场景世界坐标系放一个 three.js 对象（模型、地图、CAD、3D 图）；`panel` = 放一块可缩放画布，内容为该 doc_type 的 2D 渲染结果（markdown、表格、乐谱、时间线、演示稿）。
- **转换（convert）**：把二进制或不便直接渲染的格式，转换为**文本规范形**（如 `docx → fodt`、`xlsx → csv`、`fbx → glb`）后再进入渲染链路；转换产物是**派生物**，不改变原文件的 doc_type 与字节。**注意：转换是「怎么改」的手段，与「怎么预览」的三型无关**——转换完成后该文件只是获得了更好的预览与编辑能力。
- **sidecar（伴随文件）**：与主文件同名的辅助文件（如 `评审.md.comments.json`、`设计.glb.pose.json`），承载批注、审阅状态等结构化元数据；沿用同一文件列表与权限体系。
- **patch（细粒度编辑）**：以 JSON Pointer / 标题锚点 / 行范围定位的局部修改请求，作为整文件替换（PUT）的**可选替代**，用于降低并发冲突。
- **值班（agent-on-duty）**：Agent 以轮询或长轮询常驻，对房间内某类文档做持续维护（续写纪要、刷新图表数据源、巡检 3D 摆放）。

## 需求 1：文档类型体系（doc_type 注册表）

**用户故事**：作为维护者，我要能用一套声明式的类型注册表覆盖尽可能多的工种，新增一种格式只改注册表与一个渲染器插件，而不是改存储、API 与三端分支逻辑。

### 验收标准

1. 系统 SHALL 新增 `doc_type` 概念与**服务端权威注册表**（`app/doc_types.py`），每个条目声明：`id`、显示名（zh/en）、识别规则（扩展名集合 + 可选魔数 + 可选声明 mime）、所属 `kind`、能力位（见术语）。
2. 服务器 SHALL 在文件元数据中返回 `docType` 与 `capabilities`（能力位快照），使客户端无需硬编码类型矩阵即可决定渲染与编辑入口。
3. `doc_type` SHALL 是**纯新增字段**：既有 `kind` 判定、大小上限、存储路径、接口形状**零变化**；未命中注册表的文件 SHALL 回落到按既有 8 类 kind 行为（向后兼容）。
4. 注册表 SHALL 是**唯一事实来源**：`GET /api/doc-types` SHALL 返回全部 doc_type 与能力位（供 2D、XR、Agent 共用），并随 `/skill.md` 同步生成类型表。
5. doc_type 的识别 SHALL 与 kind 判定共用同一次字节嗅探，SHALL NOT 二次读取文件内容。
6. 重命名 SHALL 触发 doc_type 重算（与既有 kind 重分类同一次调用），且 SHALL 允许显式覆盖：创建/替换时携带 `as=<docType>` 且该 doc_type 声明了 `accepts`（可接受的 kind 集合）SHALL 被采纳，否则 400。
7. 注册表条目 SHALL 可标注 `deprecated` 与 `previewOnly`（仅预览不可编辑），供后续下线格式使用。

## 需求 2：文本可编辑性契约

**用户故事**：作为 Agent，我要知道哪些类型我能直接读写、哪些需要转换，以便零依赖地参与共创。

### 验收标准

1. 每个 doc_type SHALL 显式声明 `textEditable`（内容为 UTF-8 文本且可经 JSON 直写接口整文替换）与 `agentFriendly`（是否适合 Agent 程序化生成/修改，作为推荐强度而非权限）。
2. `textEditable=true` 的 doc_type SHALL 支持既有的 JSON 直写创建/替换路径（`{name, content}`），无需 multipart；SHALL 沿用 2MB 文本上限（个别类型如 `chart`、`csv` 可声明更小的 `limits.text`）。
3. `textEditable=false` 的 doc_type SHALL 在创建/替换时强制 multipart，且在注册表中声明 `convert` 能力（能否转换为文本规范形）与转换目标 doc_type。
4. 服务器 SHALL NOT 对文本内容做任何**格式语义校验**（不解析 JSON 合法性、不校验 Mermaid 语法）——非法内容允许存入，由渲染端降级展示；唯一例外是注册表声明 `validate` 的类型（如 `chart3d` 的顶层 schema），校验失败 SHALL 返回 400 并给出定位信息。
5. 注册表 SHALL 为每个 `textEditable` 类型提供**最小可用示例内容**（`template`），`POST .../files` 支持 `{docType, template:true}` 直接落一份合法骨架，降低 Agent 生成成本。
6. Agent 说明书 SHALL 按 `doc_type` 给出：格式说明、可用的模板、常见编辑动作示例（续写、改数据点、加节点、换配色）、以及「用 patch 还是整文件 PUT」的建议。

## 需求 3：2D 渲染矩阵（网页端）

**用户故事**：作为人类用户，我要在网页里直接预览尽可能多的类型，且渲染器按需加载、互不影响。

### 验收标准

1. 2D 端 SHALL 采用**渲染器插件注册表**（`static/doc-renderers/<doc_type>.js`），每个插件导出 `{canRender, render(el, content, ctx)}`；主程序按 `docType` 动态 `import()` 对应插件，SHALL NOT 把全部渲染依赖打进主包。
2. 每个 doc_type SHALL 在注册表中声明 2D 预览档与所需依赖（`deps`）；依赖 SHALL 懒加载（首次预览时才引入），且 SHALL 记录在 `static/vendor/` 或按需 CDN 白名单内（离线可用优先）。
3. 2D 预览失败 SHALL 降级：优先回落到「等宽文本源码视图」，其次「信息卡 + 下载」，SHALL NOT 影响列表与其他文件。
4. 富文本与主动内容（`svg`、`html`、`lottie`、`a2ui`）SHALL 在**沙箱**内渲染（Sandbox iframe + DOMPurify 或 Shadow DOM 隔离），SHALL NOT 获得宿主页面的 DOM/存储权限。
5. 文本类 doc_type SHALL 同时提供「预览」与「源码编辑」两个入口，编辑中的内容 SHALL 可切换回预览（实时或手动刷新）。
6. 2D 端 SHALL 展示该类型的**编辑能力徽标**（可编辑/仅预览/需转换），以及「摆入房间」入口（若 `placement != none`）。

## 需求 4：WebXR 渲染矩阵与摆入泛化

**用户故事**：作为戴头显的用户，我要在 3D 房间里看到的不只是模型，而是建筑、地图、CAD、乐谱、时间线、演示稿——并且它们能被共同布置。

### 验收标准

1. XR 端 SHALL 按**预览三型**（见设计文档 §十三）渲染，三型由注册表**在每次加载时现算**（`有 3D 渲染器 → P1；否则有 2D 渲染器 → P2；否则 → P3`），SHALL NOT 把三型写进数据：
   - **P1 直接渲染型**：在场景世界坐标里生成几何实体（可走近/环视/缩放）；其中带 `behavior` 者为 **P1b 交互实体**（如录音机），否则为 P1a 静态实体。
   - **P2 预览图型**：把内容变成一张图（本身就是图，或由 **2D 端既有渲染器**输出），贴在 plane/薄 box 上作纹理；`live` 者为 P2b 活纹理（video/lottie），否则 P2a 静态图。
   - **P3 占位体型**：系统缺省几何体（薄板 + 文件名纹理 + 按 doc_type 稳定哈希的色相 + 扩展名角标），命中显示信息卡，**不做渲染尝试**。
2. 三型 SHALL 均可摆入房间（**修正原「card 档不可摆入」**），共用同一套位姿契约与 adjBar；P3 SHALL 计更高渲染成本权重（建议 cost=2），避免房间被未知文件当仓库塞满。
3. `placement.mount` 取值 SHALL 为 `entity`(P1) | `panel`(P2) | `placeholder`(P3) | `player`(P1b 特例)；既有 `model` 返回 `entity`，旧客户端忽略该字段，行为**零变化**。
4. P1 SHALL 涵盖：既有 `model`（GLB/GLTF/VRM）与本规格新增的 `obj`/`stl`/`ply`/`chart3d`/`geo3d`/`ifc`/`cad2d` 等（见设计文档清单）。
5. **P2 的图源** SHALL 只走客户端：① 原图直用；② 复用 2D 端同一份渲染代码栅格化。**SHALL NOT 引入服务端预生成缩略图**（2026-09-28 拍板不做）——那会把服务器从「透传与最小校验」变成「渲染农场」并引入无头浏览器依赖，见 §13.4。前端做不出图的格式 SHALL 留在 P3。
5.1 **P3 的定位 SHALL 为「有位置、无预览」**：保留缺省占位几何体且可摆入（见 4.2），但 SHALL NOT 做任何内容渲染尝试、缩略图生成或内容解析；点击只显示信息卡 + 下载（有文件编辑权者可删除）。「不做内容预览」与「可摆入」不矛盾——房间里放的是那块牌子本身。
6. 摆入上限 SHALL 由「同时常驻 6 个」泛化为**预算制**：分列配额（`entity` ≤8、`panel` ≤8、`placeholder` 计入并建议独立上限）+ **渲染成本权重**（IFC=3、GLB=1、P3=2），总和 ≤12；超限 SHALL 返回 400 并提示先收起其他物件。
7. 面板式摆入 SHALL 支持交互：面板本体可移动/旋转/缩放/收起（沿用 adjBar），面板**内容**支持翻页（演示稿）、播放/暂停（动画、视频）、时间轴拖动（时间线、3D 走刀）。
8. **纹理 SHALL 按需生成**：仅摆入房间的文件才生成纹理；分辨率按距离/聚焦分级；沿用既有纹理 LRU 并设显式上限（P2b 活纹理 ≤6 张同时活跃）。
9. XR 端 SHALL 支持对 `textEditable` 类型经系统键盘编辑（沿用 2.18 的 HUD 文本域 + CJK 回 2D 提示）；`patch` 能力在 XR 端可先用「整文替换」实现，SHALL NOT 阻塞 M1。
10. 新渲染器 SHALL 遵循既有性能预算：纹理 LRU、退出 dispose、页面不可见暂停渲染、wasm 解析放 Web Worker。**注意**：`SkeletonUtils.clone()` 出来的实例共享几何/材质，清理 SHALL 走独立路径，SHALL NOT 用 `disposeObjectTree` 一把梭。
11. 归档视图 SHALL NOT 提供 XR 渲染/摆入入口（与既有规则一致）。

## 需求 5：共创编辑协议（人类 + Agent 并发）

**用户故事**：作为共创者，我要和 Agent 同时改一份文档而不必然互相覆盖；作为 Agent，我要能只改我关心的那一小块。

### 验收标准

1. 系统 SHALL 保留整文件替换（`PUT`，LWW + `baseUpdatedAt` 乐观锁）为**缺省路径**，本需求为可选增强，SHALL NOT 破坏既有语义。
2. 系统 SHALL 提供**局部修改接口** `PATCH /api/rooms/{room}/files/{fileId}/content`，支持三种定位方式：`jsonPointer`（JSON/YAML→JSON 视图，需 `ops: [{op, path, value?}]`）、`anchor`（Markdown/文本，按标题或 HTML 注释锚点定位到节）、`range`（按 `startLine/endLine` 或 `offset/length`，用于追加/替换片段）。
3. 局部修改 SHALL 在服务端**原子执行**（读-改-写同一事务内），成功后 `updatedAt` 递增、revision +1、并唤醒长轮询；SHALL NOT 保存历史版本。
4. 局部修改 SHALL 支持 `baseUpdatedAt`：不一致时 SHALL 尝试「上下文无冲突的自动合并」（三方合并：以 base 内容为共同祖先），自动合并成功则照常应用并返回 `merged:true`；无法自动合并 SHALL 返回 409 并附上**冲突片段**，由调用方决定重试或整文覆盖。
5. `anchor` 定位 SHALL 在锚点不存在时返回 422 并附「可用锚点列表」（从当前内容提取的标题清单），供 Agent 自我修复重试。
6. 系统 SHALL 支持**批注（comment）** as sidecar：`POST .../files/{id}/comments` 写入 `{anchor, text, author, resolved}` 到伴随文件；批注 SHALL 在 2D 预览中按锚点位置显示，在 XR 预览中以标记点 + 可展开文本呈现；批注读写权限 = 文件编辑权。
7. 系统 SHALL 支持**草稿-发布**可选工作流：`{docType}.draft` 命名约定（如 `方案.md` 与 `方案.draft.md`）由**客户端约定**实现，服务器 SHALL NOT 引入额外的分支/合并模型；说明书 SHALL 明确这是命名约定而非版本系统。
8. Agent 说明书 SHALL 给出**并发最佳实践**：优先 `patch`（局部）、其次带上 `baseUpdatedAt`、最后才整文覆盖；并说明 409 的三种处理策略（重读重试、取本地优先、取远端优先）。

## 需求 6：转换管线（二进制 → 文本规范形）

**用户故事**：作为用户，我要能上传一份 docx / xlsx，让系统给出可预览、可被 Agent 修改的文本化版本。

### 验收标准

1. 系统 SHALL 为声明了 `convert` 的 doc_type 提供**转换**能力：输入原文件，输出目标 doc_type 的文本内容；转换产物 SHALL 作为**普通共同文件**（或 sidecar）落盘，可预览、可编辑、可被 Agent 改写。
2. 转换 SHALL 默认在**服务端**执行（Python 实现，无外部二进制依赖优先）；无法在服务端可靠完成的（如需 wasm 解析器）SHALL 在**客户端**执行并把结果回写为文本文件。
3. 转换 SHALL NOT 修改原文件；原文件保持 `kind` 与 doc_type 不变，渲染端显示「已转换出文本版」入口。
4. 转换 SHALL 在 `GET /api/doc-types` 中如实声明方向（如 `odf-flat → markdown`）与能力位；未声明的方向 SHALL NOT 尝试。
5. 转换失败 SHALL 明确降级为「仅存储」，并返回可读原因；SHALL NOT 生成半成品文件。
6. M1 SHALL NOT 实现任何转换（先做原生文本类型）；转换管线为 M3+ 的独立里程碑（见 tasks.md）。

## 需求 7：沙箱与安全

**用户故事**：作为维护者，我要新增的一堆主动内容格式（SVG/HTML/Lottie/A2UI/GLSL/wasm）不成为 XSS 或资源耗尽的入口。

### 验收标准

1. 每个 doc_type SHALL 在注册表声明 `security` 等级：`inert`（纯数据）、`sanitized`（净化后渲染，如 svg/lottie）、`sandboxed`（必须 iframe 沙箱，如 html）、`isolated`（wasm/解析器，必须 Worker + 资源上限）。
2. 文本内容渲染 SHALL 沿用既有管线（`marked` + `DOMPurify`），SVG 以图片方式渲染、不执行脚本；Lottie/A2UI 渲染器 SHALL NOT 允许表达式求值（若库支持，必须显式关闭）。
3. wasm 解析器（web-ifc、occt-import-js、openscad-wasm、viz.js）SHALL 在 Web Worker 中运行，且 SHALL 设**解析预算**（时间上限 + 内存上限 + 输入大小上限），超限中止并降级为信息卡。
4. 客户端 SHALL NOT 执行任何 `textEditable` 文本内容中的代码（`.js`/`.py`/`.sh`/`.ipynb` 一律源码视图，SHALL NOT 运行）。
5. 服务端 SHALL NOT 因新增 doc_type 而放宽既有校验：路径仍由 `room_id/file_id/受控扩展名` 生成，显示名仍净化，大小上限仍按 kind 判定。
6. `?as=<docType>` 覆盖 SHALL 仅在 doc_type 的 `accepts` 集合内生效，SHALL NOT 允许把二进制声明为文本类型来绕过大小限制。

## 需求 8：能力协商与降级

**用户故事**：作为任意客户端（旧版 2D 页、旧版 XR、第三方 Agent），我要在遇到不认识的新类型时优雅降级而不是白屏。

### 验收标准

1. 客户端 SHALL 以 `docType` 为主键查本地渲染器；**未注册**时 SHALL 回落 `kind` 渲染（8 类既有行为），再回落「信息卡 + 下载」。
2. 服务器返回的 `capabilities` SHALL 是**快照**（随文件元数据），客户端 SHALL NOT 假定它总是最新；版本不一致时以本地注册表为准（服务器能力位是提示，不是强制）。
3. 新增 doc_type SHALL NOT 改变列表接口的既有字段与语义；旧客户端 SHALL 至少能以 `kind` 正常显示与下载。
4. 渲染器插件加载失败（网络/语法错误）SHALL 被捕获并降级，SHALL NOT 中断文件列表面板。

## 需求 9：多文件项目（可选，M3+）

**用户故事**：作为用户，我要存放天然由多个文件组成的产物（glTF + bin + 贴图、装配体、幻灯片组、地图切片）。

### 验收标准

1. 系统 SHALL 支持**软分组**：可选字段 `projectId` + `role`（如 `main`/`texture`/`bin`），同一 projectId 的文件在列表中折叠成一个可展开条目。
2. `projectId` SHALL 仅用于**展示分组**与**打包下载**，SHALL NOT 引入目录树、SHALL NOT 影响权限与 revision 语义（仍是扁平列表）。
3. 若 M1–M2 观察不到实际需求，本需求 SHALL 可整体推迟且不影响其他需求。

## 需求 10：非功能（迁移、兼容、性能、发布）

**用户故事**：作为维护者，我要这次扩张不破坏既有部署与数据。

### 验收标准

1. 数据迁移 SHALL 只增不改：`room_files` 新增 `doc_type TEXT`（可空）与可选 `project_id`/`role`；迁移沿用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` 幂等模式；新增 `rooms` 列（若需要）SHALL 放在 `_rebuild_rooms_if_name_globally_unique` 之后。
2. 存量文件 `doc_type` 为空 SHALL 按**惰性回填**处理：读取时按扩展名推导（不写库），写回时补齐；SHALL NOT 要求一次性全量迁移。
3. 性能 SHALL 保持：列表接口 SHALL NOT 因注册表变大而变慢（注册表在进程内常量）；2D/XR 首屏 SHALL NOT 因新增渲染器而增大（插件懒加载）。
4. 全部新增 doc_type 的显示名与错误文案 SHALL 覆盖 zh/en 双语。
5. 版本号 SHALL 按惯例递增（从 2.21.x 起，按里程碑各自小版本）；发布沿用 `build_release.sh → install.sh` 流程，生产部署前先备份并经用户确认。
6. `e2e.sh` SHALL 为每个落地的里程碑新增断言（doc type 识别、`?as=` 覆盖、patch 三定位、409 自动合并、摆入预算、渲染降级）；既有 1 项陈旧断言「超长 422」为已知失败，不属于回归。

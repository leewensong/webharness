# 需求文档 — 房间共同文件（Room Shared Files）

## 简介

为每个房间新增一份**共同文件**列表：房间成员（人类与 Agent）共享的一组「最新状态」文件。典型用途：会议纪要、任务清单、脑图/流程图、架构设计 GLB、界面设计图、演示视频——聊天消息是过程流，共同文件是房间的**当前结果**，两者互补。

核心语义（用户已拍板，不再讨论）：

- **房间级共享**：文件属于房间，不属于任何个人；每个房间一份列表，初始为空。
- **成员默认可读写**：人类与 Agent 缺省都有创建/修改权限；有管理权限的用户/Agent（房主与 roomAgent，即既有「治理者」概念）可以更改设置收紧。
- **只存最新（Last-Write-Wins）**：任何成员替换文件后，大家看到的就是最新内容；**不保存历史变更版本**，旧内容即刻消失。
- **类型不受限制**：理论上任意类型都可存；但推荐入选标准有两条——① 方便实时预览（WebXR 内可直接查看/渲染，或可转换后渲染）；② 方便修改（基于文本的 XML / JSON / Markdown 等基础格式，可程序化读写、可 diff）。
- **内容路由**：聊天消息（含富文本：Mermaid / SVG / chart / a2ui）承载**一次性表达**；**会持续修改或调整**的内容做成共同文件；**3D 内容（GLB/GLTF/VRM）一律创建与修改于共同文件**，避免聊天记录的多媒体冗余、只保留最新状态。

设计原则（延续既有架构决策，见 `.kiro/specs/webharness/design.md` 与 `docs/generative-ui.md`）：**数据与渲染分离**。共同文件只存「数据（文件字节）+ 类型标识（kind/mime）」，不存任何渲染样式；2D 网页、WebXR 3D、Agent 各自按类型选择预览方式或直接处理数据。延续「无长连接」架构：全部走短 HTTP + 轮询（复用既有长轮询唤醒机制），Agent 零安装接入（同一套 Bearer token API）。

### 术语

- **共同文件（room file）**：房间文件列表中的一项，由服务器分配 `fileId`，带显示名 `name`、种类 `kind`、内容与元数据。
- **文件种类（kind）**：服务器按「扩展名 + 声明 mime + 魔数」归类的预览策略分类，共 8 类：`markdown`、`text`、`image`、`svg`、`audio`、`video`、`model`、`other`（详见设计文档）。
- **文本类文件**：kind ∈ {markdown, text, svg} 的文件，内容是 UTF-8 文本，可通过 JSON 接口直接创建/替换（Agent 零依赖写入），也可在 2D 与 XR 内直接编辑。
- **治理者**：房主或房间管理 Agent（roomAgent），既有概念（`_is_room_governor`），本规格中「有管理权限的用户/Agent」即指治理者。
- **文件编辑权**：成员级权限 `canEditFiles` 与房间级开关 `filesLocked` 的合成结果（缺省所有人可编辑）。
- **revision（修订号）**：每房间一个递增计数器，任何文件列表相关变更（创建/替换/重命名/删除/锁定）都会 +1；客户端用它做增量感知。
- **LWW**：Last-Write-Wins，最后写入者覆盖一切；本规格不提供版本历史、diff、回滚。

## 需求 1：文件列表（房间级、缺省为空、成员可见）

**用户故事**：作为房间成员（人类或 Agent），我要看到本房间的全部共同文件及其元数据，以便了解房间当前的工作产物。

### 验收标准

1. WHEN 房间创建 THEN 系统 SHALL 自动提供一份空的共同文件列表，无需任何显式初始化。
2. WHEN 已加入房间的成员请求文件列表 THEN 系统 SHALL 返回全部文件元数据（id、name、kind、mime、size、description、createdBy、createdAt、updatedBy、updatedAt、contentUrl）与房间当前 revision。
3. WHEN 非成员请求文件列表或内容 THEN 系统 SHALL 拒绝（403）；归档房间的文件经归档接口只读访问（需求 11）。
4. 文件可见性 SHALL 与聊天权限正交：`can_view_history=0` 只约束聊天历史，SHALL NOT 约束共同文件——任何成员可见全部共同文件（共同文件是「当前状态」而非「历史记录」）。
5. 列表 SHALL 一次返回全部（单房上限 200 个文件，见需求 2；不提供分页）。

## 需求 2：创建与替换（LWW，无历史版本）

**用户故事**：作为成员，我要创建新文件、用新内容整体替换旧文件；替换后所有人立即看到最新内容，系统不保留旧版本。

### 验收标准

1. WHEN 成员创建文件 THEN 系统 SHALL 支持两种方式：文本类用 JSON `{name, content, description?}` 直写；任意类型用 multipart（`file` + `name` + `description?`）。二进制内容（图片/音视频/GLB 等）SHALL NOT 走 JSON 文本接口（按扩展名判定为非文本类时返回 400）。
2. 文件名 SHALL 长 1–128 字符、去除路径分量与控制字符、同一房间内唯一（不区分大小写）；重名创建 SHALL 返回 409。
3. WHEN 文件大小超限 THEN 系统 SHALL 拒绝：文本类 ≤2MB、二进制 ≤50MB（413）；单房文件数超过 200 THEN SHALL 拒绝（400，提示上限）。
4. WHEN 成员替换文件内容（按 fileId 整体替换）THEN 系统 SHALL 用新内容原子覆盖旧内容，SHALL NOT 保留任何历史版本或备份；kind/mime/size/updatedBy/updatedAt SHALL 按新内容重新识别与记录。
5. WHEN 替换请求携带 `baseUpdatedAt` 且与服务器当前值不一致 THEN 系统 SHALL 返回 409（文件已被他人更新），防止静默覆盖；不带 `baseUpdatedAt` THEN SHALL 按 LWW 直接覆盖。
6. 替换 SHALL NOT 改变 fileId 与显示名；改名走重命名接口（需求 3）。
7. WHEN 创建或替换成功 THEN 系统 SHALL 递增房间 revision 并唤醒该房间的长轮询等待者（复用既有 `notify_room` 机制）。
8. 创建/替换权限 = 文件编辑权（需求 5）；与发言权 `can_speak`、附件权 `can_upload`、全体禁言 `muted` 完全无关（聊天附件是消息流的一部分，共同文件是共享状态，两套权限不混用）。
9. WHEN 房间已归档 THEN 一切写操作 SHALL 被拒绝（410/404）。

## 需求 3：重命名、描述与删除

**用户故事**：作为成员，我要给文件改名、补充说明、删除不需要的文件，让列表保持整洁准确。

### 验收标准

1. 成员 SHALL 可按 fileId 重命名（新名遵守需求 2.2 规则）与修改 `description`（≤500 字）；重命名 SHALL NOT 影响内容与 fileId；contentUrl 按 id 生成，天然不受改名影响。
2. WHEN 新名与房内其他文件冲突 THEN 系统 SHALL 返回 409；成功改名与改描述 SHALL 递增 revision。
3. WHEN 成员删除文件 THEN 系统 SHALL 删除其元数据与磁盘内容文件，并递增 revision；重复删除返回 404（幂等语义：第二次已不存在）。
4. 重命名/删除权限 = 文件编辑权（需求 5）。
5. `description` SHALL 在列表中返回，用于说明文件用途（可选字段，缺省为空）。

## 需求 4：文件类型与预览策略

**用户故事**：作为成员，我要在 2D 网页与 WebXR 房间里直接查看文件内容；类型不限，但推荐的类型应能「存进去就看得见」。

### 验收标准

1. 文件类型 SHALL 不受限制：任意扩展名/mime 均可存入；无法识别的归入 `other`，仅存储与下载，不做预览。
2. 服务器 SHALL 按「扩展名 + 声明 mime + 魔数」把每个文件归入 8 类 kind 之一：`markdown`（.md/.markdown）、`text`（.txt/.json/.xml/.yaml/.csv/.html/.mermaid/.drawio 及各类代码等文本基础格式）、`image`（png/jpg/gif/webp/bmp）、`svg`（文本基础格式 + 图片式渲染的双重性质，独立成类）、`audio`、`video`（mp4/webm/mov）、`model`（GLB/GLTF/VRM，魔数即 glTF 家族）、`other`（其余全部）。
3. 官方推荐 SHALL 写入文档：优先选择「文本基础格式」（便于 diff 与程序化修改）与「WebXR 可渲染或可转换渲染」（便于房间内实时预览）；这是推荐不是强制。
4. 2D 网页预览矩阵 SHALL 为：markdown → Markdown 渲染（沿用消息富文本管线，含消毒）；text → 等宽文本（.mermaid/.mmd 走 Mermaid 图渲染）；image → 内联图片；svg → 以图片方式内联（SHALL NOT 内联执行其脚本）；audio/video → 播放器；model → GLB 取景缩略图预览 + 下载（复用既有 3D 文件卡片缩略图管线）；other → 仅下载。
5. WebXR 预览矩阵 SHALL 为：markdown → 富文本渲染栅格化面板；text → 文本面板（复用既有滚动/超长折叠机制）；image/svg → 纹理平面（可走近查看）；video → VideoTexture 平面（可播放/暂停）；model → 「摆入房间」常驻展示（需求 10）或「临时预览」（复用既有聊天 3D 模型附件的放置管线，本地不写状态、≤6 LRU、关面板即收起）；audio/other → 信息卡 + 引导到 2D 网页。
6. WHEN 任一类型预览/渲染失败 THEN 客户端 SHALL 降级（文本视图或占位卡）并提示，SHALL NOT 影响列表与其余文件。
7. 所有预览取内容 SHALL 经 Bearer 鉴权拉取（沿用 blob 方案：fetch → objectURL/纹理，不走免鉴权直链）。
8. 3D 内容（GLB/GLTF/VRM）SHALL 一律在共同文件中创建与修改：Agent 说明书 SHALL 把它列为硬性行为指引；人类 UI 在上传 3D 类型的聊天附件时 SHALL 给出「建议存入共同文件」的提示（不强制阻断，既有聊天 3D 附件功能保持兼容）。

## 需求 5：权限与治理

**用户故事**：作为房主/roomAgent，我要在缺省「人人可编辑」的基础上，按需收紧某些成员或整个房间的文件编辑权。

### 验收标准

1. 缺省 SHALL 为：全体成员（人类与 Agent）均可创建/替换/重命名/删除共同文件（新成员加入即有编辑权）。
2. 成员级权限 `canEditFiles` SHALL 加入既有成员权限体系（与 canSpeak/canUpload/canViewHistory 并列，默认允许），由治理者通过既有成员权限接口设置。
3. 房间级开关 `filesLocked` SHALL 加入既有房间设置（与 muted 并列，默认关闭）：开启后除治理者外任何成员不可写文件。
4. 治理者（房主与 roomAgent）SHALL 恒不受 `canEditFiles=0` 与 `filesLocked` 限制（与「房主不可被禁言」同一原则），且 SHALL 是唯一能更改这两项设置的角色。
5. 权限判定 SHALL 在服务端强制执行（403），2D、XR、Agent 三端同一标准；有效编辑权 SHALL 在房间详情中返回（`files.canEdit`），供客户端置灰入口。
6. 权限 SHALL NOT 影响读取：读文件列表与内容只要求成员身份（需求 1.4）。
7. `filesLocked` 变化 SHALL 递增 revision 并唤醒长轮询等待者。

## 需求 6：实时同步（轮询体系内）

**用户故事**：作为成员，当别人创建或修改了文件，我要在下一个轮询周期内看到列表变化；作为 Agent，我也要有低成本的感知手段。

### 验收标准

1. 房间 revision SHALL 在以下变更时 +1：创建、替换、重命名、改描述、删除、filesLocked 切换；房间详情 SHALL 返回 `files` 概要（revision/locked/canEdit/count）。
2. 文件列表接口 SHALL 支持增量长轮询：`sinceRevision` + `wait`（≤30 秒，复用既有 wait 模式与 `notify_room` 唤醒），revision 无变化时挂起等待，有变化或超时后返回当前列表与 revision。
3. WHEN 文件发生变更 THEN 已打开的 2D/XR 客户端 SHALL 在一个轮询周期（≤2 秒或被长轮询唤醒）内更新列表。
4. WHEN 正在预览的文件被他人替换 THEN 客户端 SHALL 依据该文件 `updatedAt` 变化提示或自动刷新到新内容（预览器以最新内容为准，无版本概念）。
5. 聊天消息流 SHALL NOT 因文件操作产生合成消息（v1 不做文件活动通知，见设计文档「暂缓项」）。

## 需求 7：2D Web UI

**用户故事**：作为人类用户，我要在网页里管理房间文件：浏览、上传、在线编辑、预览、下载、删除，房主还能调权限。

### 验收标准

1. 聊天顶栏 SHALL 新增「文件」入口；打开文件抽屉/弹窗：文件列表（种类图标、名称、大小、更新者、更新时间、描述）+ 操作（预览/下载/编辑/重命名/删除）+「上传文件」「新建文本」按钮 + 房间文件数与锁定状态展示。
2. 上传：文件选择器 + 名称（缺省取文件名）+ 可选描述；新建文本：名称 + 内容编辑器。
3. 文本类编辑 SHALL 用文本编辑器（textarea）完成，保存走替换接口并携带 `baseUpdatedAt`；WHEN 保存遇到 409 THEN SHALL 提示「文件已被他人更新」并提供**重载最新 / 强制覆盖**两个选择。
4. 预览 SHALL 按需求 4.4 矩阵实现；model 类 2D 端为缩略图预览（交互式查看在 XR 端）。
5. 治理者 SHALL 在「管理房间」弹窗看到文件锁定开关，在成员权限表看到 `canEditFiles` 列。
6. 文件抽屉打开期间 SHALL 以 ~2 秒轮询列表（revision 未变不重渲染）；关闭即停。
7. 全部新增文案 SHALL 覆盖 zh/en 双语（含服务端错误映射）；未授权操作按服务端 403 呈现提示。

## 需求 8：WebXR 3D 体验

**用户故事**：作为戴头显的人类用户，我要在 3D 房间里打开文件列表面板、查看各类文件，也能直接改文本文件——大屏协作才成立。

### 验收标准

1. 3D 端 SHALL 提供「文件」入口与文件列表面板（名称、种类图标、更新者与时间；点击打开预览），并随轮询同步列表（复用既有房间/消息轮询桥，不新建第二套会话）。
2. 预览 SHALL 按需求 4.5 矩阵实现：model 类放置进场景可环视（上限与释放遵循既有聊天模型放置的 LRU/dispose 规则）；image/video 平面支持既有聚焦/走近查看交互。
3. 3D 端 SHALL 支持文本类文件的编辑（经系统键盘输入，保存走替换接口 + baseUpdatedAt，冲突提示同需求 7.3）；WHEN 目标设备系统键盘不可用 THEN 降级为只读并引导到 2D。
4. 二进制文件上传与下载 SHALL 引导到 2D 网页完成（头显内不做文件选择器/下载器）。
5. WHEN 成员无文件编辑权 THEN 编辑/上传入口 SHALL 置灰或提示；列表与预览不受影响。
6. 新面板/纹理 SHALL 遵循既有性能预算（纹理 LRU、退出 dispose、页面不可见暂停渲染）。
7. 归档视图（viewingArchive）SHALL NOT 提供 3D 文件入口（与既有「归档内禁用 3D 入口」规则一致）。

## 需求 9：Agent API 与说明书

**用户故事**：作为 Agent，我要用同一套 HTTP API 读写房间共同文件——维护纪要、发布脑图/流程图、上传成果 GLB，并把这份能力写进 skill 说明书。

### 验收标准

1. 全部文件接口 SHALL 对人类与 Agent 同一标准（同一 Bearer token 体系，无 Agent 专用通道）；Agent 与人类的权限判定完全一致（需求 5）。
2. 既有 Agent API 语义 SHALL 零变化：本需求全部为新增端点/字段，旧客户端与旧脚本（inbox.py/watch.py）不受影响。
3. `GET /skill.md` SHALL 新增「共同文件」章节：接口表（列表/创建/替换/重命名/删除/内容下载/世界摆放 + sinceRevision/wait）、kind 矩阵与推荐类型、大小与数量上限、LWW 与 baseUpdatedAt 语义、内容路由规则（见下条）、典型工作流示例（Agent 持续维护 `会议纪要.md`、生成 `.mermaid` 脑图/流程图、上传评审用 GLB 并摆进房间、读取人类上传的设计图后回写反馈）。
4. Agent 对文件变更的感知 SHALL 可用「文件列表长轮询」实现；v1 不要求改造 inbox.py/watch.py（脚本扩展列为可选任务）。
5. OpenAPI（/docs）随端点自动更新；文档 SHALL 提示 Agent「先读列表拿到 fileId 与当前 updatedAt，再决定是否带 baseUpdatedAt 替换」。
6. 说明书 SHALL 写明**内容路由规则**：一次性表达（含 Mermaid / SVG / chart / a2ui 图）→ 聊天富文本消息（聊天本就支持画图等富文本，一次性内容留在消息流即可）；会持续修改或调整的内容（纪要、计划、脑图、流程图、设计稿）→ 共同文件；3D 内容（GLB/GLTF/VRM）→ **一律**共同文件（创建与修改都走文件，不作为新的聊天附件发布），避免聊天流多媒体冗余、只保留最新状态。

## 需求 10：3D 文件的世界摆放（WebXR 常驻展示与位姿记录）

**用户故事**：作为有文件编辑权的成员，我要把共同文件里的 3D 模型摆进 WebXR 房间场景——显示或关闭、移动/旋转/缩放——位姿记录在文件里并对全房间同步，房间的 3D 内容成为可共同布置的共享状态。

### 验收标准

1. kind=model 的共同文件 SHALL 支持「世界摆放」状态：是否在 XR 房间内常驻显示 + 位姿（position / rotation / scale），持久化在文件记录中；其他 kind 请求摆放 SHALL 拒绝（400）。
2. WHEN 文件编辑权持有者在 XR 内「摆入房间」THEN 系统 SHALL 以初始位姿写入并显示：初始 scale 按包围盒最大边 ~1m 归一化、位置取摆放者面前空位、朝向面对摆放者；此后该模型 SHALL 对房间内所有 XR 成员在同一坐标渲染。
3. WHEN 授权成员在 XR 内对已摆放模型移动、旋转、缩放或选择关闭/再显示 THEN 系统 SHALL 保存位姿并递增 revision、唤醒长轮询；其他客户端 SHALL 在一个轮询周期内同步。
4. 位姿 SHALL 以 JSON `{position:[x,y,z], rotation:[x,y,z]（Euler XYZ 弧度）, scale:[x,y,z]}` 存储，作用于模型原始尺寸的根节点；坐标系即房间 3D 场景世界坐标系（与 map3d 内置场景同一坐标系）；归一化只在首次摆放时由客户端换算成显式 scale 存入，跨端一致。
5. 同时常驻显示的已摆放模型 SHALL 有上限（20）；超限时开启 SHALL 被拒绝（400，提示先关闭其他模型）。
6. 摆放状态（可见性、位姿、更新者与时间）SHALL 在文件元数据 `world` 字段返回（未摆放为 null）；2D 端 SHALL 只读展示「已摆入房间」徽标（不做 2D 位姿编辑器）。
7. 摆放变更权限 SHALL 等于文件编辑权（需求 5）；filesLocked 锁定下摆放与调整 SHALL 同样被拒（403），读取与渲染不受影响。
8. 临时预览与常驻摆放 SHALL 是两个明确区分的动作：预览本地临时（不写状态、≤6 LRU、关面板即收起），摆入房间共享持久（写状态）；列表面板操作与文档 SHALL 明确区分两者。
9. Agent SHALL 可经同一 API 设置/调整摆放（如程序化编排房间布景）；归档房间 SHALL 禁止摆放变更，已摆放状态在归档中只读可见。
10. WHEN 房间 3D 场景切换（如内置会议室 ↔ 狼人杀）THEN 位姿按同一世界坐标系保留（可能不合新场景身位，成员可再调整），系统 SHALL NOT 自动清除位姿。

## 需求 11：归档、数据保留与非功能

**用户故事**：作为维护者，我要共同文件遵循既有数据与安全惯例：迁移只增幂等、数据不丢、权限不漏、发布流程不变。

### 验收标准

1. WHEN 房间归档 THEN 共同文件 SHALL 保留并可经归档接口只读访问（列表 + 内容下载）；一切写操作拒绝。归档 SHALL NOT 删除任何文件数据（无清理任务）。
2. 数据迁移 SHALL 沿用 db.py 既有模式（CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD COLUMN，幂等只增）；新增 rooms 列 SHALL 放在 rooms 表重建逻辑之后（既有迁移注释的约束）。
3. 安全：内容端点 SHALL 校验成员/归档访问身份；显示名净化后入库、磁盘路径 SHALL 仅由 room_id/fileId/受控扩展名生成、不含用户输入；文本预览消毒沿用既有管线（marked+DOMPurify），svg 以图片方式渲染不执行脚本；存储路径遍历不可达（内容按 fileId 寻址）。
4. `data/files/` SHALL 加入 .gitignore（与 data/uploads/ 同一模式）。
5. e2e.sh SHALL 新增共同文件全流程断言（创建/列表/替换/409 冲突/权限 403/世界摆放：model 摆放→返回可见位姿、非 model 400、上限 400、关闭、无编辑权 403/归档只读；注意既有 1 项陈旧断言「超长 422」为已知失败，不属于本需求回归）。
6. 版本号 SHALL 按惯例递增（当前 2.17.0 → 落地 **2.18.0**），发布沿用 build_release.sh → install.sh 流程（生产部署前先备份并经用户确认）。

## 需求 12：个人资源库/商店导入（下一阶段桥接）

> 本需求不改变本规格已经确定的房间级所有权、LWW 和成员权限；它定义个人资源进入 `room_files` 时的边界。资源库、审核、商店和积分的完整需求见 `.kiro/specs/friends-private-rooms-resource-marketplace/`。

**用户故事**：作为房间成员，我要选择自己拥有的个人资源或商店中已授权的资源，把它作为房间共同文件使用。

### 验收标准

1. WHEN 成员从资源库导入文件 THEN 系统 SHALL 将具体资源版本复制为一条新的 `room_file` 快照，并保留 `sourceResourceId`、`sourceResourceVersionId`、`sourceLicenseId`、`purchasedBy` 等 provenance 字段。
2. 私有资源 SHALL 只允许资源所有者/合法 Agent 控制者导入；已审核且上架的商店资源 SHALL 允许有 `canEditFiles` 且房间未锁定的成员选择，并按资源商店的积分授权规则处理。
3. 导入 SHALL 复用本规格的文件名净化、kind/mime/大小限制、房间文件名唯一、`files_revision` 和 `notify_room`；同名冲突必须在扣款前返回 409。
4. WHEN 同一购买者在同一房间重复选择同一资源版本 THEN 系统 SHALL 复用已存在的授权/文件且不得重复扣款；不同版本视为新的来源版本。
5. 导入后的 `room_file` SHALL 继续按照本规格的 LWW、成员 `canEditFiles`、`filesLocked`、世界摆放和归档只读规则工作；资源库更新、下架、拒绝或删除不得自动改写/删除房间快照。
6. WHEN 房间成员替换导入文件的内容 THEN 系统 SHALL 保留来源字段并标记 `sourceDetached=true`，表示当前字节已脱离原资源版本，但不抹掉归属和审计线索。
7. 房间成员只能看到来源的安全元数据；来源字段不得成为绕过房间文件权限或读取资源库原件的旁路。
8. 私聊房间的导入仍以房间成员资格为前提；好友/邀请门槛由 WebHarness 新规格判定，不能由共同文件接口自行放宽。

## 需求 13：共同文件一律可摆入 3D 房间（图片盒子 / 媒体播放器 / 多格式 3D 模型）

> 本需求扩展需求 4.5 与需求 10：世界摆放从「仅 kind=model」放宽到「一切可视媒体」。参考实现：媒体播放器交互参考 FPP（<https://github.com/FXGVR/FPP>，WebXR 内直接画进 framebuffer 的播放面板：播放/暂停、±10 秒、进度条拖动、静音、循环）；多格式 3D 加载参考本地 webxr-3d-viewer（three.js 0.186 各格式 Loader + 统一归一化）。

**用户故事**：作为房间成员，我上传到共同文件的图片、视频、音频和各种格式的 3D 模型，都应该能像 GLB 一样被摆进 3D 房间、被移动/旋转/缩放，而不必先转换格式。

### 验收标准

1. 可摆放 kind SHALL 扩展为 `model`、`image`、`svg`、`video`、`audio`（统称「世界对象」）；`markdown`/`text`/`other` 请求摆放仍 SHALL 返回 400。摆放接口、位姿格式、LWW、权限、归档只读、`visible:false` 保留位姿等语义与需求 10 完全一致，`world` 字段对上述 kind 一律返回。
2. **图片类缺省 3D 盒子**：image/svg 摆入后 SHALL 渲染为一块薄盒（相框）：正反两面贴图片、侧边为中性色边框，长宽按图片原始宽高比、最长边归一为 1（初始 scale 再由需求 10.2 的 ~1m 规则换算）；svg SHALL 位图化后贴图（不执行脚本）。操作（选中、移动、旋转、缩放、升降、收起、撤销）SHALL 与 GLB 模型完全相同。
3. **媒体类缺省播放器模型**：video/audio 摆入后 SHALL 渲染为一台播放器：上部屏幕（视频画面；音频显示封面卡：🎵 + 文件名）+ 外框，下部控制条（播放/暂停、后退 10 秒、前进 10 秒、静音、循环、进度条 + 当前/总时长）。
   - 点击控制条按钮/进度条 SHALL 直接执行播放控制（进度条点击即跳转），SHALL NOT 进入摆放调整；点击屏幕或外框 SHALL 进入调整（与模型一致）。
   - 播放状态（播放/暂停/进度/静音）SHALL 为**本地状态**，不写服务器、不跨端同步（同步播放列为暂缓项）；缺省静音、不自动播放（浏览器自动播放策略 + 避免多台播放器同时出声）。
   - 收起、删除、内容替换、退出 3D 时 SHALL 暂停并释放媒体元素与纹理。
4. **流媒体**：`.m3u8`（HLS 播放列表）SHALL 归入 kind=video（mime `application/vnd.apple.mpegurl`）；播放器 SHALL 优先用原生 HLS（Safari/visionOS），否则按需加载内置 hls.js；播放列表本身经 Bearer 鉴权取回，列表内引用的分片地址按原样访问（建议写绝对地址）。
5. **多格式 3D 模型**：kind=model SHALL 在 GLB/GLTF/VRM 之外识别 `.fbx .obj .stl .ply .3mf .usdz .usd .usda .usdc`（按扩展名；mime 用 `model/*` 细分），全部可在 XR 摆入房间与 2D 缩略图预览；加载器按需动态 import（不进 3D 首屏）。
   - 只能单文件自包含：OBJ 外挂 .mtl / FBX 外挂贴图缺失时 SHALL 以中性材质照常显示几何，不报错中断。
   - 无法线的几何 SHALL 自动计算法线；无面片的 PLY SHALL 以点云渲染（顶点色优先）。
   - 「临时预览」与聊天 3D 附件管线仍只接 glTF 家族；非 glTF 模型的临时预览入口 SHALL 隐藏（只提供摆入房间）。
6. **用途隔离**：3D 形象与房间场景 SHALL 仍只接受 glTF 家族（GLB/GLTF/VRM）——资源库里的 FBX/OBJ 等即便过审也 SHALL NOT 可被选为形象或场景（409 `resource_kind_mismatch`）。
7. 上限 SHALL 按类别分开计数（同时 `visible=true`）：3D 模型 ≤20、图片 ≤12、媒体播放器 ≤3；超限 400 并提示先收起同类对象。
8. 2D 端 SHALL 为所有已摆放 kind 显示「已摆入房间」徽标；model 缩略图 SHALL 支持新增格式。
9. Agent 说明书 SHALL 更新 kind 矩阵（新格式 + 可摆放 kind）与摆放接口说明；「3D 内容一律共同文件」规则覆盖新格式。

## 需求 14：视频播放器的投影 / 立体 / 片源切换（WebXR）

> 参考 FPP（<https://github.com/FXGVR/FPP>）：全景投影选择、左右/上下立体、面板上的播放控件与进度拖动。

**用户故事**：作为在 3D 房间里看视频的成员，我要根据自己的片源切换平面 / VR180 / VR360 投影和 2D / 左右 / 上下立体，并且能临时换一个视频地址，而不必重新上传文件。

### 验收标准

1. 视频播放器控制条 SHALL 提供三个额外按钮：投影（平面 → VR180 → VR360 循环）、立体（2D → 左右 → 上下 循环）、片源。
2. 投影：`平面` SHALL 用现有平面屏幕；`VR180` SHALL 用朝前的内视半球；`VR360` SHALL 用内视全球面（equirectangular UV）。球面 SHALL 不参与射线拾取（点球面不会进入摆放调整），球体半径按播放器本地单位约 2.4。
3. 球面跟随：观看者走进球心附近（距离 < 球半径）时球心 SHALL 跟随观看者头部、其朝向随观看者转向；离开后 SHALL 回到播放器位置、朝播放器正面。只有当前观看者受影响（本地渲染），不改变共享位姿。
4. 立体：`左右`（SBS）取画面左半给左眼、右半给右眼；`上下`（OU）取上半给左眼、下半给右眼。沉浸式下 SHALL 按眼分层渲染（three WebXR 的 layer 1=左眼、layer 2=右眼）；非沉浸式（桌面预览）SHALL 显示完整单画面。
5. 投影/立体/自定义片源 SHALL 是**本地偏好**：按「房间 + 文件」存 localStorage，不写服务器、不跨端同步；同房间其他人各有各的设置。
6. 片源：`片源` 按钮 SHALL 打开 HUD 输入（沉浸式走系统键盘），接受 `http(s)://` 或站内 `/` 开头的地址；留空或「恢复原片源」SHALL 回到文件自带内容。无效地址 SHALL 就地提示且不改变当前播放。`.m3u8` SHALL 自动走 HLS（原生优先，否则 hls.js；站内地址补 Bearer）。
7. 更换片源 SHALL 先暂停并释放旧片源（hls.destroy / revoke objectURL），再加载新片源；失败时提示并保留原片源可用。
8. 音频播放器 SHALL 只显示播放/进度/静音/循环/片源，不显示投影与立体按钮；投影/立体对音频无意义。
9. 播放状态与投影/立体均为本地状态，SHALL NOT 写入 `room_files`；文件内容被替换（updatedAt 变化）时按既有逻辑重建播放器，本地偏好保留。

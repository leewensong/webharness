# 实施计划 — 房间共同文件（Room Shared Files）

> 新会话从这里开始。按阶段推进：服务器先行（Agent 立即可用）→ 2D 全功能 → XR 体验 → 文档与发布。仓库当前版本 2.17.0（2026-09-24 scroll panel 发布），本规格落地版本 **2.18.0**。每个 Phase 结束可独立验证（e2e.sh / 浏览器 / 桌面 3D 预览）。

## Phase 1：服务器数据层与 API（完成后 Agent 即可全流程使用）

- [x] 1. 数据层与迁移
  - `app/db.py`：`CREATE TABLE IF NOT EXISTS room_files`（字段见设计文档，含世界摆放四列 `world_visible/world_pose/world_updated_by/world_updated_at`——表是本版本新建，直接进 CREATE TABLE，无需 ALTER）+ `idx_room_files_name`（room_id+name NOCASE 唯一）+ `idx_room_files_world`（`WHERE world_visible=1` 部分索引）；`FILES_DIR = DATA_DIR / "files"`（与 `UPLOADS_DIR` 并列，`init_db()` 里 `mkdir`）；`data/files/` 进 `.gitignore`
  - `_add_column_if_missing`：`rooms.files_locked INTEGER NOT NULL DEFAULT 0`、`rooms.files_revision INTEGER NOT NULL DEFAULT 0` —— **必须放在 `_rebuild_rooms_if_name_globally_unique` 之后**（rooms 重建只搬显式列）；`room_members.can_edit_files INTEGER NOT NULL DEFAULT 1`（与 can_speak/can_upload 同批）
  - 存量库直升：默认 0/0/1 = 缺省全员可编辑；不回填
  - _需求：1.1、5.1、5.2、5.3、11.2_

- [x] 2. 类型识别与校验 helper
  - `app/main.py` 常量：`MAX_FILE_TEXT_BYTES = 2MB`、`MAX_FILE_BYTES = 50MB`、`MAX_FILES_PER_ROOM = 200`、`MAX_FILE_DESCRIPTION_CHARS = 500`、`MAX_WORLD_MODELS = 6`、`TEXT_FILE_EXTS` 白名单、`VIDEO_EXTS`、`IMAGE_EXTS`（复用既有）
  - `_classify_file(name, declared_mime, data) -> (kind, mime)`：glTF 魔数（复用 `_gltf_mime`）→ 图片魔数 → `.svg` → `_audio_ext` → 视频（扩展名 + ftyp/webm 魔数）→ `.md/.markdown` → 文本扩展名/text 声明 → `other`；声明 mime 只在白名单命中时采信
  - `_validate_file_name(name)`：去路径分量、拒控制字符、1–128、非空（显示名允许中文与空格；仅入库与展示，不进磁盘路径）
  - _需求：2.2、4.2、11.3_

- [x] 3. 文件端点（列表与长轮询）
  - `GET /api/rooms/{room}/files`：`_require_membership`；响应 `{roomName, revision, files:[...]}`（`_room_files_dict`：id/name/kind/mime/size/description/createdBy/createdAt/updatedBy/updatedAt/contentUrl`?v=updatedAt`/world）；排序 `updated_at DESC, id ASC`
  - `sinceRevision` + `wait`（0–30，`MAX_LONG_POLL_SECONDS` 复用）长轮询：revision 未变则 `await asyncio.wait_for(_room_event(room_id).wait(), ...)` 循环至超时；任何文件变更 `notify_room(room_id)`（共享事件，消息活动会假唤醒，醒来只比对一次 revision，无害）
  - `GET /api/rooms/{room}/files/{fileId}` 单条元数据
  - _需求：1.2、1.5、6.1、6.2_

- [x] 4. 创建与替换（LWW）
  - `POST /api/rooms/{room}/files`：JSON `{name, content, description?}`（Pydantic `FileCreate`，content ≤2MB——按扩展名判定非文本类直接 400「请用 multipart 上传」）与 multipart `file`+`name`+`description?` 两条路径；`_check_file_edit_allowed`（权限合成见任务 5）→ 名字校验/唯一（409）→ 数量上限（400）→ 大小上限（413，文本/二进制两档）→ 落盘 `data/files/<room_id>/<file_id><ext>`（临时文件 + `os.replace`）→ 入库 → `files_revision+1` + `notify_room`
  - `PUT /api/rooms/{room}/files/{fileId}`：JSON `{content, baseUpdatedAt?}` 或 multipart `file`+`baseUpdatedAt?`；`baseUpdatedAt` 不符 409「文件已被 {updatedBy} 更新」；成功后 kind/mime/size/updated_by/updated_at 重算、原子覆盖、revision+1 + `notify_room`；不带 baseUpdatedAt = LWW 直接覆盖
  - _需求：2.1–2.9_

- [x] 5. 重命名 / 描述 / 删除 + 权限合成
  - `PATCH /api/rooms/{room}/files/{fileId}` `{name?, description?}`：改名重算 kind/mime（新扩展名 + 存量 mime，不重读字节）；重名 409；revision+1
  - `DELETE /api/rooms/{room}/files/{fileId}`：`unlink(missing_ok=True)`（失败照删行）+ 删行；二次 404；revision+1
  - `_check_file_edit_allowed(room, member, user_id)`：治理者放行 → `files_locked` 403「房间共同文件已锁定」→ `can_edit_files=0` 403「你已被禁止编辑房间文件」（镜像 `_check_action_allowed` 的 muted/can_speak 合成）；读取不进此判定
  - _需求：3.1–3.4、5.5、5.6_

- [x] 6. 内容下载与既有接口扩展
  - `GET /api/rooms/{room}/files/{fileId}/content`：成员校验；`FileResponse`（存储 mime、inline、显示名按 RFC 5987 编码 Content-Disposition）；`?download=1` 改 attachment；文件缺失 404
  - `GET /api/archives/{roomId}/files` + `/files/{fileId}/content`：复用 `_require_archive_access`，只读（含已摆放状态只读可见）
  - 房间详情 `myPermissions` 加 `canEditFiles`；`_room_dict` 详情路径加 `files` 概要 `{revision, locked, canEdit, count}`（`canEdit` = 治理者 or `!locked && can_edit_files`；列表接口不加，避免 ROOM_LIST_SQL 膨胀）
  - `RoomUpdate` 加 `filesLocked`（「没有需要修改的字段」判定同步扩展；变更后 revision+1 + `notify_room`）；`PermissionUpdate` 加 `canEditFiles`（「房主与 roomAgent 不可被限制」既有保护自动覆盖）
  - _需求：4.7、5.4、5.7、6.1、11.1_

- [x] 7. 3D 世界摆放端点（需求 10）
  - `PUT /api/rooms/{room}/files/{fileId}/placement`：`PlacementUpdate`（`visible: bool` 必填；`visible=true` 时 `position:[x,y,z]` 必填、`rotation` 缺省 [0,0,0]、`scale` 缺省 [1,1,1]，数值经 Pydantic 校验）；仅 kind=model（其他 400「只有 3D 模型文件可摆入房间」）；权限 = `_check_file_edit_allowed`；开启时校验 `MAX_WORLD_MODELS`（400，提示先关闭其他模型）；LWW 无 baseUpdatedAt；成功写 `world_visible/world_pose/world_updated_by/world_updated_at`，**不动 `updated_at`**（不污染内容乐观锁），revision+1 + `notify_room`；归档房间拒绝
  - `_room_files_dict` 对 model 类组装 `world` 块 `{visible, pose, updatedBy, updatedAt}`（未摆放 null）；删除文件自然连带走摆放状态
  - _需求：10.1、10.4、10.5、10.6、10.7、10.9_

- [x] 8. e2e 服务器段
  - `scripts/e2e.sh` 新增「共同文件」段：JSON 创建 → 列表含 revision → multipart 上传 → 同名 409 → PUT 替换 → 旧 baseUpdatedAt 409 → PATCH 改名（重名 409）→ DELETE → 404 → `canEditFiles=0` 403 → `filesLocked` 后普通成员 403 / 房主 200 → 归档后房间接口 410、归档接口 200；摆放链路：model 文件 PUT placement → 列表 world 块返回 → 非 model 400 → 无编辑权 403 → filesLocked 下 403 → 第 7 个开启 400 → `visible:false` 关闭后 world 保留 pose
  - 注意既有陈旧断言「超长 422」是已知失败项，不属于本需求回归
  - _需求：11.5_

## Phase 2：2D Web UI（static/index.html）

- [x] 9. 文件抽屉与列表
  - chat-top「📁 文件」按钮 + `#filesDlg`（抽屉式）：头部（N/200 + 锁定徽标 + 「上传文件」「新建文本」）+ 列表（kind 图标/名称/描述摘要/大小/更新者/相对时间 + 行内操作：预览｜编辑(文本类)｜下载｜重命名｜删除）；model 行加「已摆入房间」只读徽标（取自 `world.visible`，2D 不做位姿编辑）
  - 打开期间每 2s `GET files`；revision 未变不重渲染；关闭即停（沿用 stopPoll 风格的定时器管理）
  - _需求：7.1、7.6、6.3、10.6_

- [x] 10. 上传 / 新建 / 重命名 / 删除
  - 上传弹窗：`<input type=file>` + 名称（缺省净化文件名）+ 描述 → multipart POST；413/409 服务端消息 toast（`mapApiError` 补英文映射：「房间共同文件已锁定」「你已被禁止编辑房间文件」「同名文件已存在」「文件已被 … 更新」「文件超过 … 上限」）
  - 既有聊天附件上传路径选到 3D 类型（.glb/.gltf/.vrm）→ toast「3D 内容建议存入共同文件（可在 XR 房间摆放）」，不阻断（软引导）
  - 新建文本：名称 + textarea → POST；重命名/描述：小弹窗 PATCH；删除：确认框 DELETE
  - _需求：7.1、7.2、7.7、4.8_

- [x] 11. 文本编辑器与冲突处理
  - `#fileEditorDlg`（等宽 textarea + 字数 + 保存/取消）；编辑现有文件带 `baseUpdatedAt`；409 → 「已被 X 更新」对话（重载最新 / 强制覆盖 / 取消）；草稿 localStorage `webharness.filedraft.<房间名>.<fileId>`（房间草稿同一模式，发送成功清 key）
  - 编辑入口只对 kind ∈ {markdown, text, svg} 显示
  - _需求：7.3、2.5、6.4_

- [x] 12. 预览矩阵
  - `#filePreviewDlg`：markdown → 复用消息富文本管线（marked+DOMPurify）渲染进容器；`.mermaid/.mmd` → mermaid；text → `<pre>`；image → blob `<img>`；svg → blob `<img>`（不内联 DOM）；audio/video → 原生控件；model → 复用 3D 文件卡片离屏取景缩略图（动态 import、URL 缓存、失败降级静态图标）+ 下载按钮；other → 仅下载
  - 预览打开期间文件 `updatedAt` 变化 → 提示「文件已更新」+ 重新打开按钮；全部内容 fetch 走 `authBlobUrl`（Bearer → blob）
  - _需求：4.4、4.6、4.7、6.4_

- [x] 13. 治理面 + i18n
  - 「管理房间」弹窗加 `filesLocked` 开关（PATCH rooms）；成员权限表加「文件」列（PUT permissions，与 canSpeak/canUpload/canViewHistory 同一表格与交互）
  - 全部新文案 zh/en 双语（I18N 字典）；`data-i18n` 静态文本 + `t()/tf()` 动态文本
  - _需求：7.5、7.7_

## Phase 3：WebXR 3D 端（static/xr/）

- [x] 14. xr-files.js 骨架与列表面板
  - 新模块 `static/xr/xr-files.js`；`xr-main.js` 挂「文件」入口按钮（工具区，与 3D 发送入口并列）；打开/关闭生命周期与切房销毁
  - 列表面板：CanvasTexture 文本面板（复用既有滚动面板基建），行 = kind 图标字符 + 名称 + 更新者 + 时间，行可射线点选 → 预览；面板打开期间 2s 轮询 revision，变更重绘
  - i18n 键进 `xr-i18n.js`（zh/en，3D 加载时合并，沿用既有模式）
  - _需求：8.1、6.3、8.6_

- [x] 15. 分类型预览
  - markdown：经 index.html 最小桥（`renderFileMarkdown(text) → 离屏 DOM`，复用消息管线，~15 行纯增量）→ xr-panels 栅格化管线成纹理面板
  - text：CanvasTexture 文本面板，长文复用滚动/「展开」副面板机制
  - image/svg：`api()` fetch blob → `createImageBitmap` → 纹理平面（svg 位图化，不执行脚本）；复用图片消息取回管线与聚焦交互
  - video：blob objectURL → `THREE.VideoTexture` 平面 + 点按播放/暂停
  - model：双动作——「临时预览」复用 `placeChatModel` 既有管线（本地 ≤6 LRU、不写状态、关面板收起）；「摆入房间」走任务 16 世界摆放层
  - audio/other：信息卡（类型/大小/更新者/「请到 2D 网页查看/下载」）
  - 全部预览失败降级占位卡，不影响列表（`dispose` 纳入既有预算）
  - _需求：4.5、4.6、8.2、8.6、10.8_

- [x] 16. 世界摆放层（需求 10）
  - 同步：进房与 revision 变化 → 渲染所有 `world.visible=true` 的 model（fetch blob → GLTFLoader.parse → 直接套 `world_pose`，不二次归一化）；异步加载线框占位；删除/隐藏即时移除并 dispose
  - 摆入：列表面板 model 行「摆入房间」→ 初始位姿（摆放者面前空位 + 朝向摆放者 + Box3 ~1m 归一化 scale，客户端算好）→ 一次 `PUT placement`；超上限/无权限按服务端 403/400 提示
  - 调整：点击已摆放模型 → 移动/旋转/缩放/关闭；移动与旋转复用 `dragBegin/dragMove/dragEnd` 射线-拖拽基建，手柄端 `handlePick` 指向 + 摇杆组合；拖拽 ~500ms 节流保存、松手即 `PUT placement`；关闭 = `{visible:false}` 保留位姿；无编辑权/filesLocked 入口置灰
  - _需求：10.2、10.3、10.5、10.7、10.8、10.10_

- [x] 17. XR 内文本编辑
  - 文本类编辑面板复用既有 3D 文本输入模式（系统键盘；保存 `api()` PUT + baseUpdatedAt；409 提示并重载）；无编辑权时入口置灰；系统键盘不可用设备降级只读 + 引导 2D；中文长文引导 2D（Quest 无中文 IME）
  - _需求：8.3、8.4、8.5_

- [ ] 18. 回归与真机抽查
  - 桌面 3D 预览全流程：文件列表、各 kind 预览、编辑保存、锁定后置灰、摆入/拖拽调整/关闭（位姿刷新页面后保持一致）、退出释放（内存回归：反复进出无泄漏）
  - 头显（Quest）真机抽查列表面板、model 摆放与拖拽、video 预览、系统键盘编辑（用户执行；本地无设备）
  - _需求：8.1–8.6、10.1–10.10_

## Phase 4：文档、版本与发布

- [x] 19. Agent 说明书（含内容路由规则）
  - `.cursor/skills/webharness-api/SKILL.md` 新章节「共同文件」：接口表 + curl 示例（JSON 直写 / multipart / baseUpdatedAt 替换 / 改名 / 删除 / 下载 / 世界摆放）、kind 矩阵与推荐类型（文本基础格式 + WebXR 可渲染）、上限、摆放坐标契约（世界坐标系 / Euler 弧度 / 显式 scale）、「先 GET 列表拿 fileId 与 updatedAt」流程建议、`sinceRevision+wait` 感知节奏
  - **内容路由规则**（表格，置章节开头）：一次性表达（含 Mermaid/SVG/chart/a2ui 图）→ 聊天富文本；会迭代内容 → 共同文件；**3D 内容（GLB/GLTF/VRM）一律共同文件**（硬性行为指引，不作为新聊天附件发布）；典型工作流 5 例（纪要 / mermaid 图 / GLB 评审 / 读设计图回写意见 / placement 布景）
  - 同步 `/skill.md` 同源（既有机制自动生效）；OpenAPI /docs 随端点更新
  - _需求：9.1–9.6_

- [x] 20. 人类文档与 README
  - `docs/HUMAN.md` + `docs/HUMAN.en.md`、`static/guide.html` + `static/guide.en.html` 增「共同文件」段（入口、上传/编辑/权限、类型推荐表、内容路由建议、3D 摆放说明）；`README.md` 接口速查表补端点（含 placement）+ 功能段
  - _需求：9.3、9.6、11.5_

- [ ] 21. 版本与发布
  - `app/main.py` version → **2.18.0**（FastAPI description 摘要补共同文件与 3D 摆放能力）
  - 全量回归：e2e.sh（注意 1 项已知陈旧断言）+ 2D 全功能走查（确认文件抽屉不侵入既有轮询/未读逻辑）+ 桌面 3D 回归
  - 发布：生产备份 → `./deploy/build_release.sh` → install.sh（生产部署需用户确认后执行）
  - _需求：11.5、11.6_

## 可选增强（不阻塞 2.18.0）

- [ ] inbox.py 增加 `--files` 输出（revision + 变更文件名列表，供值班 Agent 感知）
- [ ] 2D model 预览升级交互式轨道查看器；XR 音频播放（AudioContext 管线已存在）；2D 位姿编辑器/摆放开关
- [ ] 文件活动系统提示消息（房间可选设置）
- [ ] 聊天 3D 附件改服务端硬拒（当前软提示）
- [ ] 视频上限放宽 + Range 流式；多模型成组/吸附摆放与过渡动效（需要时再立项）

## 验证方式

- 本地 `.claude/launch.json` 的 `webharness-local` 起 8768；浏览器验证 2D 全流程（上传、编辑、409 冲突弹窗、锁定、权限、归档只读、model 徽标）。
- Agent 侧：按 SKILL.md 新章节的 curl 示例逐步验证（含 baseUpdatedAt 冲突、sinceRevision 长轮询、placement 摆放链路）。
- 3D：桌面 3D 预览走查任务 18 清单（含摆入/拖拽/关闭后位姿跨客户端同步）；Quest 真机由用户抽查（本地无设备）。
- 回归基线：e2e.sh 通过（除 1 项已知陈旧断言）；2D 聊天、语音、私聊、归档等功能不受影响。
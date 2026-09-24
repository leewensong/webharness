# 设计文档 — 房间共同文件（Room Shared Files）

## 架构总览

延续「数据与渲染分离」与「无长连接」两条既有决策。共同文件 = 房间级共享的**数据 + 类型标识**；文件字节落盘、元数据入 SQLite；2D 网页、WebXR 3D、Agent 三类消费者走**同一套短 HTTP API**（Bearer token），变更感知靠 revision + 轮询/长轮询（复用 `notify_room` 唤醒机制），不引入 WebSocket。

```
                     SQLite 元数据 room_files + rooms.files_revision
                     磁盘内容   data/files/<room_id>/<file_id><ext>
                                    │  同一套 HTTP API（列表/CRUD/内容 + revision 长轮询）
                    ┌───────────────┼────────────────┐
             2D 渲染端（现状扩展）   WebXR 3D 端        Agent（短 HTTP）
        static/index.html 文件抽屉   static/xr/xr-files.js   curl / python 脚本
        上传/编辑/预览/管理          列表面板 + 分类型预览     程序化读写（纪要/图/GLB）
```

与聊天附件（`data/uploads/`）的边界：附件是**消息流的一部分**（随消息撤回/归档联动）；共同文件是**房间共享状态**（与消息无关，独立生命周期，LWW 替换）。两者权限、目录、接口完全分开，不共用 `can_upload`。

**内容路由原则**（写进 Agent 说明书与人类文档）：聊天消息承载**一次性表达**（富文本 Mermaid/SVG/chart/a2ui 本就支持画图）；**会迭代的内容**进共同文件；**3D 内容（GLB/GLTF/VRM）一律进共同文件**（创建与修改、含世界摆放），聊天流不再以新增 3D 附件作为推荐路径——避免多媒体冗余、只保留最新状态。既有聊天 3D 附件功能保持兼容，仅加提示引导。

## 数据模型

### room_files（新表）

```sql
CREATE TABLE IF NOT EXISTS room_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    content_path TEXT,                -- data/files/ 下的相对路径，如 "3/42.glb"
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 3D 世界摆放（需求 10）：仅 kind=model 有意义
    world_visible INTEGER NOT NULL DEFAULT 0,
    world_pose TEXT,                  -- JSON {"position":[x,y,z],"rotation":[x,y,z],"scale":[x,y,z]}
    world_updated_by INTEGER REFERENCES users(id),
    world_updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_room_files_name
    ON room_files(room_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_room_files_world
    ON room_files(room_id) WHERE world_visible = 1;
```

- `world_pose` 的**坐标契约**：`position/rotation/scale` 三元组（rotation 为 Euler XYZ 弧度），作用于模型**原始尺寸**的根节点；坐标系即房间 3D 场景世界坐标系（与 `map3d` 内置场景同一原点）。首次摆放的归一化（包围盒最大边 ~1m）由摆放端换算成显式 `scale` 写入，之后所有端直接套用、不再二次归一化——跨端一致。服务器不理解坐标，只做 schema 校验与存取（延续「服务器只做透传与最小校验」惯例）。

- 显示名 `name` 净化后入库（去路径分量与控制字符，≤128 字符，允许中文与空格，`_sanitize_filename` 的 Unicode `\w` 保留中文的特性行为不变，但另写一个宽松版校验器：文件名不进磁盘路径，无需像附件那样激进替换）。
- 磁盘路径只由 `room_id/file_id + 写入时扩展名` 组成，**不含任何用户输入**；改名不移动磁盘文件（路径中的扩展名仅便利于人工排障，改名后过期属预期）。
- 内容替换 = 临时文件 + `os.replace` 原子覆盖；删除 = `unlink(missing_ok=True)` + 删行（失败照删行，沿用撤回附件的容错风格）。

### rooms / room_members（改造，迁移只增）

```sql
ALTER TABLE rooms ADD COLUMN files_locked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rooms ADD COLUMN files_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE room_members ADD COLUMN can_edit_files INTEGER NOT NULL DEFAULT 1;
```

- **迁移位置约束**：`rooms` 的新列必须加在 `_rebuild_rooms_if_name_globally_unique` 之后（db.py 既有注释：重建只搬显式列出的字段）。
- `files_revision` 是列表变更的版本号：创建/替换/重命名/改描述/删除/filesLocked 切换均 `+1`。删除类变更无法用 `MAX(updated_at)` 推导，所以用计数器而不是时间戳。
- 存量房间 `files_locked=0`、存量成员 `can_edit_files=1` → 行为即「缺省全员可编辑」，无需回填。

### 内容为什么不进 SQLite（决策记录）

头像/3D 形象/房间场景存 BLOB 是「小、随主体、单条」的场景；共同文件**上限最大（50MB 视频/GLB）、可整体替换、数量最多（200/房）**——进库会让 WAL 与备份膨胀、Response 需要全量读入内存。附件落盘 `data/uploads/` 已是先例，文件响应可用 `FileResponse` 流式。故：内容落盘 `data/files/`，元数据入库。`data/files/` 加入 `.gitignore`（与 `data/uploads/` 同一模式）。

## 类型识别与上限

### kind 矩阵（服务器权威分类）

按「魔数 → 扩展名 → 声明 mime」顺序判定，复用既有 helper（`_gltf_mime`、`_is_image_upload` 的魔数分支、`_audio_ext`）：

| kind | 识别 | 2D 预览 | XR 预览 | 文本可编辑 |
| --- | --- | --- | --- | --- |
| `markdown` | .md/.markdown | Markdown 渲染（marked+DOMPurify） | 2D 管线 DOM 栅格化面板 | ✅ |
| `text` | 文本扩展名/`text/*`/json|xml|yaml 等（含 .mermaid/.mmd/.drawio/代码） | 等宽文本；.mermaid/.mmd 走 Mermaid 渲染 | 文本面板（滚动/超长折叠） | ✅ |
| `image` | png/jpg/gif/webp/bmp 魔数或扩展名 | 内联图片 | 纹理平面 | ❌ |
| `svg` | .svg 或 `image/svg+xml`（文本格式 + 图片渲染的双重性质，独立成类） | `<img>` 方式内联（不执行脚本） | 纹理平面 | ✅（按文本编辑源码） |
| `audio` | `_audio_ext` 命中 | `<audio>` 播放器 | 信息卡 + 引导 2D（本期） | ❌ |
| `video` | .mp4/.webm/.mov/.m4v 或魔数 | `<video>` 播放器 | VideoTexture 平面（可播放/暂停） | ❌ |
| `model` | glTF 魔数（GLB/VRM）或 .gltf/.glb/.vrm 扩展名 | GLB 取景缩略图（复用 3D 文件卡片管线） | 临时预览放置 + 世界摆放（常驻显示、记录位姿，见需求 10） | ❌ |
| `other` | 以上全部未命中 | 仅下载 | 信息卡 + 引导 2D | ❌ |

- `.gltf`（JSON 文本）按「类型不受限制」原则**收下**（kind=model），但它可能引用外部 .bin/贴图导致加载失败——与房间场景接口不同，这里不硬拒，预览失败时降级为源码文本视图并提示「建议自包含 GLB」（理由：房间场景是专用字段可以收紧，共同文件是通用存储）。
- 文本扩展名白名单（节选）：`.txt .md .markdown .json .xml .yaml .yml .csv .tsv .ini .cfg .toml .log .html .htm .css .js .mjs .ts .py .rb .go .rs .java .c .h .cpp .cs .php .sh .sql .mermaid .mmd .drawio .puml .plantuml .tex .srt .vtt` + 声明 mime `text/*`、`application/{json,xml,yaml,javascript}`。未命中白名单的未知扩展名即使恰好是 UTF-8 也归 `other`（防止把 .docx 等二进制当文本渲染出乱码）。
- **改名重分类**：重命名后按「新扩展名 + 存量 mime」重算 kind/mime，不重读内容字节（`model.glb → model.fbx` 因存量 mime 仍为 model；`notes.txt → notes.md` 升为 markdown）。

### 上限（常量，沿用 main.py 顶部常量风格）

| 常量 | 值 | 语义 |
| --- | --- | --- |
| `MAX_FILE_TEXT_BYTES` | 2MB | kind ∈ {markdown,text,svg} 的内容上限（JSON 直写与编辑器都走这个量级） |
| `MAX_FILE_BYTES` | 50MB | 其余二进制上限（对齐房间场景上限） |
| `MAX_FILES_PER_ROOM` | 200 | 单房文件数上限 |
| `MAX_FILE_DESCRIPTION_CHARS` | 500 | 描述长度 |
| `MAX_WORLD_MODELS` | 6 | 同时常驻显示（world_visible=1）的 model 文件数，开启时服务端校验（400） |
| 文件名 | 1–128 字符 | 房内唯一（NOCASE） |

## API 设计

全部需 `Authorization: Bearer <token>`，全部为**纯新增**（既有接口只增可选字段）。

| 方法 | 路径 | 说明 | 约束 |
| --- | --- | --- | --- |
| GET | `/api/rooms/{room}/files` | 列表 + revision；支持 `sinceRevision` + `wait`（0–30s）长轮询 | 成员 |
| POST | `/api/rooms/{room}/files` | 创建。JSON `{name, content, description?}`（仅文本类，按扩展名判定）或 multipart `file` + `name` + `description?` | 成员 + 文件编辑权 |
| GET | `/api/rooms/{room}/files/{fileId}` | 单文件元数据（409 冲突后回查用） | 成员 |
| GET | `/api/rooms/{room}/files/{fileId}/content` | 内容（inline 预览；`?download=1` 改附件下载，Content-Disposition 用显示名） | 成员 |
| PUT | `/api/rooms/{room}/files/{fileId}` | 整体替换。JSON `{content, baseUpdatedAt?}` 或 multipart `file` + `baseUpdatedAt?`；LWW，`baseUpdatedAt` 不符 409 | 成员 + 文件编辑权 |
| PATCH | `/api/rooms/{room}/files/{fileId}` | 改名 / 改描述 `{name?, description?}`（改名重分类 kind/mime） | 成员 + 文件编辑权 |
| DELETE | `/api/rooms/{room}/files/{fileId}` | 删除（内容文件一并 unlink；已摆放的一并消失） | 成员 + 文件编辑权 |
| PUT | `/api/rooms/{room}/files/{fileId}/placement` | 设置/更新世界摆放 `{visible, position?, rotation?, scale?}`；仅 kind=model（其他 400）；`visible:true` 时 position 必填、rotation 缺省 [0,0,0]、scale 缺省 [1,1,1]；超上限 400；LWW（无 baseUpdatedAt）；成功 revision+1 + `notify_room` | 成员 + 文件编辑权 |
| GET | `/api/archives/{roomId}/files` | 归档房间文件列表（只读） | 归档访问权 |
| GET | `/api/archives/{roomId}/files/{fileId}/content` | 归档文件内容下载 | 归档访问权 |

既有接口的**扩展**（全部可选字段，旧客户端零感知）：

- `GET /api/rooms/{room}` 房间详情增加：

```json
"files": {"revision": 7, "locked": false, "canEdit": true, "count": 2},
"myPermissions": {"canSpeak": true, "canUpload": true, "canViewHistory": true, "canEditFiles": true}
```

  `canEdit` 为**有效**编辑权（治理者恒真；否则 `!files_locked && can_edit_files`）；`myPermissions.canEditFiles` 为成员原始列值。
- `PATCH /api/rooms/{room}`（治理者）增加 `filesLocked: bool`（与 muted 并列；「没有需要修改的字段」判定同步扩展）。
- `PUT /api/rooms/{room}/permissions/{username}`（治理者）增加 `canEditFiles: bool`。既有保护「房主与 roomAgent 不可被限制」自动覆盖文件权限。

### 列表响应

```json
{
  "roomName": "general",
  "revision": 7,
  "files": [
    {
      "id": 3, "name": "会议纪要.md", "kind": "markdown", "mime": "text/markdown",
      "size": 12345, "description": "每轮会议后由纪要 Agent 更新",
      "createdBy": "wilson", "createdAt": "2026-09-24 10:00:00",
      "updatedBy": "minutes-bot", "updatedAt": "2026-09-24 12:30:00.123",
      "contentUrl": "/api/rooms/general/files/3/content?v=2026-09-24%2012%3A30%3A00.123",
      "world": null
    },
    {
      "id": 7, "name": "演示样机.glb", "kind": "model", "mime": "model/gltf-binary",
      "size": 8388608, "description": "评审用样机（XR 内已摆放）",
      "createdBy": "wilson", "createdAt": "2026-09-24 11:00:00",
      "updatedBy": "wilson", "updatedAt": "2026-09-24 11:00:00.456",
      "contentUrl": "/api/rooms/general/files/7/content?v=2026-09-24%2011%3A00%3A00.456",
      "world": {
        "visible": true,
        "pose": {"position": [0.5, 0, -2.5], "rotation": [0, 3.14, 0], "scale": [1.2, 1.2, 1.2]},
        "updatedBy": "wilson", "updatedAt": "2026-09-24 13:00:00.789"
      }
    }
  ]
}
```

- 排序 `updated_at DESC, id ASC`（最近活跃在前）。
- `contentUrl` 由服务器按 `updatedAt` 现算（与房间场景 URL 同模式）——改名不失效、替换后天然换 URL。
- `world` 仅对 kind=model 有意义（非 model 恒为 null，客户端忽略即可）；**摆放变更不改 `updatedAt`/contentUrl**（内容未变），只写 `world_updated_*` 并递增 revision——避免污染内容编辑的 baseUpdatedAt 乐观锁。
- 摆放接口的并发语义：LWW 无乐观锁；XR 拖拽期间客户端 ~500ms 节流保存、松手即保存，并发互踩最多导致位姿跳变（再拖即可），不值得为此加锁。
- 单条元数据响应同上（含 revision 不含列表）。

### LWW 与 baseUpdatedAt（乐观锁，可选）

- 语义用户已拍板：**只存最新、无历史**。`PUT` 不带 `baseUpdatedAt` = 无条件覆盖（Agent 批量刷新场景的最省事路径）。
- 带 `baseUpdatedAt`（客户端从列表里拿到的 `updatedAt`）时服务端比对当前值，不符返回 `409 文件已被 {updatedBy} 更新`；客户端（2D 编辑器 / XR 编辑面板）此时二选一：重载最新内容，或去掉 baseUpdatedAt 强制覆盖。**防的是「编辑期间被别人覆盖」的无感知丢失**，不是并发控制。

### 错误语义（新增行，沿用既有状态码习惯）

| 状态码 | 场景 |
| --- | --- |
| 400 | 非文本类走 JSON 接口、数量超上限（200）、文件名非法/为空、PATCH 无字段 |
| 401 / 403 | 既有语义：未认证 / 非成员 / 无编辑权 / 房间文件已锁定 |
| 404 | 文件不存在（含重复删除） |
| 409 | 同名冲突（创建/改名）、baseUpdatedAt 不符 |
| 410 | 房间已结束（写接口走 `_require_active_room`） |
| 413 | 超过大小上限（文本 2MB / 二进制 50MB） |

### 长轮询（复用既有机制，不新建事件通道）

```python
@app.get(".../files")
async def list_files(..., since_revision: int | None = None, wait: int = 0):
    while True:
        revision, items = 读当前
        if since_revision is None or revision != since_revision or remaining_wait <= 0:
            return {roomName, revision, files}
        await asyncio.wait_for(_room_event(room_id).wait(), timeout=remaining_wait)
        # 消息等活动也会唤醒（共享事件）；醒来只花一次 revision 比对，无副作用
```

- 文件变更统一 `notify_room(room_id)`（与发消息/场景上传同一函数）：同时唤醒消息长轮询者（他们拉到空消息批，既有行为无害）与文件长轮询者。
- 客户端节奏：2D/XR 房间详情轮询（2s）带 `files.revision`，变了才拉列表；Agent 可用 `sinceRevision+wait=25` 挂起等待（与 inbox.py 的消息长轮询同节奏）。

## 权限模型

判定顺序完全镜像 `_check_action_allowed`（muted + can_speak 的合成关系）：

```
if 治理者（房主或 roomAgent）: 放行
elif room.files_locked: 403 "房间共同文件已锁定"
elif member.can_edit_files == 0: 403 "你已被禁止编辑房间文件"
else: 放行
```

- 读取（列表/内容/归档访问）不进此判定，只查成员身份。
- 与 `muted`/`can_speak`/`can_upload` **无关**：禁言的人仍可编辑文件，被禁上传附件的人仍可上传共同文件（聊天管控与共享状态管控分离，房主要分别设置）。
- `can_view_history=0` 不影响文件（文件是当前状态，非历史）。

## 2D Web UI（static/index.html）

- **入口**：chat-top 增加「📁 文件」按钮（未读感不做强需求；按钮带文件数角标可选）。
- **抽屉/弹窗** `#filesDlg`：头部（房间文件 N/200、锁定状态徽标、「上传文件」「新建文本」按钮）；列表行 = kind 图标 + 名称 + 描述摘要 + 大小 + 更新者 + 相对时间（model 行加「已摆入房间」只读徽标，取自 `world.visible`，2D 不做位姿编辑）；行操作（预览 / 编辑(文本类) / 下载 / 重命名 / 删除，治理者多「锁定」开关在管理弹窗而非此处）。
- **上传**：`<input type="file">` + 名称（缺省净化后的文件名）+ 描述 → multipart POST；另在**既有聊天附件上传路径**上，选到 3D 类型（.glb/.gltf/.vrm）时 toast 提示「3D 内容建议存入共同文件（可在 XR 房间摆放）」，不阻断（软引导，见需求 4.8）。
- **新建/编辑文本**：`#fileEditorDlg`（等宽 textarea + 字数）→ POST 或 PUT（带 baseUpdatedAt）；409 时弹「已被 X 更新」对话（重载最新 / 强制覆盖）。编辑草稿存 `localStorage`（`webharness.filedraft.<房间名>.<fileId>`，房间草稿同一模式）。
- **预览** `#filePreviewDlg` 按需求矩阵：markdown 走消息富文本同一管线渲染进预览容器（marked+DOMPurify + 代码高亮）；mermaid 文件走 mermaid；svg/img 用 blob objectURL `<img>`；audio/video 原生控件；model 复用 3D 文件卡片的离屏取景缩略图（动态 import、按 URL 缓存、失败降级静态图标）+ 下载按钮；other 只下载。
- **治理**：「管理房间」弹窗加 filesLocked 开关（PATCH rooms）；成员权限表加「文件」列（PUT permissions）。
- **轮询**：抽屉打开期间每 2s `GET files`，revision 未变不重渲染；预览中文件按 `updatedAt` 变化提示「文件已更新 → 重新打开」。
- **i18n**：全部新键补 zh/en；`mapApiError` 增加「房间共同文件已锁定」「你已被禁止编辑房间文件」「同名文件已存在」「文件已被 … 更新」「超过大小上限」等映射。

## WebXR 3D 端（static/xr/）

新模块 `xr-files.js`（与 xr-rooms.js 同层级），`xr-main.js` 负责挂载/销毁与入口按钮，index.html 只加一个最小桥（详见「与既有代码的集成」）。

- **入口**：3D 界面常驻工具区加「文件」按钮（复用既有 3D 发送输入面板的工具按钮排布）；打开后挂文件列表面板。
- **列表面板**：CanvasTexture 文本面板（复用既有滚动面板/超长折叠基建），每行 = 图标字符 + 名称 + 更新者 + 时间；行可射线点选（既有拾取模式）→ 打开预览；revision 轮询（面板打开时 2s 或挂在既有房间轮询上）。
- **预览（按矩阵）**：
  - `markdown`：经 index.html 桥把 markdown 渲染成离屏 DOM → 复用 xr-panels 栅格化管线成纹理面板（与消息面板同外观、同 LRU）。
  - `text`：CanvasTexture 文本面板，长文走既有滚动/「展开」副面板机制。
  - `image`/`svg`：`api()` 拉内容 blob → `createImageBitmap` → 纹理平面（复用图片消息的取回管线；svg 以图片位图化，天然不执行脚本）。
  - `video`：blob objectURL → `<video>` → `THREE.VideoTexture` 平面；点按播放/暂停；复用聚焦/走近交互。
  - `model`：两个动作——「临时预览」复用聊天 3D 模型附件的「取回 → GLTFLoader.parse → Box3 归一化 ~1m → 地面放置 → 再点收起」既有管线与上限（本地 ≤6 LRU、关面板即收起、不写状态），与「摆入房间」共享持久摆放（见「世界摆放层」）。
  - `audio`/`other`：信息卡（类型 + 大小 + 更新者 + 「请到 2D 网页查看/下载」）。
- **世界摆放层（需求 10）**：
  - 同步：进房与 revision 变化 → 列表里所有 `world.visible=true` 的 model 按位姿渲染（`api()` fetch blob → `GLTFLoader.parse` → 直接套 `world_pose`，**不再二次归一化**——首次摆放端已把归一化算进显式 scale）；异步加载期间显示线框占位；删除/隐藏的模型即时移除并 dispose。
  - 交互：列表面板 model 行提供「摆入房间」（初始位姿 = 摆放者面前空位 + 朝向摆放者 + Box3 ~1m 归一化 scale，客户端算好后一次 `PUT placement`）；点击已摆放模型进入调整（移动/旋转/缩放/关闭/再显示），移动与旋转复用既有 `dragBegin/dragMove/dragEnd` 射线-拖拽基建，手柄端复用 `handlePick` 指向 + 摇杆组合（具体手势实施时定）；拖拽中 ~500ms 节流 `PUT placement`、松手即保存；关闭 = `{visible:false}`（保留位姿）。
  - 无编辑权或 filesLocked：摆入/调整入口置灰，已摆放模型照常渲染（摆放是共享状态，读取不受限）。
  - 性能：常驻模型上限 6（服务端校验）；单个 ≤50MB；加载失败降级为地面占位框标注文件名。
- **编辑**：文本类编辑面板 = 既有 3D 文本输入模式（系统键盘 overlay；Quest 实测可用、无中文 IME——中文输入引导到 2D 或快捷短语）；保存走 `api()` PUT（带 baseUpdatedAt），409 弹提示并重载。系统键盘不可用的设备上编辑入口降级为只读。
- **上传/下载**：不做头显内文件选择器/下载器，面板上给「在 2D 网页操作」提示。
- **资源纪律**：列表面板/预览面板全部纳入既有纹理 LRU 与 dispose 预算；切房/退出 3D 全量释放；归档视图不提供入口。

## Agent 与文档

- **SKILL.md** 新章节「共同文件」：接口表 + curl 示例（JSON 直写文本、multipart 上传、带 baseUpdatedAt 替换、改名、删除、下载、世界摆放）、kind 矩阵与推荐类型（文本基础格式 / WebXR 可渲染）、上限、「先 GET 列表拿 fileId 与 updatedAt」的写入流程建议、感知方式（`sinceRevision` + `wait` 与消息长轮询同节奏），以及**内容路由规则**：

| 内容性质 | 放哪 | 理由 |
| --- | --- | --- |
| 一次性表达 / 通知 / 讨论（含 Mermaid / SVG / chart / a2ui 图） | 聊天富文本消息 | 聊天本就支持画图等富文本，一次性内容留在消息流即可 |
| 会持续修改调整的内容（纪要、计划、脑图、流程图、设计稿） | 共同文件 | 只保留最新状态，房间共享，人人可见最新 |
| 3D 内容（GLB/GLTF/VRM） | **一律**共同文件 | 避免聊天流多媒体冗余；可摆进 XR 房间常驻展示 |
- **典型工作流**（写进 SKILL.md，让 Agent 知道这能力怎么用）：
  1. 纪要 Agent 每阶段 PUT 更新 `会议纪要.md`；
  2. 分析 Agent 产出 `架构脑图.mermaid` / `流程图.mermaid`，人类在 2D/XR 直接看图；
  3. 设计 Agent 上传评审用 `demo.glb`，头显内环视；
  4. 人类上传 `设计稿.png`，Agent GET 内容读取后回写 `评审意见.md`；
  5. 布景 Agent 用摆放接口（`PUT .../placement`）把多个 GLB 按坐标摆成房间展示，位姿对全员同步（程序化编排场景的官方入口）。
- **值班脚本**：inbox.py/watch.py v1 零改动（文件感知走列表长轮询即可）；可选任务给 inbox.py 增加 `--files` 输出 revision（非必须）。
- **人类文档**：docs/HUMAN.md + HUMAN.en.md、static/guide.html + guide.en.md 增「共同文件」段；README.md 接口速查表补 8 个端点 + 功能段。

## 安全

- 全部文件接口走 `require_user` + 成员/归档访问校验；内容端点无免鉴权直链（2D/XR 取内容都走 fetch + Bearer → blob，与语音/形象同一模式）。
- 显示名：去路径分量（`Path(name).name`）、拒绝控制字符、≤128 字符、房内唯一；**磁盘路径只由 id 生成**，不存在用显示名拼路径的路径穿越面。
- svg 以 `<img>`/位图渲染（不内联 DOM，不执行脚本）；markdown 预览沿用消息管线的 DOMPurify 消毒。
- mime 由服务端判定后入库，客户端按 kind 而非用户声明选渲染器（声明 mime 只在白名单命中时采信）。
- 上限：单文件 2MB/50MB、单房 200 个、描述 500 字；磁盘总量不设硬配额（单机自部署 + 房间成员受治理者控制，超限治理者可锁可删——写进文档说明）。

## 与既有代码的集成

| 文件 | 改动 |
| --- | --- |
| `app/db.py` | `room_files` 新表 + `idx_room_files_name`；`rooms.files_locked/files_revision`（**必须放在 `_rebuild_rooms_if_name_globally_unique` 之后**）、`room_members.can_edit_files`；`FILES_DIR = DATA_DIR / "files"` 与 `UPLOADS_DIR` 并列 |
| `app/main.py` | 常量（上限/扩展名白名单/`MAX_WORLD_MODELS`）、`_classify_file()`（复用 `_gltf_mime`/`_audio_ext`/图片魔数）、Pydantic 模型（FileCreate/FileReplace/FileMetaUpdate/PlacementUpdate/PermissionUpdate+RoomUpdate 扩展）、8+2+1 个端点（含 placement）、`_room_files_dict()`、写路径统一 `notify_room`；房间详情 `_room_dict` 增 `files` 概要（懒查询，仅详情接口算 count/revision）；版本 2.18.0 |
| `static/index.html` | 文件抽屉 + 预览 + 编辑器 + 上传 + 治理开关/权限列 + model 行「已摆入房间」徽标 + 聊天附件 3D 类型软提示 + i18n 键 + mapApiError + 2s 轮询；给 XR 的最小桥（markdown→离屏 DOM 渲染函数，约 15 行，复用消息管线） |
| `static/xr/xr-files.js`（新） | 列表面板、分类型预览、编辑面板、世界摆放层（同步/摆入/拖拽调整/关闭）、轮询与 dispose |
| `static/xr/xr-main.js` | 入口按钮、模块加载与销毁挂接 |
| `scripts/e2e.sh` | 共同文件断言段（见下） |
| `.gitignore` | `data/files/` |
| 文档 | SKILL.md / HUMAN{,.en}.md / guide{,.en}.html / README.md |

**e2e.sh 断言**（沿用 check 风格）：创建 JSON 文本 → 列表含 revision=1 → 二进制 multipart 上传 → 同名 409 → PUT 替换（revision 递增、updatedAt 变化）→ 带 baseUpdatedAt 旧值 409 → PATCH 改名（重名 409）→ DELETE → 二次 DELETE 404 → `canEditFiles=0` 成员 PUT 403 → 房主 `filesLocked=true` 后普通成员 POST 403、房主 PUT 200 → 归档后列表 410、归档接口列表 200；摆放链路：model 文件 PUT placement（visible+position）→ 列表 world 块返回 → 非 model 文件 400 → 无编辑权成员 403 → filesLocked 下 403 → 第 7 个开启 400 → `visible:false` 关闭后 world 仍保留 pose。

## 兼容性

- 全部端点/字段纯新增；`PermissionUpdate`/`RoomUpdate` 新字段可选，旧调用方零变化；`_room_dict` 新增 `files` 键对旧前端是多余键（既有「纯增量」惯例）。
- 旧 Agent token 与既有接口语义零变；skill.md 增章节不破坏既有结构。
- 迁移幂等只增，任意旧版本库直接升上来。

## 暂缓项（明确不做，防范围膨胀）

- **版本历史 / diff / 回滚**：用户已拍板只存最新。
- **文件活动进聊天流的系统通知消息**：v1 不做合成消息（避免污染消息流与归档一致性）；未来可作为可选设置。
- **目录/分组/标签**：平铺列表 + 描述字段够 v1。
- **2D 端 model 交互式轨道查看器**：缩略图先行（交互查看在 XR）。
- **2D 端位姿编辑器 / 2D 摆放开关**：摆放操作在 XR 与 Agent API；2D 只读徽标。
- **多模型成组/吸附摆放、摆放过渡动效、按距离渐进加载**：摆放层 v1 先做直接同步渲染。
- **XR 端音频播放**：信息卡 + 引导 2D；AudioContext 管线已存在，后续按需补。
- **Range/流式边下边播**：50MB 上限 + blob 方案（全量拉取后播）够 v1；未来放宽视频上限时再做。
- **全文检索、外链文件（kind=url）、模板预置文件**。
- **inbox.py --files**：可选任务，不阻塞发布。

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 50MB 全量读入内存（UploadFile.read） | 与房间场景同上限同模式（单机自部署、并发低）；更大文件列入暂缓 |
| 并发替换互踩（两人同时改一个文件） | LWW 语义用户已接受；编辑器默认带 baseUpdatedAt 乐观锁提示 |
| revision 并发递增 | SQLite 单写者串行，`files_revision = files_revision + 1` 原子 |
| 列表轮询流量放大（每客户端 2s） | 列表仅元数据（200 × ~300B ≈ 60KB）；revision 比对后才重渲染；Agent 走长轮询 |
| XR 纹理/模型内存 | 沿用既有 LRU（≤6 模型、纹理预算）与全量 dispose |
| 文件名注入 / 路径穿越 | 显示名净化 + 磁盘路径 id 化 + Content-Disposition 按 RFC 5987 编码 |
| svg XSS / markdown XSS | svg 一律位图/`<img>`；markdown 走既有 DOMPurify 管线 |
| 头显无文件选择器 | 产品决策：上传/下载引导 2D，文档写明 |
| Quest 系统键盘无中文 IME | 编辑面板支持但文档引导中文长文用 2D；XR 编辑定位「短改」 |
| 房间场景切换后位姿悬空 | 位姿按世界坐标保留不清除（需求 10.10），成员自行再调整 |
| Quest 上多个常驻模型拖性能 | 上限 6（服务端校验）+ 单模型 ≤50MB + 异步加载占位；实测后可调常量 |
| 摆放拖拽的写入放大 | 客户端 ~500ms 节流 + 松手保存；LWW 下并发互踩最多位姿跳变，可再拖 |
| 内容路由靠约定而非硬约束 | Agent 侧写进 SKILL.md 硬性行为指引；人类侧 UI 软提示；服务端不拒绝（保既有聊天 3D 附件兼容） |

## 开放问题（实施前与用户确认）

1. 上限数值是否合适：单房 200 个 / 文本 2MB / 二进制 50MB（均为常量，后续可调）。
2. 视频是否需要超过 50MB（若需要，本期需引入 Range 流式，范围会扩大——暂按不做）。
3. XR 端音频播放是否要进 v1（暂按信息卡 + 引导 2D）。
4. 文件变更是否需要在聊天流出现系统提示消息（暂按不做）。
5. 2D 端 model 预览是否需要交互式查看器（暂按缩略图 + 下载）。
6. 同时常驻 3D 模型上限 6 是否合适（Quest 性能实测后可调常量）。
7. 聊天附件中的 3D 类型是否改为服务端硬拒（暂按文档指引 + 上传软提示，不破坏既有 3D 文件卡片功能）。
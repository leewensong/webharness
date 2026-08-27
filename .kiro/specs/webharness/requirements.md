# 需求文档 — WebHarness 单机聊天室

## 简介

在一台机器上运行的轻量聊天室服务（指定 IP + 端口）。人类用户通过 Web UI 使用，Agent 通过纯 HTTP 短请求 API 使用（无 WebSocket / 长连接）。所有数据用本地 SQLite 持久化。

当前代码已实现 v1 基线（需求 1、需求 3 的房间密码部分、需求 4 的文本消息部分、需求 6 的基础聊天界面、需求 7 的初版说明书），本规格是在此基础上的完整目标。

### 术语

- **人类用户**：以用户名 + 密码注册的账户，使用 Web UI。
- **Agent 用户**：由人类用户创建并拥有的账户，使用 Ed25519 密钥对签名登录，只能走 API。
- **主人（owner）**：某个 Agent 的创建者（人类用户）。
- **房主**：房间的创建者。
- **在线**：某房间内 5 分钟内有活动（加入、读消息、发消息、查询房间详情）。
- **public 房间**：对所有人可见、可自由加入的房间。

## 需求 1：人类账户

**用户故事**：作为人类用户，我要用用户名/密码注册并登录，以便使用聊天室。

### 验收标准

1. WHEN 提交合法用户名与密码到注册接口 THEN 系统 SHALL 创建人类账户，并用 PBKDF2-HMAC-SHA256 存储密码摘要。
2. WHEN 注册的用户名已存在 THEN 系统 SHALL 返回 409。
3. WHEN 用户名/密码登录成功 THEN 系统 SHALL 返回带过期时间的 Bearer token（默认 7 天）。
4. WHEN 受保护接口缺少 token、token 无效或已过期 THEN 系统 SHALL 返回 401。
5. 用户名仅允许 `A-Za-z0-9_.-`，长度 2–32；密码长度 4–128。

## 需求 2：Agent 账户与密钥对登录

**用户故事**：作为主人，我要为我的 Agent 创建账户并上传它的公钥；Agent 私钥只保存在 Agent 本地，用签名挑战登录，避免密码外泄。

### 验收标准

1. Agent SHALL 在本地生成 Ed25519 密钥对，私钥文件只保存在 Agent 所在机器，永不发送给服务器。
2. WHEN 人类用户提交 agent 用户名 + 公钥 THEN 系统 SHALL 创建 `kind=agent` 的账户，并记录其主人为当前用户。
3. WHEN Agent 以用户名请求登录挑战 THEN 系统 SHALL 返回一次性随机 nonce，有效期不超过 5 分钟。
4. WHEN Agent 提交对 nonce 的 Ed25519 签名 THEN 系统 SHALL 用已存公钥验签，通过后签发与需求 1 相同格式的 Bearer token。
5. nonce SHALL 一次性使用，验签成功或失败后即作废，防重放。
6. WHEN 主人查看自己的 Agent 列表 THEN 系统 SHALL 返回该主人名下全部 Agent 及其状态。
7. 主人 SHALL 可编辑自己的 Agent：重命名、轮换公钥、停用、删除。
8. `kind=agent` 的账户 SHALL NOT 调用 Agent 管理接口（Agent 不能拥有 Agent）。
9. 被停用的 Agent SHALL NOT 登录，已签发 token 继续有效至过期（v1.1 可接受；后续可加吊销表）。
10. Agent 用户名与人类用户名共享同一命名空间（全局唯一）。

## 需求 3：房间

**用户故事**：作为用户（人类或 Agent），我要按名字创建/加入房间，管理自己的房间，并能发现 public 房间。

### 验收标准

1. WHEN 按 `roomName` 加入且房间不存在 THEN 系统 SHALL 创建该房间，操作者成为房主。
2. WHEN 创建房间 THEN 操作者可设置可见性（`private` 默认 / `public`）和可选的加入密码。
3. WHEN 加入 `private` 且有密码的房间且自己不是成员 THEN 必须提供正确密码，否则返回 403。例外：当前用户是房主，或该房间由当前用户名下的 Agent 创建时，可免密加入。
4. WHEN 加入 `public` 房间 THEN 任何已登录用户 SHALL 可直接加入（无需密码），并受需求 5 权限约束。
5. WHEN 获取「我的房间列表」THEN 系统 SHALL 返回「我创建的」+「我已加入的」+「我名下 Agent 创建的（含 private）」未结束房间，含是否房主、是否有密码、在线人数。
6. WHEN 获取「public 房间列表」THEN 系统 SHALL 返回所有 `visibility=public` 且未结束的房间（无论是否加入）。
7. 房主 SHALL 可修改房间名称、加入密码（含取消密码）、可见性，并可归档房间。
8. WHEN 房主归档房间 THEN 系统 SHALL 将其从所有活动列表中移除，保留内部 `id` 与聊天记录；该房间名可被新房间复用。归档后按房间名加入/发言 SHALL 找不到该归档（可创建同名新房）。
9. WHEN 获取归档列表 THEN 系统 SHALL 返回当前用户作为房主、Agent 主人或前成员可见的已归档房间（按内部 id 标识）。
10. WHEN 查看归档 THEN 系统 SHALL 允许只读查阅历史消息与附件，禁止发言与上传。
11. WHEN 加入房间成功 THEN 响应 SHALL 包含该房间的在线用户列表（5 分钟活动窗口）。
12. 房间名规则同用户名，长度 1–64。活动房间名全局唯一（大小写不敏感）；已归档房间不占用该唯一约束。

## 需求 4：消息与在线状态

**用户故事**：作为房间成员，我要查阅最近聊天、发送消息、上传附件，并看到房间内谁在线。

### 验收标准

1. WHEN 成员查阅消息 THEN 系统 SHALL 按时间正序返回最近 `limit` 条（默认 50，上限 200）。
2. WHEN 请求带 `afterId` THEN 系统 SHALL 只返回 id 大于该值的新消息（增量轮询）。
3. WHEN 成员发送文本消息 THEN 系统 SHALL 持久化并返回该消息（内容 1–2000 字符）。
4. WHEN 成员（通常是 Agent）调用流式接口 THEN 系统 SHALL 先创建一条 `streaming=true` 的文本消息，允许作者追加 `delta` 或整段替换，并在 `done=true` 时结束；每次更新 SHALL 唤醒该房间的长轮询。增量读取可用 `streamIds` + `sinceUpdatedAt` 拿到同一 id 的后续内容。Web UI SHALL 按消息 id 合并并显示流式光标。
5. WHEN 成员上传附件且拥有附件权限 THEN 系统 SHALL 保存文件并产生一条 `type=attachment` 或（若为图片）`type=image` 的消息；文件大小上限 20MB，文件名需净化。图片消息在 Web UI 中内联显示。
6. 任何成员下载附件 THEN 系统 SHALL 校验其房间成员身份。
7. 加入、读消息、发消息、查询房间详情 SHALL 刷新该用户在房间内的活动时间。
8. WHEN 查询房间详情 THEN 系统 SHALL 返回 5 分钟内有活动的在线用户列表。

## 需求 5：房间权限（房主管理）

**用户故事**：作为房主，我要控制成员在房间里的行为，包括发言、上传附件、查看加入前的历史，以及必要时全体禁言。

### 验收标准

1. 每个成员有三项权限：`can_speak`、`can_upload`、`can_view_history`，加入时 SHALL 默认全部允许。
2. WHEN 房主设置某成员权限 THEN 系统 SHALL 保存并对后续操作生效。
3. WHEN 成员 `can_speak=0` 或被全体禁言 THEN 其发言 SHALL 返回 403。
4. WHEN 成员 `can_upload=0` THEN 其附件上传 SHALL 返回 403。
5. WHEN 成员 `can_view_history=0` THEN 其查阅消息 SHALL 只返回加入之后产生的消息（以加入时刻的消息水位为准）。
6. WHEN 房主开启全体禁言 THEN 除房主外任何人发言 SHALL 返回 403。
7. 房主 SHALL NOT 受以上任何权限限制；房主不可被禁言。
8. 权限 SHALL 持久化，改名/改密/改可见性不影响已设置的成员权限。

## 需求 6：Web UI（人类）

**用户故事**：作为人类用户，我要在浏览器里完成全部操作：登录、聊天、管房间、管我的 Agent。

### 验收标准

1. 登录/注册页；登录状态保存在浏览器本地，过期自动回到登录页。
2. 房间列表分「我的」与「公开」两个视图；公开房间可一键加入。
3. 创建房间时可设置名称、可见性、加入密码。
4. 聊天视图：消息气泡（区分自己/他人）、右侧在线用户栏、每 2 秒增量轮询。
5. 房主管理面板：改名、改/取消密码、切换可见性、全体禁言开关、结束房间、成员权限编辑。
6. 「我的 Agent」面板：创建 Agent（粘贴公钥）、列表、重命名、轮换公钥、停用/删除，并展示 Agent 接入指引（说明书本机地址）。
7. 房间被结束时，界面 SHALL 停止轮询并提示。
8. 消息区支持发送附件（文件选择器）。
9. 「我的 / 公开」房间列表中，已加入且有未读他人消息的房间 SHALL 在名称旁显示蓝色数字气泡；超过 9 条显示 `9+`。打开该房间并拉取消息后未读清零。当前正在查看的房间不显示未读气泡。
10. 房间列表条目 SHALL 单击即进入（已加入）/ 加入（public 或 Agent 所建房），不提供单独的房间名加入表单；创建房间 SHALL 通过「＋ 创建房间」按钮打开设置弹窗（名称 / 可见性 / 加入密码）。
11. 各房间 SHALL 有独立的输入框草稿（`localStorage` key `webharness.draft.<房间名>`）：切换房间时保存当前房间草稿并恢复目标房间草稿；发送成功后清空该房间草稿。

## 需求 7：Agent API 与说明书

**用户故事**：作为 Agent，我要拿到一份自包含的聊天室说明书，照着就能完成密钥生成、登录、进房、读写消息。

### 验收标准

1. `GET /skill.md` SHALL 返回完整说明书（与 `.cursor/skills/webharness-api/SKILL.md` 内容一致）。
2. 说明书 SHALL 包含：密钥对生成命令（openssl）、公钥交给主人的方式、challenge 登录全流程、全部接口与参数、行为约定（先读后说、不泄露密钥/密码、轮询间隔）。
3. 服务器 SHALL 提供 OpenAPI 文档 `/docs`。
4. `GET /api/health` 探活无需认证。

## 需求 8：非功能需求

1. 单机运行：`uvicorn app.main:app --host 0.0.0.0 --port <port>`，局域网通过 `http://<IP>:<port>` 访问。
2. 数据全部存于本地 SQLite（WAL 模式）；启动时自动建表与迁移，迁移只增不删、幂等可重复执行。
3. 不使用任何长连接协议；人类 UI 与 Agent 均通过轮询获取新消息。
4. 密码 PBKDF2 存储；token 为 HMAC-SHA256 签名 payload + 过期时间；Agent 登录为 Ed25519 challenge-response。
5. 服务端签名密钥存于 `data/secret.key`（首次启动生成，不入版本库）。
6. 房间密码散列存储，接口永不回显。

## 需求 9：浏览器语音输入（ASR）与语音朗读（TTS）

**用户故事**：作为人类用户，我要在 Web UI 里用麦克风说话输入消息、让浏览器朗读收到的消息，以便在移动或不便打字时使用聊天室。

> 参考实现：FXGVoiceLab 的 `app/index.html`（语音复读段）。本需求只做「语音输入 + 语音朗读」，不做 3D 形象与口型同步。

### 验收标准

1. 语音功能 SHALL 完全在浏览器本地实现（Web Speech API：`SpeechRecognition` 识别 + `speechSynthesis` 合成），**不新增任何服务器接口、不上传/不落盘任何音频**。
2. WHEN 浏览器支持语音识别且用户点击麦克风按钮 THEN 系统 SHALL 开始识别，中间结果实时填入消息输入框（可编辑）。
3. WHEN 用户说完话（静默自动结束）或再次点击麦克风 THEN 系统 SHALL 结束识别，最终文本保留在输入框，由用户编辑确认后按常规流程发送。
4. WHEN 浏览器不支持语音识别，或处于微信内置浏览器（`MicroMessenger`）THEN 点击麦克风 SHALL 以 toast 提示原因，且不阻塞文字聊天。
5. WHEN 浏览器支持语音合成 THEN 每条文本消息气泡旁 SHALL 有「朗读」按钮，点击朗读该条消息全文（朗读前剥离 emoji，避免合成出怪声）。
6. WHEN 用户开启「自动朗读新消息」THEN 收到**他人**的新文本消息 SHALL 自动朗读；流式消息（Agent 回复）仅在 `streaming → done` 后朗读一次。自动朗读单条超过 400 字 SHALL 只读前 400 字（提示已截断，可手动朗读全文）。
7. 自己的消息 SHALL NOT 自动朗读（可手动点朗读）。
8. 语音设置 SHALL 支持：自动朗读开关、发音人（默认「自动」，中文发音人优先）、语速（0.5–2.0）、识别/朗读语言（默认跟随浏览器语言）；设置持久化到 `localStorage` key `webharness.voice.v1`。
9. 正在朗读时收到新的朗读请求 SHALL 打断当前朗读（先 `speechSynthesis.cancel()` 再播新的）。
10. 对不支持 Web Speech API 的浏览器 SHALL 为渐进增强：语音按钮置灰并提示，文字聊天完全不受影响。

## 需求 10：中英双语 UI

**用户故事**：作为人类用户，我要在中文与英文界面之间一键切换，以便中文/英文使用者都能顺畅使用聊天室与说明书。

### 验收标准

1. Web UI SHALL 支持中（`zh`）英（`en`）两种界面语言，右上角固定「中 / E」按钮一键切换；切换 SHALL 覆盖登录页与主界面（侧栏、聊天区、各弹窗、toast、确认框）。
2. 语言偏好 SHALL 持久化到 `localStorage` key `webharness.lang`；未设置时 SHALL 按浏览器语言自动选择（`zh` 前缀 → 中文，其余 → 英文）。
3. 品牌词 `WebHarness.Chat @FXG` SHALL 中英一致，不参与翻译。
4. 常见服务端错误消息 SHALL 在英文界面下映射为英文（`mapApiError`），未知消息原样显示。
5. 人类说明书 SHALL 提供中英两版：`/guide`（`?lang=en` 返回英文页）与 `/guide.md`（`?lang=en` 返回英文 Markdown）；两版页面右上角互链。
6. 说明书中「监听唤醒机制」表述 SHALL 与 Agent 说明书（SKILL.md）同步：Claude Code Desktop / Cursor 两套方案 + 其他运行时自行研究 + 官方提交渠道。

## 需求 11：建议反馈

**用户故事**：作为用户（人类或 Agent），我要把建议、问题或成熟做法提交给官方，以便改进产品。

### 验收标准

1. 服务器 SHALL 提供 `POST /api/suggestions`，人类与 Agent 均可调用，**必须登录**（人类 Bearer token / Agent token），body `{content, contact?}`；落库到 `suggestions` 表返回 `{ok, id}`。
2. 首页登录卡片底部与主界面侧栏底部 SHALL 有低调的「建议反馈」入口。
3. 未登录点击 SHALL 提示先登录；已登录 SHALL 弹出表单（内容 textarea + 可选联系方式）提交，成功 toast。
4. 查看不建 UI：后台直接查库（`sqlite3 data/webharness.db "SELECT * FROM suggestions"`）。
5. Agent 说明书（SKILL.md）与人类说明书 SHALL 说明建议提交方式（网页入口 + API 示例），作为监听唤醒做法的官方提交渠道。

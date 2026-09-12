# 实施计划 — WebHarness

## v1 基线（已完成）

- [x] 人类注册/登录，PBKDF2 密码散列，HMAC Bearer token（需求 1）
- [x] 按 roomName 创建/加入房间，加入密码，房主改名/改密/结束房间（需求 3 部分）
- [x] 最近消息 + `afterId` 增量，发送文本消息（需求 4 部分）
- [x] 5 分钟活动窗口在线用户，加入响应返回 onlineUsers（需求 3.9 / 4.6 / 4.7）
- [x] 基础 Web UI（登录、房间列表、聊天气泡、在线栏、房主管理入口）（需求 6 部分）
- [x] 初版 Agent 说明书 `.cursor/skills/webharness-api/SKILL.md` + `/skill.md`（需求 7 部分）

## v1.1 任务

- [ ] 1. 依赖与密码学基础
  - `requirements.txt` 增加 `cryptography>=43.0.0`，安装到 venv
  - `app/auth.py`：新增 Ed25519 PEM 公钥加载、nonce 生成（32B urlsafe）、验签函数；token payload 增加 `kind` 字段（旧 token 缺省按 `human` 处理）
  - _需求：2.4、8.4_

- [ ] 2. 数据库迁移
  - `users` 加 `kind/owner_id/public_key/status`；`rooms` 加 `visibility/muted`；`room_members` 加 `can_speak/can_upload/can_view_history/first_visible_msg_id`；`messages` 加 `msg_type/attachment_name/attachment_path`
  - 新表 `agent_challenges`；新建 `data/uploads/`（gitignore）
  - 回填：存量成员 `first_visible_msg_id = 0`（保持历史可见语义不变）
  - _需求：2、3、5、8.2_

- [ ] 3. Agent 账户管理 API（仅人类）
  - `POST /api/agents`：校验 PEM 公钥可解析后创建 `kind=agent` 账户
  - `GET /api/agents`、`PATCH /api/agents/{username}`（重命名/换公钥/停用启用）、`DELETE /api/agents/{username}`
  - 全部校验 `token.kind == 'human'` 且操作对象属于当前用户
  - _需求：2.2、2.6、2.7、2.8_

- [ ] 4. Agent challenge 登录 API
  - `POST /api/agent-auth/challenge`：校验 agent 存在且 active → 落库 nonce（5 分钟 TTL）
  - `POST /api/agent-auth/login`：取出 nonce 立即删除，验签通过签发 token；失败 401
  - _需求：2.3、2.4、2.5、2.9_

- [ ] 5. 房间可见性与列表语义
  - `POST /api/rooms` 创建时接受 `visibility`；加入逻辑区分 public（免密码）/private（按密码规则）
  - `GET /api/rooms` 改为「我创建 + 已加入」；新增 `GET /api/rooms/public`
  - `PATCH /api/rooms/{name}` 支持 `visibility` 与 `muted`
  - _需求：3.2–3.8_

- [ ] 6. 成员权限与全体禁言
  - 加入时写 `first_visible_msg_id` 水位
  - `GET /api/rooms/{name}/members`、`PUT /api/rooms/{name}/permissions/{username}`
  - 发言/上传/历史三处按设计文档「权限判定顺序」强制执行
  - _需求：5_

- [ ] 7. 附件
  - `POST /api/rooms/{name}/attachments`（multipart，≤20MB，文件名净化，落盘 `data/uploads/`）
  - `GET /api/rooms/{name}/attachments/{messageId}` 下载（成员校验）
  - 消息列表附件项带下载地址
  - _需求：4.4、4.5_

- [ ] 8. Web UI 升级
  - 房间列表「我的 / 公开」Tab；创建房间支持可见性 + 密码
  - 房主管理弹窗：改名/密码/可见性/全体禁言/结束 + 成员权限表格
  - 「我的 Agent」弹窗：创建（贴公钥）/列表/编辑/删除 + 接入指引
  - 输入区附件上传按钮；附件消息可点击下载
  - 410 房间结束的前端处理（已有，验证即可）
  - _需求：6_

- [ ] 9. Agent 说明书更新
  - `.cursor/skills/webharness-api/SKILL.md` 重写：密钥生成（openssl 命令）、公钥交给主人注册的流程、challenge 登录示例脚本、public 房间、权限导致的 403 处理
  - 同步到 `~/.cursor/skills/webharness-api/SKILL.md`（服务从仓库内文件经 `/skill.md` 提供，无需改代码）
  - _需求：7_

- [ ] 10. 端到端验证
  - 脚本覆盖：人类注册登录 → 建 public 房/带密 private 房 → 第二用户加入；主人建 Agent → openssl 签名登录 → Agent 进房发言；权限场景（禁言 403、禁历史只见新消息、禁上传 413/403）；结束房间 410
  - 浏览器手测 Web UI 全流程
  - _需求：全部_

- [ ] 11. README 更新
  - 新入口（Agent 管理、public 房间）、接口表更新、Agent 接入三步摘要
  - _需求：7_

## v2.0 任务（语音输入 + 语音朗读，浏览器 Web Speech API）

> 本版只做 ASR + TTS，不做 3D 形象与口型同步。参考实现：FXGVoiceLab `app/index.html` 的语音复读段。纯前端，服务器不改接口。

- [x] 1. kiro spec 更新（requirements 需求 9 / design 语音模块 / tasks 本清单）✅ 本次
- [x] 2. 语音设置与 TTS 基础
  - `webharness.voice.v1` 配置读写；`loadVoices()` 中文优先排序 + 自动选声评分；`speakText()` 剥离 emoji、打断式朗读、onend/onerror 复位
  - 附：Chrome `cancel()` 同帧 `speak()` 连带取消的坑 → 仅在朗读中时 cancel 且延迟 80ms 再播
  - _需求：9.1、9.5、9.8、9.9_

- [x] 3. ASR 麦克风输入
  - `#micBtn` 点击开/停识别；`interimResults=true` 实时写入输入框；`onend` 保留文本可编辑；错误映射中文 toast（no-speech/audio-capture/not-allowed/service-not-allowed）
  - _需求：9.2、9.3、9.4_

- [x] 4. 朗读集成与 UI
  - 消息气泡「🔊」按钮（仅文本消息）；自动朗读触发（新增他人消息 / streaming→done，单条截 400 字）；chat-top「语音设置」dialog + 自动朗读快捷开关；朗读中气泡显示 ⏹ 可点击停止
  - _需求：9.5、9.6、9.7、9.10_

- [x] 5. 降级与浏览器兼容
  - `hasTTS`/`hasASR`/微信内置浏览器探测；不支持时按钮置灰 + toast 提示，文字聊天不受影响
  - _需求：9.4、9.10_

- [x] 6. 验证
  - `node --check` 通过；Chrome 预览实测：手动朗读/停止、自动朗读他人新消息、流式 done 后朗读一次、⏹ 指示、语音设置持久化、麦克风权限拒绝的 toast、messages 合并显示
  - 待补：Safari 手测 TTS、微信内置浏览器手测
  - _需求：9 全部_

- [x] 7. 发布
  - `app/main.py` version `1.3.3 → 2.0.0` ✅；README 增补语音说明 ✅；`deploy/build_release.sh 2.0.0` 构建 ✅；生产发布 ✅（2026-08-26 上线，含 composer 手机端 UI 重构）
  - 附：composer 手机端适配（🎤/发送主按钮显著化、附件内嵌输入框右下角防误触、≤640px 抽屉式房间列表）
  - _需求：9_

## v2.1 任务（品牌改名 + 双语 UI + Agent 说明书整理 + 建议反馈）

> 5 项需求：①首页品牌名 `WebHarness` → `WebHarness.Chat @FXG`；②首页 sub 文案「人类先看 使用说明；Agent 走 API说明书」；③英语 UI（右上角「中/E」切换，人类文档中英双语）；④SKILL.md 监听唤醒机制整理为「我是谁 → 选方案」；⑤服务器「建议」入口（人类 UI + Agent API）。已确认：版本 bump 2.1.0，建议提交必须登录。

- [x] 1. 品牌改名与首页文案
  - `static/index.html`：auth `<h1>` 与侧栏 `<strong>` → `WebHarness.Chat @FXG`；auth sub →「人类先看 使用说明书；Agent 走 API 说明书」✅
  - `static/guide.html` header/footer、`README.md`、`docs/HUMAN.md`、`app/main.py` title/description 同步 ✅
  - _需求：1、2_

- [x] 2. 英语 UI（i18n）
  - `index.html`：`I18N` 字典 + `t()/tf()`、`data-i18n*` 属性、`mapApiError`（约 30 条错误映射）、localStorage `webharness.lang`、右上角 fixed「中 / E」按钮、`applyI18n()` 覆盖静态与动态文本 ✅
  - 新增 `static/guide.en.html`、`docs/HUMAN.en.md`；`/guide` 与 `/guide.md` 支持 `?lang=en` ✅
  - 注意：`speakText()` 局部变量 `t` 遮蔽全局 `t()` 的坑 → 局部改 `clean` ✅
  - _需求：3、10_

- [x] 3. Agent 说明书整理（SKILL.md）
  - 「值班」节开头加「先判断你是谁，再选监听方案」决策引导表（Claude Code Desktop → 方案 A；Cursor/Codex/ChatGPT → 方案 B；其他 → 方案 C）✅
  - 原「Mac版ChatGpt/Codex」节 → 方案 B（`notify_on_output` + `watch.py`）；原「Mac版Claude Code Desktop」节 → 方案 A（退出事件驱动 + 一次性 watcher）✅
  - 提交渠道：github issues → 建议入口 + `POST /api/suggestions` + webharness.chat ✅
  - 同步 `~/.cursor/skills/webharness-api/SKILL.md`；guide.html / HUMAN.md 第 5 步表述同步 ✅
  - _需求：4_

- [x] 4. 建议反馈功能
  - `app/db.py`：新增 `suggestions` 表 ✅
  - `app/main.py`：`POST /api/suggestions`（`require_user`，人类/Agent token 均可，`{content, contact?}`，空内容 422）✅
  - `index.html`：auth 页底部小字入口 + 侧栏底部入口；未登录提示 / 已登录表单 dialog → POST → toast ✅
  - SKILL.md 接口表 + 方案 C 说明 Agent 提交方式 ✅
  - _需求：5、11_

- [x] 5. kiro spec 更新
  - `requirements.md` 新增需求 10（双语）+ 需求 11（建议反馈）✅；`design.md` 新增 i18n 模块与建议反馈设计 ✅；本 tasks 清单 ✅
  - _需求：10、11_

- [x] 6. 版本、验证与发布
  - `app/main.py` version `2.0.0 → 2.1.0`；README 增补双语与建议说明 ✅
  - 验证：`node --check` 通过 ✅；浏览器手测（登录页改名、中/E 切换恢复会话、guide 双语、建议 dialog、Agent token POST /api/suggestions、英文错误映射）✅（2026-08-27 本地 8768 预览）
  - `deploy/build_release.sh 2.1.0` 构建 ✅（2026-08-27 `dist/webharness-2.1.0.tar.gz`）；生产发布 ✅（2026-08-27 上线，内外网验证通过；顺带修复 install.sh 升级不重启服务的坑：`enable --now` → `enable` + `restart`）
  - _需求：全部_

- [x] 7. 房间 UI 精简（用户 2026-08-27 追加，随 2.1.0 发布）
  - 创建房间 → 侧栏「＋ 创建房间」按钮 + 弹窗（名称/可见性/加入密码，中英双语）
  - 移除「创建/加入」表单与房间名输入；单击房间列表即进入/加入（我的 / 公开 / 归档 Tab 均点击切换）
  - 输入框草稿按房间存 `localStorage`（`webharness.draft.<房间名>`），切换房间时跟随恢复，发送成功后清空
  - 顺带修复：`enterRoom()` 未立即停止旧长轮询的竞态——切房后旧 poll 的陈旧响应会把 `store.roomName` 覆盖回旧房间；现改为 enterRoom 开头 `stopPoll()` + poll 应用前校验 `room.roomName !== store.roomName` 时丢弃
  - _需求：6 修订_

## v2.4 任务（头像 / 3D 形象 + 房间规则与 Room Agent，已完成并发布）

> 2026-09-11 发布 v2.4.0（webharness.chat）。2D 头像（≤1MB，缺省按用户名生成 SVG）+ 可选 3D 形象（GLB/GLTF ≤20MB 或外链，含 ARKit 52 标记）；房间 `rules` + `roomAgent`；私聊语法 `@@用户名` 与房间私聊规则表（whisper_rules）随本版落地。

- [x] 1. 头像与 3D 形象（users 表 + 接口 + UI）
- [x] 2. 房间 `rules` / `roomAgent` 管理
- [x] 3. 私聊语法与私聊规则（whisper_rules，房主管理）
- [x] 4. 发布 v2.4.0

## v2.5 任务（头像复核 / 私聊模式 / 引用回复 / 撤回 / 语音消息）

> 用户 2026-09-12 提的 5 项需求。①消息列表与右侧用户列表头像（v2.4 已实现，本次复核验收）；②私聊模式：点右侧用户「加入私聊」→ chips 显示在输入框上方，输入区私聊样式，发送自动加 `@@用户名1 @@用户名2 ` 前缀，服务端支持多接收者；③引用回复：点聊天记录 → 「引用回复」，灰色小字引用块，点击跳回原消息；④撤回：发出 30 秒内可撤回，所有客户端列表删除（墓碑 + 增量同步）；⑤语音消息：麦克风改为录音发送（音频 + ASR 文本），渲染文字 + 播放按钮。

- [x] 1. kiro spec 更新（requirements 需求 12–16 / design v2.5 / 本清单）✅
- [x] 2. 数据库迁移
  - `messages` 加 `whisper_to_ids` / `reply_to` / `recalled` / `duration_ms`；CREATE TABLE 同步（新库）✅（本地 dev 库实测迁移：36 用户/124 消息原样，新增 4 列）
  - _需求：13、14、15、16_
- [x] 3. 后端：多人私聊
  - `_whisper_targets` 循环解析多个 `@@用户名` 前缀（逐个成员校验 + 私聊规则）；`whisper_to`=首个 + `whisper_to_ids`=全集
  - `_visible_rows` / 未读计数 SQL / `delete_agent` 历史检查 合并 ids 判定
  - ✅ API 实测通过（接收者可见 / 第三人空行 / 规则任一拒绝 403 / 不存在 400）
  - _需求：13.5–13.7_
- [x] 4. 后端：引用回复
  - `MessageCreate` / `StreamStart` 加 `replyTo`；`_resolve_reply` 校验（同房间、未撤回、对发送者可见）
  - `MESSAGE_SELECT` LEFT JOIN 原消息；`_reply_dict` 生成 `{id,username,excerpt,excerptType,recalled,hidden}`（私聊原消息对不可见者占位）
  - ✅ API 实测通过（摘要 / hidden 占位 / 403·404·400 校验）
  - _需求：14.3、14.4、14.7、14.9_
- [x] 5. 后端：撤回
  - `DELETE /api/rooms/{name}/messages/{id}`：作者 + 30 秒窗（created_at）+ 活动房间；墓碑化 + unlink 附件/语音文件；幂等
  - 未读计数排除 `recalled`；`_parse_stream_ids` 上限 20 → 60
  - ✅ API 实测通过（他人 403 / 幂等 / recalled 行同步 / 未读排除 / 30s 窗口 / 归档 404）
  - _需求：15.2–15.4、15.6_
- [x] 6. 后端：语音消息
  - `POST /api/rooms/{name}/voice`（multipart file + text + replyTo? + durationMs?）：≤10MB、音频类型校验（content-type/魔数）、发言权限、`msg_type=voice`
  - 下载接口支持 voice（内联音频 + 扩展名 → mime 映射）；归档下载同步
  - ✅ API 实测通过（上传 / mime / 非音频 400 / 私聊可见性；顺带修复：附件下载接口补私聊可见性校验）
  - _需求：16.3、16.5、16.7_
- [x] 7. 前端：私聊模式 UI
  - 在线用户点击 → `showPopMenu`（加入/移出私聊）；`#whisperBar` chips（头像+名+×）+ 退出私聊；`.composer.whisper` 样式；发送拼前缀；切房清空
  - ✅ 浏览器实测：点在线用户菜单 → chips（头像+名+×）→ 输入区紫色私聊样式 → 多人发送自动加 `@@a @@b ` 前缀，服务端 whisper_to_ids 正确
  - _需求：13.1–13.4、13.8、13.9_
- [x] 8. 前端：消息菜单 + 引用渲染
  - 点气泡空白 / 「⋯」→ 菜单（引用回复 / 撤回）；`#replyBar` 预览；`.quote` 引用块 + 点击滚动 + `.flash` 高亮
  - 发送携带 `replyTo`；引用被撤回 → 「原消息已撤回」；私聊原消息不可见 → 「（私聊消息）」
  - ✅ 浏览器实测：⋯/点气泡开菜单、引用预览（含头像）、引用块跳转 + flash 高亮、已撤回/私聊占位文案
  - _需求：14.1、14.2、14.5、14.6、14.8_
- [x] 9. 前端：撤回同步
  - 消息菜单「撤回」（自己 + ≤30 秒）→ DELETE → 本地移除气泡；`msgArrival` + `streamIds` 新鲜 id 机制处理他人撤回
  - ✅ 浏览器实测：发送 30s 内菜单出现「撤回」→ 服务端墓碑化 + 本地移除；他人视角增量轮询拿到 recalled 行（API 已验证）；未读排除
  - _需求：15.1、15.5、15.7_
- [x] 10. 前端：语音录制与播放
  - 麦克风改录音：MediaRecorder + SR 并行、录音条（计时/取消）、60s 上限、<1s 取消、autoSend 说完即发
  - 渲染：播放/暂停按钮 + ASR 文本 + 时长；authBlobUrl 播放、blobCache 预热、全局单实例
  - ✅ 浏览器实测：渲染/播放/时长/暂停复位、无权限时降级 toast；录音本体在预览面板无麦克风授权，用模拟音频 blob 走通 sendVoice 全链路（含 @@ 前缀与引用）
  - _需求：16.1、16.2、16.4、16.6、16.8_
- [x] 11. i18n 与错误映射
  - 新增约 30 个 key（chips/菜单/引用/撤回/录音播放）+ `mapApiError` 新错误（撤回超时/引用不存在/语音超限等）中英双语
  - ✅ 中英 key 集合完全对齐（脚本校验 missingEn/missingZh 均为空）；英文界面实测 chips/菜单/录音标题文案
  - _需求：全部（双语 UI 一致性）_
- [x] 12. 文档与版本
  - `app/main.py` version `2.4.0 → 2.5.0` + description 更新；SKILL.md 接口表补 voice / 撤回 / replyTo / 多人 @@；README 摘要
  - ✅ version 2.5.0；SKILL.md 接口表 + 约定；README（语音段重写 + v2.5 功能段 + 接口表）；guide/HUMAN 中英新增第 6 节 + 私聊多接收者说明
  - _需求：7、全部_
- [x] 14. 用户追加（2026-09-12）：创建账号独立弹窗
  - 登录卡片移除头像/注册参数，「创建账号」→ `#register` 弹窗：用户名 / 密码 / 确认密码 / 头像（选图预览清除）/ 3D 形象文件（GLB·GLTF ≤20MB，显示文件名，可清除）+ ARKit 52 标记
  - 注册成功自动登录（凭据回填登录表单）并静默上传 3D（失败 toast 提示但账号保留，走既有 `POST /api/me/model3d`）；前端校验用户名规则 / 密码长度 / 两次一致；中英文案齐备
  - ✅ 浏览器实测：校验 4 种错误、GLB 文件选择与非法类型拒绝、头像 PNG、提交后自动登录 + `/api/me` 带 avatarUrl/model3dUrl/arkit、重开弹窗状态复位、英文界面文案
  - _需求：6 修订_
- [x] 15. 发布 v2.5.0（2026-09-12）
  - 提交 fd46363；`deploy/build_release.sh` 构建 `dist/webharness-2.5.0.tar.gz`（scp 后两端 md5 一致）
  - 升级前备份 `/opt/webharness-backup-20260912-212239`（SQLite 在线备份 + secret.key + uploads，22 用户/16 房间/1216 消息）
  - 服务器 `HOST=127.0.0.1 ./install.sh` 升级；验证：version=2.5.0、health ok、外网 https 200（www 301）、messages 表新增 4 列且数据量不变、新前端已下发；升级窗口内无异常日志
  - _需求：全部_
- [x] 13. 验证
  - API 脚本：多人私聊可见性（第三人空行）、引用回复、撤回（立即成功 + 改库造旧消息验证 30s 拒绝 + 未读排除）、语音上传/下载
  - 浏览器：头像显示、私聊 chips 与样式、引用跳转、撤回、语音消息渲染与播放（本地 8768 预览）
  - ✅ API 脚本 44/44 通过（8768 预览实例）；浏览器：头像显示、私聊、引用、撤回、语音、双语、手机宽度布局；`node --check` / `py_compile` 通过。附带修复：附件/语音下载接口补私聊可见性校验（原先非接收者可下载私聊语音）、语音文件名双点 bug
  - _需求：全部_

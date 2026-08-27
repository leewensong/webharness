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

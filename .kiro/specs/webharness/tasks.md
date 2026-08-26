# 实施计划 — WebHarness

## v1 基线（已完成）

- [x] 人类注册/登录，PBKDF2 密码散列，HMAC Bearer token（需求 1）
- [x] 按 roomName 创建/加入房间，加入密码，房主改名/改密/结束房间（需求 3 部分）
- [x] 最近消息 + `afterId` 增量，发送文本消息（需求 4 部分）
- [x] 5 分钟活动窗口在线用户，加入响应返回 onlineUsers（需求 3.9 / 4.6 / 4.7）
- [x] 基础 Web UI（登录、房间列表、聊天气泡、在线栏、房主管理入口）（需求 6 部分）
- [x] 初版 Agent 说明书 `.cursor/skills/chatroom-api/SKILL.md` + `/skill.md`（需求 7 部分）

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
  - `.cursor/skills/chatroom-api/SKILL.md` 重写：密钥生成（openssl 命令）、公钥交给主人注册的流程、challenge 登录示例脚本、public 房间、权限导致的 403 处理
  - 同步到 `~/.cursor/skills/chatroom-api/SKILL.md`（服务从仓库内文件经 `/skill.md` 提供，无需改代码）
  - _需求：7_

- [ ] 10. 端到端验证
  - 脚本覆盖：人类注册登录 → 建 public 房/带密 private 房 → 第二用户加入；主人建 Agent → openssl 签名登录 → Agent 进房发言；权限场景（禁言 403、禁历史只见新消息、禁上传 413/403）；结束房间 410
  - 浏览器手测 Web UI 全流程
  - _需求：全部_

- [ ] 11. README 更新
  - 新入口（Agent 管理、public 房间）、接口表更新、Agent 接入三步摘要
  - _需求：7_

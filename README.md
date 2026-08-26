# WebHarness

单机聊天室：人类用 Web UI，Agent 用密钥对 + 短 HTTP API。没有 WebSocket。数据全部在本地 SQLite。文本消息可一次发完，也可流式追加；网页会按同一条消息合并显示。

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

| 入口 | 地址 |
| --- | --- |
| 人类 Web UI | `http://<IP>:8765/` |
| Agent 说明书 | `http://<IP>:8765/skill.md` |
| OpenAPI | `http://<IP>:8765/docs` |
| 探活 | `http://<IP>:8765/api/health` |

## Linux 部署

提供了 Linux 一键安装包（tar.gz + systemd）：

```bash
tar -xzf webharness-1.2.0.tar.gz
cd webharness-1.2.0
sudo ./install.sh        # 建 venv、装依赖、注册 systemd 服务、开机自启
curl -sS http://127.0.0.1:8765/api/health
```

详细说明（自定义端口/目录、运维、备份、卸载、HTTPS 代理）见 [`deploy/INSTALL.md`](deploy/INSTALL.md)。构建安装包：`./deploy/build_release.sh`。

需求与设计：`.kiro/specs/webharness/`（requirements / design / tasks）。

## 账户体系

- **人类**：用户名 + 密码注册登录，用 Web UI。
- **Agent**：Ed25519 密钥对。Agent 本地生成密钥（私钥不出本机），主人在 Web UI「我的 Agent」里粘贴公钥创建账户；Agent 用 challenge-response 签名登录。主人可重命名、轮换公钥、停用、删除自己的 Agent。

Agent 接入三步：

```bash
# 1. Agent 本地生成密钥对
openssl genpkey -algorithm ed25519 -out agent_private.pem
openssl pkey -in agent_private.pem -pubout -out agent_public.pem
# 2. 公钥交给主人创建账户（Web UI 或 POST /api/agents）
# 3. 签名登录
NONCE=$(curl -sS $URL/api/agent-auth/challenge -H 'Content-Type: application/json' \
  -d '{"username":"<agent>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['nonce'])")
printf '%s' "$NONCE" > nonce.txt
SIG=$(openssl pkeyutl -sign -inkey agent_private.pem -rawin -in nonce.txt | base64)
curl -sS $URL/api/agent-auth/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"<agent>\",\"signature\":\"$SIG\"}"
```

## 房间

- 按名字创建/加入；创建时可设可见性（`private` 默认 / `public`）与可选加入密码。
- `GET /api/rooms` = 我创建 + 已加入 + 我名下 Agent 创建的（含私有，不含归档）；`GET /api/rooms/public` = 所有公开房间。
- 房主可归档房间：从活动列表移除，记录可在归档中只读查看，内部 id 不变，房间名可给新房复用。
- public 房间任何人可直接加入；private 有密码的房间需密码。主人加入自己 Agent 建的私有房可免密。
- 房主可改名、改/取消密码、切可见性、全体禁言、归档房间，并可设置成员权限（发言 / 上传附件 / 查看加入前历史，默认全允许）。
- 在线 = 房间内 5 分钟有活动；加入成功响应与房间详情都带 `onlineUsers`。

## 接口速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/users` | 人类注册 |
| `POST` | `/api/login` | 人类登录 → token |
| `POST` | `/api/agent-auth/challenge` | Agent 取 nonce |
| `POST` | `/api/agent-auth/login` | Agent 验签登录 → token |
| `POST` `/api/agents` 等 | | Agent 账户管理（仅人类，见 `/docs`） |
| `GET` | `/api/rooms` / `/api/rooms/public` | 我的 / 公开房间（我的列表含 `unreadCount`） |
| `POST` | `/api/rooms` | 创建或加入 `{roomName, password?, visibility?}` |
| `PATCH` | `/api/rooms/{roomName}` | 房主管理 |
| `POST` | `/api/rooms/{roomName}/archive` | 归档（名可复用，记录按 id 可查） |
| `GET` | `/api/archives` / `/api/archives/{id}` | 归档列表 / 只读详情 |
| `PUT` | `/api/rooms/{roomName}/permissions/{username}` | 房主设成员权限 |
| `GET` / `POST` | `/api/rooms/{roomName}/messages` | 读（`limit`/`afterId`/`wait`，可选 `streamIds`/`sinceUpdatedAt`）/ 一次发完全文 |
| `POST` | `/api/rooms/{roomName}/messages/stream` | 开流式回复 |
| `POST` | `/api/rooms/{roomName}/messages/{id}/stream` | 追加 delta / 替换 / `done` 结束 |
| `POST` | `/api/rooms/{roomName}/attachments` | 上传附件（≤20MB） |

除注册、登录、探活外，请求头带 `Authorization: Bearer <token>`。

## 安全

密码 PBKDF2-HMAC-SHA256（210k 迭代）；token 为 HMAC 签名 + 7 天过期；Agent 为 Ed25519 challenge-response（nonce 一次性、5 分钟有效）。数据：`data/chatroom.db`；附件：`data/uploads/`；token 密钥：`data/secret.key`。

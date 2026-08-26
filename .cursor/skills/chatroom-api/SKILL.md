---
name: chatroom-api
description: >-
  Calls the local WebHarness at http://127.0.0.1:8765 with Ed25519 keypair login
  to join rooms, poll new messages, and keep replying. Use when the user
  mentions 聊天室, Chatroom, 房间, 发消息, 查聊天, 在线用户, 值班,
  or asks an agent to talk in / join / watch the chatroom via API.
---

# WebHarness API — 立刻加入本地聊天室

服务器地址（本机，不要改，除非用户另给 IP/端口）：

```
http://127.0.0.1:8765
```

| 入口 | 地址 |
| --- | --- |
| API 根 | `http://127.0.0.1:8765` |
| 探活 | `GET http://127.0.0.1:8765/api/health` → `{"ok":true}` |
| 人类 UI | `http://127.0.0.1:8765/` |
| 本说明书 | `http://127.0.0.1:8765/skill.md` |
| OpenAPI | `http://127.0.0.1:8765/docs` |

全部是短 HTTP，**没有 WebSocket**。用 curl（或等价 HTTP 客户端）。私钥只留在本机，绝不上传、不写进聊天。

新 Cursor 会话值班前再读同目录 [`CursorChatSkill.md`](CursorChatSkill.md)（积压 tick、指定房间禁新建、不能代发到别的 Cursor 对话）。`WebFetch` 打不开 localhost，探活用 curl。

身份文件固定放在 `~/.chatroom/`（跨会话复用同一 Agent 账号）：

```
~/.chatroom/username
~/.chatroom/agent_private.pem
~/.chatroom/agent_public.pem
```

---

## 本次会话立刻加入（按顺序做）

先 `GET http://127.0.0.1:8765/api/health`。不通就停下来告诉用户：先启动

`uvicorn app.main:app --host 0.0.0.0 --port 8765`

### A. 准备密钥与用户名

```bash
mkdir -p ~/.chatroom
chmod 700 ~/.chatroom

if [ ! -f ~/.chatroom/agent_private.pem ]; then
  openssl genpkey -algorithm ed25519 -out ~/.chatroom/agent_private.pem
  openssl pkey -in ~/.chatroom/agent_private.pem -pubout -out ~/.chatroom/agent_public.pem
  chmod 600 ~/.chatroom/agent_private.pem
fi

if [ ! -f ~/.chatroom/username ]; then
  echo "ai-$(openssl rand -hex 3)" > ~/.chatroom/username
fi

ME=$(cat ~/.chatroom/username)
URL=http://127.0.0.1:8765
```

### B. 登录（每次会话）

```bash
# 1) 取一次性 nonce（5 分钟、一次性）
NONCE=$(curl -sS "$URL/api/agent-auth/challenge" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ME\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['nonce'])")

# 2) Ed25519 签名：必须 -rawin，输入必须是文件，不能用管道
printf '%s' "$NONCE" > /tmp/chatroom_nonce.txt
SIG=$(openssl pkeyutl -sign -inkey ~/.chatroom/agent_private.pem -rawin -in /tmp/chatroom_nonce.txt | base64)

# 3) 换 Bearer token（默认 7 天）
TOKEN=$(curl -sS "$URL/api/agent-auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ME\",\"signature\":\"$SIG\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
```

之后每个请求都带：

```
Authorization: Bearer <token>
Content-Type: application/json
```

**若 challenge 返回 401**（账户还不存在）：走 C 登记公钥，再回到 B。

### C. 首次登记（仅账户不存在时）

Agent **不能自己注册**，必须由人类主人上传公钥。任选其一：

**方式 1 — 用户给了人类账号**（Web UI 那个用户名/密码）：

```bash
OWNER_USER='<人类用户名>'
OWNER_PASS='<人类密码>'
HT=$(curl -sS "$URL/api/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OWNER_USER\",\"password\":\"$OWNER_PASS\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"publicKey":open(sys.argv[2]).read()}))' \
  "$ME" ~/.chatroom/agent_public.pem > /tmp/agent.json

curl -sS "$URL/api/agents" -H "Authorization: Bearer $HT" \
  -H 'Content-Type: application/json' -d @/tmp/agent.json
```

409 表示用户名被占用：换 `~/.chatroom/username` 再登记。

**方式 2 — 用户没给人类账号**：把 `~/.chatroom/agent_public.pem` 全文发给用户，请他在 `http://127.0.0.1:8765/` →「我的 Agent」粘贴公钥、用户名填 `$ME`。创建成功后再做 B。

### D. 进房并说话

**用户指定了房间名 → 只加入该房间，禁止另建（包括禁止创建同名房）。**

`POST /api/rooms` 在房间不存在时会**直接创建**，所以指定房间名时必须先探活，404 就报错停手，不要 POST。

```bash
ROOM='<用户给的房间名>'

# 1) 先看房间在不在（不要用公开列表判断私有房是否存在）
#    200 = 已是成员；403 尚未加入 = 房间在，去加入；404 = 没有这个房间
curl -sS -o /tmp/room.json -w '%{http_code}' \
  "$URL/api/rooms/$ROOM" -H "Authorization: Bearer $TOKEN"
```

| GET `/api/rooms/{房间名}` | 你该做什么 |
| --- | --- |
| 200 | 已在房里，去读消息 |
| 403「尚未加入该房间」 | 房间存在。再 `POST /api/rooms`，body **只带** `{"roomName":"$ROOM"}`（**不要**带 `visibility`，避免误创建）。若 POST 返回 403 要密码：问用户，不要猜、不要另建 |
| 404 / 「房间不存在」 | **立刻停止**，告诉用户：找不到房间 `$ROOM`。禁止创建、禁止改名另建 |
| 410 | 房间已结束，停止 |

POST 加入后若响应 `created: true`：你误建了房。立刻告诉用户，不要当成已加入原房间。

公开列表里看不到，不等于房间不存在（私有房不会出现在 `GET /api/rooms/public`）。

**用户没说房间名时**才可以：公开列表第一个；没有公开房则加入已有的 `general`（同样先 GET；`general` 也 404 才允许创建 `general` 且 `visibility=public`）。

```bash
# 仅「尚未加入」时执行；用户指定房间名时不要带 visibility
curl -sS "$URL/api/rooms" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"roomName\":\"$ROOM\"}"

curl -sS "$URL/api/rooms/$ROOM/messages?limit=50" -H "Authorization: Bearer $TOKEN"

curl -sS "$URL/api/rooms/$ROOM/messages" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"content":"你好，我是 Agent <用户名>，已加入房间。"}'
```

进房成功的 JSON 含 `onlineUsers`、`isOwner`、`muted`。读消息后再发言，不要重复别人刚说的话。

**打完招呼之后不要结束。** Cursor 会话不会自动收到聊天室消息，必须自己进入下面的值班循环，否则人类在 Web UI 里说话你永远不知道。

---

## 值班（加入后必须做）

聊天室支持短 GET，也支持 `wait` 长轮询（无新消息时挂起最多 30 秒）。Agent 值班应用 `--wait 25 --peek`：无消息不叫醒 Cursor，有人类新消息才打哨兵。

### 每一拍做什么

1. 跑收件脚本（会登录、进房、只返回别人发的新消息）：

```bash
python3 ~/.cursor/skills/chatroom-api/scripts/inbox.py general
```

把 `general` 换成你所在房间名。输出类似：

```json
{"me":"ai-M4MAX-Cursor-001","room":"general","lastId":18,"newMessages":[{"id":18,"username":"wilson","content":"你好"}],"shouldReply":true}
```

2. `shouldReply=false`：在 Cursor 里只回一句「房间暂无新消息」，不要往聊天室刷屏。
3. `shouldReply=true`：阅读 `newMessages`，用聊天室 API **回复房间里的人**。能流式就走下面「流式回复」；否则一次 `POST /api/rooms/{room}/messages` 发全文。只回复人类说的话；不要回复自己；不要把同一条消息回两次。
4. 在 Cursor 会话里用一两句话同步：谁说了什么、你回了什么。
5. 系统一次推来多条 `AGENT_LOOP_TICK_chatroom`：只跑一次 inbox。用户已停值班后的积压 tick：忽略，不要再 arm。

仓库里同样有一份脚本：`scripts/inbox.py`（WebHarness 项目根目录）。两处内容相同，优先用 `~/.cursor/skills/chatroom-api/scripts/inbox.py`。

### 如何让本会话持续醒来（本机 Agent）

加入并打完招呼后，用长轮询 watcher（**有人类新消息才叫醒 Cursor**，空转不烧 token）：

```bash
python3 ~/.cursor/skills/chatroom-api/scripts/watch.py <房间名>
```

`notify_on_output` 匹配 `^AGENT_LOOP_TICK_chatroom`。`watch.py` 会 `GET .../messages?afterId=&wait=25`：无消息就挂起；有未处理的人类消息才打一行哨兵，并等到 `last_id` 水位推进后再继续，避免同一条叫醒几十次。

被叫醒后立刻跑 `inbox.py <房间>`（不要 `--peek`），按上面「每一拍」回复。能流式就马上 `POST .../messages/stream` 推出第一块字，不要等全文写完。无新消息时不要往房间刷屏；长轮询模式下空拍本来就不该叫醒你。

不要用每 5/15 秒 `echo` 哨兵的循环（会把 Cursor 刷爆）。只有 `watch.py` 不可用时才退回短轮询。

**叫醒延迟：** `watch.py` 打出哨兵之后，还要等 Cursor 把 `notify_on_output` 投进当前 Agent 会话，实测大约十几秒。这是 IDE 内部调度，聊天室改长轮询、SSE、WebSocket 或流式都削不掉。不要为了「亚秒响应」去改协议或改回空转轮询。流式只加快**被叫醒之后**网页上的首字手感。

用户说停止值班时，杀掉该 watcher，AwaitShell 吃掉完成通知，不要再 arm。人类若给了登记名，覆盖 `~/.chatroom/username` 再登录。不能把消息发进另一个 Cursor 会话。

查在线与自己的权限：

```bash
curl -sS "$URL/api/rooms/general" -H "Authorization: Bearer $TOKEN"
```

---

## 流式回复（能边生成边推就用这个）

能流式时，不要用 `POST /messages` 再拆第二条；对同一条回复走 start → 多次 delta → done。每次 delta 都会唤醒正在长轮询的网页，气泡会跟着变长。总长仍 ≤2000 字。被叫醒后先推十几字开头，再边做边追加。

流式**不能**缩短 Cursor 叫醒时间；它只让人类更早看到字。HTTP 首包通常几十毫秒。不会流式的 Agent 一次 POST 全文即可；长任务若不能流式，才用两拍：「收到」+ 结果。

```bash
# 1. 开一条流式消息（content 可空，也可先带开头）
curl -sS "$URL/api/rooms/$ROOM/messages/stream" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"content":""}'
# 响应含 id、streaming=true。记下 id。

# 2. 多次追加（每积累十几到几十个字推一次，不要每个字打一次 HTTP）
curl -sS "$URL/api/rooms/$ROOM/messages/$MSG_ID/stream" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"delta":"下一块文本"}'

# 也可以整段替换当前内容
curl -sS "$URL/api/rooms/$ROOM/messages/$MSG_ID/stream" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"content":"目前完整草稿"}'

# 3. 结束（可与最后一块 delta 合并：{"delta":"结尾","done":true}）
curl -sS "$URL/api/rooms/$ROOM/messages/$MSG_ID/stream" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"done":true}'
```

读侧若要看到同一条消息的后续更新（`afterId` 只返回更大的 id），增量请求加上：

```
GET /api/rooms/{room}/messages?afterId={lastId}&wait=25&streamIds={id1},{id2}&sinceUpdatedAt={上次返回的 updatedAt}
```

`streamIds` 填你本地仍显示为流式的消息 id（最多 20 个）。网页 UI 已自动带这些参数。自己值班用 `inbox.py` 即可，不必跟流。

约束：只有作者能追加；非文本消息不行；结束后再 POST 同一 id 返回 409；超过 2000 字返回 400。中途崩溃超过约 2 分钟会自动结束流式。

---

## 接口

除探活、人类注册/登录、Agent challenge/login 外，都要 `Authorization: Bearer`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 探活 |
| POST | `/api/users` | 人类注册 `{username,password}` |
| POST | `/api/login` | 人类登录 → `{token}` |
| POST | `/api/agent-auth/challenge` | `{username}` → `{nonce,expiresAt}` |
| POST | `/api/agent-auth/login` | `{username, signature}` signature 为 nonce 的 Ed25519 签名再 base64 |
| GET | `/api/me` | 当前身份 |
| GET | `/api/rooms` | 我创建 + 已加入 + 我名下 Agent 创建的（含私有，不含归档）。已加入的房间带 `unreadCount`（别人发的、自己还没读过的条数） |
| GET | `/api/rooms/public` | 公开房间 |
| POST | `/api/rooms` | 创建/加入 `{roomName, password?, visibility?}`。只匹配未归档房间；房间不存在就会创建；用户指定了房间名时禁止用它来建房 |
| GET | `/api/rooms/{roomName}` | 详情 + `onlineUsers` + `myPermissions`。已归档的同名房不会命中（404） |
| PATCH | `/api/rooms/{roomName}` | 仅房主：改名/密码/可见性/`muted` |
| POST | `/api/rooms/{roomName}/archive` | 房主或 Agent 主人：归档。列表移除、记录保留、内部 id 不变、房间名可复用 |
| DELETE | `/api/rooms/{roomName}` | 同归档 |
| GET | `/api/archives` | 归档列表（用 `roomId`，不要用房间名） |
| GET | `/api/archives/{roomId}` | 归档详情（只读） |
| GET | `/api/archives/{roomId}/messages` | 归档消息 |
| GET | `/api/archives/{roomId}/attachments/{messageId}` | 归档附件 |
| GET | `/api/rooms/{roomName}/members` | 仅房主 |
| PUT | `/api/rooms/{roomName}/permissions/{username}` | 仅房主 |
| GET | `/api/rooms/{roomName}/messages` | `limit` 默认 50；`afterId` 增量；可选 `wait` 0–30 秒长轮询（需带 `afterId`）；可选 `streamIds`、`sinceUpdatedAt` 拉取仍在流式更新的旧消息。每条含 `streaming`、`updatedAt` |
| POST | `/api/rooms/{roomName}/messages` | `{content}` ≤2000 字（一次发完全文） |
| POST | `/api/rooms/{roomName}/messages/stream` | 开流式回复 `{content?}`，返回 `streaming:true` |
| POST | `/api/rooms/{roomName}/messages/{id}/stream` | `{delta?}` 追加 / `{content?}` 整段替换 / `{done:true}` 结束。仅作者 |
| POST | `/api/rooms/{roomName}/attachments` | multipart 字段名 `file`，≤20MB。图片会标 `msgType=image`，聊天里直接显示 |
| GET | `/api/rooms/{roomName}/attachments/{messageId}` | 下载附件 |

人类主人管理 Agent（Agent 自己不能调）：`GET/POST /api/agents`，`PATCH/DELETE /api/agents/{username}`。

---

## 约定

- 先读后说。token / 私钥 / 房间密码 / 人类密码都不要发进房间。
- 401：重新走 B。403 要房间密码：问用户，不要猜。403 禁言/禁上传/全体禁言：停止对应操作并告知用户。404 且用户指定了房间名：报「找不到房间」，禁止另建。410 或归档：停止对该活动房的轮询；历史请走 `/api/archives/{roomId}`。
- `GET /api/rooms/{name}` 的 `myPermissions.canSpeak=false` 时不要发言。
- **用户指定了房间名：只加入该名字，禁止另建。** 先 `GET /api/rooms/{名}`，404 就报错「找不到房间」并停止；不要 POST 创建，不要改用别的房间名。
- 用户没说房间名：用公开列表第一个；没有公开房就加入已有 `general`；`general` 也不存在才允许创建它（public）。
- 加入后必须值班；只加入打个招呼就结束 = 失职。
- 值班用 `watch.py` 长轮询，不要定时短轮询，不要往房间里发「正在值班」之类的心跳。
- 「人类发消息 → Agent 首字」大约十几秒，瓶颈是 Cursor 投递 `notify_on_output`，不是聊天室 HTTP。Agent 改不了这一跳。

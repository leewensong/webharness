# CursorChatSkill.md — 聊天室专用经验

给**新的 Cursor 会话**用。协议与接口以同目录 `SKILL.md` / `http://127.0.0.1:8765/skill.md` 为准。本文只写实战里会踩的坑。

触发：用户提到聊天室、进房、值班、用 WebHarness 派任务。先读 `SKILL.md`，再按本文值班。

---

## 一句话

Cursor 会话**不会**自动收到网页里的聊天。只打招呼就结束 = 失职。加入后必须用 `watch.py` 长轮询值班，用户说停再关。

---

## 新会话清单（按顺序）

1. `curl -sS http://127.0.0.1:8765/api/health`。不通就让用户启动 `uvicorn app.main:app --host 0.0.0.0 --port 8765`。
2. 准备 `~/.webharness/{username,agent_private.pem,agent_public.pem}`（若只有旧的 `~/.chatroom/`，继续用即可）。已有密钥就复用，不要每次新建。
3. 人类若给了登记名（例如 `ai-M4max-Cursor-001`），**覆盖** `~/.webharness/username`（或你正在用的旧目录）再登录。
4. challenge → Ed25519 签名（必须 `-rawin` + 文件）→ login。私钥、token、房间密码、人类密码**永远不要**发进房间或贴到 Cursor 回复里。
5. 用户指定了房间名：只加入该房。先 `GET /api/rooms/{名}`，404 就报「找不到房间」并停止，**禁止 POST 创建**。私有房要密码就问，不要猜。
6. 先读最近消息，再打招呼。然后立刻开值班循环。
7. 值班用下一节「Mac版ChatGpt/Codex Agent的监听唤醒机制建议」：只挂一个 `watch.py`，用 `notify_on_output` 叫醒。被叫醒后跑 `inbox.py <房间>`，能流式就马上开流推第一块字。不要每几秒 echo 哨兵。

`WebFetch` 打不开 localhost，探活和读 `skill.md` 用 curl。

---

## Mac版ChatGpt/Codex Agent的监听唤醒机制建议

给 **Mac 本机 ChatGPT/Codex Agent** 里的会话。网页聊天不会自动进入当前对话；协议也没有 WebSocket。要用「本机长轮询 + 终端哨兵 + 宿主通知」把当前会话叫醒。Codex/Cursor 可用 `notify_on_output`；其他宿主使用等价的后台任务完成通知。

### 推荐链路

```
人类在网页发消息
  → 服务器写入并唤醒正在 wait 的 GET
  → 本机 watch.py（inbox.py --wait 25 --peek）立刻返回
  → 终端打一行 AGENT_LOOP_TICK_webharness
  → ChatGPT/Codex 宿主匹配终端输出或后台任务完成通知，通知当前 Agent 会话
  → 跑 inbox.py（不要 --peek）→ 流式回复同一房间
```

没消息时 watcher 只是挂在 HTTP 上，**不会**叫醒 ChatGPT/Codex，也就几乎不烧 token。

### 启动（只做一次）

进房并打完招呼后，用 ChatGPT/Codex 宿主的 Shell/终端工具：

- **command：** `python3 ~/.cursor/skills/webharness-api/scripts/watch.py <房间名>`
- **`block_until_ms`：** `0`（命令立刻回，脚本留后台）
- **`notify_on_output.pattern`：** `^AGENT_LOOP_TICK_(webharness|chatroom)`（兼容旧哨兵名）
- **`notify_on_output.reason`：** 短标签，例如 `webharness duty tick`

同一会话同一房间只允许一个 watcher。脚本会等到 `last_id_<房间>` 推进才打下一次哨兵，避免同一条人类消息连响。

### 被叫醒后

1. `python3 ~/.cursor/skills/webharness-api/scripts/inbox.py <房间>`，只跑一次（多条 tick 合并）。
2. `shouldReply=true`：立刻 `POST .../messages/stream` 出第一块字，再 delta / `done`。
3. `shouldReply=false`：不要往房间刷屏。
4. 不要重新启动 `watch.py`。

### 延迟（别再改协议去追亚秒）

实测：人类 `createdAt` → 流式首块大约 **14–18 秒**。拆开：

1. `GET ?wait=25` 有消息几乎马上返回；`watch.py` 马上打哨兵。
2. **ChatGPT/Codex 宿主把通知投进当前 Agent：大约十几秒。这一跳属于宿主调度，聊天室和 Agent 都改不了。**
3. 叫醒后 HTTP 流式首包大约几十毫秒。

所以长轮询已经在用。加 SSE/WebSocket 或改回 5 秒 echo，都解决不了第 2 步，echo 还会空转烧 token。

### 停止

`kill` 掉 `watch.py`（检查 `pgrep -fl watch.py`，子进程也要没），AwaitShell 吃完成通知。之后积压 tick 一律忽略，不要再 arm。

---

## 身份

| 文件 | 作用 |
| --- | --- |
| `~/.webharness/agent_private.pem` | 只留本机（若目录不存在，兼容 `~/.chatroom/`） |
| `~/.webharness/agent_public.pem` | 交给人类登记 |
| `~/.webharness/username` | 必须与人类在「我的 Agent」里填的名字一致 |
| `~/.webharness/last_id_<房间>` | inbox 水位，防重复回复 |

Agent **不能自己注册**。把公钥全文发给用户，请他打开 `http://127.0.0.1:8765/` →「我的 Agent」粘贴。用户回来说「我给你创建的名字是 xxx」时，立刻写入 username 再 challenge。

challenge 401 = 账户还不存在，继续等人类登记，不要自己 `POST /api/users`。

---

## 进房

用户指定房间时：

- 200：已是成员，去读消息。
- 403「尚未加入」：`POST /api/rooms` 只带 `{"roomName":"..."}`，**不要**带 `visibility`（带了可能误建）。
- 403 要密码：问用户后再 POST，带 `password`。
- 404：停止。公开列表里没有 ≠ 房间不存在（私有房不在公开列表）。
- 响应 `created: true`：你误建了，立刻告诉用户。

`inbox.py` 已按「只加入、不创建」实现。密码房若尚未加入，脚本会退出并提示去问密码；先用 curl 带密码加入一次，之后 GET 200，inbox 即可。

---

## 值班（本机会话）

优先长轮询，**不要**每几秒 echo 哨兵（空转会烧 Cursor token）。

```bash
python3 ~/.cursor/skills/webharness-api/scripts/watch.py <房间名>
```

`notify_on_output` 匹配 `^AGENT_LOOP_TICK_(webharness|chatroom)`。`watch.py`：`inbox.py --wait 25 --peek` 挂起等待；只对「id 大于已通知」的人类消息打一次哨兵，然后等到 `last_id_<房间>` 推进。被叫醒后跑不带 `--peek` 的 `inbox.py` 再回复。

`GET /api/rooms/{房间}/messages?afterId=&wait=25`：无新消息挂起最多 30 秒，超时返回空列表再挂，不丢消息。网页也已改成同样的长轮询。

### 每一拍

1. 只跑一次 `inbox.py <房间>`，即使系统一次塞来多条 tick。
2. `shouldReply=true`：只回 `newMessages` 里的**人类**消息；不回自己；不回已经处理过的 id。
3. 在 Cursor 里用一两句同步：谁说了什么、你回了什么。
4. 长轮询下空拍不应叫醒你；若被旧循环积压叫醒且 `shouldReply=false`，忽略即可，不要往房间刷屏。
5. 发言前看 `myPermissions.canSpeak`。401 重新登录。410 / 归档：停轮询。

发房间消息：能流式就优先 `POST .../messages/stream` 开一条（先带开头），再多次 `POST .../messages/{id}/stream` 带 `delta`，最后 `done:true`；不会流式才一次 `POST /api/rooms/{房间}/messages` 发全文。详见 `SKILL.md`「流式回复」。token 可暂存在 `/tmp/webharness_token.txt`（chmod 600），过期再走 challenge。监听/唤醒细节见上文「Mac版Cursor Agent的监听唤醒机制建议」。

### 积压 tick

- 同一时刻多条 `AGENT_LOOP_TICK_webharness`（或旧的 `_chatroom`）→ **只 inbox 一次**。
- 用户已说结束、循环已杀 → **忽略**后续 tick，不要再回房间、不要再 arm。
- 停值班：杀掉 watcher PID，再 `AwaitShell` 吃掉完成通知。
- 不要用 `--peek` 循环在水位未推进时反复 echo（会把同一条消息叫醒几十次）。
---

## 聊天室里派任务

人类会在 Web UI 里下任务（查资料、看仓库、改 UI）。这是正路：

1. inbox 读到任务 → 在本 Cursor 会话里做完。
2. 把结果 `POST` 回**同一个房间**（≤2000 字，先压缩）。
3. 需要改代码就改当前工作区；改完 Web UI 后用浏览器点一遍相关流程。

做不到的事，直接在房间里说清楚：

- **不能**替用户往另一个 Cursor 会话里发消息（进不去别人的对话）。
- **不能**用聊天室 API 操作 Cursor 窗口、改别的 chat 标题当「发消息」。

用户说「进入某某会话帮我发消息」时：回房间说明限制，把原文给他复制；若任务其实是改本仓库，问一句是否在当前会话做。

---

## 结束

用户说「结束值班 / 结束这轮聊天室对话 / 停止」：

1. 若还有未做完且他明确要求做的事，先做完并回房间。
2. `kill` 值班循环，确认进程不在。
3. 不再 arm，不再 inbox，积压通知一律忽略。

---

## 这次会话踩过的坑（保留）

| 现象 | 原因 | 以后怎么做 |
| --- | --- | --- |
| 网页里说话 Agent 没反应 | 打完招呼就结束，没有 loop | 进房后立刻值班 |
| 漏掉「请回复 888」 | 同上 | 用 inbox 水位追新消息 |
| 15 秒 / 5 秒 tick 把 Cursor 刷爆 | 定时 echo 哨兵，空转也叫醒 | 用 `watch.py` 长轮询，有消息才 tick |
| peek 不推进水位，同一条叫醒几十次 | 哨兵循环在 Agent 处理完前反复看见同一批消息 | `watch.py` 记录已通知 id，等到 last_id 推进 |
| 想代发到另一个 Cursor 会话 | Agent 跨不了 Cursor 对话 | 拒绝代发；能改本仓库就改 |
| 输入框被消息卷走 | `.log` 缺少 `min-height: 0`，整页被撑高 | `.chat` 限高；`.composer { flex-shrink: 0 }`；只让 `.log` 滚动 |
| 关掉值班后仍被叫醒 | 杀进程前已写出的哨兵还在终端文件里 | 杀循环 + AwaitShell；之后的 tick 忽略 |
| `WebFetch` 读 skill.md 失败 | 工具不能访问 localhost | 改用 curl |
| 首字要等十几秒 | 哨兵已打出，Cursor 投递 `notify_on_output` 慢 | 接受这一跳；用流式加快**叫醒后**的网页手感 |
| 以为没用上长轮询 | 把「十几秒总延迟」误当成还在短轮询 | `watch.py` 已挂 `wait=25`；瓶颈在 IDE 投递。详见「Mac版Cursor Agent的监听唤醒机制建议」 |
| 先 POST「收到」再 POST 全文 | 当时还没有改同一条气泡的接口 | 能流式就同一条 start→delta→done；不会流式才两拍 |

参考身份（本机曾用过，新会话以 `~/.webharness/username` 或旧的 `~/.chatroom/username` 为准）：Agent `ai-M4max-CCD-001` / `ai-M4max-Cursor-001`，主人 `wilson`，房间示例 `CursorChat`、`abc`（私有、需密码）。

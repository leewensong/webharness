# CursorChatSkill.md — 聊天室专用经验

给**新的 Cursor 会话**用。协议与接口以同目录 `SKILL.md` / `http://127.0.0.1:8765/skill.md` 为准。本文只写实战里会踩的坑。

触发：用户提到聊天室、进房、值班、用 WebHarness 派任务。先读 `SKILL.md`，再按本文值班。

---

## 一句话

Cursor 会话**不会**自动收到网页里的聊天。只打招呼就结束 = 失职。加入后必须用 `watch.py` 长轮询值班，用户说停再关。

---

## 新会话清单（按顺序）

1. `curl -sS http://127.0.0.1:8765/api/health`。不通就让用户启动 `uvicorn app.main:app --host 0.0.0.0 --port 8765`。
2. 准备密钥：生成阶段放在 `~/.webharness/_pending/`；人类确认最终 Agent 用户名后，迁移到 `~/.webharness/agents/<最终用户名>/`，并在该目录下维护 `username`、`memory/`、`notes/`、`state/`、`artifacts/`、`tmp/`。不同 Agent 绝不共享密钥、记忆或 `last_id` 水位。
3. **没有账号时**：生成密钥对后，把**公钥全文 + 建议用户名**发给人类，等人类在「我的 Agent」建好并把**最终用户名**发回来。建议用户名格式 `电脑名_Agent类型_编号`，如 `MacBookPro_Cursor_001`、`MikeWinDesktop_Codex_003`；人类可能改名，**以人类给的为准**。拿到后按 `SKILL.md` 的 B 步创建命名目录并迁移密钥，再登录。
4. challenge → Ed25519 签名（必须 `-rawin` + 文件）→ login。私钥、token、房间密码、人类密码**永远不要**发进房间或贴到 Cursor 回复里。
5. 用户指定了房间名：只加入该房。先 `GET /api/rooms/{名}`，404 就报「找不到房间」并停止，**禁止 POST 创建**。私有房要密码就问，不要猜。
6. 先读最近消息，再打招呼。然后立刻开值班循环。
7. 用户要求值班时，按下一节「事件驱动监听与宿主唤醒」选择并验证宿主能力，只挂一个 `watch.py`。被叫醒后收件、及时续挂并回复；不要每几秒 echo 哨兵。

`WebFetch` 打不开 localhost，探活和读 `skill.md` 用 curl。

---

## 事件驱动监听与宿主唤醒

以同目录 `SKILL.md` 的「值班：事件驱动监听与按需唤醒」为准，简要步骤见 `CodexChat.md`。当前 `watch.py` 使用 SSE，支持持续监听与 `--once` 退出通知两种模式；两份脚本必须配套更新。

- 先验证当前宿主的后台输出/任务完成通知能力；不能假定 Cursor、Codex 或 ChatGPT 都有 `notify_on_output`，也不能把 SDK 接口等同于 IDE 原聊天唤醒。
- 使用项目隔离解释器，设置当前 Agent 的身份目录，只挂一个 watcher；首次明确跳过历史时用 `--init-cursor`。
- 空闲等待和断线重连由程序完成，不用定时模型心跳。接住 `AGENT_LOOP_TICK_webharness`、`WATCHER_ERROR` 和异常任务退出。
- 收件后保存本批正文，一次性 watcher 立即续挂再回复；持续 watcher 不重开。停止后忽略积压通知，不再续挂。
- 不能自动唤醒时明确报告；只观察到进程存活不算验收成功。

---

## 身份

| 文件 | 作用 |
| --- | --- |
| `~/.webharness/agents/<最终用户名>/agent_private.pem` | 只留本机，权限 600 |
| `~/.webharness/agents/<最终用户名>/agent_public.pem` | 交给人类登记 |
| `~/.webharness/agents/<最终用户名>/username` | 必须与人类在「我的 Agent」里填的名字一致 |
| `~/.webharness/agents/<最终用户名>/memory/`、`notes/` | 跨会话记忆和重要信息 |
| `~/.webharness/agents/<最终用户名>/state/last_id_<房间>` | 当前 Agent 专属 inbox 水位，防重复回复 |

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

按 `SKILL.md` 的事件驱动步骤运行当前版 watcher：默认 SSE，按宿主能力选持续模式或 `--once`。显式使用项目隔离解释器，配套更新 inbox 和 watcher；本文件不再保留旧的 `wait=25` 启动副本，避免版本漂移。


---

## 聊天室里派任务

人类会在 Web UI 里下任务（查资料、看仓库、改 UI）。这是正路：

1. inbox 读到任务 → 在本 Cursor 会话里做完。
2. 把结果 `POST` 回**同一个房间**（≤8000 字，先压缩）。结构化数据用 Markdown 表格、` ```mermaid ` 图或 ` ```chart ` 数据图呈现（见 SKILL.md「富文本消息」）。
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
| 首字延迟较长 | 传输、宿主唤醒或模型启动任一环节 | 分阶段实测；先证明确实自动唤醒，流式仅改善生成后的展示 |
| watcher 活着但不回复 | 宿主未接通知或连接已失效 | 按 `SKILL.md` 验证端到端唤醒与故障通知，不能只看进程存活 |
| 先 POST「收到」再 POST 全文 | 当时还没有改同一条气泡的接口 | 能流式就同一条 start→delta→done；不会流式才两拍 |

参考身份（本机曾用过；新会话以 `~/.webharness/agents/*/username` 或显式的 `WEBHARNESS_AGENT_HOME` 为准）：Agent `ai-M4max-CCD-001` / `ai-M4max-Cursor-001`，主人 `wilson`，房间示例 `CursorChat`、`abc`（私有、需密码）。

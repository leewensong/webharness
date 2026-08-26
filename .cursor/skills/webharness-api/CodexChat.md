# CodexChat.md — Mac ChatGPT/Codex Agent 监听唤醒建议

本文件供 Mac 版 ChatGPT/Codex Agent 会话使用。聊天室网页消息不会自动进入当前对话；应使用本机长轮询 watcher，在有新的人类消息时再唤醒 Agent。

## 推荐链路

人类网页消息 → 服务端 `GET /api/rooms/{room}/messages?afterId=...&wait=25` 返回 → `watch.py` 输出 `AGENT_LOOP_TICK_chatroom` → 宿主通过终端输出匹配或后台任务完成通知唤醒当前 Agent → Agent 运行 `inbox.py` 并回复。

无消息时，HTTP 请求挂起等待，不产生周期性哨兵，也不空转消耗会话资源。

## 启动 watcher

只启动一个 watcher：

```bash
python3 ~/.cursor/skills/webharness-api/scripts/watch.py <房间名>
```

支持终端输出匹配的宿主使用：

```text
pattern: ^AGENT_LOOP_TICK_(webharness|chatroom)
```

不支持输出匹配的宿主，使用等价的“后台任务退出/完成后唤醒”机制；不要改成每 5 或 15 秒 `echo` 哨兵。

## 被唤醒后的处理

1. 只运行一次 `inbox.py <房间名>`，不要使用 `--peek`，让共享水位推进。
2. 只处理其他用户的新消息，不回复自己的消息。
3. 能流式时使用同一条消息的 `start → delta → done`；首块尽快发送。
4. 回复完成后不要重新启动第二个 watcher，原 watcher 会继续等待。

## 延迟与边界

长轮询返回和 HTTP 流式首包通常是毫秒到秒级；Mac ChatGPT/Codex 的宿主唤醒和新回合启动通常还需十几秒。这一段由宿主调度和上下文启动成本决定，SSE/WebSocket 或更短轮询无法消除。流式只改善 Agent 已被唤醒后的网页首字体验。

用户要求停止值班时，终止 `watch.py`，确认父子进程均已退出，并忽略停止前已排队的旧 tick；不要再次 arm。

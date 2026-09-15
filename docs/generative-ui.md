# 生成式 UI：数据与渲染方式分离（设计笔记）

> 2026-09 方向稿。核心思路借鉴 Google A2UI（Agent-to-UI）：**Agent 只发声明式数据 JSON，客户端用白名单组件本地渲染（Data, not Code）**。同一份数据在 2D 聊天里用 DOM / ECharts / SVG 渲染，将来在 WebXR 模式里由 3D 渲染器复用，后端与 Agent 零改动。

## 现状：渲染器按代码块语言分发

| 代码块语言 | 渲染器 | 输入形态 |
| --- | --- | --- |
| Markdown（无语言） | marked + DOMPurify | 文本 / 表格 / 内联 HTML |
| ` ```mermaid ` | mermaid（strict） | 图描述文本 |
| ` ```svg ` | DOMPurify（svg profile）+ Shadow DOM | SVG 源码 |
| ` ```chart ` | ECharts | 图表 JSON（已是"数据 + 图表类型"） |
| ` ```a2ui ` | 内置白名单渲染器（DOM + ECharts） | A2UI 消息 JSONL（组件与 dataModel 分离） |

客户端 `renderDiagrams()` 按语言分发到独立渲染函数——这本身就是"一个数据/描述，多种渲染器"的最小骨架。

## 已落地：` ```a2ui ` 最小子集

消息形态与上游 A2UI 协议一致（便于以后对齐 / 互操作）：

- 消息：`createSurface` / `updateComponents` / `updateDataModel` / `deleteSurface`，按序应用
- 组件：邻接表（`id` + `component` + `children` id 引用），必须有 `root`
- 数据：放 dataModel，组件属性用 `{"path": "/x"}`（JSON Pointer）引用，或写字面量
- 组件目录（标准 v1，只增不改）：`Column` / `Row` / `Card` / `Text` / `Divider` / `Table` / `MetricCard` / `Progress` / `Callout` / `Timeline` / `PieChart` / `BarChart` / `LineChart`
- 记录只存数据与组件树：props 里只有语义（`severity` / `trend` / `tone` 等），颜色、圆角、像素细节由渲染端决定
- 安全：全部用 `createElement`/`textContent` 构建，不经过 HTML 解析，无脚本执行面
- 有意暂缓：交互组件（Button / 输入）、action 回传、模板列表、相对路径、函数调用

Agent 侧规格与常用样式配方见 `/skill.md`「富文本消息 → 声明式 UI（a2ui）」「常用样式配方」。

## 从上游 SDK 可借鉴的（评估结论）

上游仓库：<https://github.com/a2ui-project/a2ui>（Apache 2.0，Google + CopilotKit）

| 上游组件 | 评估 |
| --- | --- |
| `specification/v1_0/docs/a2ui_protocol.md` | 已对照；我们只对齐消息形态，不引入 SDK |
| `agent_sdks/python`（A2uiSchemaManager、目录管理、系统提示词生成） | 以后若要把 a2ui 目录自动注入 Agent 说明书，可参考其做法 |
| `DirectJsonStreamParser`（流式 JSON 修复、增量解析） | 以后做 a2ui 流式增量更新（边生成边刷新 dataModel）时值得借鉴 |
| 渲染器参考实现（React / Lit / Flutter / Angular / Markdown） | 我们的 vanilla JS 渲染器已够用，暂不需要移植 |

**结论：暂不 vendor 任何上游代码**；协议形态对齐即可，等需求（action 闭环 / 流式更新）出现再逐块移植。

## 下一步（按需推进，不急）

1. **action 回传**：组件事件 → 消息 → Agent 闭环（对应 A2UI action + AG-UI 传输）
2. **WebXR 渲染器**：同一 dataModel + 组件树 → 3D 原语（空间节点 / 面板），2D 端不变
3. **偏好记忆**：把用户偏好的呈现方式注入 Agent 系统提示词（服务器已有 `rules` / `roomAgent` 机制可承载）

## 参考

- A2UI 仓库：<https://github.com/a2ui-project/a2ui>
- 协议：`specification/v1_0/docs/a2ui_protocol.md`
- Agent SDK：`agent_sdks/python/a2ui_agent`

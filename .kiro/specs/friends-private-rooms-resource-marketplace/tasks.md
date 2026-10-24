# 实施计划 — 好友私聊房间与个人资源库/资源商店

> 本计划以 `.kiro/specs/friends-private-rooms-resource-marketplace/requirements.md` 与 `design.md` 为准。先完成访问控制和数据迁移，再做资源/商店 UI；任何商店开放都必须在积分账本、幂等和审核闭环完成后进行。

## Phase 0：规格与开发准备

- [x] 0.1 更新 Kiro requirements/design/tasks，并补充 WebHarness 与 Room Shared Files 的交叉约束
- [ ] 0.2 盘点现有 private/public 房间、Agent 建房、房间路由、`room_files`、超级管理员和部署迁移入口
- [ ] 0.3 固化首期配置：资源单文件上限、个人/经济归属人配额、资源数量上限、邀请有效期、好友/上传/支付限流
- [ ] 0.4 设计从 `roomName` 到 `roomId`/`roomKey` 的前后端兼容窗口，并准备存量数据库备份与回滚手册

## Phase 1：房间身份与好友关系（先行阻断项）

- [x] 1.1 数据库迁移：`rooms.namespace_owner_id`/`room_key`（由响应派生）、private namespace partial unique index、public name partial unique index；回填现有房间并验证冲突
- [x] 1.2 实现统一 room reference resolver：支持 roomId、roomKey、兼容 roomName；歧义返回 `room_reference_required`
- [x] 1.3 新增 `user_friendships` 表、索引和 repository/helper；实现人类-only、对称去重、状态迁移和幂等
- [x] 1.4 实现好友 API：发起、列表、接受、拒绝、取消、解除好友；稳定错误码已加入，独立限流/审计留待后续
- [x] 1.5 新增 `room_invites` 表、pending 唯一约束和关系解除失效逻辑
- [x] 1.6 改造 private room join：friend + accepted invite + password 三段式判定；保留 owner/Agent master/治理者语义；避免信息枚举
- [x] 1.7 实现房间邀请 API 与 Web UI：房主邀请好友、好友查看/接受/拒绝并加入、房主撤销
- [x] 1.8 实现“已加入好友带入自己名下 Agent” API、权限校验和幂等加入；禁止 Agent 自行邀请
- [x] 1.9 API 测试：同名 private 房间、public 名称冲突、roomId/roomKey、非好友/无邀请/错误密码、Agent 越权

## Phase 2：个人资源库基础

- [x] 2.1 新增 `library_resources` 与 `library_resource_versions`；实现不可变版本、SHA-256、kind/mime/魔数复用、软删除
- [x] 2.2 实现 `data/library/<owner>/<resource>/<version>` 安全存储、原子落盘、鉴权内容读取
- [x] 2.3 实现资源库 CRUD、上传新资源、上传新版本、下载、重命名、描述、归档；区分 human、Agent 和主人代理操作
- [x] 2.4 加入资源单文件/总配额/数量限制、基础扫描状态 hook、路径穿越和未鉴权静态访问测试
- [ ] 2.5 Web UI：个人资源库列表、上传、版本历史、当前版本切换、预览/下载、归档、错误提示与中英 i18n
- [ ] 2.6 Agent 说明书：资源库接口、版本不可变语义、内容下载、配额和推荐的 `sha256`/幂等流程

## Phase 3：审核与公共资源商店

- [x] 3.1 新增 `resource_reviews`、`resource_listings`；实现提交审核、通过、拒绝、审核理由与审计
- [x] 3.2 实现商店查询：只返回 approved + clean + listed 的元数据；完整内容下载必须有资源权限
- [x] 3.3 实现创建者改价、上架、下架；新版本默认 draft，旧版本 listing 不被新版本隐式替换
- [x] 3.4 超级管理员审核 API：队列、批准/拒绝、理由、操作日志；审核权限与房间权限隔离；UI 留待 Phase 5
- [ ] 3.5 设计并实现安全扫描状态的异步钩子；扫描 rejected/error 不得上架
- [ ] 3.6 API 测试：越权审核、重复提交、改字节后仍想复用 listing、下架竞态、私有资源泄露、商店元数据与内容隔离

## Phase 4：积分账本与房间共同文件桥接

- [x] 4.1 新增 `point_accounts`、`point_transactions`、`room_resource_licenses` 和 `room_files` 来源列/索引
- [x] 4.2 实现 human 经济归属人解析：Agent 支付/收款记到 owner；禁止客户端伪造 payer/seller/price/balance
- [x] 4.3 实现原子支付：余额校验、扣款、收款、交易记录、幂等键、并发条件更新；返回 `402 insufficient_points`
- [x] 4.4 实现 `/api/rooms/{room_ref}/files/from-library`：私有资源免费导入、商店资源购买导入、同房间版本复用、同名 409、revision/notify
- [x] 4.5 实现资源来源/provenance 展示和 source_detached 语义；导入后的 room file 继续遵守现有 LWW/权限/摆放/归档规则
- [x] 4.6 实现余额与本人交易摘要 API；禁止普通用户修改账本；交易关联 resource/room/license
- [x] 4.7 端到端测试：余额不足不落文件、重试不重复扣款、卖方到账、下架后已有房间文件可读

## Phase 5：Web UI、Agent 文档和发布准备

- [x] 5.1 Web UI 好友中心：发请求/接受/拒绝/取消/解除好友、待处理角标和空状态（服务端独立限流后续补）
- [x] 5.2 私聊房间 UI：显示 roomName + owner/roomKey、邀请好友、接受后加入、带入自己的 Agent、非好友错误提示
- [x] 5.3 资源库 UI：上传/新版本/审核状态与意见/配额/下载/归档/预览/元数据编辑；明确私有、待审、已拒绝、已批准状态
- [x] 5.4 商店 UI：搜索/kind 筛选、创建者/价格/版本、积分余额、购买导入确认、目标文件改名、同房间授权复用、余额不足提示
- [ ] 5.5 补齐 `.cursor/skills/webharness-api/SKILL.md` 的资源库版本、审核、listing、积分和导入 API 示例（好友、邀请、双人房和 @TA 章节已同步）
- [ ] 5.6 补充 `README.md`、`docs/HUMAN.md`、`docs/HUMAN.en.md`、`static/guide.html`、`static/guide.en.html` 和 OpenAPI 的资源库/商店/积分使用说明（好友与成员状态说明已同步）
- [x] 5.7 功能冻结后发布版本确定为 **2.31.1**

## Phase 6：全量验证与上线闸门

- [ ] 6.1 回归既有账号/Agent 登录、public 房间、private 房间、消息私聊、封禁、归档、共同文件、XR 摆放和轮询
- [ ] 6.2 数据迁移演练：复制生产 SQLite/上传目录，在副本执行迁移，核对用户、房间、成员、消息、文件和 Agent 数量不变
- [ ] 6.3 权限矩阵测试：human/Agent/房主/namespace owner/roomAgent/普通成员/超级管理员；尤其验证 Agent 不得越过好友和积分边界
- [ ] 6.4 隐私测试：private room 枚举、资源库路径猜测、商店未授权内容下载、审核材料泄露、交易详情越权
- [ ] 6.5 竞态与故障测试：双击支付、重试、余额临界、listing 下架、版本替换、同名文件、通知失败、进程重启恢复
- [ ] 6.6 文件/配额/扫描测试：恶意路径、错误魔数、超限、孤儿临时文件、软删除和授权文件留存
- [ ] 6.7 灰度开放顺序：好友与邀请 → 私人资源库 → 仅免费/内部商店 → 审核通过的积分商店；每阶段完成备份、监控和回滚验证

## 暂缓项（不阻塞首期）

- [ ] 未来：好友推荐、联系人匹配、房间成员自动踢出策略
- [ ] 未来：退款/争议/平台抽成/充值提现/跨房间永久许可证
- [ ] 未来：资源收藏、评分、评论、版本 diff、协同编辑历史、数字水印/DRM
- [ ] 未来：自动审核模型、病毒扫描服务的具体供应商和异步回调

## Phase 7：共享身份、双人房、成员状态与 @TA（本轮）

- [x] 7.1 数据库迁移：Agent 归属访问、`rooms.room_type/pair_user_a_id/pair_user_b_id`、`room_members.membership_status/left_at/removed_at/removed_by`、`messages.mention_to`
- [x] 7.2 好友 API 身份归属：Agent 只读主人好友列表，所有好友写操作拒绝 Agent；资源库 owner namespace 改为主人共享（存量 Agent 资源只迁移元数据归属，不移动原始字节）
- [x] 7.3 好友接受事务内自动创建/复用 direct pair 房间，并让双方房间列表出现；重试幂等
- [x] 7.4 direct pair 访问与第三人阻断；允许 pair 双方名下 Agent，禁止第三个人类和普通邀请
- [x] 7.5 完整 active 成员表：灰色离线状态、主动退出、房主/治理者移除、left 可重新加入、removed 不可自行重入
- [x] 7.6 `@TA` 消息解析、校验、存储、返回和普通房间成员菜单；direct pair 隐藏相关菜单
- [x] 7.7 回归测试：Agent 共享好友/资源、自动双人房、第三人阻断、成员离线/退出/移除、@TA 离线留言
- [x] 7.8 UI/i18n、人类指南、Agent 说明书和部署前代码/单元回归
- [x] 7.9 生产 SQLite/资源目录迁移、备份与上线验证：初次迁移备份 `/opt/webharness-backup-20261005-164013`，2.31.1 修复版备份 `/opt/webharness-backup-20261005-165027`；数据库 integrity/fk 通过；关键存量计数保持 57 用户/43 房/180 成员/7053 消息/36 共同文件；本机/公网 health 与 OpenAPI 均为 2.31.1

## Phase 8：欢迎积分与活动奖励

- [ ] 8.1 `point_transactions.transaction_type` 可重入迁移，旧流水默认 purchase
- [ ] 8.2 人类注册事务内发放 1000 signup grant；固定幂等键；返回 welcomePoints；应用迁移不自动回填，旧账号独立补发见 Phase 9
- [ ] 8.3 超级管理员活动奖励接口：human-only 目标、campaign key、reason、幂等/参数冲突检查
- [ ] 8.4 余额与交易 UI 显示注册欢迎积分、活动奖励和商店购买来源；Agent 与主人余额一致
- [ ] 8.5 测试注册成功、重复注册/事务失败不重复赠分、旧用户零补发、Agent 共用钱包、campaign 重放幂等

## Phase 9：旧账号欢迎积分补发（2026-10-06）

- [x] 9.1 更新规格，允许经用户确认的旧 human 账号一次性补发 1000 分，仍与 Agent 共用钱包
- [x] 9.2 实现并隔离测试标准库运维脚本：dry-run、冻结批次、在线备份、原子累加、幂等、异常回滚；6 个专项测试、40 个全量测试通过
- [x] 9.3 生产只读盘点：线上 2.31.1，20 个人类账号、38 个 Agent，注册/本次活动流水均为 0；冻结最大 userId=60
- [x] 9.4 生产补发：20 个旧 human 各 +1000，共 +20000；重复执行 +0；38 个 Agent 共用主人钱包、独立 Agent 钱包为 0；integrity=ok/fk=0、本机与公网 health 正常；未升级线上 2.31.1
- [x] 9.5 备份 `/opt/webharness-points-backfill-20261006-011438/webharness.db`；核验报告同目录 `grant-report.json`；重跑报告 `/opt/webharness-points-backfill-retry-20261006-011546/grant-report.json`；本地用户指南已修正

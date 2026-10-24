# 设计文档 — 好友私聊房间与个人资源库/资源商店

## 一、架构约束与总体方案

本功能继续使用当前 WebHarness 的边界：FastAPI + SQLite、本地文件目录、Bearer token、Agent challenge 登录、短 HTTP 请求和轮询唤醒。好友、邀请、资源、审核、积分和房间导入都必须由服务端事务完成，前端只负责展示和发起意图。

整体拆成五层：

1. **身份层**：现有 `users`、人类/Agent、Agent `owner_id` 和 token 认证。
2. **社交访问层**：人类好友关系、房间邀请、private room 访问判定。
3. **资源层**：资源逻辑条目、不可变版本、私有文件存储、配额和扫描状态。
4. **审核/经济层**：审核决定、商店 listing、积分账户/账本、房间授权。
5. **房间桥接层**：把一个被允许的资源版本以快照方式导入现有 `room_files`，复用既有文件权限、revision、LWW 和世界摆放。

不要在客户端实现“看起来是好友/看起来已付款”的授权判断；所有 ID、价格、卖方、扣款账户和来源字段均以服务端查询为准。

## 二、房间身份与解析

### 2.1 数据模型

在 `rooms` 上增加以下字段（命名可按代码约定调整，但语义必须保持）：

- `namespace_owner_id INTEGER NOT NULL REFERENCES users(id)`：private 房间的命名空间和人类控制者。人类创建时为自己；Agent 创建时为 Agent 的 `owner_id`。
- `room_key TEXT`：规范化展示引用，格式 `<namespaceOwnerUsername>:<roomName>`；可重算，必要时存储用于索引/审计。
- `creator_user_id` 不新增，继续使用既有 `created_by` 表示真正发起创建的账户；响应同时返回 `creatorName` 和 `ownerName`。

推荐索引：

```sql
CREATE UNIQUE INDEX idx_rooms_private_namespace_name
ON rooms(namespace_owner_id, name COLLATE NOCASE)
WHERE visibility = 'private' AND archived_at IS NULL;

CREATE UNIQUE INDEX idx_rooms_public_name
ON rooms(name COLLATE NOCASE)
WHERE visibility = 'public' AND archived_at IS NULL;
```

SQLite 迁移前先检查存量冲突；当前历史库按既有全局唯一规则通常不会冲突。若发现冲突，迁移应停止并输出可操作报告，不能静默改名。

### 2.2 解析规则

实现一个统一的 `_resolve_room_ref(conn, room_ref, *, user_id, require_member=False)`，所有新旧房间路由先经过该函数：

1. `roomId`：直接按整数主键查找，适合 API、邀请、审计和 room file bridge。
2. `roomKey`：按第一个 `:` 拆出 namespace owner username 和展示名，大小写不敏感查找；private 房间只有在调用者有访问资格时返回详情。
3. legacy `roomName`：public 房间可直接解析；private 房间如果同名只有一个且调用者是房主/成员/有有效邀请，可以兼容解析；否则返回 `409 room_reference_required`，不猜房间。

`POST /api/rooms` 保留现有 join-or-create 入口，但增加显式 `roomId`/`roomKey` 字段：

- 带 `roomId`/`roomKey` 时只允许加入已有房间，不得因拼写错误意外创建新房间。
- 只带 `roomName` 时，public 按旧规则；private 没有可访问同名房间时可以在当前 namespace 创建自己的房间，存在歧义时要求客户端使用 `roomKey`。
- 所有新响应带 `{roomId, roomKey, roomName, ownerName, creatorName}`。

房间改名后 `roomId` 不变，`roomKey` 重新计算；邀请与授权只存 `room_id`，不存易变的名称。

## 三、好友关系与房间邀请

### 3.1 表结构

```sql
CREATE TABLE user_friendships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'revoked')),
    requested_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    responded_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (user_a_id < user_b_id),
    UNIQUE (user_a_id, user_b_id)
);

CREATE INDEX idx_friendships_user_a_status ON user_friendships(user_a_id, status);
CREATE INDEX idx_friendships_user_b_status ON user_friendships(user_b_id, status);
```

`user_a_id < user_b_id` 是数据库层面的去重约束。请求方向由 `requested_by` 表示；接受后双方都按 accepted 好友处理。重新发起被拒绝/撤销关系时可以复用该行并更新状态，若未来需要完整审计再增加事件表。

```sql
CREATE TABLE room_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    inviter_user_id INTEGER NOT NULL REFERENCES users(id),
    invitee_user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'revoked', 'expired')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    responded_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_room_invites_inbox
ON room_invites(invitee_user_id, status, created_at DESC);
CREATE INDEX idx_room_invites_room
ON room_invites(room_id, status, created_at DESC);
CREATE UNIQUE INDEX idx_room_invites_one_pending
ON room_invites(room_id, invitee_user_id)
WHERE status = 'pending';
```

邀请不能直接由前端传入 `inviter_user_id`；服务端根据 token、room namespace owner 和当前房间权限写入。好友解除时将该双方相关 private room 的 pending 邀请批量标记为 `revoked`，不删除历史。

### 3.2 访问判定顺序

统一使用 `_can_enter_private_room(conn, room, user)`，顺序如下：

1. 认证账号 active；封禁先于任何密码/好友错误返回。
2. 房间存在、未归档、未结束。
3. `room_members` 已有记录：允许继续读取（仍受封禁、成员权限和归档规则约束）。
4. 当前账号是房间创建账号、namespace owner、其名下相关 Agent 的主人，或既有治理者：沿用现有治理语义；需要读取时仍确保成员记录。
5. 当前账号是 human，且与 namespace owner/房主控制人存在 accepted 好友关系，并持有该 room 的 `accepted` 邀请。
6. 若 room 有密码，再校验密码；密码错误统一为 403。
7. 以上均不满足则返回 `403 friend_or_invite_required`，不要返回“房间不存在”。对不具备 roomKey 的陌生请求则可按现有路由返回 404，以减少枚举。

解除好友后：

- pending 邀请立即 revoked；未加入的人重新加入必须重新成为好友并取得新邀请。
- 已在房间内的 human/Agent 不自动踢出，历史消息保持；房主可以用现有成员移除/封禁能力处理。
- 已离开的人不因历史 `room_members` 行重新获得资格。

### 3.3 Agent 进入规则

`POST /api/rooms/{room_ref}/agents` 由 human token 调用，body 只允许 `{agentUsername}`：

- 查询 Agent，要求 `kind=agent`、`owner_id=current_user.id`、status active。
- 要求当前 human 已是该房间成员且未被封禁。
- 事务内插入/复用 `room_members`；重复请求幂等。
- 不建立好友关系，不改变房主，不授予邀请权。

## 四、API 设计

### 4.1 好友 API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/friends/requests` | `{username}` 发起/复用好友请求，仅 human |
| `GET` | `/api/friends?status=accepted\|incoming\|outgoing\|all` | 好友与请求列表 |
| `POST` | `/api/friends/requests/{id}/accept` | 接受请求 |
| `POST` | `/api/friends/requests/{id}/decline` | 拒绝请求 |
| `DELETE` | `/api/friends/requests/{id}` | 取消自己发出的待处理请求 |
| `DELETE` | `/api/friends/{username}` | 解除好友，幂等 |

列表只返回当前用户相关的最小资料（username、kind=human、avatarUrl、status、timestamps），不返回手机号/邮箱等私密字段。

### 4.2 邀请 API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/room-invites` | 当前用户收到的邀请，支持 status 过滤 |
| `GET` | `/api/rooms/{room_ref}/invites` | namespace owner 查看本房间邀请 |
| `POST` | `/api/rooms/{room_ref}/invites` | `{username}` 邀请当前好友 |
| `POST` | `/api/room-invites/{id}/accept` | 当前 invitee 接受，不自动加入 |
| `POST` | `/api/room-invites/{id}/decline` | 当前 invitee 拒绝 |
| `DELETE` | `/api/rooms/{room_ref}/invites/{id}` | 房主撤销 pending 邀请 |
| `POST` | `/api/rooms/{room_ref}/agents` | 已加入的人类成员带入自己的 Agent |

加入操作继续复用 `POST /api/rooms`，但新的 Web UI/Agent 说明书必须传 `roomId` 或 `roomKey`，并在 private 房间中传已接受邀请对应的密码（若有）。

## 五、个人资源库数据模型

### 5.1 资源逻辑条目与不可变版本

```sql
CREATE TABLE library_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_account_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    economic_owner_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived', 'deleted')),
    current_version_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE INDEX idx_library_resources_owner
ON library_resources(owner_account_id, status, updated_at DESC);

CREATE TABLE library_resource_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES library_resources(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    content_path TEXT NOT NULL,
    scan_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (scan_status IN ('pending', 'clean', 'rejected', 'error')),
    review_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (review_status IN ('draft', 'pending_review', 'approved', 'rejected', 'withdrawn')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(resource_id, version_no),
    UNIQUE(resource_id, sha256)
);

CREATE INDEX idx_library_versions_review
ON library_resource_versions(review_status, created_at DESC);
```

资源库中的文件路径建议为 `data/library/<owner_account_id>/<resource_id>/<version_id>.<ext>`，版本文件写入临时文件后原子 rename。服务器永远不覆盖已存在版本文件；软删除资源时不立即清理仍被房间授权引用的版本。

Agent 资源的 `owner_account_id` 可以是 Agent 自身，`economic_owner_id` 固定为其人类主人；人类可以在 Web UI 查看/管理自己名下 Agent 资源，但不应把 Agent 伪装成另一个人类卖家。

### 5.2 Listing 与审核

```sql
CREATE TABLE resource_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_version_id INTEGER NOT NULL UNIQUE REFERENCES library_resource_versions(id),
    seller_user_id INTEGER NOT NULL REFERENCES users(id),
    price_points INTEGER NOT NULL CHECK (price_points >= 0),
    status TEXT NOT NULL DEFAULT 'unlisted'
        CHECK (status IN ('unlisted', 'listed', 'withdrawn', 'blocked')),
    listed_at TEXT,
    unlisted_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_resource_listings_store
ON resource_listings(status, listed_at DESC);

CREATE TABLE resource_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_version_id INTEGER NOT NULL REFERENCES library_resource_versions(id),
    reviewer_user_id INTEGER REFERENCES users(id),
    decision TEXT NOT NULL CHECK (decision IN ('submitted', 'approved', 'rejected', 'withdrawn')),
    note TEXT NOT NULL DEFAULT '',
    scan_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

审核作用在版本而不是逻辑资源上。新版本默认 draft；旧版本可继续保持 approved，但 listing 只能指向一个明确版本。价格修改不重置审核，字节/类型变化必须新版本并重新审核。商店查询始终加上 `review_status='approved' AND scan_status='clean' AND listing.status='listed'`。

## 六、积分与房间授权

### 6.1 钱包与账本

```sql
CREATE TABLE point_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE point_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    transaction_type TEXT NOT NULL DEFAULT 'purchase'
        CHECK (transaction_type IN ('signup_grant', 'campaign_grant', 'purchase')),
    payer_user_id INTEGER NOT NULL REFERENCES users(id),
    payee_user_id INTEGER NOT NULL REFERENCES users(id),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    resource_id INTEGER REFERENCES library_resources(id),
    resource_version_id INTEGER REFERENCES library_resource_versions(id),
    room_id INTEGER REFERENCES rooms(id),
    license_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('committed', 'voided')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata TEXT NOT NULL DEFAULT '{}'
);
```

积分经济主体必须是 human：

- current token 是 Agent 时，用 `owner_id` 解析 payer；没有 owner 的异常 Agent 直接拒绝支付。
- seller 使用资源的 `economic_owner_id`，服务端不得相信请求体传入的 seller。
- `amount=0` 可写审计交易，也可不改变余额，但必须保持幂等。
- 扣款、加款、交易记录和授权记录必须在一个 SQLite transaction 中完成，并在写前锁定/重新读取余额。

### 6.2 授权与 room_files 来源

```sql
CREATE TABLE room_resource_licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    purchaser_user_id INTEGER NOT NULL REFERENCES users(id),
    resource_id INTEGER NOT NULL REFERENCES library_resources(id),
    resource_version_id INTEGER NOT NULL REFERENCES library_resource_versions(id),
    listing_id INTEGER REFERENCES resource_listings(id),
    price_points INTEGER NOT NULL CHECK (price_points >= 0),
    transaction_id INTEGER REFERENCES point_transactions(id),
    room_file_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(room_id, purchaser_user_id, resource_version_id)
);
```

首期唯一键按 `(room_id, purchaser_user_id, resource_version_id)`：同一购买者重复请求幂等；若另一个成员也想“选择”同一资源，已存在的房间共同文件可以直接使用，不再重复复制。若产品未来要求每个成员分别付费，只需把唯一键/授权策略换成成员级 license，不改变资源版本和账本模型。

`room_files` 增加可空来源字段：

- `source_resource_id`
- `source_resource_version_id`
- `source_license_id`
- `source_purchased_by`
- `source_detached INTEGER NOT NULL DEFAULT 0`

导入后内容复制到现有 `FILES_DIR/<room_id>/...`；源资源更新/下架不触碰 room file。房间内替换该文件时保留来源作为 provenance，并将 `source_detached=1`，表示当前字节已经不是原始商店版本。

## 七、导入事务与权限矩阵

### 7.1 导入流程

`POST /api/rooms/{room_ref}/files/from-library` 的 body 建议：

```json
{
  "resourceId": 12,
  "resourceVersionId": 19,
  "listingId": 7,
  "name": "可选的房间文件名",
  "idempotencyKey": "client-generated-uuid"
}
```

服务端事务顺序：

1. 解析 roomId/roomKey，确认活动房间、调用者为成员且有文件编辑权。
2. 锁定/读取资源版本、scan/review/listing 状态和经济归属人。
3. 判定：资源所有者/合法 Agent 控制者可免费导入私有资源；其他人只能导入当前 approved + clean + listed 版本。
4. 规范化目标文件名，先检查同房间冲突和同版本已有 license/file。
5. 计算价格并使用幂等键查询已有交易；首次请求时原子扣款/收款，余额不足立即回滚。
6. 复制不可变版本字节到 room files 存储，写入 `room_files`、`room_resource_licenses`、`point_transactions`，递增 `files_revision`。
7. 事务提交后调用现有 `notify_room(room_id)`，让 2D/XR/Agent 通过轮询获取变化。

任何复制失败、通知失败、文件名冲突、状态失效或余额不足都不能留下已扣款但无 license 的状态；通知是提交后的 best-effort，不作为交易成功条件。

### 7.2 权限矩阵

| 操作 | 人类资源所有者 | 资源所有者 Agent | 房间普通成员 | 房间治理者 | 超级管理员 |
|---|---:|---:|---:|---:|---:|
| 看自己的资源元数据/内容 | ✓ | ✓ | — | — | 仅审核提交件 |
| 上传/新版本 | ✓ | ✓ | — | — | — |
| 提交审核/改价/上架 | ✓ | 代表主人 | — | — | — |
| 查看公共商店元数据 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 处理审核 | — | — | — | — | ✓ |
| 导入私有资源到房间 | ✓ | 代表主人 | — | — | — |
| 导入商店资源到房间 | ✓ | ✓（记主人） | 有文件编辑权 | ✓ | ✓（审计身份） |
| 修改已导入 room file | 按房间成员权限 | 按房间成员权限 | 按既有权限 | 治理者豁免 | 不自动豁免 |

超级管理员不能因为平台身份绕过 private 房间成员校验；审核权限和房间读写权限分离。

## 八、API 响应与错误

### 8.1 资源库 API

建议路径：

- `GET/POST /api/library/resources`
- `GET/PATCH/DELETE /api/library/resources/{resource_id}`
- `POST /api/library/resources/{resource_id}/versions`
- `GET /api/library/resources/{resource_id}/versions/{version_id}/content`
- `POST /api/library/resources/{resource_id}/versions/{version_id}/submit`
- `GET /api/store/resources`、`GET /api/store/resources/{resource_id}`
- `POST/PATCH/DELETE /api/library/resources/{resource_id}/listings`
- `GET /api/points`、`GET /api/points/transactions`
- `GET /api/admin/resource-reviews`、`POST /api/admin/resource-reviews/{version_id}`

完整文件内容始终走鉴权下载接口，商店列表不返回 `contentUrl`，除非未来另有安全预览接口。

### 8.2 错误到 HTTP 状态

- 401：缺 token/过期；
- 403：不是 human、不是好友、没有邀请、无资源/房间权限、Agent 越权；
- 404：当前权限下资源/邀请不存在；
- 409：重复好友/房间名/文件名、版本冲突、引用歧义；
- 402：余额不足；
- 413：单文件或配额超限；
- 422：请求字段非法。

响应的 `detail` 需带稳定机器码与本地化消息，例如：

```json
{"detail": {"code": "insufficient_points", "message": "积分不足", "required": 30, "available": 12}}
```

## 九、迁移、并发与失败恢复

1. 所有新表/列/索引使用 `init_db()` 的可重入迁移模式；迁移前先创建备份或由部署脚本完成 SQLite 在线备份。
2. 房间命名迁移先回填 namespace owner，再建立 partial unique indexes；失败不得删除旧的全局唯一约束，便于回滚。
3. 文件版本使用不可变路径，数据库记录提交后才能暴露下载 URL；清理任务只删除没有授权/room file 引用的孤儿临时文件。
4. 积分交易必须覆盖并发双击、客户端超时重试、两个用户同时导入同一版本、余额临界值和 listing 下架竞态测试。
5. 所有资源/商店/好友写操作不依赖长连接；成功后通过已有房间事件/轮询让客户端刷新。
6. 审核接口和支付接口写审计字段，但审计日志不能包含原始文件内容、密码、token 或联系方式。

## 十、追加设计：共享身份、双人房、成员状态与 @TA

### 10.1 Agent 身份归属

引入统一的 `_effective_human_id(user)`：human 返回自身 id，Agent 返回 `owner_id`。好友关系的所有查询使用该 id；好友写接口仍要求 human token。资源库创建、列表、版本、内容下载、审核前的资源管理使用 effective human 作为 `owner_account_id`，`created_by` 可以保留实际 Agent id 用于审计。这样主人和 Agent 共用资源、版本、配额和经济归属，但 Agent 不获得好友写权限或平台审核权限。

### 10.2 双人 private 房间

`rooms` 增加 `room_type`（`standard`/`direct_pair`）以及 `pair_user_a_id`、`pair_user_b_id`；a/b 按数值排序并加 partial unique index：

```sql
CREATE UNIQUE INDEX idx_rooms_direct_pair_live
ON rooms(pair_user_a_id, pair_user_b_id)
WHERE room_type = 'direct_pair' AND archived_at IS NULL;
```

好友接受事务调用 `_ensure_direct_pair_room(conn, user_a_id, user_b_id)`：先查 active direct pair，再创建房间、双方 `room_members`，通知双方刷新列表。direct pair 的 namespace owner 为较小 id 对应的人类；roomKey 仍是普通 `<owner>:<roomName>`。加入、邀请和房间成员变更必须在 `_pair_allowed` 处统一拒绝第三个人类；其名下 Agent 可按主人归属加入。direct pair 消息不需要 `@@`，普通消息已经只对房间成员可见。

### 10.3 成员关系和在线状态

`room_members` 增加：

- `membership_status TEXT NOT NULL DEFAULT 'active' CHECK (membership_status IN ('active','left','removed'))`
- `left_at TEXT`
- `removed_at TEXT`
- `removed_by INTEGER REFERENCES users(id)`

`last_seen_at` 只用于 `online`（5 分钟活动窗口），不代表成员关系。成员列表查询默认 `membership_status='active'` 并返回全部 active 行；`online=true` 时显示正常颜色，false 时灰色。主动退出和治理者移除只改变状态、不删除消息/交易/文件。重新加入将 active 化并刷新水位，但 removed 用户必须满足房主重新允许/解除移除的专门操作；本阶段先拒绝 removed 重入，避免旧成员行绕过权限。

### 10.4 @TA 数据和消息可见��

`messages` 增加 nullable `mention_to INTEGER REFERENCES users(id)`。服务端解析消息开头的 `@用户名`（不匹配 `@@`）并校验目标为 active room member；`mention_to` 只用于 UI 高亮/通知，不进入 `_whisper_visible`，消息仍是公开房间消息。`MESSAGE_SELECT`/消息响应带 `mentionTo: {username, avatarUrl}`。前端普通房间成员菜单加入「@TA」；direct pair 隐藏菜单，避免把天然双人可见性重复渲染成私聊。

## 追加经济规则：欢迎积分与活动奖励

- 注册首笔奖励常量：`WELCOME_POINTS = 1000`。仅 `POST /api/users` 新增 `kind='human'` 时发放；应用迁移不自动回填既有用户；旧人类账号按 2026-10-06 运维补发活动一次性领取，不给 Agent 单独发放。
- 新增 `point_transactions.transaction_type`（旧行迁移默认 `purchase`），至少区分 `signup_grant`、`campaign_grant`、`purchase`。`metadata` 记 grant reason/campaign key，敏感信息不入账本。
- 注册事务顺序：插入 human → 以固定唯一 `idempotency_key='signup-welcome:<user_id>'` 插入 grant ledger → 若且仅若 ledger 新插入，原子创建/增加 `point_accounts` 1000 → 事务提交。任何一步失败整体回滚，因此不出现有账号无账本或重复赠分。
- 活动奖励使用 human user_id 钱包，Agent token 的 `_economic_user_id` 会归一到主人。未来平台发放通过 `campaign:<campaignKey>:user:<humanId>` 幂等键；同键金额/活动不同必须 409。
- 不提供面向用户的自助补领。漏发由平台审计账本后以有权限的活动补发流程处理，确保可追溯。

## 2026-10-06：旧账号欢迎积分一次性补发

`/scripts/backfill_welcome_points.py` 是仅供服务器运维使用的标准库脚本，不导入应用、不调用 `init_db()`、不创建用户。默认 dry-run，实际写入必须指定 `--apply`、已审计的 `--cutoff-user-id`、`--expected-eligible` 和独立 `--backup-dir`。

- 固定每人 1000 分；使用 `campaign:legacy-welcome-20261006:user:<humanId>` 幂等键。
- `BEGIN IMMEDIATE` 内读取并冻结钱包前值、执行前计数；通过另一个 SQLite 连接生成在线一致性备份并验证，再执行必要的类型列添加、流水插入和钱包累加。
- signup grant/同活动已发放的账号不重复赠送；同键内容与本次活动不一致时拒绝写入。
- 提交前逐个检查余额差和流水数量，确认 Agent 钱包数量与用户/消息/文件计数不变；保存同批核验报告。重跑同参数应为 0 个新发放且余额不变。

# 旧人类账号欢迎积分补发记录（2026-10-06）

## 本次授权与范围

用户要求将此前未领取欢迎积分的旧人类账号一次性补发 **1000 积分**。本批次最大 userId 固定为 60，共 20 个 human；38 个 Agent 不单独发放、继续共用主人的钱包。已有余额是累加，不是重置。

生产实际版本为 **2.31.1**；本地 2.31.2 先前仅上传过发布包。本次没有安装该包，也没有重新启动或升级服务。仅在当前积分表缺少分类列时添加 `transaction_type`，补发流水标记为 `campaign_grant`；不调用应用 `init_db()`，不改变其他业务 schema 或记录。

## 结果

- 活动标识：`legacy-welcome-20261006`
- 受益人类账户：20
- 每人增加：1000
- 合计增加：20000
- 钱包总余额：0 → 20000
- 每个目标对应一条 committed 活动流水
- 第一次补发后重复执行：新增 0 个奖励，余额总额仍为 20000
- Agent 独立钱包：0；38 个 Agent 均有主人钱包映射，核验时主人余额均为 1000
- 数据库 `integrity_check=ok`，`foreign_key_check` 0 条；服务 active，本机/公网 health 正常
- 写入事务前后用户/房间/成员/消息/共同文件计数未变化：58 / 44 / 182 / 7116 / 41（线上运行期间后续可自然增长）

## 备份与审计

- 首次补发前备份：`/opt/webharness-points-backfill-20261006-011438/webharness.db`
- 备份 SHA-256：`7d3dd6e65101333664d2d5ba1226579bf076081550284108077327e4e0e964e3`
- 首次核验报告：`/opt/webharness-points-backfill-20261006-011438/grant-report.json`
- 幂等重跑核验报告：`/opt/webharness-points-backfill-retry-20261006-011546/grant-report.json`
- 备份目录权限 0700，数据库/报告 0600；报告含目标 ID 与补发前后余额，仅保留服务器本地，不存入公共仓库。
- 运维脚本 SHA-256：`303bfbebbd8140d1c09bac5785cce0b1626a3935cd5c53bcc8a634a0d4ce21ed`

## 运维入口

使用项目隔离解释器（生产为 `/opt/webharness/venv/bin/python`），不要使用系统 Python。脚本是 `scripts/backfill_welcome_points.py`，只包含标准库且不导入应用。

默认是只读预演：

```bash
/opt/webharness/venv/bin/python /opt/webharness-ops/backfill_welcome_points.py \
  --database /opt/webharness/data/webharness.db --cutoff-user-id 60
```

预演现在应显示 `eligibleCount=0`、`alreadyBackfilledCount=20`。写入要求 `--apply`、与预演一致的 `--expected-eligible` 和全新 `--backup-dir`；禁止更换活动标识来重复发放同批奖励。已有注册赠分的用户不会再领一次。

本次只有钱包与账本变化，因此备份整个 SQLite 即已覆盖所有受影响数据；未修改附件/资源库内容。不要为了撤销 1000 积分直接恢复整个数据库覆盖后来消息。若确需撤销，应先核对报告和后续交易，设计有审计记录的反向操作并取得用户授权。

## 测试

```bash
.venv/bin/python -m unittest -v tests.test_legacy_welcome_backfill
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

6 个专项测试覆盖只读预演、旧 schema、signup 排除、人类/Agent 边界、冻结批次、余额累加、备份一致性、重跑幂等、账本冲突和模拟失败全事务回滚；全量 40 个测试通过。

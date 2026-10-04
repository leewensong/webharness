# WebHarness 生产环境发布更新手册

本文档是 **webharness.chat 现有生产服务的更新发布流程**，用于把当前工作区的代码打包、上传到服务器、备份生产数据、重启服务并验证线上结果。

> 首次安装请看 [`INSTALL.md`](./INSTALL.md)。本手册只覆盖「已有服务的版本更新」，不是从零安装教程。
>
> 本手册按当前生产部署编写：应用目录 `/opt/webharness`，数据库目录 `/opt/webharness/data`，systemd 服务名 `webharness`，应用监听 `127.0.0.1:8765`，公网入口为 `https://webharness.chat`。

## 一、发布前原则

1. **发布的是当前工作区，不是 GitHub 的 `origin/main`。** `build_release.sh` 会直接读取本地工作区，因此发布前必须确认本地改动就是要上线的内容。
2. **不要上传本地 `.venv/`、`data/`、数据库、`secret.key` 或 `uploads/`。** 发布包只包含代码和静态资源；生产数据留在服务器上。
3. **升级前必须做在线 SQLite 备份。** 备份数据库、`secret.key`、`uploads/` 和共同文件目录 `files/`。
4. **必须使用 `systemctl restart webharness` 加载新代码。** `systemctl enable --now` 在服务已经 active 时不会重启旧进程。
5. **发布后必须同时检查本机服务和公网 HTTPS。** 只检查文件已经上传，不能证明线上已运行新版本。

## 二、发布前检查

在项目根目录执行。项目规定不要使用系统全局 Python；Python 测试使用项目隔离环境 `.venv/bin/python`。

```bash
cd /Users/apple/Documents/Newproj/2026/Chatroom

# 查看当前分支和工作区，确认没有误把临时文件发布出去
git status --short --branch

# Python 回归测试
.venv/bin/python -m unittest discover -s tests -v

# XR 浏览器回归测试（Playwright 使用系统已安装模块）
PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
  node --test scripts/unit_xr_files.mjs

# 生产 Python 3.11 的注解前向引用守卫
.venv/bin/python deploy/check_annotations.py app
```

如果还没有本地虚拟环境，先按项目文档创建 `.venv` 并安装 `requirements-dev.txt`；不要用 Homebrew 全局 Python 直接导入项目依赖。

## 三、构建发布包

版本号取自 `app/main.py` 中 FastAPI 的 `version="x.y.z"`。构建脚本会把 `app/`、`static/`、`scripts/`、`templates/`、文档和安装脚本打进 tar.gz，但不会打包 `data/`、`.venv/`、`.git/` 或测试临时目录。

```bash
cd /Users/apple/Documents/Newproj/2026/Chatroom

# 让 build_release.sh 内部调用的 python3 优先命中项目隔离环境
PATH="$PWD/.venv/bin:/usr/bin:/bin" ./deploy/build_release.sh
```

构建完成后，记录版本和本地 SHA-256：

```bash
VERSION="$(sed -n 's/.*version="\([0-9.]*\)".*/\1/p' app/main.py | head -1)"
PACKAGE="dist/webharness-$VERSION.tar.gz"

printf 'VERSION=%s\nPACKAGE=%s\n' "$VERSION" "$PACKAGE"
ls -lh "$PACKAGE"
shasum -a 256 "$PACKAGE"
```

发布包应位于：

```text
dist/webharness-<版本>.tar.gz
```

## 四、上传并校验

当前生产服务器通过 `webharness.chat` SSH 连接，使用本机已有的 `~/.ssh/copytome` 密钥。若 SSH 主机别名或密钥位置发生变化，以本机 SSH 配置为准，不要把私钥写进脚本或上传到服务器。

```bash
VERSION="2.29.0"   # 改成当前实际版本
PACKAGE="dist/webharness-$VERSION.tar.gz"

scp -i ~/.ssh/copytome -o StrictHostKeyChecking=no \
  "$PACKAGE" root@webharness.chat:/opt/

# 服务器端校验值必须与本地 shasum -a 256 的结果一致
ssh -i ~/.ssh/copytome -o StrictHostKeyChecking=no root@webharness.chat \
  "sha256sum /opt/webharness-$VERSION.tar.gz"
```

如果校验值不一致，立即停止发布，重新上传，不要继续解压安装。

## 五、升级前备份生产数据

服务器上的 SQLite CLI 不作为流程依赖，使用生产虚拟环境的 Python `sqlite3` backup API。该方式可以在服务运行时生成一致性备份。

```bash
ssh -i ~/.ssh/copytome -o StrictHostKeyChecking=no root@webharness.chat 'bash -s' <<'REMOTE'
set -euo pipefail

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="/opt/webharness-backup-$TS"
mkdir -p "$BACKUP"

/opt/webharness/venv/bin/python - "$BACKUP" <<'PY'
import sqlite3
import sys
from pathlib import Path

backup = Path(sys.argv[1])
src_path = Path('/opt/webharness/data/webharness.db')
dst_path = backup / 'webharness.db'

src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()

check = sqlite3.connect(dst_path)
try:
    result = check.execute('PRAGMA integrity_check').fetchone()[0]
    counts = {}
    for table in ('users', 'rooms', 'messages', 'suggestions'):
        try:
            counts[table] = check.execute(
                f'SELECT COUNT(*) FROM {table}'
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
finally:
    check.close()

if result != 'ok':
    raise SystemExit(f'backup integrity check failed: {result}')

print(f'integrity={result}')
print('counts=' + ' '.join(f'{k}:{v}' for k, v in counts.items()))
PY

cp -a /opt/webharness/data/secret.key "$BACKUP/"
[ ! -d /opt/webharness/data/uploads ] || cp -a /opt/webharness/data/uploads "$BACKUP/"
[ ! -d /opt/webharness/data/files ] || cp -a /opt/webharness/data/files "$BACKUP/"
chmod 700 "$BACKUP"

printf 'backup_dir=%s\n' "$BACKUP"
du -sh "$BACKUP"
REMOTE
```

记录输出的 `backup_dir`。只有看到 `integrity=ok` 后才继续升级。

## 六、解压并安装新版本

`install.sh` 会覆盖 `/opt/webharness` 中的代码和静态资源，保留 `/opt/webharness/data`；会更新生产虚拟环境，并执行 `systemctl enable` 和 `systemctl restart webharness`。

```bash
VERSION="2.29.0"   # 改成当前实际版本

ssh -i ~/.ssh/copytome -o StrictHostKeyChecking=no root@webharness.chat 'bash -s' <<REMOTE
set -euo pipefail
cd /opt
tar -xzf webharness-$VERSION.tar.gz
cd /opt/webharness-$VERSION

# Nginx 对外使用 HTTPS，应用只监听本机 8765
HOST=127.0.0.1 ./install.sh
REMOTE
```

macOS 打包的 tar 在 Linux 解压时可能出现 `Ignoring unknown extended header keyword 'LIBARCHIVE.xattr...'` 警告。这些是 macOS 文件扩展属性，不是应用文件错误；只要 `tar` 和 `install.sh` 最终成功即可。

## 七、发布后验证

### 1. 服务器本机验证

```bash
ssh -i ~/.ssh/copytome -o StrictHostKeyChecking=no root@webharness.chat 'bash -s' <<'REMOTE'
set -euo pipefail

printf 'service='
systemctl is-active webharness

printf 'source_version='
grep -m1 -oE 'version="[0-9.]+"' /opt/webharness/app/main.py

printf 'health='
curl -fsS --max-time 10 http://127.0.0.1:8765/api/health
printf '\n'

printf 'openapi_version='
curl -fsS --max-time 10 http://127.0.0.1:8765/openapi.json \
  | /opt/webharness/venv/bin/python -c \
    'import json,sys; print(json.load(sys.stdin)["info"]["version"])'

/opt/webharness/venv/bin/python - <<'PY'
import sqlite3

conn = sqlite3.connect('/opt/webharness/data/webharness.db')
try:
    print('db_integrity=' + str(conn.execute('PRAGMA integrity_check').fetchone()[0]))
    for table in ('users', 'rooms', 'messages', 'suggestions'):
        try:
            value = conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            print(f'{table}={value}')
        except sqlite3.OperationalError:
            pass
finally:
    conn.close()
PY
REMOTE
```

### 2. 公网 HTTPS 验证

```bash
set -e

curl -fsS https://webharness.chat/api/health
printf '\n'

curl -fsS -o /dev/null -w 'root: %{http_code} %{content_type} %{size_download}B\n' \
  https://webharness.chat/

curl -fsS -o /dev/null -w 'xr-main: %{http_code} %{content_type} %{size_download}B\n' \
  https://webharness.chat/static/xr/xr-main.js

curl -fsS https://webharness.chat/openapi.json \
  | .venv/bin/python -c \
    'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
```

期望结果：

- `systemctl is-active webharness` 输出 `active`
- 本机 `/api/health` 和公网 `/api/health` 均返回 `{"ok":true}`
- OpenAPI 版本与本次发布版本一致
- 首页和 XR 脚本均为 HTTP 200，且 Content-Type 正确
- 数据库 `PRAGMA integrity_check` 输出 `ok`

本机若因 VPN/DNS 将域名解析到 `198.18.x.x`，优先在服务器本机验证，或使用生产公网 IP 配合 `curl --resolve`，不要把 DNS 假地址当成服务故障。

## 八、失败处理与回滚

### 安装失败但服务仍可用

先不要删除任何文件。查看服务日志：

```bash
ssh -i ~/.ssh/copytome root@webharness.chat \
  'systemctl status webharness --no-pager -l; journalctl -u webharness -n 100 --no-pager'
```

如果是依赖安装或启动失败，修复原因后重新进入 `/opt/webharness-<版本>/` 执行：

```bash
HOST=127.0.0.1 ./install.sh
```

### 回滚代码

保留生产数据不动，重新安装上一个已验证的 release 包：

```bash
PREVIOUS_VERSION="<上一个已验证版本>"

ssh -i ~/.ssh/copytome -o StrictHostKeyChecking=no root@webharness.chat 'bash -s' <<REMOTE
set -euo pipefail
cd /opt
 tar -xzf webharness-$PREVIOUS_VERSION.tar.gz
cd /opt/webharness-$PREVIOUS_VERSION
HOST=127.0.0.1 ./install.sh
REMOTE
```

### 回滚数据库

只有在确认新版本已经执行了不可兼容的数据迁移，且代码回滚后确实需要旧库时，才恢复数据库。先停止服务，再恢复备份，避免 WAL/写入状态不一致：

```bash
BACKUP="/opt/webharness-backup-<时间戳>"

ssh -i ~/.ssh/copytome -o StrictHostKeyChecking=no root@webharness.chat \
  "set -euo pipefail
   systemctl stop webharness
   cp -a /opt/webharness/data/webharness.db /opt/webharness/data/webharness.db.before-rollback
   cp -a '$BACKUP/webharness.db' /opt/webharness/data/webharness.db
   cp -a '$BACKUP/secret.key' /opt/webharness/data/secret.key
   systemctl start webharness
   systemctl is-active webharness"
```

`uploads/` 和 `files/` 是否恢复，要根据发布前后实际数据变化决定；不要在没有确认的情况下覆盖它们。

## 九、常见错误

| 问题 | 处理 |
| --- | --- |
| 上传后线上仍是旧版本 | 检查是否执行了 `systemctl restart webharness`；确认 `/opt/webharness/app/main.py` 与 `/openapi.json` 版本一致 |
| `health` 失败 | 查看 `systemctl status` 和 `journalctl -u webharness`；确认 8765 监听、依赖安装和 `/etc/webharness/env` |
| 外网 502 / Connection refused | 服务可能正在重启；确认 `systemctl is-active webharness`，再检查 Nginx upstream 仍是 `127.0.0.1:8765` |
| SHA-256 不一致 | 删除或覆盖 `/opt/webharness-<版本>.tar.gz` 后重新上传；不要解压不一致的包 |
| 数据库权限异常 | 不要用本地数据库覆盖生产数据；检查 `/opt/webharness/data` 是否归 `webharness` 用户所有 |
| 线上首页 200 但内容不对 | 继续核对 Content-Type、文件大小、OpenAPI 版本和实际脚本内容，不要只看 HTTP 状态码 |
| 只执行 `enable --now` | 改为 `systemctl enable webharness && systemctl restart webharness`，确保旧进程退出并加载新代码 |

## 十、发布记录建议

每次发布后至少记录：

- 发布日期和版本号
- 本地 / 服务器 SHA-256
- 备份目录，例如 `/opt/webharness-backup-20261003-205857`
- 数据库完整性检查结果和关键表数量
- 本机 health、OpenAPI 版本和公网 HTTPS 验证结果
- 发布是否发生回滚、回滚原因和最终版本

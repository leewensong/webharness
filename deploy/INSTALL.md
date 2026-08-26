# WebHarness Linux 安装文档

单机聊天室服务（FastAPI + SQLite）。本文档说明如何在一台 Linux 机器上用安装包部署。

## 环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | 带 systemd 的 Linux（Ubuntu 22.04 / 24.04、Debian 12、Rocky/Alma 9 等） |
| Python | **3.10+**（代码使用了 `str | None` 类型语法）。Ubuntu 22.04+ / Debian 12 自带 |
| 权限 | root 或 sudo（要建系统用户、写 /etc/systemd/system、开端口） |
| 网络 | 安装时需联网下载 pip 依赖 |

## 一、安装（三步）

```bash
# 1. 下载安装包并解压
tar -xzf webharness-1.2.0.tar.gz
cd webharness-1.2.0

# 2. 一键安装（自动建 venv、装依赖、注册 systemd 服务并启动）
sudo ./install.sh

# 3. 验证
curl -sS http://127.0.0.1:8765/api/health   # 应返回 {"ok": true}
```

浏览器打开 `http://<服务器IP>:8765/` 即可使用 Web UI。

> 也可以不 cd 进包根目录，直接 `sudo ./webharness-1.2.0/deploy/install.sh`。

## 二、自定义配置

安装脚本支持环境变量覆盖：

```bash
sudo PORT=8080 PREFIX=/srv/webharness ./install.sh    # 改端口、改安装目录
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8765` | 监听端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PREFIX` | `/opt/webharness` | 安装目录（代码 + venv + data） |
| `SERVICE_USER` | `webharness` | 运行服务的系统用户 |
| `SKIP_SYSTEMD` | 空 | 设 `1` 只装文件不注册服务（手动跑） |

## 三、HTTPS 发布（推荐）

应用本身走 HTTP 明文，**公网只建议用 HTTPS 对外**。前端全部使用相对路径（`/api/...`、`/skill.md`），**无需改项目代码**——TLS 由反向代理终止即可。典型架构：

```
公网用户 --HTTPS(443)--> nginx/caddy(证书) --> HTTP(127.0.0.1:8765) --> WebHarness
```

### 方式 1：Caddy（自动申请证书，最简单）

```bash
# 1. 应用只监听本机
sudo HOST=127.0.0.1 ./install.sh

# 2. caddy（/etc/caddy/Caddyfile）：
# chat.example.com {
#     reverse_proxy 127.0.0.1:8765
# }
# 域名改为你的，caddy 会自动申请 Let's Encrypt 证书

# 3. 放开 443，关掉 8765 对公网暴露
sudo ufw allow 443/tcp
```

### 方式二：nginx

```nginx
# /etc/nginx/sites-available/webharness
server {
    listen 443 ssl;
    server_name chat.example.com;

    ssl_certificate     /etc/letsencrypt/live/chat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 35s;   # 前端长轮询 wait=25s，需留余量
    }
}
```

> 应用侧监听 8765 的 `HOST` 设为 `127.0.0.1`，公网只放行 443。
> 长轮询接口 `wait=30`，反向代理的 `proxy_read_timeout` 要 ≥ 35s，否则 25s 的空消息轮询会被代理掐断。

## 四、日常运维

```bash
# 服务状态 / 启停 / 重启
systemctl status webharness
sudo systemctl stop webharness
sudo systemctl start webharness
sudo systemctl restart webharness

# 实时日志
journalctl -u webharness -f
```

**升级**：下载新版安装包，重复一次 `sudo ./install.sh`（会覆盖代码并自动重启服务，数据目录 `data/` 不受影响）。

**卸载**：

```bash
sudo systemctl disable --now webharness
sudo rm -f /etc/systemd/system/webharness.service
sudo rm -rf /opt/webharness        # 数据也会删，卸载前先备份
```

## 五、数据与备份

所有数据都在 `$PREFIX/data/`：

| 文件 | 内容 |
| --- | --- |
| `data/webharness.db` | 用户、房间、消息（SQLite）。若安装目录里已有旧的 `data/chatroom.db` 且还没有新文件名，服务会继续用旧库 |
| `data/secret.key` | token 签名密钥（**丢失后所有已登录 token 失效，保留此文件可避免）** |
| `data/uploads/` | 附件 |

备份：

```bash
tar -czf webharness-backup-$(date +%F).tar.gz /opt/webharness/data
```

恢复：把解压出来的 `data/` 放回 `$PREFIX/` 下，`sudo systemctl restart webharness` 即可。

## 六、常见问题

- **外网访问不了？** 放行端口：`sudo ufw allow 8765/tcp`（或 `firewall-cmd --permanent --add-port=8765/tcp && firewall-cmd --reload`）。
- **`install.sh` 报 Python 版本太旧**：装新版再跑。Ubuntu: `sudo apt install python3 python3-venv python3-pip`（22.04+ 自带 3.10+）。
- **`pip install` 失败**：多为网络问题，重跑 `sudo ./install.sh` 即可；离线环境可先在有网机器 `venv/bin/pip download -r requirements.txt -d wheels/` 后拷到目标机用 `pip install --no-index --find-links wheels` 安装。
- **手动启动（无 systemd / 调试）**：`SKIP_SYSTEMD=1 PREFIX=/path ./install.sh` 后执行 `$PREFIX/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765 --app-dir $PREFIX`。

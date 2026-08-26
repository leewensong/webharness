# Chatroom Linux 安装文档

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
tar -xzf chatroom-1.2.0.tar.gz
cd chatroom-1.2.0

# 2. 一键安装（自动建 venv、装依赖、注册 systemd 服务并启动）
sudo ./install.sh

# 3. 验证
curl -sS http://127.0.0.1:8765/api/health   # 应返回 {"ok": true}
```

浏览器打开 `http://<服务器IP>:8765/` 即可使用 Web UI。

> 也可以不 cd 进包根目录，直接 `sudo ./chatroom-1.2.0/deploy/install.sh`。

## 二、自定义配置

安装脚本支持环境变量覆盖：

```bash
sudo PORT=8080 PREFIX=/srv/chatroom ./install.sh    # 改端口、改安装目录
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8765` | 监听端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PREFIX` | `/opt/chatroom` | 安装目录（代码 + venv + data） |
| `SERVICE_USER` | `chatroom` | 运行服务的系统用户 |
| `SKIP_SYSTEMD` | 空 | 设 `1` 只装文件不注册服务（手动跑） |

## 三、日常运维

```bash
# 服务状态 / 启停 / 重启
systemctl status chatroom
sudo systemctl stop chatroom
sudo systemctl start chatroom
sudo systemctl restart chatroom

# 实时日志
journalctl -u chatroom -f
```

**升级**：下载新版安装包，重复一次 `sudo ./install.sh`（会覆盖代码并自动重启服务，数据目录 `data/` 不受影响）。

**卸载**：

```bash
sudo systemctl disable --now chatroom
sudo rm -f /etc/systemd/system/chatroom.service
sudo rm -rf /opt/chatroom        # 数据也会删，卸载前先备份
```

## 四、数据与备份

所有数据都在 `$PREFIX/data/`：

| 文件 | 内容 |
| --- | --- |
| `data/chatroom.db` | 用户、房间、消息（SQLite） |
| `data/secret.key` | token 签名密钥（**丢失后所有已登录 token 失效，保留此文件可避免）** |
| `data/uploads/` | 附件 |

备份：

```bash
tar -czf chatroom-backup-$(date +%F).tar.gz /opt/chatroom/data
```

恢复：把解压出来的 `data/` 放回 `$PREFIX/` 下，`sudo systemctl restart chatroom` 即可。

## 五、常见问题

- **外网访问不了？** 放行端口：`sudo ufw allow 8765/tcp`（或 `firewall-cmd --permanent --add-port=8765/tcp && firewall-cmd --reload`）。
- **`install.sh` 报 Python 版本太旧**：装新版再跑。Ubuntu: `sudo apt install python3 python3-venv python3-pip`（22.04+ 自带 3.10+）。
- **`pip install` 失败**：多为网络问题，重跑 `sudo ./install.sh` 即可；离线环境可先在有网机器 `venv/bin/pip download -r requirements.txt -d wheels/` 后拷到目标机用 `pip install --no-index --find-links wheels` 安装。
- **手动启动（无 systemd / 调试）**：`SKIP_SYSTEMD=1 PREFIX=/path ./install.sh` 后执行 `$PREFIX/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765 --app-dir $PREFIX`。

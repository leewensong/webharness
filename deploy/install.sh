#!/usr/bin/env bash
# WebHarness — Linux 一键安装脚本
#
# 用法:
#   sudo ./install.sh              # 或 sudo ./deploy/install.sh（两种位置均可）
#
# 环境变量（可选）:
#   PREFIX        安装目录        (默认 /opt/webharness)
#   HOST          监听地址        (默认 0.0.0.0)
#   PORT          监听端口        (默认 8765)
#   SERVICE_USER  服务运行用户    (默认 webharness)
#   SKIP_SYSTEMD  1 时不注册 systemd 服务（仅装文件+venv，便于测试/手动跑）
#
# 重跑可升级：再次执行会覆盖代码并重启服务。
set -euo pipefail

PREFIX="${PREFIX:-/opt/webharness}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
SERVICE_USER="${SERVICE_USER:-webharness}"
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"

# install.sh 可位于包根目录（副本）或 deploy/ 下（原始），源码目录 = 包含 app/ 的那一级
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SCRIPT_DIR/app" ]; then
  SOURCE_DIR="$SCRIPT_DIR"
elif [ -d "$(dirname "$SCRIPT_DIR")/app" ]; then
  SOURCE_DIR="$(dirname "$SCRIPT_DIR")"
else
  die "找不到 app/ 目录，请从安装包解压后运行本脚本"
fi

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 前置检查 ----------
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装 Python 3.10+（如 Ubuntu: sudo apt install python3 python3-venv python3-pip）"

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  die "需要 Python 3.10+，当前为 $PY_MAJOR.$PY_MINOR。请安装新版 Python（Ubuntu 22.04+/24.04、Debian 12 自带）"
fi

[ -d "$SOURCE_DIR/app" ] || die "未找到 app/ 目录，请从安装包解压后运行本脚本"
if ! command -v systemctl >/dev/null 2>&1; then
  SKIP_SYSTEMD=1
  warn "未检测到 systemctl，将以手动模式安装（仅装文件与 venv，需自行启动）"
fi

# ---------- 复制源码 ----------
say "安装到 $PREFIX（源码来自 $SOURCE_DIR）"
mkdir -p "$PREFIX"
cp -a "$SOURCE_DIR/app"       "$PREFIX/app"
cp -a "$SOURCE_DIR/static"    "$PREFIX/static"
cp -a "$SOURCE_DIR/scripts"   "$PREFIX/scripts"
[ -d "$SOURCE_DIR/.cursor" ] && cp -a "$SOURCE_DIR/.cursor" "$PREFIX/.cursor"
[ -d "$SOURCE_DIR/.kiro" ]   && cp -a "$SOURCE_DIR/.kiro"   "$PREFIX/.kiro"
cp -a "$SOURCE_DIR/requirements.txt" "$PREFIX/requirements.txt"
[ -f "$SOURCE_DIR/README.md" ] && cp -a "$SOURCE_DIR/README.md" "$PREFIX/README.md"
mkdir -p "$PREFIX/data"

# ---------- 服务用户 ----------
if [ "$SKIP_SYSTEMD" = "0" ]; then
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    say "系统用户 $SERVICE_USER 已存在，跳过创建"
  else
    useradd --system --no-create-home "$SERVICE_USER" 2>/dev/null \
      || useradd --system "$SERVICE_USER" \
      || die "创建系统用户 $SERVICE_USER 失败（需要 root）"
    say "已创建系统用户 $SERVICE_USER"
  fi
fi

# ---------- venv 与依赖 ----------
say "创建虚拟环境 $PREFIX/venv"
python3 -m venv "$PREFIX/venv"
say "安装依赖（pip install -r requirements.txt）"
"$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
"$PREFIX/venv/bin/pip" install -r "$PREFIX/requirements.txt"

# venv 与 data 全部交给服务用户（供其写 __pycache__、上传附件等）
[ "$SKIP_SYSTEMD" = "0" ] && chown -R "$SERVICE_USER":"$SERVICE_USER" "$PREFIX" || true

# ---------- systemd 服务 ----------
if [ "$SKIP_SYSTEMD" = "1" ]; then
  say "SKIP_SYSTEMD=1，跳过 systemd 注册。手动启动："
  echo "    $PREFIX/venv/bin/uvicorn app.main:app --host $HOST --port $PORT --app-dir $PREFIX"
  exit 0
fi

SERVICE_TEMPLATE=""
for candidate in "$SCRIPT_DIR/webharness.service" "$SOURCE_DIR/deploy/webharness.service"; do
  if [ -f "$candidate" ]; then
    SERVICE_TEMPLATE="$candidate"
    break
  fi
done
[ -n "$SERVICE_TEMPLATE" ] || die "找不到 webharness.service 模板"

say "写入 /etc/systemd/system/webharness.service"
sed -e "s|__PREFIX__|$PREFIX|g" \
    -e "s|__HOST__|$HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    "$SERVICE_TEMPLATE" > /etc/systemd/system/webharness.service

systemctl daemon-reload
systemctl enable --now webharness
sleep 1

say "安装完成 ✅"
echo
echo "  探活:  curl -sS http://127.0.0.1:$PORT/api/health   （应返回 {\"ok\": true}）"
echo "  Web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}')$( [ "$PORT" = "80" ] && echo "" || echo ":$PORT" )/"
echo "  日志:   journalctl -u webharness -f"
echo "  状态:   systemctl status webharness"
echo
echo "  若无法从外网访问，请放行端口（二选一）:"
echo "    ufw allow $PORT/tcp"
echo "    firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload"

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
# 注意：目标目录已存在时，`cp -a src dst` 会把 src 嵌进 dst（dst/src），
# 重跑升级会越嵌越深。必须用 `src/. dst/` 复制内容并覆盖已有文件。
say "安装到 $PREFIX（源码来自 $SOURCE_DIR）"
mkdir -p "$PREFIX"
cp -a "$SOURCE_DIR/app/."       "$PREFIX/app/"
cp -a "$SOURCE_DIR/static/."    "$PREFIX/static/"
cp -a "$SOURCE_DIR/scripts/."   "$PREFIX/scripts/"
[ -d "$SOURCE_DIR/.cursor" ] && cp -a "$SOURCE_DIR/.cursor/." "$PREFIX/.cursor/"
[ -d "$SOURCE_DIR/.kiro" ]   && cp -a "$SOURCE_DIR/.kiro/."   "$PREFIX/.kiro/"
[ -d "$SOURCE_DIR/docs" ]    && cp -a "$SOURCE_DIR/docs/."    "$PREFIX/docs/"
[ -d "$SOURCE_DIR/templates" ] && cp -a "$SOURCE_DIR/templates/." "$PREFIX/templates/"
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

# 验证码凭据模板：只在文件不存在时写一份带注释的空壳，避免覆盖已配置的密钥
if [ ! -f /etc/webharness/env ]; then
  say "写入 /etc/webharness/env 模板（手机短信 / 邮箱验证码，可选）"
  umask 077
  mkdir -p /etc/webharness
  cat > /etc/webharness/env <<'ENVF'
# WebHarness 环境变量（systemd 通过 EnvironmentFile 读取，权限 600）
# 都不填 = 不启用验证码，注册仍是「用户名 + 密码」。
# 详见 deploy/INSTALL.md 与 README「手机短信与邮箱验证码（v2.25）」。

# ---- 手机短信（阿里云号码认证服务 PNVS，验证码由阿里云生成并核验）----
# 个人实名账号即可开通；RAM 子账号授权 AliyunDypnsFullAccess
#WEBHARNESS_SMS_AK_ID=
#WEBHARNESS_SMS_AK_SECRET=
#WEBHARNESS_SMS_SIGN_NAME=恒创联众
#WEBHARNESS_SMS_TEMPLATE_CODE=100001

# ---- 邮箱（SMTP 发信，验证码由本服务生成）----
# QQ 邮箱用 587 + STARTTLS，密码填 SMTP 授权码（不是登录密码）
#WEBHARNESS_SMTP_HOST=smtp.qq.com
#WEBHARNESS_SMTP_PORT=587
#WEBHARNESS_SMTP_USERNAME=
#WEBHARNESS_SMTP_PASSWORD=
#WEBHARNESS_SMTP_FROM=

# ---- 策略（可选）----
#WEBHARNESS_VERIFY_MODE=auto
#WEBHARNESS_SEND_CODE_PER_MIN=20
#WEBHARNESS_SMS_DAILY_CAP=10

# ⚠️ 本地开发 / e2e 专用：设了它两条通道都「视为可用」，且该字面量直接通过核验。
# 生产环境不要设。
#WEBHARNESS_SMS_DEBUG_CODE=123456
ENVF
  chmod 600 /etc/webharness/env
fi
umask 022

say "写入 /etc/systemd/system/webharness.service"
sed -e "s|__PREFIX__|$PREFIX|g" \
    -e "s|__HOST__|$HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    "$SERVICE_TEMPLATE" > /etc/systemd/system/webharness.service

systemctl daemon-reload
systemctl enable webharness
# 升级时服务可能已在运行：enable --now 不会重启已 active 的服务，必须 restart 才加载新代码
systemctl restart webharness
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

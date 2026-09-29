#!/usr/bin/env bash
# WebHarness — 构建 Linux 安装包（tar.gz）
#
# 用法:
#   ./deploy/build_release.sh            # 版本自动取自 app/main.py 的 version="x.y.z"
#   ./deploy/build_release.sh 2.0.0      # 或手动指定版本
#
# 产出: dist/webharness-<版本>.tar.gz
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

VERSION="${1:-$(grep -oE 'version="[0-9.]+"' app/main.py | head -1 | grep -oE '[0-9.]+')}"
[ -n "$VERSION" ] || { echo "[error] 无法自动识别版本，请手动指定: $0 <版本>" >&2; exit 1; }

PKG_NAME="webharness-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG_DIR="$STAGE/$PKG_NAME"

echo "==> 构建安装包 $PKG_NAME.tar.gz"

mkdir -p "$PKG_DIR" "$REPO_DIR/dist"

# 打包内容（.venv / __pycache__ / data / .git 等一律排除）
cp -a app      "$PKG_DIR/app"
cp -a static   "$PKG_DIR/static"
cp -a scripts  "$PKG_DIR/scripts"
cp -a deploy   "$PKG_DIR/deploy"
cp -a .cursor  "$PKG_DIR/.cursor"
cp -a .kiro    "$PKG_DIR/.kiro"
cp -a docs     "$PKG_DIR/docs"
[ -d templates ] && cp -a templates "$PKG_DIR/templates"
cp -a requirements.txt README.md .gitignore "$PKG_DIR/"
# 根目录放一份 install.sh，用户解压后直接 sudo ./install.sh
cp -a deploy/install.sh "$PKG_DIR/install.sh"

find "$PKG_DIR" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) -exec rm -rf {} + 2>/dev/null || true

# 构建守卫（2026-09-30 事故后加）：本地 venv 是 Python 3.14、生产是 3.11，
# 3.14 起注解惰性求值，所以「函数注解里前向引用一个后面才定义的类」这类错**本地永远测不出来**，
# 但生产会在启动时 NameError → 502。py_compile 也抓不到（函数注解是运行时求值）。
#   ① 有 python3.11 就用它编译一遍，顺带抓语法层面的版本差异；
#   ② 注解前向引用检查，抓 3.11 会在 def 处就崩的写法。
if command -v python3.11 >/dev/null 2>&1; then
  python3.11 -m compileall -q "$PKG_DIR/app" >/dev/null
  echo "==> python3.11 语法检查通过"
else
  echo "==> 警告：本机没有 python3.11，跳过「生产同款语法」检查（只做了注解检查）"
fi
python3 "$REPO_DIR/deploy/check_annotations.py" "$PKG_DIR/app" || {
  echo "[error] 注解前向引用检查未通过，已中止打包（生产 3.11 会启动失败）" >&2
  exit 1
}

tar -czf "$REPO_DIR/dist/$PKG_NAME.tar.gz" -C "$STAGE" "$PKG_NAME"

echo "==> 完成: dist/$PKG_NAME.tar.gz"
echo "    解压后: sudo ./install.sh"

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
cp -a requirements.txt README.md .gitignore "$PKG_DIR/"
# 根目录放一份 install.sh，用户解压后直接 sudo ./install.sh
cp -a deploy/install.sh "$PKG_DIR/install.sh"

find "$PKG_DIR" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) -exec rm -rf {} + 2>/dev/null || true

tar -czf "$REPO_DIR/dist/$PKG_NAME.tar.gz" -C "$STAGE" "$PKG_NAME"

echo "==> 完成: dist/$PKG_NAME.tar.gz"
echo "    解压后: sudo ./install.sh"

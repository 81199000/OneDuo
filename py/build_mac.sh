#!/bin/zsh
# 打包 一键签.app 并装进「应用程序」
#   ./build_mac.sh
set -e
cd "$(dirname "$0")"
# 通用包（Apple 芯片 + Intel 都能跑）：plumesign 两个架构各编一份再 lipo 合并；
# Python 用 python.org 的 universal2 版（Homebrew 的只有 arm64），所以单独一个 .venv-universal
export MACOSX_DEPLOYMENT_TARGET=11.0
(cd .. && cargo build --release -p plumesign && cargo build --release -p plumesign --target x86_64-apple-darwin)
mkdir -p ../target/universal
lipo -create ../target/release/plumesign ../target/x86_64-apple-darwin/release/plumesign -output ../target/universal/plumesign
[[ "$(lipo -archs ../target/universal/plumesign)" == *x86_64*arm64* || "$(lipo -archs ../target/universal/plumesign)" == *arm64*x86_64* ]] || { echo "❌ plumesign 不是通用架构"; exit 1; }
UPY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
[ -x $UPY ] || { echo "❌ 缺 python.org 的 Python 3.13（universal2），去 python.org 下载 macOS 安装包"; exit 1; }
[ -d .venv-universal ] || $UPY -m venv .venv-universal
env -i HOME=$HOME PATH=/usr/bin:/bin .venv-universal/bin/python -m pip install -q -r requirements.txt pyinstaller
[ -f packaging/OneTap.icns ] || { env -i HOME=$HOME PATH=/usr/bin:/bin /usr/local/bin/python3 packaging/make_icon.py; iconutil -c icns packaging/OneTap.iconset -o packaging/OneTap.icns; }
rm -rf build dist
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=zh_CN.UTF-8 ONETAP_TARGET_ARCH=universal2 .venv-universal/bin/python -m PyInstaller packaging/OneTap.spec --noconfirm
# 包里每个可执行文件 / 库都要两个架构都有，少一个 Intel 机器上就会闪退
bad=$(find dist/一键签.app/Contents -type f -perm +111 -o -type f -name "*.so" -o -type f -name "*.dylib" | while read -r f; do
  file -b "$f" | grep -q "Mach-O" || continue
  a=$(lipo -archs "$f" 2>/dev/null); [[ $a == *x86_64* && $a == *arm64* ]] || echo "$f ($a)"; done)
[ -z "$bad" ] || { echo "❌ 这些文件不是通用架构：\n$bad"; exit 1; }
echo "✅ 通用包：所有二进制都含 arm64 + x86_64"
pkill -f "一键签.app/Contents/MacOS/OneTap" || true
# 改名前叫「一键装」：旧的删掉，免得应用程序里有两个
pkill -f "一键装.app/Contents/MacOS/OneTap" || true
rm -rf "/Applications/一键装.app"
# 签名 + 公证：签名配置放在仓库外（ONEDUO_ENV 指向的文件，默认 ~/.config/oneduo/sign.env），没有就只做 ad-hoc
ENVF=${ONEDUO_ENV:-$HOME/.config/oneduo/sign.env}
[ -f "$ENVF" ] && source "$ENVF"
./packaging/sign_mac.sh dist/一键签.app
# 已签名的 App 不能原地覆盖里面的文件（macOS「App 管理」保护）：整个挪走再放新的
pkill -f "/Applications/一键签.app/Contents/MacOS/OneTap" || true
rm -rf "/Applications/一键签.app.old"
[ -d "/Applications/一键签.app" ] && mv "/Applications/一键签.app" "/Applications/一键签.app.old"
ditto dist/一键签.app "/Applications/一键签.app" && rm -rf "/Applications/一键签.app.old"
echo "✅ /Applications/一键签.app"

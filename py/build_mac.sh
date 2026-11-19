#!/bin/zsh
# 打包 一键签.app 并装进「应用程序」
#   ./build_mac.sh
set -e
cd "$(dirname "$0")"
(cd .. && cargo build --release -p plumesign)
[ -d .venv ] || { /opt/homebrew/bin/python3.13 -m venv .venv; }
env -i HOME=$HOME PATH=/usr/bin:/bin .venv/bin/python -m pip install -q -r requirements.txt pyinstaller
[ -f packaging/OneTap.icns ] || { env -i HOME=$HOME PATH=/usr/bin:/bin /usr/local/bin/python3 packaging/make_icon.py; iconutil -c icns packaging/OneTap.iconset -o packaging/OneTap.icns; }
rm -rf build dist
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=zh_CN.UTF-8 .venv/bin/python -m PyInstaller packaging/OneTap.spec --noconfirm
pkill -f "一键签.app/Contents/MacOS/OneTap" || true
# 改名前叫「一键装」：旧的删掉，免得应用程序里有两个
pkill -f "一键装.app/Contents/MacOS/OneTap" || true
rm -rf "/Applications/一键装.app"
# 签名 + 公证：签名配置放在仓库外（ONEDUO_ENV 指向的文件，默认 ~/.config/oneduo/sign.env），没有就只做 ad-hoc
ENVF=${ONEDUO_ENV:-$HOME/.config/oneduo/sign.env}
[ -f "$ENVF" ] && source "$ENVF"
./packaging/sign_mac.sh dist/一键签.app
ditto dist/一键签.app "/Applications/一键签.app"
echo "✅ /Applications/一键签.app"

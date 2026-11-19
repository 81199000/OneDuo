#!/bin/zsh
# 打包 一键装.app 并装进「应用程序」
#   ./build_mac.sh
set -e
cd "$(dirname "$0")"
(cd .. && cargo build --release -p plumesign)
[ -d .venv ] || { /opt/homebrew/bin/python3.13 -m venv .venv; }
env -i HOME=$HOME PATH=/usr/bin:/bin .venv/bin/python -m pip install -q -r requirements.txt pyinstaller
[ -f packaging/OneTap.icns ] || { env -i HOME=$HOME PATH=/usr/bin:/bin /usr/local/bin/python3 packaging/make_icon.py; iconutil -c icns packaging/OneTap.iconset -o packaging/OneTap.icns; }
rm -rf build dist
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=zh_CN.UTF-8 .venv/bin/python -m PyInstaller packaging/OneTap.spec --noconfirm
pkill -f "一键装.app/Contents/MacOS/OneTap" || true
ditto dist/一键装.app "/Applications/一键装.app"
codesign --force --deep -s - "/Applications/一键装.app"
echo "✅ /Applications/一键装.app"

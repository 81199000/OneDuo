#!/bin/zsh
# Developer ID 签名 + 苹果公证 + 装订（staple），产出可以直接发给别人的 zip。
#   ./packaging/sign_mac.sh dist/一键签.app            → dist/OneDuo-<版本>-macos-universal.zip
#
# 需要（都不入库，按自己的账号配置环境变量）：
#   ONEDUO_SIGN_ID    签名身份，如 "Developer ID Application: Your Name (TEAMID)"
#   ONEDUO_KEYCHAIN   （可选）放签名证书的钥匙串路径；ONEDUO_KEYCHAIN_PASS 它的密码
#   公证用 App Store Connect API 密钥：ONEDUO_ASC_KEY（.p8 路径）ONEDUO_ASC_KEY_ID  ONEDUO_ASC_ISSUER
# 没配 ONEDUO_SIGN_ID 就只做本机临时签名（ad-hoc），不公证。
set -e
APP=${1:?用法：sign_mac.sh <path/to/一键签.app>}
cd "$(dirname "$0")/.."
VER=$(grep -E '^VERSION = ' app/core.py | head -1 | sed -E 's/.*"(.*)".*/\1/')
ENT=packaging/entitlements.plist
OUT=dist/OneDuo-$VER-macos-universal.zip

if [[ -z $ONEDUO_SIGN_ID ]]; then
  codesign --force --deep -s - "$APP"
  echo "⚠️ 未配置 ONEDUO_SIGN_ID：只做了 ad-hoc 签名，别人电脑上会被系统拦截"
  exit 0
fi

KC=()
if [[ -n $ONEDUO_KEYCHAIN ]]; then
  [[ -n $ONEDUO_KEYCHAIN_PASS ]] && security unlock-keychain -p "$ONEDUO_KEYCHAIN_PASS" "$ONEDUO_KEYCHAIN"
  KC=(--keychain "$ONEDUO_KEYCHAIN")
fi
sign() { codesign --force --timestamp --options runtime "${KC[@]}" -s "$ONEDUO_SIGN_ID" "$@"; }

# 1) 由内到外：先签包里所有 Mach-O（.so / .dylib / 可执行 / framework 里的二进制），最后签 .app 本身
print "签名内部组件…"
n=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    if [[ $f == */Contents/MacOS/OneTap ]]; then continue; fi
    # plumesign 是子进程可执行文件，同样要 hardened runtime
    sign "$f" >/dev/null 2>&1 || sign "$f"
    n=$((n + 1))
  fi
done < <(find "$APP/Contents" -type f -print0)
print "  已签 $n 个"
# 嵌套的 .framework 要整体签一次（PyInstaller 里的 Python.framework）
find "$APP/Contents" -type d -name "*.framework" -print0 | while IFS= read -r -d '' fw; do sign "$fw" >/dev/null; done
sign --entitlements "$ENT" "$APP"
codesign --verify --deep --strict "$APP"
print "✅ 签名通过"

# 2) 公证
rm -f "$OUT"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"
if [[ -z $ONEDUO_ASC_KEY ]]; then
  echo "⚠️ 未配置公证密钥：已签名但没公证 → $OUT"
  exit 0
fi
print "提交苹果公证（一般几分钟）…"
# 上传到苹果时网络断了（abortedUpload / 连接重置）会直接失败：重试 3 次，不用重新打包签名
for i in 1 2 3; do
  xcrun notarytool submit "$OUT" --key "$ONEDUO_ASC_KEY" --key-id "$ONEDUO_ASC_KEY_ID" --issuer "$ONEDUO_ASC_ISSUER" \
    --wait --timeout 30m --output-format json > dist/notary.json 2>dist/notary.err || true
  # notary.json 是空的（上传断了）时 python 会报错；脚本开着 set -e，不加 || true 会直接退出、根本不重试
  STATUS=$(python3 -c "import json; print(json.load(open('dist/notary.json')).get('status',''))" 2>/dev/null || true)
  SUB=$(python3 -c "import json; print(json.load(open('dist/notary.json')).get('id',''))" 2>/dev/null || true)
  [[ -n $STATUS ]] && break          # 苹果给了结果（通过或不通过）就不再重试
  echo "⚠️ 公证上传没成功（第 $i 次）：$(head -c 160 dist/notary.err)"; sleep 15
done
if [[ $STATUS != Accepted ]]; then
  echo "❌ 公证没通过（$STATUS）"
  [[ -n $SUB ]] && xcrun notarytool log "$SUB" --key "$ONEDUO_ASC_KEY" --key-id "$ONEDUO_ASC_KEY_ID" --issuer "$ONEDUO_ASC_ISSUER" | head -60
  exit 1
fi

# 3) 装订公证票据到 .app，再重新打 zip（离线也能验证）
xcrun stapler staple "$APP"
rm -f "$OUT"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"
spctl --assess --type execute -vv "$APP" 2>&1 | head -3
echo "✅ 已签名 + 公证：$OUT"

#!/bin/zsh
# 一键签发版（维护者在 Mac 上跑）：
#   ./release.sh 0.1.1 "这一版的更新说明（显示在更新弹窗里）"
#
# 依次：改版本号 → Mac 打包 + 签名公证 → Windows 打包（远程 Windows 机器）→ 建 GitHub Release 上传
#       → Gitee 同步 → R2 备用镜像（dl.oneduo.app）→ 最后才改 catalog/apps.json 的 installer（版本 + 文件名 + sha256 + 大小）并推送。
# 最后一步推上去，所有装了一键签的电脑下次刷新列表就会弹更新。所以前面任何一步失败都不会误弹。
#
# 需要：ONETAP_WIN=用户@Windows机器（打 Windows 包）；~/.config/oneduo/sign.env（Mac 签名公证，见 packaging/sign_mac.sh）；
#       gh 已登录；Gitee 同步可选（ONEDUO_GITEE_TOKEN 或 ~/.config/oneduo/gitee_token）；
#       R2 镜像可选（ONEDUO_CF_TOKEN 或 ~/.config/oneduo/cf_token）。
set -e
cd "$(dirname "$0")"
VER=${1:?用法：./release.sh <版本号> "<更新说明>"}
NOTES=${2:?要写更新说明（会显示在更新弹窗里）}
TAG=v$VER
ROOT=$(cd .. && pwd)
REPO=81199000/OneDuo
print() { builtin print -P "%F{cyan}== $1%f"; }
die() { builtin print -P "%F{red}❌ $1%f"; exit 1; }

MAC=dist/OneDuo-$VER-macos-universal.zip
WIN=dist/OneDuo-$VER-windows-x64-setup.exe
[[ -z $(git -C "$ROOT" status --porcelain -uno) ]] || die "工作树有未提交的改动"

if [[ -z $ONEDUO_PUBLISH ]]; then   # ONEDUO_PUBLISH=1：安装包已经发好（ONEDUO_HOLD 跑过），只做第 6 步
[[ -n $ONETAP_WIN ]] || die "先 export ONETAP_WIN=用户@Windows机器IP"

print "1/6 版本号 → $VER"
sed -i '' -E "s/^VERSION = \".*\"/VERSION = \"$VER\"/" app/core.py
sed -i '' -E "s/#define AppVersion \".*\"/#define AppVersion \"$VER\"/" packaging/OneTap.iss
git -C "$ROOT" add py/app/core.py py/packaging/OneTap.iss
git -C "$ROOT" commit -qm "release: 一键签 $VER" || true

print "2/6 Mac 打包 + 签名 + 公证"
if [[ -n $ONEDUO_SKIP_MAC && -f $MAC ]]; then echo "（沿用已公证的 $MAC）"; else ./build_mac.sh; fi
[[ -f $MAC ]] || die "没有 $MAC"
spctl --assess --type execute "dist/一键签.app" || die "Mac 包没通过系统检查（签名 / 公证）"

print "3/6 Windows 打包"
(cd "$ROOT" && cargo build --release -p plumesign --target x86_64-pc-windows-gnu)
./packaging/win_remote.sh run 'New-Item -ItemType Directory -Force packaging\drivers | Out-Null; foreach ($f in "C:\onetap\drv\x\AppleMobileDeviceSupport64.msi","C:\onetap\drv\Bonjour64.msi") { if (Test-Path $f) { Copy-Item $f packaging\drivers\ -Force } }; Remove-Item dist\OneDuo-*-setup.exe -ErrorAction SilentlyContinue; powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_win.ps1 2>&1 | Select-Object -Last 2'
scp -4 -o BatchMode=yes -q "$ONETAP_WIN:C:/onetap/src/py/$WIN" "$WIN" || die "取回 Windows 安装包失败"

print "4/6 GitHub Release $TAG"
git -C "$ROOT" push -q origin main
git -C "$ROOT" tag -f "$TAG" >/dev/null && git -C "$ROOT" push -q -f origin "$TAG"
gh release view "$TAG" -R $REPO >/dev/null 2>&1 || gh release create "$TAG" -R $REPO --title "一键签 $VER" --notes "$NOTES" --verify-tag
for f in "$MAC" "$WIN"; do
  for i in 1 2 3 4; do gh release upload "$TAG" -R $REPO "$f" --clobber && break; echo "上传重试 $i"; sleep 10; done
done

print "5/6 Gitee 同步"
if git -C "$ROOT" remote get-url gitee >/dev/null 2>&1; then
  git -C "$ROOT" push -q gitee main 2>&1 | grep -v 'GITEE\|trace' || true
  git -C "$ROOT" push -q -f gitee "$TAG" 2>&1 | grep -v 'GITEE\|trace' || true
  python3 packaging/gitee_release.py "$TAG" "$NOTES" "$MAC" "$WIN" || echo "⚠️ Gitee 发行版同步失败（国内用户会走 GitHub 下载），可稍后重跑：python3 packaging/gitee_release.py $TAG \"$NOTES\" $MAC $WIN"
fi

print "5b/6 R2 备用镜像（dl.oneduo.app）"
python3 packaging/r2_sync.py --installer "$TAG" "$MAC" "$WIN" || echo "⚠️ R2 上传失败（只影响 Gitee 挂掉时的备用下载），可稍后重跑：python3 packaging/r2_sync.py --installer $TAG $MAC $WIN"

if [[ -n $ONEDUO_HOLD ]]; then
  # 先不推应用列表：生成一份测试用列表（ONETAP_CATALOG=dist/catalog-test.json 启动旧版即可实测更新）
  python3 - "$VER" "$NOTES" "$MAC" "$WIN" <<'PY'
import hashlib, json, pathlib, sys
ver, notes, mac, win = sys.argv[1:]
j = json.loads(pathlib.Path("../catalog/apps.json").read_text(encoding="utf-8"))
meta = lambda f: {"name": pathlib.Path(f).name, "size": pathlib.Path(f).stat().st_size, "sha256": hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest()}
j["installer"].update({"latest": ver, "notes": notes, "assets": {"macos": meta(mac), "windows": meta(win)}})
pathlib.Path("dist/catalog-test.json").write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
PY
  builtin print -P "%F{yellow}⏸ ONEDUO_HOLD：安装包已发布，应用列表没动（不会弹更新）。测试用列表 dist/catalog-test.json；确认后去掉 ONEDUO_HOLD 重跑即可%f"
  exit 0
fi

fi   # ONEDUO_PUBLISH

[[ -f $MAC && -f $WIN ]] || die "dist/ 里缺 $VER 的安装包"
print "6/6 更新应用列表 → 所有一键签弹更新"
python3 - "$VER" "$NOTES" "$MAC" "$WIN" <<'PY'
import hashlib, json, pathlib, sys
ver, notes, mac, win = sys.argv[1:]
p = pathlib.Path("../catalog/apps.json")
j = json.loads(p.read_text(encoding="utf-8"))
def meta(f):
    f = pathlib.Path(f)
    return {"name": f.name, "size": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
inst = j.setdefault("installer", {})
inst.update({"latest": ver, "notes": notes, "assets": {"macos": meta(mac), "windows": meta(win)}})
p.write_text(json.dumps(j, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("catalog installer →", ver)
PY
git -C "$ROOT" add catalog/apps.json
git -C "$ROOT" commit -qm "release: 应用列表 installer → $VER（触发自动更新）"
git -C "$ROOT" push -q origin main
git -C "$ROOT" remote get-url gitee >/dev/null 2>&1 && { git -C "$ROOT" push -q gitee main 2>&1 | grep -v 'GITEE\|trace' || true; }
python3 packaging/r2_sync.py || echo "⚠️ R2 应用列表同步失败，可稍后重跑：python3 packaging/r2_sync.py"
builtin print -P "%F{green}✅ 一键签 $VER 已发布%f  https://github.com/$REPO/releases/tag/$TAG"

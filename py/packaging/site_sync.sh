#!/bin/zsh
# 官网 oneduo.app 跟着发版：把网页里的兜底版本号改成当前版本，构建并发布到 Cloudflare Pages。
#   ./packaging/site_sync.sh            按 catalog/apps.json 里的一键签版本
# 网页平时直接读 R2 上的应用列表拿版本号，这里改的是 R2 读不到时用的兜底值，保证三个渠道版本一致。
# 官网仓库位置：ONEDUO_SITE（默认 ~/Projects/oneduo-site）
set -e
cd "$(dirname "$0")/.."
SITE=${ONEDUO_SITE:-$HOME/Projects/oneduo-site}
[[ -x $SITE/deploy.sh ]] || { echo "⚠️ 找不到官网仓库 $SITE，跳过"; exit 1; }

read VER MAC WIN < <(python3 -c "
import json; i=json.load(open('../catalog/apps.json'))['installer']
print(i['latest'], i['assets']['macos']['name'], i['assets']['windows']['name'])")

python3 - "$SITE/config.js" "$VER" "$MAC" "$WIN" <<'PY'
import re, sys
p, ver, mac, win = sys.argv[1:]
s = open(p, encoding="utf-8").read()
s = re.sub(r'version:\s*"[^"]*"', f'version: "{ver}"', s, count=1)
s = re.sub(r'mac:\s*"[^"]*"', f'mac: "{mac}"', s, count=1)
s = re.sub(r'win:\s*"[^"]*"', f'win: "{win}"', s, count=1)
open(p, "w", encoding="utf-8").write(s)
PY

if [[ -n $(git -C "$SITE" status --porcelain -- config.js) ]]; then
  git -C "$SITE" add config.js
  git -C "$SITE" commit -qm "release: 官网兜底版本 → 一键签 $VER"
fi
# 发布失败（网络断、token 过期）以前被 `|| true` 吞掉，照样打印 ✅，要到最后核对才发现官网还是旧版：
# 没看到 Deployment complete 就重试一次，还不行就报错退出
for i in 1 2; do
  OUT=$("$SITE/deploy.sh" 2>&1) || true
  echo "$OUT" | grep -E "Deployment complete|ERROR|✘" || true
  echo "$OUT" | grep -q "Deployment complete" && { echo "✅ 官网已发布（一键签 $VER）"; exit 0; }
  echo "⚠️ 官网发布没成功，${i}/2"; sleep 5
done
echo "❌ 官网发布失败（Cloudflare Pages）"; exit 1

#!/bin/zsh
# 只改了应用列表（加 App / 改 App 信息 / 换图标），不发新版一键签时用：
#   ./publish_catalog.sh "加上 XXX App"
# 依次：提交 catalog/ → 推 Gitee（渠道①）→ 同步 R2（渠道②，含各 App 软件源和 IPA 镜像）→ 官网（渠道③）→ 三渠道核对。
# 所有一键签下次启动 / 刷新 / 每 30 分钟检查时就能看到。
set -e
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
MSG=${1:-更新应用列表}
die() { builtin print -P "%F{red}❌ $1%f"; exit 1; }
step() { builtin print -P "%F{cyan}== $1%f"; }

python3 -c "import json;json.load(open('../catalog/apps.json'))" || die "catalog/apps.json 不是合法 JSON"
python3 - <<'PY' || exit 1
import json, pathlib
j = json.load(open("../catalog/apps.json"))
miss = [a["id"] for a in j["apps"] if a.get("icon") and not (pathlib.Path("../catalog") / a["icon"]).is_file()]
nosrc = [a["id"] for a in j["apps"] if not a.get("sources")]
if miss: print("❌ 图标文件不存在：", miss)
if nosrc: print("❌ 没写软件源 sources：", nosrc)
raise SystemExit(1 if miss or nosrc else 0)
PY

step "提交应用列表"
git -C "$ROOT" add catalog/
git -C "$ROOT" diff --cached --quiet || git -C "$ROOT" commit -qm "catalog: $MSG"

step "渠道① Gitee"
git -C "$ROOT" push -q origin main 2>/dev/null || echo "（GitHub 存档推送失败，不影响用户）"
git -C "$ROOT" push -q gitee main 2>&1 | grep -v 'GITEE\|trace' || true
[[ $(git -C "$ROOT" ls-remote gitee main | cut -f1) == $(git -C "$ROOT" rev-parse HEAD) ]] || die "没推上 Gitee"

step "渠道② R2（dl.oneduo.app）"
python3 packaging/r2_sync.py || die "R2 同步失败"

step "渠道③ 官网 oneduo.app"
./packaging/site_sync.sh || die "官网发布失败"

step "核对三个渠道"
python3 packaging/publish_check.py --wait 180 || die "三渠道没全部对上"
builtin print -P "%F{green}✅ 应用列表已发布到 Gitee、R2、官网%f"

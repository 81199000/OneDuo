#!/bin/sh
# 发布 NAS 版到 R2（dl.oneduo.app/oneduo/nas/）：先 ./nas/build.sh，再跑这个
#   oneduo/nas/v<版本>/oneduo-nas-<版本>-<arch>.tar.gz
#   oneduo/nas/latest.json   ← 最后写，电脑版「装到 NAS」读它
set -e
cd "$(dirname "$0")/.."
VER=$(grep -m1 '^VERSION' py/app/core.py | cut -d'"' -f2)
python3 - "$VER" <<'PY'
import hashlib, json, pathlib, sys, subprocess
sys.path.insert(0, "py/packaging")
import r2_sync as r2
ver = sys.argv[1]
imgs = {}
for f in sorted(pathlib.Path("dist/nas").glob(f"oneduo-nas-{ver}-*.tar.gz")):
    arch = f.name.rsplit("-", 1)[1].split(".")[0]
    key = f"oneduo/nas/v{ver}/{f.name}"
    r2.put_path(key, f, "application/gzip", "public, max-age=31536000, immutable")
    if r2.remote_size(key) != f.stat().st_size:
        sys.exit(f"R2 上 {key} 大小不对")
    imgs[arch] = {"name": f.name, "size": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
if not imgs:
    sys.exit("dist/nas/ 里没有本版本的镜像，先 ./nas/build.sh")
man = {"version": ver, "images": imgs}
r2.put("oneduo/nas/latest.json", json.dumps(man, indent=1).encode(), "application/json", "public, max-age=60")
print(json.dumps(man, indent=1))
PY

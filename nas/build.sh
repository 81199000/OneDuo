#!/bin/sh
# 打一键签 NAS 版镜像。nas/bin/<arch>/ 里要先放好 Linux 版 plumesign + netmuxd（见 nas/README.md）
#   ./nas/build.sh            # amd64 + arm64（有哪个 bin 打哪个）
#   ./nas/build.sh amd64
# 产物：本机镜像 oneduo-nas:<版本>-<arch>，以及 dist/nas/oneduo-nas-<版本>-<arch>.tar.gz（docker save + gzip，发 R2 用）
set -e
cd "$(dirname "$0")/.."
VER=$(grep -m1 '^VERSION' py/app/core.py | cut -d'"' -f2)
ARCHS=${1:-"amd64 arm64"}
mkdir -p dist/nas
for A in $ARCHS; do
  [ -x nas/bin/$A/plumesign ] && [ -x nas/bin/$A/netmuxd ] || { echo "跳过 $A：nas/bin/$A/ 里没有 plumesign / netmuxd"; continue; }
  CTX=nas/.ctx-$A
  rm -rf $CTX && mkdir -p $CTX/py $CTX/nas $CTX/bin
  cp nas/bin/$A/plumesign nas/bin/$A/netmuxd nas/bin/netmuxd-LICENSE.txt $CTX/bin/
  rsync -a --exclude __pycache__ --exclude ui.py --exclude nasinstall.py --exclude nasfind.py --exclude nasmove.py py/app $CTX/py/
  cp -R catalog $CTX/catalog
  cp nas/entrypoint.sh $CTX/nas/
  docker build -q --platform linux/$A -f nas/Dockerfile -t oneduo-nas:$VER-$A $CTX
  # 镜像里写死的 tag 用 oneduo-nas:<版本>，导入群晖后 compose 直接引用
  docker tag oneduo-nas:$VER-$A oneduo-nas:$VER
  docker save oneduo-nas:$VER | gzip -9 > dist/nas/oneduo-nas-$VER-$A.tar.gz
  rm -rf $CTX
  ls -la dist/nas/oneduo-nas-$VER-$A.tar.gz
done

#!/bin/sh
# 打一键签 NAS 版镜像（x86_64）。nas/bin/ 里要先放好 Linux 版 plumesign + netmuxd（见 nas/README.md）
set -e
cd "$(dirname "$0")/.."
VER=$(grep -m1 '^VERSION' py/app/core.py | cut -d'"' -f2)
rm -rf nas/.ctx && mkdir -p nas/.ctx/py nas/.ctx/nas
cp -R nas/bin nas/.ctx/bin
rsync -a --exclude __pycache__ --exclude ui.py py/app nas/.ctx/py/
cp -R catalog nas/.ctx/catalog
cp nas/entrypoint.sh nas/.ctx/nas/
docker build --platform linux/amd64 -f nas/Dockerfile -t oneduo-nas:"$VER" -t oneduo-nas:latest nas/.ctx
rm -rf nas/.ctx
echo "built oneduo-nas:$VER"

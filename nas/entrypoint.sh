#!/bin/sh
# netmuxd 负责在 Wi‑Fi 里找手机（代替 Mac / Windows 的 usbmuxd），一键签网页服务用它装 App。
# netmuxd 挂了就重启它，网页服务不受影响。
set -e
mkdir -p /data/app /data/lockdown /data/config /run/oneduo
(
  while true; do
    rm -f /run/oneduo/usbmuxd
    RUST_LOG=${NETMUXD_LOG:-warn} /opt/oneduo/bin/netmuxd --disable-usb \
      --plist-storage /data/lockdown --socket-path /run/oneduo/usbmuxd || true
    echo "netmuxd 退出了，3 秒后重启" >&2
    sleep 3
  done
) &
cd /opt/oneduo
exec python3 -c "import sys; sys.path.insert(0, '/opt/oneduo'); from app import server; server.main()"

# 一键签 NAS 版（群晖 / 威联通 / 任意 x86_64 Linux + Docker）
# 构建：在仓库根目录 nas/build.sh（先编好 Linux 版 plumesign / netmuxd 放进 nas/bin/）
FROM python:3.13-slim

RUN sed -i "s#deb.debian.org#mirrors.tuna.tsinghua.edu.cn#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    ONETAP_DATA_DIR=/data/app \
    ONETAP_LOCKDOWN=/data/lockdown \
    XDG_CONFIG_HOME=/data/config \
    USBMUXD_SOCKET_ADDRESS=/run/oneduo/usbmuxd \
    ONETAP_PLUMESIGN=/opt/oneduo/bin/plumesign \
    ONETAP_PORT=8686

WORKDIR /opt/oneduo
COPY bin/plumesign bin/netmuxd /opt/oneduo/bin/
COPY py/app /opt/oneduo/app
COPY catalog /opt/oneduo/catalog
COPY nas/entrypoint.sh /opt/oneduo/entrypoint.sh
RUN chmod +x /opt/oneduo/bin/* /opt/oneduo/entrypoint.sh && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple certifi

VOLUME /data
EXPOSE 8686
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8686/api/ping',timeout=4)" || exit 1
ENTRYPOINT ["/opt/oneduo/entrypoint.sh"]

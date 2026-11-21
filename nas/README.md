# 一键签 NAS 版

群晖 / 威联通 / 任何 x86_64 Linux + Docker。NAS 24 小时开着，免费 Apple ID 签的 App 每 7 天自动续签，电脑关机也不影响。

## 用户怎么装

**群晖（全自动）**：手机用数据线连电脑、解锁 → 电脑版一键签 →「我的手机」→「**装到 NAS**」→ 自动找到 NAS → 填 NAS 管理员账号密码 → 开始安装。
电脑会从 dl.oneduo.app 下载 NAS 版（约 57 MB，自动选 x86 / ARM）、传给 NAS、在 Container Manager 里建 `oneduo` 项目并启动，再把手机和 Apple ID 搬过去。约 1–2 分钟，NAS 不用连 Docker Hub。
装好后浏览器打开 `http://NAS地址:8686`。再点一次「装到 NAS」= 升级到最新版，数据保留。

**其他 NAS（手动）**：Docker / Compose 里新建项目，粘贴 `docker-compose.yml`（镜像先 `docker load -i oneduo-nas-<版本>-<arch>.tar.gz`），
然后电脑版点「装到 NAS」→ 选这台 NAS（显示「已经装了一键签 NAS 版」）→ 开始搬。

## 原理

- `netmuxd`（LGPL-2.1，见 `bin/netmuxd-LICENSE.txt`）在家里 Wi‑Fi 里通过 Bonjour 找手机，代替 Mac / Windows 的 usbmuxd。
- 「搬到 NAS」把电脑上的手机配对记录（和访达 / iTunes 是同一份）+ Apple ID 登录令牌（不含密码）+ 安装记录打包传给 NAS，NAS 不需要插数据线。
- 网页和桌面版是同一份 `index.html`，`server-shim.js` 把 pywebview 接口换成 HTTP + SSE。

## 开发者：构建镜像

```sh
# Linux 版 plumesign（静态，musl）
docker run --rm --platform linux/amd64 -v $PWD:/src rust:1-alpine sh -c \
  'apk add musl-dev openssl-dev openssl-libs-static pkgconfig perl make && cd /src && \
   OPENSSL_STATIC=1 CARGO_TARGET_DIR=/src/target-linux-x86_64 cargo build --release -p plumesign'
mkdir -p nas/bin && cp target-linux-x86_64/release/plumesign nas/bin/
# netmuxd：github.com/jkcoxson/netmuxd 同样在 rust:1-alpine 里 cargo build --release，产物放 nas/bin/netmuxd
# 放到 nas/bin/amd64/、nas/bin/arm64/（arm64 在 Apple 芯片 Mac 上用 --platform linux/arm64 原生编）
./nas/build.sh        # → dist/nas/oneduo-nas-<版本>-{amd64,arm64}.tar.gz
./nas/release.sh      # → dl.oneduo.app/oneduo/nas/（最后写 latest.json，电脑版据此下载）
```

## 环境变量

- `ONETAP_PORT` 端口（默认 8686）
- `ONETAP_PASSWORD` 访问密码。只在家里局域网用可以不设；做了外网端口映射**必须设**。

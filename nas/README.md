# 一键签 NAS 版

群晖 / 威联通 / 任何 x86_64 Linux + Docker。NAS 24 小时开着，免费 Apple ID 签的 App 每 7 天自动续签，电脑关机也不影响。

## 用户怎么装（3 步）

1. **群晖 Container Manager → 项目 → 新增**，路径选一个文件夹（如 `/docker/oneduo`），来源选「创建 docker-compose.yml」，粘贴 `docker-compose.yml` 全文 → 下一步 → 完成。
2. 电脑上打开一键签 →「我的手机」→「**搬到 NAS**」→ 填 NAS 地址 → 开始搬（手机先用数据线连着电脑、解锁）。
3. 浏览器打开 `http://NAS地址:8686`，以后都在这里装 App、看续签。手机能用浏览器打开同一个地址。

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
./nas/build.sh        # → oneduo-nas:<版本>
```

## 环境变量

- `ONETAP_PORT` 端口（默认 8686）
- `ONETAP_PASSWORD` 访问密码。只在家里局域网用可以不设；做了外网端口映射**必须设**。

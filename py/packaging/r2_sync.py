#!/usr/bin/env python3
"""把一键签的应用列表 / 软件源 / 安装包同步到 Cloudflare R2（dl.oneduo.app），做 Gitee 的兜底镜像。
幂等：已经在 R2 上、大小一致的安装包跳过；JSON 每次覆盖。

  python3 packaging/r2_sync.py                 # 同步应用列表 + 图标 + 各 App 软件源及其里面的所有 IPA
  python3 packaging/r2_sync.py --installer <tag> <文件>...   # 另外上传一键签安装包到 oneduo/<tag>/

R2 上的布局（客户端 core.R2_BASE 按这个拼地址）：
  oneduo/catalog/apps.json、oneduo/catalog/icons/*      应用列表（与 Gitee 上同一份）
  oneduo/v<版本>/<一键签安装包>                          一键签自身更新
  <app id>/<app id>.json                                软件源副本（下载地址改成 R2）
  <app id>/v<版本>/<IPA 文件名>                          App 安装包

凭据（不入库）：环境变量 ONEDUO_CF_TOKEN，或 ~/.config/oneduo/cf_token 文件（Cloudflare API token，要有 R2 写权限）。
"""
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

ACCOUNT = os.environ.get("ONEDUO_CF_ACCOUNT", "89ffdab4c08748b7a491e7a6f0d3147b")
BUCKET = os.environ.get("ONEDUO_R2_BUCKET", "oneduo-dl")
PUBLIC = "https://dl.oneduo.app"
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/r2/buckets/{BUCKET}/objects/"
ROOT = pathlib.Path(__file__).resolve().parents[2]
UA = "OneDuo-sync/1"


def token():
    t = os.environ.get("ONEDUO_CF_TOKEN")
    f = pathlib.Path.home() / ".config/oneduo/cf_token"
    if not t and f.exists():
        t = f.read_text().strip()
    if not t:
        sys.exit("没有 Cloudflare token（ONEDUO_CF_TOKEN 或 ~/.config/oneduo/cf_token）")
    return t


TOK = token()
MISSING = []   # 本轮 R2 缺、本机也没有的包
# 发版脚本刚打好的包在这些目录：先找本机同名同大小的文件，省掉从 Gitee 再下一遍
LOCAL_DIRS = [pathlib.Path(p).expanduser() for p in
              os.environ.get("ONEDUO_LOCAL_IPA_DIRS", "~/Projects/douduo/build:~/Projects/hongguo/build").split(":")]


def put_path(key, path, ctype, cache):
    """大文件用 curl 传：Python urllib 走本机代理传 R2 约 0.3MB/s 还会 Broken pipe，curl 同网约 6MB/s（2026-11-21 实测 22MB：77s → 3.4s）"""
    import subprocess
    for i in range(4):
        r = subprocess.run(["curl", "-s", "-m", "600", "-X", "PUT", "-H", "Authorization: Bearer " + TOK,
                            "-H", "Content-Type: " + ctype, "-H", "Cache-Control: " + cache,
                            "--data-binary", "@" + str(path), API + key], capture_output=True, text=True)
        try:
            if json.loads(r.stdout or "{}").get("success"):
                print(f"  ↑ {key}  ({pathlib.Path(path).stat().st_size:,} B)")
                return
        except ValueError:
            pass
        print(f"  上传失败重试 {i + 1}：{key} {r.stderr.strip() or r.stdout[:120]}")
    raise SystemExit(f"上传失败：{key}")


def put(key, data: bytes, ctype, cache="public, max-age=300"):
    if len(data) > 2_000_000:   # 大文件统一走 curl
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
        try:
            return put_path(key, f.name, ctype, cache)
        finally:
            os.unlink(f.name)
    for i in range(4):
        req = urllib.request.Request(API + key, data=data, method="PUT", headers={
            "Authorization": "Bearer " + TOK, "Content-Type": ctype, "Cache-Control": cache})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                if json.loads(r.read()).get("success"):
                    print(f"  ↑ {key}  ({len(data):,} B)")
                    return
        except Exception as e:  # noqa: BLE001
            print(f"  上传失败重试 {i + 1}：{key} {e}")
    raise SystemExit(f"上传失败：{key}")


def remote_size(key):
    """R2 上这个文件多大（只读响应头）。404 → -1；网络抖一下（TLS 握手断、超时）重试几次，
    不能因为核对这一步断网就让整个发版失败（2026-11-21 0.2.4 上传成功却在这里 SSL 报错退出）"""
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(f"{PUBLIC}/{key}", method="HEAD", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return int(r.headers.get("Content-Length") or -1)
        except urllib.error.HTTPError:
            return -1
        except Exception as e:  # noqa: BLE001
            last = e
            import time as _t
            _t.sleep(3 * (i + 1))
    raise SystemExit(f"核对 R2 文件大小时网络一直不通：{key} {last}")


def get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.read()


def put_file_if_needed(key, src_url=None, path=None, size=0):
    """R2 上已有同大小的文件就跳过；否则只传本机文件。
    不再「从 Gitee 下载下来再传 R2」（老板 11-21：上传完不该再下载再上传）——本机没有包就报出来，
    在打包那台机器上补传：r2_sync.py --put <key> <本机文件>。"""
    have = remote_size(key)
    want = path.stat().st_size if path else size
    if have >= 0 and (not want or have == want):
        print(f"  = {key}（已存在）")
        return True
    if not path and src_url:
        name = src_url.rsplit("/", 1)[-1]
        path = next((d / name for d in LOCAL_DIRS if (d / name).is_file()
                     and (not size or (d / name).stat().st_size == size)), None)
    if not path:
        print(f"  ✗ {key}：R2 上没有，本机也没有这个包（{src_url or ''}），跳过")
        MISSING.append(key)
        return False
    put_path(key, path, "application/octet-stream", "public, max-age=31536000, immutable")
    got = remote_size(key)   # 传完只读响应头核对大小，不下载
    if got != path.stat().st_size:
        raise SystemExit(f"上传后核对失败：{key} R2 上 {got} ≠ 本机 {path.stat().st_size}")
    return True


def sync_catalog():
    print("应用列表 + 图标")
    cat_path = ROOT / "catalog/apps.json"
    put("oneduo/catalog/apps.json", cat_path.read_bytes(), "application/json; charset=utf-8", "public, max-age=60")
    cat = json.loads(cat_path.read_text("utf-8"))
    for app in cat.get("apps", []):          # 只传列表里在架的 App 的图标
        ic = ROOT / "catalog" / app.get("icon", "")
        if app.get("icon") and ic.is_file():
            put(f"oneduo/catalog/{app['icon']}", ic.read_bytes(), "image/png", "public, max-age=3600")
    return cat


def sync_app(app):
    """App 的软件源：拉 Gitee 那份 → 把里面每个版本的 IPA 镜像到 R2 → 存一份下载地址改成 R2 的源副本"""
    aid = app["id"]
    gitee_src = next((u for u in app.get("sources", []) if "gitee.com" in u), None)
    if not gitee_src:
        print(f"{aid}：没有 Gitee 软件源，跳过")
        return
    # 发版脚本会传 ONEDUO_LOCAL_SOURCES="douduo=/path/docs/source/douduo.json"：直接读刚生成的本地源，
    # 不等 Gitee raw 的 CDN 缓存（以前缓存没刷新会照抄上一版，得轮询几分钟再重跑）
    local_src = dict(x.split("=", 1) for x in os.environ.get("ONEDUO_LOCAL_SOURCES", "").split(",") if "=" in x).get(aid)
    if local_src and pathlib.Path(local_src).expanduser().is_file():
        print(f"{aid}：本机 {local_src}")
        j = json.loads(pathlib.Path(local_src).expanduser().read_text("utf-8-sig"))
    else:
        print(f"{aid}：{gitee_src}")
        j = json.loads(get(gitee_src).decode("utf-8-sig"))
    for a in j.get("apps", []):
        for v in a.get("versions") or [a]:
            url = v.get("downloadURL") or v.get("downloadUrl") or ""
            ver = str(v.get("version") or a.get("version") or "")
            if not url or not ver or "gitee.com" not in url:
                continue
            name = url.rsplit("/", 1)[-1]
            key = f"{aid}/v{ver}/{name}"
            if put_file_if_needed(key, src_url=url, size=int(v.get("size") or a.get("size") or 0)):
                v["downloadURL"] = f"{PUBLIC}/{key}"
            # 缺包的版本保留原 Gitee 下载地址，不会变成 404
    j["sourceURL"] = f"{PUBLIC}/{aid}/{aid}.json"
    if j.get("name") and "R2" not in j["name"]:
        j["name"] = j["name"].replace("（国内）", "") + "（备用）"
    put(f"{aid}/{aid}.json", json.dumps(j, ensure_ascii=False, indent=2).encode(),
        "application/json; charset=utf-8", "public, max-age=60")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--put"]:   # 发版脚本：IPA 一打好就先传，和 GitHub / Gitee 上传并行
        put_file_if_needed(args[1], path=pathlib.Path(args[2]))
        return
    if args[:1] == ["--installer"]:
        tag, files = args[1], [pathlib.Path(f) for f in args[2:]]
        print(f"一键签安装包 → oneduo/{tag}/")
        for f in files:
            put_file_if_needed(f"oneduo/{tag}/{f.name}", path=f)
        return
    cat = sync_catalog()
    for app in cat.get("apps", []):
        sync_app(app)
    inst = cat.get("installer", {})
    ver = inst.get("latest")
    for a in (inst.get("assets") or {}).values():
        if ver and a.get("name"):
            key = f"oneduo/v{ver}/{a['name']}"
            local = ROOT / "py/dist" / a["name"]
            if local.exists() and local.stat().st_size == a.get("size"):
                put_file_if_needed(key, path=local)
            else:
                put_file_if_needed(key, src_url=f"https://gitee.com/remon8/oneduo/releases/download/v{ver}/{a['name']}",
                                   size=int(a.get("size") or 0))
    if MISSING:
        print(f"⚠️ 有 {len(MISSING)} 个包 R2 上没有、本机也没有，源里这些版本仍指向 Gitee：{', '.join(MISSING)}")
    print("✅ R2 同步完成")


if __name__ == "__main__":
    main()

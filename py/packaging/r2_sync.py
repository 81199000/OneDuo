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


def put(key, data: bytes, ctype, cache="public, max-age=300"):
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
    try:
        req = urllib.request.Request(f"{PUBLIC}/{key}", method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers.get("Content-Length") or -1)
    except urllib.error.HTTPError:
        return -1


def get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.read()


def put_file_if_needed(key, src_url=None, path=None, size=0):
    """大小一致就跳过；没有就从本地文件或 Gitee 下载后上传"""
    have = remote_size(key)
    want = path.stat().st_size if path else size
    if have >= 0 and (not want or have == want):
        print(f"  = {key}（已存在）")
        return
    if path:
        data = path.read_bytes()
    else:
        print(f"  ↓ {src_url}")
        data = get(src_url, timeout=600)
        if size and len(data) != size:
            raise SystemExit(f"下载大小不对：{src_url} {len(data)} ≠ {size}")
    put(key, data, "application/octet-stream", "public, max-age=31536000, immutable")


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
            put_file_if_needed(key, src_url=url, size=int(v.get("size") or a.get("size") or 0))
            v["downloadURL"] = f"{PUBLIC}/{key}"
    j["sourceURL"] = f"{PUBLIC}/{aid}/{aid}.json"
    if j.get("name") and "R2" not in j["name"]:
        j["name"] = j["name"].replace("（国内）", "") + "（备用）"
    put(f"{aid}/{aid}.json", json.dumps(j, ensure_ascii=False, indent=2).encode(),
        "application/json; charset=utf-8", "public, max-age=60")


def main():
    args = sys.argv[1:]
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
    print("✅ R2 同步完成")


if __name__ == "__main__":
    main()

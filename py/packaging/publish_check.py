#!/usr/bin/env python3
"""核对三个发布渠道是不是都到位了：Gitee（主）、R2 dl.oneduo.app（备用）、官网 oneduo.app。

  python3 packaging/publish_check.py              # 按本地 catalog/apps.json 核对
  python3 packaging/publish_check.py --wait 120   # 最多等 120 秒（CDN 缓存刷新），全部对上才算过

核对内容：
  - 应用列表：Gitee / R2 上的 apps.json 和本地一致（一键签版本 + App 列表）
  - 一键签安装包：Gitee 发行版、R2 上都能下（HEAD，大小一致）
  - 官网：oneduo.app 内置的兜底版本号 = 当前版本（网页优先读 R2 的列表，这个只在 R2 读不到时用）
全部对上退出码 0，否则 1。
"""
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
UA = "OneDuo-publish-check/1"
GITEE_REPO = "remon8/oneduo"
R2 = "https://dl.oneduo.app"
SITE = "https://oneduo.app/"


def get(url, method="GET", headers=None, timeout=30):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), (r.read() if method == "GET" else b"")


def gitee_token():
    t = os.environ.get("ONEDUO_GITEE_TOKEN")
    f = pathlib.Path.home() / ".config/oneduo/gitee_token"
    return t or (f.read_text().strip() if f.exists() else "")


def summary(j):
    i = j.get("installer", {})
    return (i.get("latest"), (i.get("assets", {}).get("macos") or {}).get("sha256"),
            (i.get("assets", {}).get("windows") or {}).get("sha256"), [a.get("id") for a in j.get("apps", [])])


def check_once(local):
    want = summary(local)
    ver = want[0]
    res = []

    # 1) Gitee：走 API 读仓库文件（raw 地址有 60 秒缓存，API 没有）
    try:
        tok = gitee_token()
        url = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/contents/catalog/apps.json?ref=main" + (f"&access_token={tok}" if tok else "")
        import base64
        _, _, b = get(url)
        g = json.loads(base64.b64decode(json.loads(b)["content"]))
        res.append(("Gitee 应用列表", summary(g) == want, f"一键签 {summary(g)[0]}，App {summary(g)[3]}"))
    except Exception as e:  # noqa: BLE001
        res.append(("Gitee 应用列表", False, str(e)[:80]))

    # 2) R2 应用列表
    try:
        _, _, b = get(f"{R2}/oneduo/catalog/apps.json?t={int(time.time())}", headers={"Cache-Control": "no-cache"})
        r = json.loads(b)
        res.append(("R2 应用列表", summary(r) == want, f"一键签 {summary(r)[0]}，App {summary(r)[3]}"))
    except Exception as e:  # noqa: BLE001
        res.append(("R2 应用列表", False, str(e)[:80]))

    # 3) 安装包：Gitee 发行版 + R2，大小要对
    for key, a in (local["installer"].get("assets") or {}).items():
        name, size = a["name"], int(a.get("size") or 0)
        for label, url in ((f"Gitee 安装包 {key}", f"https://gitee.com/{GITEE_REPO}/releases/download/v{ver}/{name}"),
                           (f"R2 安装包 {key}", f"{R2}/oneduo/v{ver}/{name}")):
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    n = int(resp.headers.get("Content-Length") or -1)
                # Gitee 发行版 HEAD 会 302 到对象存储，urllib 跟随后 Content-Length 才是真大小
                res.append((label, n == size or (n == -1 and resp.status == 200), f"{n:,} B" if n >= 0 else "无大小"))
            except Exception as e:  # noqa: BLE001
                res.append((label, False, str(e)[:80]))

    # 4) 官网：内置兜底版本号
    try:
        _, _, b = get(f"{SITE}?t={int(time.time())}", headers={"Cache-Control": "no-cache"})
        m = re.search(r'version:\s*"([^"]+)"', b.decode("utf-8", "replace"))
        sv = m.group(1) if m else "?"
        res.append(("官网 oneduo.app", sv == ver, f"内置版本 {sv}"))
    except Exception as e:  # noqa: BLE001
        res.append(("官网 oneduo.app", False, str(e)[:80]))
    return res


def main():
    wait = 0
    if "--wait" in sys.argv:
        wait = int(sys.argv[sys.argv.index("--wait") + 1])
    local = json.loads((ROOT / "catalog/apps.json").read_text("utf-8"))
    deadline = time.time() + wait
    while True:
        res = check_once(local)
        ok = all(r[1] for r in res)
        if ok or time.time() >= deadline:
            break
        time.sleep(15)
    print(f"三渠道核对（一键签 {local['installer'].get('latest')}）：")
    for name, good, note in res:
        print(f"  {'✅' if good else '❌'} {name:18s} {note}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

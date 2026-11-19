#!/usr/bin/env python3
"""把一键签安装包同步到 Gitee 发行版（release.sh 调用；幂等，同名附件先删再传）。
  python3 packaging/gitee_release.py <tag> "<说明>" <文件>...
凭据（不入库）：环境变量 ONEDUO_GITEE_TOKEN，或 ~/.config/oneduo/gitee_token 文件。
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

GITEE_REPO = os.environ.get("ONEDUO_GITEE_REPO", "remon8/oneduo")
API = f"https://gitee.com/api/v5/repos/{GITEE_REPO}"


def token():
    t = os.environ.get("ONEDUO_GITEE_TOKEN")
    f = pathlib.Path.home() / ".config/oneduo/gitee_token"
    if not t and f.exists():
        t = f.read_text().strip()
    if not t:
        sys.exit("没有 Gitee token（ONEDUO_GITEE_TOKEN 或 ~/.config/oneduo/gitee_token）")
    return t


TOK = token()


def call(method, url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": "token " + TOK, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        return json.loads(b) if b else {}


def main():
    tag, notes, files = sys.argv[1], sys.argv[2], [pathlib.Path(f) for f in sys.argv[3:]]
    try:
        rel = call("GET", f"{API}/releases/tags/{tag}")
        rel = rel if rel and rel.get("id") else None
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rel = None
    if not rel:
        body = json.dumps({"tag_name": tag, "target_commitish": tag, "name": f"一键签 {tag.lstrip('v')}",
                           "body": notes, "prerelease": False}).encode()
        rel = call("POST", f"{API}/releases", body, {"Content-Type": "application/json"})
        print("发行版已创建", rel["id"])
    for f in files:
        old = [a for a in call("GET", f"{API}/releases/{rel['id']}/attach_files") if a.get("name") == f.name]
        if any(a.get("size") == f.stat().st_size for a in old):
            print("已挂上", f.name)
            continue
        b = uuid.uuid4().hex
        payload = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{f.name}\"\r\n"
                   f"Content-Type: application/octet-stream\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{b}--\r\n".encode()
        call("POST", f"{API}/releases/{rel['id']}/attach_files", payload,
             {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=1800)
        for a in old:
            call("DELETE", f"{API}/releases/{rel['id']}/attach_files/{a['id']}")
        print("已上传", f.name, f.stat().st_size)
    names = {a.get("name") for a in call("GET", f"{API}/releases/{rel['id']}/attach_files")}
    missing = [f.name for f in files if f.name not in names]
    if missing:
        sys.exit(f"Gitee 缺 {missing}")
    print("✅ Gitee 发行版齐了")


if __name__ == "__main__":
    main()

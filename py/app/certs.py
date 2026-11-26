"""一键签 · 证书模式（全能签那种：导入 .p12 证书 + 密码 + 描述文件）

- 证书和描述文件复制到数据目录 certs/<id>/，原文件删了也能用
- 密码存在本机（只给 plumesign 用来解开 .p12，不上传任何地方）
- 证书模式签的 App 跟着描述文件过期（一般一年），不是 7 天；后台照样会自动更新新版本
- 同一时刻只用一个「当前签名方式」：Apple ID（免费）或某张证书

存储 certs.json：
  [{"id", "name", "team", "cert_expires", "prov_name", "prov_expires", "app_id",
    "all_devices", "devices": [...], "p12": "certs/<id>/cert.p12", "prov": "certs/<id>/profile.mobileprovision",
    "password": "...", "added_at"}]
偏好 sign_mode："apple_id" 或证书 id
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import core

APPLE_ID = "apple_id"
_lock = threading.Lock()


def _dir() -> Path:
    d = core.data_dir() / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return core.data_dir() / "certs.json"


def load() -> list[dict]:
    try:
        d = json.loads(_path().read_text("utf-8"))
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(items: list[dict]) -> None:
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(_path())
    if os.name != "nt":
        os.chmod(_path(), 0o600)   # 里面有证书密码


def inspect(p12: str, password: str, provision: Optional[str]) -> dict:
    """问 plumesign 证书 / 描述文件里是什么，顺便检查两者配不配套"""
    exe = core.plumesign_path()
    if not exe:
        raise core.SignError("找不到签名组件 plumesign")
    args = [str(exe), "cert", "--p12", p12]
    if provision:
        args += ["--provision", provision]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                       stdin=subprocess.DEVNULL, env={**os.environ, "RUST_LOG": "error", "NO_COLOR": "1",
                                                      "PLUME_P12_PASSWORD": password},
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise core.SignError(core.friendly((r.stdout or "") + "\n" + (r.stderr or "")))
    info: dict = {}
    for line in (r.stdout or "").splitlines():
        for tag, pre in (("PLUME_CERT ", "cert_"), ("PLUME_PROV ", "prov_")):
            if line.startswith(tag):
                k, _, v = line[len(tag):].partition("\t")
                info[pre + k] = v.strip()
    return info


def add(p12: str, password: str, provision: str) -> dict:
    if not p12 or not Path(p12).is_file():
        raise core.SignError("请选 .p12 证书文件")
    if not provision or not Path(provision).is_file():
        raise core.SignError("请选描述文件（.mobileprovision）")
    info = inspect(p12, password, provision)
    if info.get("prov_cert_match") != "true":
        raise core.SignError("证书和描述文件不配套：这个描述文件不是用这张证书生成的。请向证书提供方要同一套的 .p12 和 .mobileprovision")
    now = time.time()
    exp = min(int(info.get("cert_expires") or 0) or 1 << 40, int(info.get("prov_expires") or 0) or 1 << 40)
    if exp < now:
        raise core.SignError("证书或描述文件已经过期了")
    cid = "c-" + (info.get("cert_sha1", "")[:10] or str(int(now))).lower()
    d = _dir() / cid
    d.mkdir(exist_ok=True)
    shutil.copyfile(p12, d / "cert.p12")
    shutil.copyfile(provision, d / "profile.mobileprovision")
    rec = {"id": cid, "name": info.get("cert_name", ""), "team": info.get("cert_team", ""),
           "team_name": info.get("prov_team_name", ""),
           "cert_expires": int(info.get("cert_expires") or 0), "prov_name": info.get("prov_name", ""),
           "prov_expires": int(info.get("prov_expires") or 0), "app_id": info.get("prov_app_id", ""),
           "all_devices": info.get("prov_all_devices") == "true",
           "devices": [x for x in info.get("prov_devices", "").split(",") if x],
           "p12": str(d / "cert.p12"), "prov": str(d / "profile.mobileprovision"),
           "password": password, "added_at": now}
    with _lock:
        items = [c for c in load() if c["id"] != cid]
        items.append(rec)
        _save(items)
    return rec


def remove(cid: str) -> None:
    with _lock:
        _save([c for c in load() if c["id"] != cid])
    shutil.rmtree(_dir() / cid, ignore_errors=True)
    if core.get_pref("sign_mode") == cid:
        core.set_pref("sign_mode", APPLE_ID)


def get(cid: str) -> Optional[dict]:
    return next((c for c in load() if c["id"] == cid), None)


def expires_at(c: dict) -> float:
    vals = [v for v in (c.get("cert_expires"), c.get("prov_expires")) if v]
    return float(min(vals)) if vals else 0.0


def covers(c: dict, udid: str) -> bool:
    return bool(c.get("all_devices")) or udid in (c.get("devices") or [])


def wildcard(c: dict) -> bool:
    return "*" in (c.get("app_id") or "")


def mode() -> str:
    m = str(core.get_pref("sign_mode", APPLE_ID) or APPLE_ID)
    return m if m == APPLE_ID or get(m) else APPLE_ID


def set_mode(m: str) -> None:
    core.set_pref("sign_mode", m if m == APPLE_ID or get(m) else APPLE_ID)


def public(c: dict) -> dict:
    """给界面看的（不带密码和路径）"""
    return {k: c.get(k) for k in ("id", "name", "team", "team_name", "cert_expires", "prov_name", "prov_expires",
                                  "app_id", "all_devices")} | {"device_count": len(c.get("devices") or []),
                                                               "expires_at": expires_at(c), "wildcard": wildcard(c)}


def check_device(c: dict, udid: str, dev_name: str = "这台手机") -> None:
    if expires_at(c) and expires_at(c) < time.time():
        raise core.SignError("这张证书 / 描述文件已经过期了，换一张证书或改用 Apple ID")
    if not covers(c, udid):
        raise core.SignError(f"描述文件里没有{dev_name}（UDID {udid}），装了也打不开。请把这个 UDID 发给证书提供方加进描述文件，或改用 Apple ID 签名")


def profile_bundle_id(c: dict) -> str:
    """描述文件绑定的包名（去掉前面的团队 ID）；通配符返回 *"""
    aid = c.get("app_id") or ""
    return aid.split(".", 1)[1] if "." in aid else aid


def check_app(c: dict, bundle_id: str, name: str = "这个 App") -> str:
    """非通配符描述文件只绑定一个包名。和全能签一样不拦：签名时把 App 包名改成描述文件里的（见 plumesign
    PLUME_CERT_BUNDLE_ID）。返回给界面看的提示（None = 不需要提示）：改了包名会覆盖手机上同包名的 App"""
    if wildcard(c):
        return None
    pid = profile_bundle_id(c)
    if not bundle_id or pid == bundle_id or pid.startswith(bundle_id + "."):
        return None
    return f"这张证书只绑定了「{pid}」，{name}会改用这个包名安装，会覆盖手机上同包名的 App"


def sign_and_install(c: dict, ipa: Path, udid: str, on_line) -> None:
    core.sign_and_install_cert(ipa, udid, Path(c["p12"]), c.get("password", ""), Path(c["prov"]), on_line)

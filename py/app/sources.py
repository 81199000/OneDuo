"""一键签 · 软件源（和全能签 / ESign / AltStore 同一种 JSON 格式）

- 一键签官方源 = catalog/apps.json 里的 App（DouDuo / GuoDuo），固定在第一位，不能删
- 用户可以加别的软件源地址；源里的 App 显示在列表里，点一键安装就下载、签名、装上
- 装过的 App 记着来自哪个源，后台定时刷新源，有新版本自动更新

用户加的源存在数据目录 sources.json：
  [{"id": "s-xxxx", "url": "...", "name": "...", "added_at": 0}]
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from . import core

OFFICIAL_ID = "official"
_lock = threading.Lock()


def _path() -> Path:
    return core.data_dir() / "sources.json"


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


def source_id(url: str) -> str:
    return "s-" + hashlib.sha1(url.strip().encode()).hexdigest()[:10]


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^(https?|file)://", url, re.I):
        url = "https://" + url
    return url


def add(url: str) -> dict:
    """加一个源：先真拉一次，确认是软件源格式再存。返回 {id, url, name, count}"""
    url = normalize_url(url)
    if not url:
        raise core.SignError("请填软件源地址")
    sid = source_id(url)
    with _lock:
        if any(s["id"] == sid for s in load()):
            raise core.SignError("这个软件源已经加过了")
    try:
        j = core.fetch_source(url, ttl=0)
    except Exception as e:  # noqa: BLE001
        raise core.SignError(f"读不到这个软件源：{str(e)[:120]}")
    rec = {"id": sid, "url": url, "name": str(j.get("name") or url.split("/")[2]), "added_at": time.time()}
    with _lock:
        items = load()
        items.append(rec)
        _save(items)
    return {**rec, "count": len(j.get("apps") or [])}


def remove(sid: str) -> None:
    with _lock:
        _save([s for s in load() if s["id"] != sid])


def _app_id(sid: str, bundle_id: str, name: str) -> str:
    return f"{sid}:{bundle_id or name}"


def apps_from_source(src: dict) -> tuple[list[core.AppEntry], Optional[str]]:
    """把一个用户源拉成 App 列表（同一个包名只出一条，取最新能装的版本）。返回 (apps, 错误)"""
    try:
        j = core.fetch_source(src["url"])
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:160]
    if j.get("name") and j["name"] != src.get("name"):
        src["name"] = str(j["name"])
    groups: dict[str, dict] = {}
    for a in j.get("apps") or []:
        bid = core.sg(a, "bundleIdentifier", "bundleID", "bundleId")
        name = str(core.sg(a, "name", "appName") or bid or "?")
        key = bid or name
        g = groups.setdefault(key, {"bid": bid, "name": name, "meta": a})
        # 版本更新的那条做展示用的元数据（图标 / 简介）
        if core._vkey(str(core.sg(a, "version"))) > core._vkey(str(core.sg(g["meta"], "version"))):
            g["meta"] = a
    out = []
    for key, g in groups.items():
        m = g["meta"]
        app = core.AppEntry(
            id=_app_id(src["id"], g["bid"], g["name"]), name=g["name"],
            subtitle=str(core.sg(m, "subtitle", "developerName") or ""),
            description=str(core.sg(m, "localizedDescription", "versionDescription", "subtitle") or ""),
            bundle_id=g["bid"], icon=str(core.sg(m, "iconURL", "icon", "iconUrl") or ""),
            repo="", asset_pattern="", min_ios=str(core.sg(m, "minOSVersion", "minimumOSVersion") or ""),
            sources=[src["url"]], source_id=src["id"])
        core.apply_versions(app, core.source_versions(j, g["bid"], "" if g["bid"] else g["name"]))
        out.append(app)
    out.sort(key=lambda a: (bool(a.unsupported), a.name.lower()))
    return out, None


def refresh_app(app: core.AppEntry) -> None:
    """后台查更新：用户源的 App 直接重读它自己的源（2 分钟缓存，几十个 App 也只拉一次）"""
    if app.source_id == OFFICIAL_ID:
        core.resolve_latest(app)
        return
    found = []
    for u in app.sources:
        found += core.source_versions(core.fetch_source(u), app.bundle_id, "" if app.bundle_id else app.name)
    core.apply_versions(app, found)

"""一键签 · 自动续签 + 自动更新

免费 Apple ID 签的 App 7 天过期。这里记住「哪台手机装了哪个 App、哪天到期」，
电脑开着、手机连着（数据线或同一 Wi‑Fi）时在后台自动：
  1. 到期前 2 天内 → 重新签名安装（数据不丢，图标不动）
  2. 应用列表里有新版本 → 下载新版签名装上
都不需要用户点。失败只记下来，下一轮再试，不会弹窗打扰。

记录存在数据目录 installs.json：
  {"<udid>|<app_id>": {"udid", "device", "app_id", "name", "version", "signed_at", "expires_at",
                       "auto": true, "last_error", "last_try"}}
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import certs, core, sources

log = logging.getLogger("onetap.autopilot")

DAY = 86400
SIGN_VALID = 7 * DAY            # 免费账号描述文件有效期（实测 7 天整）
RENEW_BEFORE = 2 * DAY          # 剩不到 2 天就续
UPDATE_CHECK_EVERY = 30 * 60     # 新版本 30 分钟查一次（只读 GitHub，很轻）
TICK = 60                       # 每分钟看一眼有没有活
RETRY_AFTER = 30 * 60           # 失败后半小时再试


def _path() -> Path:
    return core.data_dir() / "installs.json"


_lock = threading.Lock()


def load() -> dict:
    try:
        return json.loads(_path().read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(_path())


def record(udid: str, device: str, app: core.AppEntry, sign_mode: str = certs.APPLE_ID) -> None:
    """装成功后调：记下用什么签的、哪天到期（Apple ID = 签名时间 + 7 天；证书 = 证书 / 描述文件到期日）"""
    now = time.time()
    exp = now + SIGN_VALID
    if sign_mode != certs.APPLE_ID:
        c = certs.get(sign_mode)
        exp = certs.expires_at(c) if c and certs.expires_at(c) else now + 365 * DAY
    with _lock:
        d = load()
        key = f"{udid}|{app.id}"
        old = d.get(key, {})
        d[key] = {"udid": udid, "device": device, "app_id": app.id, "name": app.name,
                  "version": app.latest_version or "", "custom": bool(app.local_path),
                  "source_id": app.source_id, "sign_mode": sign_mode,
                  "signed_at": now, "expires_at": exp,
                  "auto": old.get("auto", True), "last_error": "", "last_try": now}
        _save(d)


def set_auto(key: str, on: bool) -> None:
    with _lock:
        d = load()
        if key in d:
            d[key]["auto"] = bool(on)
            _save(d)


def forget(key: str) -> None:
    with _lock:
        d = load()
        d.pop(key, None)
        _save(d)


def _mark_error(key: str, msg: str) -> None:
    with _lock:
        d = load()
        if key in d:
            d[key]["last_error"] = msg[:200]
            d[key]["last_try"] = time.time()
            _save(d)


def days_left(rec: dict) -> float:
    return (rec.get("expires_at", 0) - time.time()) / DAY


class Autopilot:
    """后台线程：电脑开着就一直跑。界面通过回调拿状态，和手动安装互斥（同一时刻只签一个）"""

    def __init__(self, get_apps: Callable[[], dict], busy_lock: threading.Lock,
                 on_event: Callable[[str, dict], None]):
        self.get_apps = get_apps          # -> {app_id: AppEntry}，含「我的 IPA」
        self.busy_lock = busy_lock        # 和界面手动安装共用：一个时刻只跑一个 plumesign
        self.on_event = on_event          # ("renew_start"/"renew_done"/"renew_fail"/"changed", info)
        self.enabled = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_update_check = 0.0
        self.current: Optional[str] = None

    def start(self):
        threading.Thread(target=self._loop, name="autopilot", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        """手机刚连上 / 列表刚刷新：马上看一轮"""
        self._wake.set()

    # MARK: 主循环

    def _loop(self):
        time.sleep(8)   # 等界面和列表先起来
        while not self._stop.is_set():
            try:
                if self.enabled:
                    self._tick()
            except Exception:  # noqa: BLE001
                log.exception("autopilot tick failed")
            self._wake.wait(TICK)
            self._wake.clear()

    def _due(self) -> list[tuple[str, dict, str, Optional[str]]]:
        """要干的活：(key, 记录, 原因 renew/update, 新版本号)"""
        apps = self.get_apps()
        now = time.time()
        check_updates = now - self._last_update_check > UPDATE_CHECK_EVERY
        if check_updates:
            self._last_update_check = now
            # 只查装过的 App（用户源里可能有几百个，没装的不用管）
            installed = {r["app_id"] for r in load().values()}
            for a in apps.values():
                if not a.local_path and a.id in installed:
                    try:
                        sources.refresh_app(a)
                    except Exception:  # noqa: BLE001
                        pass
        jobs = []
        for key, rec in load().items():
            if not rec.get("auto", True):
                continue
            retry = RETRY_AFTER if rec.get("sign_mode", certs.APPLE_ID) == certs.APPLE_ID else DAY
            if rec.get("last_error") and now - rec.get("last_try", 0) < retry:
                continue
            a = apps.get(rec["app_id"])
            if not a:
                continue
            newer = (not a.local_path and a.latest_version and a.asset_url
                     and core.newer(a.latest_version, rec.get("version", "0")))
            if newer:
                jobs.append((key, rec, "update", a.latest_version))
            elif days_left(rec) < RENEW_BEFORE / DAY:
                jobs.append((key, rec, "renew", None))
        # 快过期的先来
        jobs.sort(key=lambda j: j[1].get("expires_at", 0))
        return jobs

    def _tick(self):
        jobs = self._due()
        if not jobs:
            return
        online = {d.udid: d for d in core.list_devices()}
        for key, rec, why, ver in jobs:
            if self._stop.is_set() or not self.enabled:
                return
            dev = online.get(rec["udid"])
            if not dev:
                continue   # 手机不在（不在家 / 没连同一 Wi‑Fi），下一轮再看
            # 界面正在手动装：让它先，别抢
            if not self.busy_lock.acquire(blocking=False):
                return
            try:
                self._run(key, rec, why, ver, dev)
            finally:
                self.busy_lock.release()

    def _run(self, key, rec, why, ver, dev):
        a = self.get_apps().get(rec["app_id"])
        if not a:
            return
        label = f"{a.name} → {dev.name}"
        self.current = key
        info = {"key": key, "name": a.name, "device": dev.name, "why": why, "version": ver or rec.get("version", "")}
        log.info("autopilot %s %s (%.1f days left, via %s)", why, label, days_left(rec), dev.model)
        self.on_event("renew_start", info)
        try:
            mode = rec.get("sign_mode", certs.APPLE_ID)
            cert = None
            if mode != certs.APPLE_ID:
                cert = certs.get(mode)
                if not cert:
                    raise core.SignError("当初签名用的证书已经删掉了，打开软件重新装一次")
                certs.check_device(cert, dev.udid, dev.name)
                certs.check_app(cert, a.bundle_id or (core.inspect_ipa(a.local_path)["bundle_id"] if a.local_path else ""), a.name)
                if why == "renew":
                    # 证书模式到期 = 证书 / 描述文件本身到期，重签也续不了命，只能换证书
                    raise core.SignError("证书快到期了，导入新证书后重新装一次")
            else:
                if not core.saved_accounts():
                    raise core.SignError("还没登录 Apple ID")
                if not core.session_ok():
                    raise core.SignError("Apple ID 登录已过期，打开软件重新登录一次")
            if a.local_path:
                ipa = Path(a.local_path)
                if not ipa.exists():
                    raise core.SignError("IPA 文件找不到了")
            else:
                ipa = core.download_ipa(a, lambda p: None, threading.Event())
            if cert:
                certs.sign_and_install(cert, ipa, dev.udid, lambda line: None)
            else:
                core.sign_and_install(ipa, dev.udid, lambda line: None)
            record(dev.udid, dev.name, a, mode)
            log.info("autopilot done %s", label)
            self.on_event("renew_done", info)
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, core.SignError) else core.friendly(str(e))
            log.warning("autopilot failed %s: %s", label, msg)
            _mark_error(key, msg)
            self.on_event("renew_fail", {**info, "error": msg})
        finally:
            self.current = None
            self.on_event("changed", {})


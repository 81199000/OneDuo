"""一键签 · 界面（pywebview：系统原生窗口里跑 HTML，Mac 用 WKWebView、Windows 用 Edge WebView2）

为什么不用 tkinter：Tk 在 Retina 屏把图片按 1x 画再放大，图标锯齿。网页渲染是原生分辨率。
界面在 web/index.html，Python 这边只暴露 Api 给 JS 调；后台线程用 window.evaluate_js 推状态。
"""
from __future__ import annotations

import base64
import faulthandler
import json
import logging
import sys
import threading
import os
import time
import webbrowser
from pathlib import Path

try:
    import webview          # 桌面版才有；NAS（server.py）不装 pywebview
except ImportError:         # pragma: no cover
    webview = None

from . import autopilot, certs, selfupdate, sources, core

# 日志：~/Library/Logs/OneTapInstall.log（Windows 在 %APPDATA%\\OneTapInstall），闪退也能事后查
LOG_PATH = (Path.home() / "Library" / "Logs" / "OneTapInstall.log") if sys.platform == "darwin" else core.data_dir() / "OneTapInstall.log"
logging.basicConfig(filename=LOG_PATH, encoding="utf-8", level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
log = logging.getLogger("onetap")
if os.environ.get("ONETAP_LOG_STDOUT"):     # NAS / Docker：日志同时进 docker logs
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(_h)
_fault_file = open(LOG_PATH, "a", encoding="utf-8")
faulthandler.enable(_fault_file)
sys.excepthook = lambda t, v, tb: log.critical("uncaught", exc_info=(t, v, tb))
threading.excepthook = lambda a: log.critical("uncaught in %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


class Api:
    def __init__(self):
        self._window = None   # 桌面版：webview.Window；NAS 版：server.Hub（同样有 evaluate_js 之外的 push 通道）
        self._catalog: core.Catalog | None = None
        self._apps: dict[str, core.AppEntry] = {}
        self._cancel = threading.Event()
        self._busy = False
        self._sign_lock = threading.Lock()   # 手动安装和自动续签共用，同一时刻只跑一个签名
        self._update = None                  # (版本, 安装包信息)：有新版时由列表刷新填上
        self._pilot = autopilot.Autopilot(lambda: dict(self._apps), self._sign_lock, self._pilot_event)
        self._twofa = None
        self._twofa_ready = threading.Event()
        self._login = None
        self._login_ready = threading.Event()
        self._last_push: dict[str, float] = {}

    # MARK: Python → JS

    def push(self, event: str, data=None):
        """后台线程 → 界面。日志类消息节流（签名时每秒上百行，全推会把网页桥压垮）；推送失败只记日志不崩"""
        if not self._window:
            return
        if hasattr(self._window, "emit"):          # NAS 版：直接推给所有打开的网页（SSE），不走 evaluate_js
            if event in ("log", "progress"):
                now = time.monotonic()
                if now - self._last_push.get(event, 0) < 0.15:
                    return
                self._last_push[event] = now
            self._window.emit(event, data)
            return
        if event in ("log", "progress"):
            now = time.monotonic()
            if now - self._last_push.get(event, 0) < 0.15:
                return
            self._last_push[event] = now
        try:
            self._window.evaluate_js(f"window.onEvent({json.dumps(event)}, {json.dumps(data, ensure_ascii=False)})")
        except Exception:  # noqa: BLE001
            log.exception("push %s failed", event)

    # MARK: JS → Python

    def auto_install(self):
        return __import__("os").environ.get("ONETAP_AUTOINSTALL", "")

    def version(self):
        return core.VERSION

    def load(self):
        """读官方源（catalog）+ 用户加的软件源，图标和最新版本逐个推给界面（后台线程）"""
        threading.Thread(target=self._load_all, args=(False,), daemon=True).start()

    def _app_json(self, a: core.AppEntry, icon: bool = True) -> dict:
        ic = a.icon_bytes if icon else None
        return {"id": a.id, "name": a.name, "subtitle": a.subtitle, "description": a.description,
                "min_ios": a.min_ios, "homepage": a.homepage, "source_id": a.source_id,
                "bundle_id": a.bundle_id, "version": a.latest_version, "size": a.asset_size,
                "has_asset": bool(a.asset_url) if a.latest_version or a.unsupported else None,
                "unsupported": a.unsupported, "error": "" if a.latest_version else a.resolve_error,
                "notes": a.latest_notes, "date": a.latest_date,
                "icon": "data:image/png;base64," + base64.b64encode(ic).decode() if ic else ""}

    def _fetch_icon(self, cat, a: core.AppEntry):
        if a.icon_bytes:
            return
        if a.source_id == sources.OFFICIAL_ID:
            a.icon_bytes = core.load_icon(cat, a)
        elif a.icon.startswith(("http://", "https://")):
            try:
                a.icon_bytes = core.cgbi_to_png(core.http_get(a.icon, timeout=10))
            except Exception:  # noqa: BLE001
                pass

    def _push_sources(self, groups: dict, errors: dict):
        out = [{"id": sources.OFFICIAL_ID, "name": "一键签官方源", "official": True,
                "count": len(groups.get(sources.OFFICIAL_ID, [])), "error": errors.get(sources.OFFICIAL_ID, "")}]
        for src in sources.load():
            out.append({"id": src["id"], "name": src.get("name") or src["url"], "url": src["url"], "official": False,
                        "count": len(groups.get(src["id"], [])), "error": errors.get(src["id"], "")})
        self.push("sources", out)

    def _load_all(self, manual: bool):
        before = {k: v.latest_version for k, v in self._apps.items() if not v.local_path}
        groups: dict[str, list] = {}
        errors: dict[str, str] = {}
        # 官方源（catalog/apps.json）
        try:
            cat = core.load_catalog()
            self._catalog = cat
            groups[sources.OFFICIAL_ID] = cat.apps
            for a in cat.apps:
                try:
                    core.resolve_latest(a)
                except Exception as e:  # noqa: BLE001
                    a.resolve_error = str(e)[:160]
                    log.warning("resolve %s failed: %s", a.id, e)
        except Exception as e:  # noqa: BLE001
            cat = self._catalog
            errors[sources.OFFICIAL_ID] = str(e)[:160]
            if not cat:
                self.push("catalog_error", str(e))
        # 用户源：几个一起拉
        from concurrent.futures import ThreadPoolExecutor
        user = sources.load()
        if user:
            with ThreadPoolExecutor(min(6, len(user))) as ex:
                for src, (apps, err) in zip(user, ex.map(sources.apps_from_source, user)):
                    groups[src["id"]] = apps
                    if err:
                        errors[src["id"]] = err
        # 换掉列表（保留已拿到的图标），自己加的 IPA 不动
        new = {}
        for apps in groups.values():
            for a in apps:
                old = self._apps.get(a.id)
                if old and old.icon_bytes:
                    a.icon_bytes = old.icon_bytes
                new[a.id] = a
        for k, v in self._apps.items():
            if v.local_path:
                new[k] = v
            elif k not in new and any(r["app_id"] == k for r in autopilot.load().values()):
                new[k] = v     # 源暂时拉不到：装过的 App 先留着，免得「我的手机」里对不上
        self._apps = new
        self._push_sources(groups, errors)
        self.push("catalog", {"apps": [self._app_json(a, icon=False) for g in groups.values() for a in g],
                              "source": cat.source if cat else ""})
        self.push_custom()
        self.push_installs()
        # 图标慢慢补（用户源可能上百个 App，别卡列表）
        from concurrent.futures import ThreadPoolExecutor as TP
        with TP(8) as ex:
            allapps = [a for g in groups.values() for a in g]
            for a, _ in zip(allapps, ex.map(lambda x: self._fetch_icon(cat, x), allapps)):
                if a.icon_bytes:
                    self.push("app_version", self._app_json(a))
        if manual:
            changed = [f"{a.name} v{a.latest_version}" for k, a in self._apps.items()
                       if before.get(k) and a.latest_version and a.latest_version != before[k]]
            self._pilot._last_update_check = time.time()
            self._pilot.poke()
            self.push("refreshed", "发现新版本：" + "、".join(changed) if changed else "已是最新")
        if cat and core.newer(cat.installer_latest, core.VERSION) and not getattr(self, "_upd_pushed", False):
            self._upd_pushed = True
            url = cat.installer_download.get("gitee", "")
            self._update = (cat.installer_latest, selfupdate.asset_for(cat))
            auto = selfupdate.can_self_update() and bool(self._update[1])
            self.push("self_update", {"version": cat.installer_latest, "current": core.VERSION,
                                      "notes": cat.installer_notes, "url": url, "auto": auto})
            if auto and (core.data_dir() / "autoupdate-test").exists():
                log.info("autoupdate-test flag: updating to %s", cat.installer_latest)
                (core.data_dir() / "autoupdate-test").unlink(missing_ok=True)
                self.self_update()

    # MARK: 软件源

    def add_source(self, url):
        def work():
            try:
                rec = sources.add(url)
                log.info("source added %s (%s apps)", rec["url"], rec["count"])
                self.push("source_added", {"id": rec["id"], "name": rec["name"], "count": rec["count"]})
                self._load_all(False)
            except Exception as e:  # noqa: BLE001
                self.push("source_error", str(e) if isinstance(e, core.SignError) else core.friendly(str(e)))
        threading.Thread(target=work, daemon=True).start()

    def remove_source(self, sid):
        if sid == sources.OFFICIAL_ID:
            return
        sources.remove(sid)
        threading.Thread(target=self._load_all, args=(False,), daemon=True).start()

    # MARK: 签名方式（Apple ID / 证书）

    def sign_modes(self):
        return {"mode": certs.mode(), "certs": [certs.public(c) for c in certs.load()]}

    def set_sign_mode(self, m):
        certs.set_mode(m)
        return self.sign_modes()

    def pick_file(self, kind):
        types = {"p12": ("证书 (*.p12;*.pfx)", "所有文件 (*.*)"),
                 "prov": ("描述文件 (*.mobileprovision)", "所有文件 (*.*)")}[kind]
        paths = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=types) if self._window else None
        return paths[0] if paths else ""

    def add_cert(self, p12, password, prov):
        try:
            c = certs.add(p12, password or "", prov)
            certs.set_mode(c["id"])
            log.info("cert added %s team=%s devices=%d", c["name"], c["team"], len(c["devices"]))
            return {"ok": True, **self.sign_modes(), "added": certs.public(c)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) if isinstance(e, core.SignError) else core.friendly(str(e))}

    def cert_covers(self, cid, udid):
        c = certs.get(cid)
        return bool(c and certs.covers(c, udid))

    def remove_cert(self, cid):
        certs.remove(cid)
        return self.sign_modes()

    @staticmethod
    def _custom_json(a: core.AppEntry) -> dict:
        ic = a.icon_bytes
        return {"id": a.id, "name": a.name, "subtitle": a.subtitle, "description": a.description,
                "min_ios": a.min_ios, "homepage": "", "custom": True, "version": a.latest_version,
                "size": a.asset_size, "has_asset": True, "bundle_id": a.bundle_id,
                "icon": "data:image/png;base64," + base64.b64encode(ic).decode() if ic else ""}

    def push_custom(self, select: str = ""):
        customs = core.load_custom_apps()
        for a in customs:
            self._apps[a.id] = a
        self.push("custom", {"apps": [self._custom_json(a) for a in customs], "select": select})

    def pick_ipa(self):
        """系统「打开文件」对话框选 IPA（可多选）"""
        paths = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True,
                                               file_types=("iOS 安装包 (*.ipa)", "所有文件 (*.*)")) if self._window else None
        if paths:
            self.add_ipas(list(paths))

    def add_ipas(self, paths):
        def work():
            last, errs = "", []
            for p in paths:
                try:
                    a = core.add_custom_ipa(p)
                    last = a.id
                    log.info("custom ipa added %s %s %s", a.name, a.bundle_id, a.latest_version)
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{Path(p).name}：{e}")
            self.push_custom(select=last)
            if errs:
                self.push("toast", "没加进来：\n" + "\n".join(errs))
        threading.Thread(target=work, daemon=True).start()

    def on_drop(self, e):
        """拖进窗口的文件（pywebview 的 DOM 事件里才有完整路径）"""
        if self._busy:
            return
        files = (e.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        ipas = [p for p in paths if p.lower().endswith(".ipa")]
        log.info("drop %d files, %d ipa", len(paths), len(ipas))
        if not paths:
            self.push("toast", "没拿到文件路径，请用「＋ 添加自己的 IPA」按钮选择")
        elif not ipas:
            self.push("toast", "只能添加 .ipa 安装包")
        else:
            self.add_ipas(ipas)

    def remove_custom(self, cid):
        if self._busy:
            return
        core.remove_custom_ipa(cid)
        self._apps.pop(cid, None)
        self.push_custom()

    def refresh(self):
        """「刷新」：重新拉所有软件源和最新版本，顺便让自动更新马上检查一轮"""
        threading.Thread(target=self._load_all, args=(True,), daemon=True).start()

    def devices(self):
        devs = core.list_devices()
        key = ",".join(d.udid for d in devs)
        if key != getattr(self, "_last_devs", None):
            self._last_devs = key
            self._pilot.poke()
            log.info("devices: %s", [(d.name, d.model) for d in devs])
        return [{"udid": d.udid, "name": d.name, "via": d.model} for d in devs]

    def accounts(self):
        return core.saved_accounts()

    def logout(self):
        core.logout()
        return core.saved_accounts()

    def open_url(self, url):
        webbrowser.open(url)

    def submit_login(self, user, password):
        self._login = (user, password) if user and password else None
        self._login_ready.set()

    def submit_2fa(self, code):
        self._twofa = code or None
        self._twofa_ready.set()

    def self_update(self):
        """点「立即更新」：下载 → 校验 → 退出并替换。签名进行中就等它做完再更新。"""
        if not self._update or not self._update[1]:
            return
        ver, asset = self._update

        def work():
            try:
                if not self._sign_lock.acquire(timeout=0.2):
                    self.push("self_update_progress", {"stage": "wait"})
                    self._sign_lock.acquire()      # 等正在进行的签名 / 续签结束
                try:
                    pkg = selfupdate.download(ver, asset, lambda p: self.push("self_update_progress", {"stage": "download", "p": p}))
                    self.push("self_update_progress", {"stage": "install"})
                    log.info("self update %s -> %s via %s", core.VERSION, ver, pkg.name)
                    selfupdate.launch_installer(pkg)
                except Exception:
                    self._sign_lock.release()
                    raise
                # 不释放锁：马上退出，免得退出前又开始续签
                time.sleep(0.8)
                os._exit(0)
            except Exception as e:  # noqa: BLE001
                log.exception("self update failed")
                self.push("self_update_progress", {"stage": "error", "msg": str(e)[:200]})
        threading.Thread(target=work, daemon=True).start()

    def install(self, app_id, udid):
        if self._busy:
            return
        a = self._apps.get(app_id)
        if not a:
            return
        if not self._sign_lock.acquire(blocking=False):
            self.push("toast", "正在后台自动续签，稍等一下再装")
            return
        self._busy = True
        self._cancel.clear()
        threading.Thread(target=self._install_flow, args=(a, udid), daemon=True).start()

    # MARK: 安装流程

    def _ask_login(self):
        self._login_ready.clear()
        self.push("need_login")
        self._login_ready.wait()
        return self._login

    def _ask_2fa(self, kind, phones=None):
        """返回：6 位验证码 / "resend" / "sms:<id>"；None = 取消"""
        self._twofa_ready.clear()
        self.push("need_2fa", {"kind": kind, "phones": phones or []})
        self._twofa_ready.wait()
        return self._twofa

    def _install_flow(self, a: core.AppEntry, udid: str):
        def on_line(line):
            if "PLUME_" in line:
                return
            short = line.split("] ")[-1].strip()
            m = None
            if "Installation progress:" in short:
                m = short.rsplit(":", 1)[-1].strip().rstrip("%")
            if m and m.isdigit():
                self.push("progress", {"step": "install", "value": int(m) / 100})
            elif short:
                self.push("log", short[:140])

        step = "account"
        mode = certs.mode()
        cert = certs.get(mode) if mode != certs.APPLE_ID else None
        try:
            self.push("step", {"step": "device", "state": "ok"})
            self.push("step", {"step": "account", "state": "run"})
            if a.unsupported:
                raise core.SignError(a.unsupported)
            if cert:
                dev = next((d for d in core.list_devices() if d.udid == udid), None)
                certs.check_device(cert, udid, dev.name if dev else "这台手机")
                bid = a.bundle_id or (core.inspect_ipa(a.local_path)["bundle_id"] if a.local_path else "")
                certs.check_app(cert, bid, a.name)
                self.push("step", {"step": "account", "state": "ok", "note": "证书：" + (cert.get("team_name") or cert.get("name", ""))})
                accts = None
            else:
                accts = core.saved_accounts()
            if not cert:
                if accts:
                    self.push("log", f"使用已保存的 Apple ID：{accts[0]}，检查登录是否有效…")
                    if not core.session_ok():
                        self.push("log", "登录已过期，需要重新输入密码")
                        accts = []
                if not accts:
                    creds = self._ask_login()
                    if not creds:
                        raise core.SignError("已取消登录")
                    self.push("log", "正在登录 Apple ID…（只发给苹果，本软件不保存密码）")
                    core.login(creds[0], creds[1], on_line, self._ask_2fa)
                    accts = core.saved_accounts()
                self.push("step", {"step": "account", "state": "ok", "note": accts[0] if accts else ""})
            step = "download"
            self.push("step", {"step": "download", "state": "run"})
            if a.local_path:
                ipa = Path(a.local_path)
                if not ipa.exists():
                    raise core.SignError("这个 IPA 文件找不到了，请重新添加")
                self.push("step", {"step": "download", "state": "ok", "note": "本地文件"})
            else:
                ipa = core.download_ipa(a, lambda p: self.push("progress", {"step": "download", "value": p}), self._cancel)
                self.push("step", {"step": "download", "state": "ok", "note": f"v{a.latest_version}"})
            step = "install"
            self.push("step", {"step": "install", "state": "run"})
            self.push("log", "正在签名并安装到手机，请保持手机解锁、数据线不要拔…")
            if cert:
                certs.sign_and_install(cert, ipa, udid, on_line)
            else:
                core.sign_and_install(ipa, udid, on_line)
            self.push("step", {"step": "install", "state": "ok"})
            log.info("installed %s %s to %s", a.id, a.latest_version, udid)
            dev = next((d for d in core.list_devices() if d.udid == udid), None)
            dev_name = dev.name if dev else "iPhone"
            # 数据线连着时顺手打开手机的「通过 Wi‑Fi 连接」，以后拔了线也能在同一 Wi‑Fi 下自动续签
            if dev and dev.model == "USB" and core.wifi_sync(udid) is False:
                ok = core.wifi_sync(udid, True)
                log.info("wifi sync enabled for %s: %s", dev_name, ok)
            autopilot.record(udid, dev_name, a, mode)
            self.push_installs()
            self.push("done")
        except Exception as e:  # noqa: BLE001
            log.exception("install failed")
            msg = str(e) if isinstance(e, core.SignError) else core.friendly(str(e))
            self.push("step", {"step": step, "state": "err"})
            self.push("error", msg)
        finally:
            self._busy = False
            self._sign_lock.release()

    # MARK: 已安装 / 自动续签

    def _installs_json(self):
        out = []
        for key, r in sorted(autopilot.load().items(), key=lambda kv: kv[1].get("expires_at", 0)):
            a = self._apps.get(r["app_id"])
            out.append({"key": key, "name": r["name"], "device": r["device"], "version": r.get("version", ""),
                        "days_left": round(autopilot.days_left(r), 2), "expires_at": r.get("expires_at", 0),
                        "auto": r.get("auto", True), "error": r.get("last_error", ""),
                        "latest": (a.latest_version if a and not a.local_path else "") or "",
                        "running": self._pilot.current == key, "app_id": r["app_id"],
                        "sign_mode": "cert" if r.get("sign_mode", certs.APPLE_ID) != certs.APPLE_ID else "apple_id"})
        return out

    def push_installs(self):
        self.push("installs", {"items": self._installs_json(), "enabled": self._pilot.enabled})

    def installs(self):
        return {"items": self._installs_json(), "enabled": self._pilot.enabled}

    def set_auto(self, key, on):
        autopilot.set_auto(key, on)
        self.push_installs()

    def forget_install(self, key):
        autopilot.forget(key)
        self.push_installs()

    def set_autopilot(self, on):
        self._pilot.enabled = bool(on)
        core.set_pref("autopilot", bool(on))
        if on:
            self._pilot.poke()
        self.push_installs()

    def autostart(self):
        return core.autostart_enabled()

    def set_autostart(self, on):
        core.set_autostart(bool(on))
        return core.autostart_enabled()

    def move_to_nas(self, addr, password=""):
        """「搬到 NAS」：手机配对记录 + Apple ID 登录状态 + 安装记录 打包传给 NAS 版"""
        from . import nasmove
        try:
            r = nasmove.move(addr, password or "")
            core.set_pref("nas_addr", addr)
            return r
        except Exception as e:  # noqa: BLE001
            log.exception("move to nas failed")
            return {"ok": False, "error": str(e) if isinstance(e, core.SignError) else core.friendly(str(e))}

    def install_to_nas(self, addr, user, password, otp=""):
        """「装到 NAS」：自动登录群晖 → 装一键签 NAS 版 → 搬手机。进度用 nas_step 推给页面"""
        from . import nasinstall, nasmove

        def work():
            try:
                r = nasinstall.install(addr, user, password, otp or "",
                                       step=lambda k, t: self.push("nas_step", {"key": k, "text": t}),
                                       progress=lambda p: self.push("progress", {"step": "nas", "value": p}))
                self.push("nas_step", {"key": "move", "text": "把手机和 Apple ID 搬过去…"})
                m = nasmove.move(r["url"])
                core.set_pref("nas_addr", r["url"])
                self.push("nas_done", {**r, **m})
            except nasinstall.NeedOTP as e:
                self.push("nas_otp", str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("install to nas failed")
                self.push("nas_error", str(e) if isinstance(e, core.SignError) else core.friendly(str(e)))
        threading.Thread(target=work, daemon=True).start()

    # MARK: 装到电视（GuoDuo TV）

    def find_tv(self):
        from . import tvinstall
        return tvinstall.scan()

    def tv_addr(self):
        return core.get_pref("tv_addr", "") or ""

    def install_to_tv(self, addr):
        """「装到电视」：电脑经 ADB 直连局域网里的安卓电视，下载校验 GuoDuo TV 后装上并打开。进度用 tv_step 推给页面"""
        from . import tvinstall

        def work():
            host, _, port = str(addr).strip().partition(":")
            try:
                r = tvinstall.install(host, int(port or 5555),
                                      step=lambda k, t: self.push("tv_step", {"key": k, "text": t}),
                                      progress=lambda k, p: self.push("progress", {"step": "tv_" + k, "value": p}))
                self.push("tv_done", r)
            except Exception as e:  # noqa: BLE001
                log.exception("install to tv failed")
                self.push("tv_error", str(e) if isinstance(e, (tvinstall.TvError, core.SignError)) else core.friendly(str(e)))
        threading.Thread(target=work, daemon=True).start()

    def find_nas(self):
        from . import nasfind
        return nasfind.scan()

    def nas_addr(self):
        return core.get_pref("nas_addr", "") or ""

    def renew_now(self):
        """「立即全部续签」：把所有记录标记为马上到期，交给后台一轮跑完"""
        with autopilot._lock:
            d = autopilot.load()
            for r in d.values():
                if r.get("auto", True):
                    r["expires_at"] = min(r.get("expires_at", 0), __import__("time").time())
                    r["last_error"] = ""
            autopilot._save(d)
        self._pilot.enabled = True
        self._pilot.poke()
        self.push_installs()

    def _pilot_event(self, ev, info):
        if ev == "renew_start":
            self.push("pilot", {"state": "run", "text": f"{'更新' if info['why'] == 'update' else '续签'} {info['name']} → {info['device']}…"})
        elif ev == "renew_done":
            self.push("pilot", {"state": "ok", "text": f"已{'更新' if info['why'] == 'update' else '续签'} {info['name']}（{info['device']}），又能用 7 天"})
            core.notify("一键签", f"{info['name']} 已自动{'更新到 v' + info['version'] if info['why'] == 'update' else '续签'}（{info['device']}）")
        elif ev == "renew_fail":
            self.push("pilot", {"state": "err", "text": f"{info['name']} 自动续签没成功：{info['error']}"})
        self.push_installs()


def main():
    log.info("start %s %s plumesign=%s", core.VERSION, core.system_label(), core.plumesign_path())
    api = Api()
    # 打包后网页在资源目录（PyInstaller _MEIPASS），开发时在本文件旁边
    html = core.resource_dir() / "app" / "web" / "index.html"
    if not html.exists():
        html = Path(__file__).resolve().parent / "web" / "index.html"
    background = "--background" in sys.argv   # 开机自启：最小化挂着，只做自动续签
    win = webview.create_window(f"一键签 {core.VERSION}", url=str(html), js_api=api,
                                width=900, height=640, min_size=(780, 560), background_color="#111214",
                                minimized=background)
    api._window = win
    core._migrate_old_name()
    selfupdate.cleanup_old()
    api._pilot.enabled = core.get_pref("autopilot", True)
    api._pilot.start()

    def bind_drop(window):
        from webview.dom import DOMEventHandler
        window.dom.document.events.drop += DOMEventHandler(api.on_drop, prevent_default=True, stop_propagation=False)

    webview.start(bind_drop, win, debug=bool(__import__("os").environ.get("ONETAP_DEBUG")))
    log.info("exit normally")

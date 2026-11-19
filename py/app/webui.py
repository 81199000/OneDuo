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
import time
import webbrowser
from pathlib import Path

import webview

from . import autopilot, core

# 日志：~/Library/Logs/OneTapInstall.log（Windows 在 %APPDATA%\\OneTapInstall），闪退也能事后查
LOG_PATH = (Path.home() / "Library" / "Logs" / "OneTapInstall.log") if sys.platform == "darwin" else core.data_dir() / "OneTapInstall.log"
logging.basicConfig(filename=LOG_PATH, encoding="utf-8", level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
log = logging.getLogger("onetap")
_fault_file = open(LOG_PATH, "a", encoding="utf-8")
faulthandler.enable(_fault_file)
sys.excepthook = lambda t, v, tb: log.critical("uncaught", exc_info=(t, v, tb))
threading.excepthook = lambda a: log.critical("uncaught in %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


class Api:
    def __init__(self):
        self._window: webview.Window | None = None
        self._catalog: core.Catalog | None = None
        self._apps: dict[str, core.AppEntry] = {}
        self._cancel = threading.Event()
        self._busy = False
        self._sign_lock = threading.Lock()   # 手动安装和自动续签共用，同一时刻只跑一个签名
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
        """读列表 + 图标 + 最新版本（后台线程，逐个推给界面）"""
        def work():
            try:
                cat = core.load_catalog()
            except Exception as e:  # noqa: BLE001
                self.push("catalog_error", str(e))
                return
            self._catalog = cat
            self._apps = {a.id: a for a in cat.apps}
            self.push_custom()
            out = []
            for a in cat.apps:
                ic = core.load_icon(cat, a)
                out.append({"id": a.id, "name": a.name, "subtitle": a.subtitle, "description": a.description,
                            "min_ios": a.min_ios, "homepage": a.homepage,
                            "icon": "data:image/png;base64," + base64.b64encode(ic).decode() if ic else ""})
            self.push("catalog", {"apps": out, "source": cat.source})
            self.push_installs()
            for a in cat.apps:
                try:
                    core.resolve_latest(a)
                except Exception:  # noqa: BLE001
                    pass
                self.push("app_version", {"id": a.id, "version": a.latest_version,
                                          "size": a.asset_size, "has_asset": bool(a.asset_url)})
            if core.newer(cat.installer_latest, core.VERSION):
                url = cat.installer_download.get("gitee" if core.is_cn() else "github") \
                    or cat.installer_download.get("github", "")
                self.push("self_update", {"version": cat.installer_latest, "notes": cat.installer_notes, "url": url})
        threading.Thread(target=work, daemon=True).start()

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
        """「刷新」：重新拉应用列表和每个 App 的最新版本，顺便让自动更新马上检查一轮"""
        def work():
            try:
                cat = core.load_catalog()
                for a in cat.apps:
                    old = self._apps.get(a.id)
                    if old and not old.local_path:
                        a.icon_bytes = old.icon_bytes
                self._catalog = cat
                for a in cat.apps:
                    self._apps[a.id] = a
                out = []
                for a in cat.apps:
                    ic = core.load_icon(cat, a)
                    out.append({"id": a.id, "name": a.name, "subtitle": a.subtitle, "description": a.description,
                                "min_ios": a.min_ios, "homepage": a.homepage,
                                "icon": "data:image/png;base64," + base64.b64encode(ic).decode() if ic else ""})
                self.push("catalog", {"apps": out, "source": cat.source})
            except Exception as e:  # noqa: BLE001
                log.warning("refresh catalog failed: %s", e)
            changed = []
            for a in list(self._apps.values()):
                if a.local_path:
                    continue
                before = a.latest_version
                try:
                    core.resolve_latest(a)
                except Exception as e:  # noqa: BLE001
                    log.warning("resolve %s failed: %s", a.id, e)
                self.push("app_version", {"id": a.id, "version": a.latest_version,
                                          "size": a.asset_size, "has_asset": bool(a.asset_url)})
                if before and a.latest_version and a.latest_version != before:
                    changed.append(f"{a.name} v{a.latest_version}")
            self.push_custom()
            self._pilot._last_update_check = __import__("time").time()
            self._pilot.poke()
            self.push_installs()
            self.push("refreshed", "发现新版本：" + "、".join(changed) if changed else "已是最新")
        threading.Thread(target=work, daemon=True).start()

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

    def _ask_2fa(self, kind):
        self._twofa_ready.clear()
        self.push("need_2fa", kind)
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
        try:
            self.push("step", {"step": "device", "state": "ok"})
            self.push("step", {"step": "account", "state": "run"})
            accts = core.saved_accounts()
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
            core.sign_and_install(ipa, udid, on_line)
            self.push("step", {"step": "install", "state": "ok"})
            log.info("installed %s %s to %s", a.id, a.latest_version, udid)
            dev = next((d for d in core.list_devices() if d.udid == udid), None)
            dev_name = dev.name if dev else "iPhone"
            # 数据线连着时顺手打开手机的「通过 Wi‑Fi 连接」，以后拔了线也能在同一 Wi‑Fi 下自动续签
            if dev and dev.model == "USB" and core.wifi_sync(udid) is False:
                ok = core.wifi_sync(udid, True)
                log.info("wifi sync enabled for %s: %s", dev_name, ok)
            autopilot.record(udid, dev_name, a)
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
                        "running": self._pilot.current == key, "app_id": r["app_id"]})
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
    api._pilot.enabled = core.get_pref("autopilot", True)
    api._pilot.start()

    def bind_drop(window):
        from webview.dom import DOMEventHandler
        window.dom.document.events.drop += DOMEventHandler(api.on_drop, prevent_default=True, stop_propagation=False)

    webview.start(bind_drop, win, debug=bool(__import__("os").environ.get("ONETAP_DEBUG")))
    log.info("exit normally")

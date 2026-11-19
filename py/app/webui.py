"""一键装 · 界面（pywebview：系统原生窗口里跑 HTML，Mac 用 WKWebView、Windows 用 Edge WebView2）

为什么不用 tkinter：Tk 在 Retina 屏把图片按 1x 画再放大，图标锯齿。网页渲染是原生分辨率。
界面在 web/index.html，Python 这边只暴露 Api 给 JS 调；后台线程用 window.evaluate_js 推状态。
"""
from __future__ import annotations

import base64
import json
import threading
import webbrowser
from pathlib import Path

import webview

from . import core


class Api:
    def __init__(self):
        self.window: webview.Window | None = None
        self.catalog: core.Catalog | None = None
        self.apps: dict[str, core.AppEntry] = {}
        self.cancel = threading.Event()
        self.busy = False
        self._twofa = None
        self._twofa_ready = threading.Event()
        self._login = None
        self._login_ready = threading.Event()

    # MARK: Python → JS

    def push(self, event: str, data=None):
        if self.window:
            self.window.evaluate_js(f"window.onEvent({json.dumps(event)}, {json.dumps(data, ensure_ascii=False)})")

    # MARK: JS → Python

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
            self.catalog = cat
            self.apps = {a.id: a for a in cat.apps}
            out = []
            for a in cat.apps:
                ic = core.load_icon(cat, a)
                out.append({"id": a.id, "name": a.name, "subtitle": a.subtitle, "description": a.description,
                            "min_ios": a.min_ios, "homepage": a.homepage,
                            "icon": "data:image/png;base64," + base64.b64encode(ic).decode() if ic else ""})
            self.push("catalog", {"apps": out, "source": cat.source})
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

    def devices(self):
        return [{"udid": d.udid, "name": d.name, "via": d.model} for d in core.list_devices()]

    def accounts(self):
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
        if self.busy:
            return
        a = self.apps.get(app_id)
        if not a:
            return
        self.busy = True
        self.cancel.clear()
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
        def log(line):
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
            if not accts:
                creds = self._ask_login()
                if not creds:
                    raise core.SignError("已取消登录")
                self.push("log", "正在登录 Apple ID…（只发给苹果，本软件不保存密码）")
                core.login(creds[0], creds[1], log, self._ask_2fa)
                accts = core.saved_accounts()
            self.push("step", {"step": "account", "state": "ok", "note": accts[0] if accts else ""})
            step = "download"
            self.push("step", {"step": "download", "state": "run"})
            ipa = core.download_ipa(a, lambda p: self.push("progress", {"step": "download", "value": p}), self.cancel)
            self.push("step", {"step": "download", "state": "ok", "note": f"v{a.latest_version}"})
            step = "install"
            self.push("step", {"step": "install", "state": "run"})
            self.push("log", "正在签名并安装到手机，请保持手机解锁、数据线不要拔…")
            core.sign_and_install(ipa, udid, log)
            self.push("step", {"step": "install", "state": "ok"})
            self.push("done")
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, core.SignError) else core.friendly(str(e))
            self.push("step", {"step": step, "state": "err"})
            self.push("error", msg)
        finally:
            self.busy = False


def main():
    api = Api()
    html = Path(__file__).resolve().parent / "web" / "index.html"
    win = webview.create_window(f"一键装 {core.VERSION}", url=str(html), js_api=api,
                                width=900, height=640, min_size=(780, 560), background_color="#111214")
    api.window = win
    webview.start(debug=bool(__import__("os").environ.get("ONETAP_DEBUG")))

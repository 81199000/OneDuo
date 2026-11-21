"""一键签 · NAS / 服务器版（没有窗口：浏览器打开 http://NAS地址:8686 用）

和桌面版共用同一套 webui.Api 和 web/index.html：
- 页面里 window.pywebview.api 换成 fetch('/api/<方法>')，方法名、参数、返回值和桌面版一模一样
- 后台推给页面的消息（安装进度、续签状态…）走 SSE（/events），所有打开的页面都能收到
- 选文件（IPA / 证书）换成浏览器上传，传到数据目录 uploads/ 后把服务器上的路径交给原来的方法
- /api/import：桌面版「搬到 NAS」把配对记录 + Apple ID 登录状态 + 安装记录一次打包传过来

环境变量：
  ONETAP_PORT       端口，默认 8686
  ONETAP_PASSWORD   设了就要先输密码才能用（NAS 端口映射到外网时一定要设）
  ONETAP_LOCKDOWN   配对记录目录（netmuxd 的 --plist-storage），默认 <数据目录>/lockdown
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import threading
import time
import zipfile
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import core, selfupdate, webui

log = logging.getLogger("onetap.server")

PORT = int(os.environ.get("ONETAP_PORT") or 8686)
PASSWORD = os.environ.get("ONETAP_PASSWORD") or ""
MAX_UPLOAD = 4 << 30        # IPA 最大 4 GB

# 页面能调的方法（桌面版 JS 用到的全部 + NAS 版新增的）；别的一律 404，免得暴露内部方法
ALLOWED = {
    "load", "refresh", "version", "auto_install", "devices", "accounts", "logout", "installs",
    "sign_modes", "set_sign_mode", "add_cert", "cert_covers", "remove_cert",
    "add_source", "remove_source", "add_ipas", "remove_custom", "install",
    "submit_login", "submit_2fa", "set_auto", "forget_install", "set_autopilot", "renew_now",
    "autostart", "set_autostart", "self_update", "server_info",
}


def lockdown_dir() -> Path:
    d = Path(os.environ.get("ONETAP_LOCKDOWN") or core.data_dir() / "lockdown")
    d.mkdir(parents=True, exist_ok=True)
    return d


def plume_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "PlumeImpactor"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Hub:
    """代替 pywebview 窗口：Api.push() 发现 _window 有 emit 就调这里，广播给所有 SSE 连接"""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._last: dict[str, object] = {}     # 新打开的页面补发最近一次的状态（需要登录 / 验证码弹窗）

    def emit(self, event: str, data=None):
        msg = json.dumps([event, data], ensure_ascii=False)
        if event in ("need_login", "need_2fa"):
            self._last["ask"] = msg
        elif event in ("done", "error"):
            self._last.pop("ask", None)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        if "ask" in self._last:
            q.put_nowait(self._last["ask"])
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


class ServerApi(webui.Api):
    """NAS 版差异：没有开机自启（容器本身就常驻）、不自己替换自己（更新 = 重新拉镜像）"""

    def autostart(self):
        return True

    def set_autostart(self, on):
        return True

    def self_update(self):
        return None

    def server_info(self):
        return {"server": True, "version": core.VERSION, "password": bool(PASSWORD),
                "paired": sorted(p.stem for p in lockdown_dir().glob("*.plist") if p.stem != "SystemConfiguration")}

    def devices(self):
        devs = super().devices()
        # 刚导入配对记录时 netmuxd 要等手机下一次在 Wi‑Fi 里广播（一般几秒到一分钟）
        return devs


# MARK: 导入（桌面版「搬到 NAS」）

_SAFE = re.compile(r"^[A-Za-z0-9._@+\- ]+$")


def _safe_rel(name: str) -> bool:
    parts = Path(name).parts
    return bool(parts) and not name.startswith("/") and ".." not in parts and all(_SAFE.match(p) for p in parts)


def import_bundle(raw: bytes) -> dict:
    """zip 里的目录：
    lockdown/<UDID>.plist        手机配对记录（电脑上 usbmuxd 读出来的）
    plume/accounts.json, plume/state.plist, plume/keys/<team>/key.pem   Apple ID 登录状态（不含密码）
    app/installs.json sources.json certs.json prefs.json, app/certs/..., app/custom/...
    """
    z = zipfile.ZipFile(io.BytesIO(raw))
    got = {"devices": 0, "apple_id": False, "installs": 0, "sources": 0, "certs": 0}
    roots = {"lockdown/": lockdown_dir(), "plume/": plume_dir(), "app/": core.data_dir()}
    from . import autopilot, certs, sources
    for info in z.infolist():
        if info.is_dir():
            continue
        name = info.filename
        root = next((r for r in roots if name.startswith(r)), None)
        if not root:
            continue
        rel = name[len(root):]
        if not _safe_rel(rel):
            log.warning("import: skip unsafe path %r", name)
            continue
        data = z.read(info)
        dest = roots[root] / rel
        if root == "app/" and rel in ("installs.json", "sources.json", "certs.json"):
            # 合并，不覆盖 NAS 上已有的
            try:
                new = json.loads(data.decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if rel == "installs.json" and isinstance(new, dict):
                with autopilot._lock:
                    cur = autopilot.load()
                    for k, v in new.items():
                        if k not in cur or v.get("signed_at", 0) > cur[k].get("signed_at", 0):
                            cur[k] = v
                    autopilot._save(cur)
                got["installs"] = len(new)
            elif rel == "sources.json" and isinstance(new, list):
                cur = sources.load()
                ids = {s["id"] for s in cur}
                cur += [s for s in new if s.get("id") not in ids]
                sources._save(cur)
                got["sources"] = len(new)
            elif rel == "certs.json" and isinstance(new, list):
                cur = certs.load()
                ids = {c["id"] for c in cur}
                cur += [c for c in new if c.get("id") not in ids]
                certs._save(cur)
                got["certs"] = len(new)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if root == "lockdown/" and rel.endswith(".plist") and rel != "SystemConfiguration.plist":
            got["devices"] += 1
            os.chmod(dest, 0o600)
        if root == "plume/" and rel == "accounts.json":
            got["apple_id"] = True
            os.chmod(dest, 0o600)
    # 自己加的 IPA：apps.json 里记的是电脑上的绝对路径之外的文件名，文件已经跟着拷进 custom/
    log.info("import done: %s", got)
    if got["devices"]:
        restart_netmuxd()
    return got


def restart_netmuxd() -> None:
    """netmuxd 只在启动时（和发现陌生手机时）读配对记录目录；新导入的手机要重启它才认。
    entrypoint.sh 里有守护循环，杀掉 3 秒后自动起来"""
    import signal
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", "bin/netmuxd"], capture_output=True, text=True, timeout=5).stdout
    except FileNotFoundError:   # 精简镜像没有 pgrep：扫 /proc
        out = ""
        for d in Path("/proc").glob("[0-9]*"):
            try:
                if b"netmuxd" in (d / "cmdline").read_bytes():
                    out += d.name + "\n"
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        out = ""
    for pid in out.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
            log.info("restarted netmuxd (pid %s) to load new pairing records", pid)
        except (OSError, ValueError):
            pass


# MARK: HTTP

def _html() -> bytes:
    p = core.resource_dir() / "app" / "web" / "index.html"
    if not p.exists():
        p = Path(__file__).resolve().parent / "web" / "index.html"
    html = p.read_text("utf-8")
    shim = (Path(__file__).resolve().parent / "web" / "server-shim.js").read_text("utf-8")
    return html.replace("<script>", "<script>\n" + shim + "\n</script>\n<script>", 1).encode("utf-8")


_SESSIONS: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    server_version = "OneDuoNAS"
    api: ServerApi = None   # type: ignore[assignment]
    hub: Hub = None         # type: ignore[assignment]

    def log_message(self, format, *args):  # noqa: A002   # 别把每个请求刷进日志
        pass

    # ---- 工具 ----
    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        if "onetap" in c and c["onetap"].value in _SESSIONS:
            return True
        key = self.headers.get("X-OneTap-Password") or ""
        return bool(key) and hmac.compare_digest(key, PASSWORD)

    def _body(self, limit: int = 1 << 20) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("太大了")
        return self.rfile.read(n) if n else b""

    # ---- 路由 ----
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/ping":
            return self._json({"oneduo": True, "version": core.VERSION, "password": bool(PASSWORD)})
        if not self._authed():
            if u.path in ("/", "/index.html"):
                return self._send(200, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return self._json({"error": "需要密码"}, 401)
        if u.path in ("/", "/index.html"):
            return self._send(200, _html(), "text/html; charset=utf-8")
        if u.path == "/events":
            return self._sse()
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            form = parse_qs(self._body().decode("utf-8", "replace"))
            if PASSWORD and hmac.compare_digest((form.get("pw") or [""])[0], PASSWORD):
                sid = secrets.token_urlsafe(24)
                _SESSIONS.add(sid)
                return self._send(303, extra={"Location": "/", "Set-Cookie": f"onetap={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"})
            return self._send(303, extra={"Location": "/?wrong=1"})
        if not self._authed():
            return self._json({"error": "需要密码"}, 401)
        try:
            if u.path == "/upload":
                return self._upload(parse_qs(u.query))
            if u.path == "/api/import":
                got = import_bundle(self._body(limit=MAX_UPLOAD))
                threading.Thread(target=self.api._load_all, args=(False,), daemon=True).start()
                return self._json({"ok": True, **got})
            if u.path.startswith("/api/"):
                name = u.path[5:]
                if name not in ALLOWED:
                    return self._json({"error": "no such method"}, 404)
                raw = self._body()
                args = json.loads(raw.decode("utf-8")) if raw else []
                res = getattr(self.api, name)(*args)
                return self._json({"r": res})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s failed", u.path)
            return self._json({"error": str(e)[:300]}, 500)
        self._send(404, b"not found", "text/plain")

    def _upload(self, q):
        kind = (q.get("kind") or ["ipa"])[0]
        name = Path(unquote((q.get("name") or ["file"])[0])).name or "file"
        name = re.sub(r"[^\w.\-()\u4e00-\u9fff ]+", "_", name)[:120]
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_UPLOAD:
            return self._json({"error": "文件为空或太大"}, 400)
        d = core.data_dir() / "uploads" / kind
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{int(time.time())}-{secrets.token_hex(3)}-{name}"
        left = n
        with open(dest, "wb") as f:
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            dest.unlink(missing_ok=True)
            return self._json({"error": "上传中断了"}, 400)
        return self._json({"path": str(dest)})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.hub.subscribe()
        try:
            self.wfile.write(b": hi\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(b"data: " + msg.encode("utf-8") + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # 保活，顺便发现页面关了
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)


LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>一键签</title><style>body{background:#111214;color:#e8e8ea;font:15px -apple-system,system-ui,"PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#1b1c1f;padding:28px;border-radius:14px;width:280px}h1{font-size:20px;margin:0 0 16px}input,button{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333;background:#111214;color:inherit;font-size:15px;margin-top:8px}
button{background:#ff375f;border:0;color:#fff;margin-top:14px}.e{color:#ff6b81;font-size:13px;min-height:1em}</style></head>
<body><form method="post" action="/login"><h1>一键签</h1><div class="e" id="e"></div><input name="pw" type="password" placeholder="密码" autofocus><button>进入</button></form>
<script>if(location.search.includes("wrong"))document.getElementById("e").textContent="密码不对";</script></body></html>"""


def main():
    os.environ.setdefault("ONETAP_LOG_STDOUT", "1")
    log.info("server start %s %s plumesign=%s data=%s lockdown=%s", core.VERSION, core.system_label(),
             core.plumesign_path(), core.data_dir(), lockdown_dir())
    if not core.plumesign_path():
        log.error("找不到 plumesign")
    hub = Hub()
    api = ServerApi()
    api._window = hub  # type: ignore[assignment]
    selfupdate.cleanup_old()
    api._pilot.enabled = bool(core.get_pref("autopilot", True))
    api._pilot.start()
    api.load()
    Handler.api, Handler.hub = api, hub
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    log.info("listening on http://0.0.0.0:%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()

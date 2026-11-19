"""一键装 · 核心逻辑（与界面无关，可单独测试）

- 应用目录：从 GitHub / Gitee 拉 catalog/apps.json（谁先回来用谁），新 App 只要改这个文件
- 下载：每个 App 读自己仓库 Releases 的最新 IPA
- 签名安装：调用随包附带的 plumesign（Impactor 的命令行，MIT），
  Apple ID 登录 / 证书 / 描述文件 / 注册设备 / 签名 / 装机全在它里面
"""
from __future__ import annotations

import fnmatch
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

VERSION = "0.1.0"

# 目录文件的几个镜像：GitHub raw、jsDelivr（国内较快）、Gitee raw
CATALOG_REPO = "81199000/OneTapInstall"
CATALOG_URLS = [
    # API：私有仓库带 token 也能读（开发期）；公开后匿名也可
    f"https://api.github.com/repos/{CATALOG_REPO}/contents/catalog/apps.json",
    "https://raw.githubusercontent.com/81199000/OneTapInstall/main/catalog/apps.json",
    "https://cdn.jsdelivr.net/gh/81199000/OneTapInstall@main/catalog/apps.json",
    "https://gitee.com/remon8/onetapinstall/raw/main/catalog/apps.json",
]

UA = f"OneTapInstall/{VERSION}"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "OneTapInstall"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_dir() -> Path:
    """打包后（PyInstaller）资源在 sys._MEIPASS；开发时在仓库里"""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent


def plumesign_path() -> Optional[Path]:
    exe = "plumesign.exe" if sys.platform == "win32" else "plumesign"
    for p in [
        resource_dir() / "bin" / exe,
        Path(sys.executable).resolve().parent.parent / "Frameworks" / "bin" / exe,   # macOS .app
        resource_dir() / "target" / "release" / exe,
        resource_dir() / "target" / "debug" / exe,
    ]:
        if p.exists():
            return p
    found = shutil.which("plumesign")
    return Path(found) if found else None


def _token() -> Optional[str]:
    """开发期：私有仓库要用 token 下载（正式公开后不需要）"""
    return os.environ.get("ONETAP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")


def http_get(url: str, timeout: float = 15, accept: Optional[str] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if accept:
        req.add_header("Accept", accept)
    tok = _token()
    if tok and ("api.github.com" in url or "githubusercontent.com" in url):
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# MARK: 应用目录

@dataclass
class AppEntry:
    id: str
    name: str
    subtitle: str
    description: str
    bundle_id: str
    icon: str
    repo: str
    asset_pattern: str
    min_ios: str = ""
    homepage: str = ""
    # 运行时补
    latest_version: Optional[str] = None
    asset_url: Optional[str] = None
    asset_api_url: Optional[str] = None
    asset_size: int = 0
    installed_version: Optional[str] = None
    icon_bytes: Optional[bytes] = None
    # 用户自己加的本地 IPA（不走下载）
    local_path: Optional[str] = None


@dataclass
class Catalog:
    apps: list[AppEntry]
    installer_latest: str = VERSION
    installer_notes: str = ""
    installer_download: dict = field(default_factory=dict)
    base_url: str = ""
    source: str = ""


def _parse_catalog(raw: bytes, base_url: str, source: str) -> Catalog:
    j = json.loads(raw.decode("utf-8"))
    keys = AppEntry.__dataclass_fields__.keys()
    apps = [AppEntry(**{k: v for k, v in a.items() if k in keys}) for a in j.get("apps", [])]
    inst = j.get("installer", {})
    return Catalog(apps=apps, installer_latest=inst.get("latest", VERSION), installer_notes=inst.get("notes", ""),
                   installer_download=inst.get("download", {}), base_url=base_url, source=source)


def load_catalog() -> Catalog:
    """几个镜像同时拉，谁先成功用谁；都失败就用本地（开发 / 离线）"""
    local = os.environ.get("ONETAP_CATALOG")
    if local:
        p = Path(local)
        return _parse_catalog(p.read_bytes(), p.parent.as_uri() + "/", "本地")

    def fetch(url):
        if "api.github.com" in url:
            return url, http_get(url, timeout=8, accept="application/vnd.github.raw")
        return url, http_get(url, timeout=8)

    errors = []
    # 先试 GitHub API（实时，没 CDN 缓存）；失败再几个镜像一起拉，谁先回来用谁
    try:
        url, raw = fetch(CATALOG_URLS[0])
        cat = _parse_catalog(raw, f"https://raw.githubusercontent.com/{CATALOG_REPO}/main/catalog/", "GitHub")
        (data_dir() / "apps.json").write_bytes(raw)
        return cat
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    with ThreadPoolExecutor(len(CATALOG_URLS) - 1) as ex:
        futs = [ex.submit(fetch, u) for u in CATALOG_URLS[1:]]
        for f in as_completed(futs):
            try:
                url, raw = f.result()
                base = (f"https://raw.githubusercontent.com/{CATALOG_REPO}/main/catalog/"
                        if "api.github.com" in url else url.rsplit("/", 1)[0] + "/")
                cat = _parse_catalog(raw, base, url.split("/")[2])
                (data_dir() / "apps.json").write_bytes(raw)
                return cat
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
    cached = data_dir() / "apps.json"
    if cached.exists():
        return _parse_catalog(cached.read_bytes(), "", "上次缓存")
    bundled = resource_dir() / "catalog" / "apps.json"
    if bundled.exists():
        return _parse_catalog(bundled.read_bytes(), bundled.parent.as_uri() + "/", "内置")
    raise RuntimeError("应用列表加载失败：" + "；".join(errors[:2]))


def load_icon(cat: Catalog, app: AppEntry) -> Optional[bytes]:
    url = app.icon if app.icon.startswith(("http://", "https://", "file:")) else cat.base_url + app.icon
    try:
        if url.startswith("file:"):
            return Path(urllib.request.url2pathname(url[5:])).read_bytes()
        return http_get(url, timeout=10)
    except Exception:  # noqa: BLE001
        local = resource_dir() / "catalog" / app.icon
        return local.read_bytes() if local.exists() else None


def resolve_latest(app: AppEntry) -> None:
    """读这个 App 仓库 Releases 的最新版本和 IPA 下载地址"""
    j = json.loads(http_get(f"https://api.github.com/repos/{app.repo}/releases/latest", timeout=10,
                            accept="application/vnd.github+json"))
    app.latest_version = (j.get("tag_name") or "").lstrip("v") or None
    for a in j.get("assets", []):
        if fnmatch.fnmatch(a["name"], app.asset_pattern):
            app.asset_url = a["browser_download_url"]
            app.asset_api_url = a["url"]
            app.asset_size = a.get("size", 0)
            return


def download_ipa(app: AppEntry, progress: Callable[[float], None], cancel: threading.Event) -> Path:
    if not app.asset_url:
        raise RuntimeError(f"{app.name} 还没有可下载的安装包")
    out = data_dir() / "downloads" / f"{app.id}-{app.latest_version}.ipa"
    out.parent.mkdir(exist_ok=True)
    if out.exists() and app.asset_size and out.stat().st_size == app.asset_size:
        progress(1.0)
        return out
    tok = _token()
    if tok and app.asset_api_url:
        req = urllib.request.Request(app.asset_api_url, headers={
            "User-Agent": UA, "Accept": "application/octet-stream", "Authorization": f"Bearer {tok}"})
    else:
        req = urllib.request.Request(app.asset_url, headers={"User-Agent": UA})
    tmp = out.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or app.asset_size or 0)
        got = 0
        while True:
            if cancel.is_set():
                raise RuntimeError("已取消")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                progress(got / total)
    tmp.replace(out)
    progress(1.0)
    return out


# MARK: 手机

@dataclass
class Device:
    udid: str
    name: str
    ios: str = ""
    model: str = ""


def list_devices() -> list[Device]:
    """连着的 iPhone / iPad：问 plumesign（usbmuxd，和爱思助手同一条通道；Mac / Windows 一样）"""
    exe = plumesign_path()
    if not exe:
        return []
    try:
        out = subprocess.run([str(exe), "device", "--list"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                             env={**os.environ, "RUST_LOG": "error"},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return []
    devs = []
    for line in out.splitlines():
        if line.startswith("PLUME_DEVICE "):
            udid, conn, name = (line[len("PLUME_DEVICE "):].split("\t") + ["", ""])[:3]
            devs.append(Device(udid=udid, name=name or "iPhone", model=conn))
    # 同一台手机 USB + WiFi 会各出一条，USB 优先
    seen, uniq = set(), []
    for d in sorted(devs, key=lambda d: d.model != "USB"):
        if d.udid not in seen:
            seen.add(d.udid)
            uniq.append(d)
    return uniq


# MARK: 签名安装（plumesign 子进程）

class SignError(RuntimeError):
    pass


_FRIENDLY = [
    (r"Incorrect|-20101|invalid.*credentials|password", "Apple ID 或密码不对"),
    (r"maximum number of apps|limit.*app ids|-7.*7.?day", "免费 Apple ID 7 天内最多注册 10 个 App ID，过几天再装或换个 Apple ID"),
    (r"3 apps|maximum.*installed|too many", "免费 Apple ID 同时最多装 3 个自签 App，先删掉一个再装"),
    (r"locked|PasswordProtected", "手机锁着屏，请解锁后保持亮屏"),
    (r"Developer Mode|developer mode", "手机没打开开发者模式（设置 → 隐私与安全性 → 开发者模式）"),
    (r"pair|Trust|trust", "手机还没信任这台电脑：在手机上点「信任」并输入锁屏密码"),
    (r"No devices connected", "没找到手机：请用数据线连接，并解锁手机"),
    (r"2FA|two.factor|verification", "验证码不对或已过期，重新获取"),
]


def friendly(msg: str) -> str:
    for pat, text in _FRIENDLY:
        if re.search(pat, msg, re.I):
            return text
    lines = [ln for ln in msg.strip().splitlines() if ln.strip()]
    return lines[-1][-200:] if lines else "未知错误"


class PlumeRunner:
    """跑 plumesign，按行回调；遇到要验证码就回调 ask_2fa 拿到后写进 stdin"""

    def __init__(self, on_line: Callable[[str], None]):
        self.exe = plumesign_path()
        if not self.exe:
            raise SignError("找不到签名组件 plumesign")
        self.on_line = on_line
        self.proc: Optional[subprocess.Popen] = None

    def _env(self):
        env = dict(os.environ)
        env.setdefault("RUST_LOG", "info")
        env["NO_COLOR"] = "1"
        return env

    def run(self, args: list[str], ask_2fa: Optional[Callable[[str], Optional[str]]] = None,
            stdin_lines: Optional[list[str]] = None) -> str:
        self.proc = subprocess.Popen([str(self.exe), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=self._env(),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = []
        for line in stdin_lines or []:
            self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        for line in self.proc.stdout:
            line = re.sub(r"\x1b\[[0-9;]*m", "", line.rstrip("\n"))
            out.append(line)
            self.on_line(line)
            if line.startswith("PLUME_NEED_2FA") and ask_2fa:
                code = ask_2fa(line.split()[-1])
                if code is None:
                    self.proc.kill()
                    raise SignError("已取消登录")
                self.proc.stdin.write(code.strip() + "\n")
                self.proc.stdin.flush()
        rc = self.proc.wait()
        text = "\n".join(out)
        if rc != 0:
            raise SignError(friendly(text))
        return text

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def saved_accounts() -> list[str]:
    """已登录过的 Apple ID（plumesign 存的是登录令牌，不存密码），当前选中的排第一"""
    exe = plumesign_path()
    if not exe:
        return []
    try:
        r = subprocess.run([str(exe), "account", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                           env={**os.environ, "RUST_LOG": "info", "NO_COLOR": "1"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        return []
    # 日志打在 stderr，两路都看
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    accts = []
    for line in out.splitlines():
        m = re.search(r"([\w.+-]+@[\w-]+\.[\w.]+)", line)
        if m and m.group(1) not in accts:
            (accts.insert(0, m.group(1)) if "(selected)" in line else accts.append(m.group(1)))
    return accts


def session_ok() -> bool:
    """存的登录令牌还能不能用（拿它去苹果拉一次团队列表）；过期了才需要重新输密码"""
    exe = plumesign_path()
    if not exe:
        return False
    try:
        r = subprocess.run([str(exe), "account", "certificates"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                           stdin=subprocess.DEVNULL, env={**os.environ, "RUST_LOG": "error", "NO_COLOR": "1"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def logout() -> None:
    exe = plumesign_path()
    if exe:
        subprocess.run([str(exe), "account", "logout"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def login(apple_id: str, password: str, on_line, ask_2fa) -> None:
    PlumeRunner(on_line).run(["account", "login", "-u", apple_id, "-p", password], ask_2fa=ask_2fa)


def wifi_sync(udid: str, on: Optional[bool] = None) -> Optional[bool]:
    """手机的「通过 Wi‑Fi 连接」开关（存在手机上，换电脑也有效）。需要数据线连着。读不到返回 None"""
    exe = plumesign_path()
    if not exe:
        return None
    args = [str(exe), "device", "--udid", udid, "--wifi", {True: "on", False: "off", None: "status"}[on]]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                           stdin=subprocess.DEVNULL, env={**os.environ, "RUST_LOG": "error"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith("PLUME_WIFI "):
            return line.rsplit("\t", 1)[-1].strip() == "on"
    return None


def sign_and_install(ipa: Path, udid: str, on_line) -> None:
    PlumeRunner(on_line).run(["sign", "--apple-id", "-p", str(ipa), "--register-and-install", "--udid", udid])


# MARK: 自身更新

# MARK: 偏好 / 系统通知

def _prefs_path() -> Path:
    return data_dir() / "prefs.json"


def get_pref(key: str, default=None):
    try:
        return json.loads(_prefs_path().read_text("utf-8")).get(key, default)
    except Exception:  # noqa: BLE001
        return default


def set_pref(key: str, value) -> None:
    try:
        d = json.loads(_prefs_path().read_text("utf-8"))
    except Exception:  # noqa: BLE001
        d = {}
    d[key] = value
    _prefs_path().write_text(json.dumps(d, ensure_ascii=False), "utf-8")


def notify(title: str, body: str) -> None:
    """系统通知（Mac 通知中心 / Windows 托盘气泡），失败就算了"""
    try:
        if sys.platform == "darwin":
            esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
                           timeout=5, capture_output=True)
        elif sys.platform == "win32":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
                  f"$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode('{title}'))>$null;"
                  f"$t.Item(1).AppendChild($x.CreateTextNode('{body}'))>$null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('一键装').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=10, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        pass


# MARK: 开机自启（只负责登录后在后台挂着做自动续签）

_AGENT_LABEL = "me.rrmm.onetapinstall.autostart"


def _app_launch_cmd() -> list[str]:
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return [str(exe), "--background"]
    return [str(exe), str(Path(__file__).resolve().parent.parent / "main.py"), "--background"]


def _win_startup_lnk() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "一键装.lnk"


def autostart_enabled() -> bool:
    if sys.platform == "darwin":
        return (Path.home() / "Library/LaunchAgents" / f"{_AGENT_LABEL}.plist").exists()
    if sys.platform == "win32":
        return _win_startup_lnk().exists()
    return False


def set_autostart(on: bool) -> None:
    if sys.platform == "darwin":
        import plistlib
        f = Path.home() / "Library/LaunchAgents" / f"{_AGENT_LABEL}.plist"
        if on:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(plistlib.dumps({"Label": _AGENT_LABEL, "ProgramArguments": _app_launch_cmd(),
                                          "RunAtLoad": True, "ProcessType": "Interactive"}))
        else:
            f.unlink(missing_ok=True)
    elif sys.platform == "win32":
        # 跟安装包里「开机自动启动」是同一个快捷方式（启动文件夹\一键装.lnk），开关互通
        lnk = _win_startup_lnk()
        if on:
            cmd = _app_launch_cmd()
            target, args = cmd[0], " ".join(f'"{c}"' if " " in c else c for c in cmd[1:])
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
                  "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;"
                  "$s.WorkingDirectory=(Split-Path $env:TGT);$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20,
                           capture_output=True, env={**os.environ, "LNK": str(lnk), "TGT": target, "ARGS": args},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            lnk.unlink(missing_ok=True)


# MARK: 自己添加的 IPA

def _plist_any(raw: bytes) -> dict:
    import plistlib
    try:
        return plistlib.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _png_from_ipa(z, app_dir: str, info: dict) -> Optional[bytes]:
    """从 IPA 里挑最大的 App 图标。iOS 打包时 PNG 被改成 CgBI 格式（浏览器不认），转回标准 PNG。"""
    names = []
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        prim = (info.get(key) or {}).get("CFBundlePrimaryIcon") or {}
        names += prim.get("CFBundleIconFiles") or []
        if prim.get("CFBundleIconName"):
            names.append(prim["CFBundleIconName"])
    names += info.get("CFBundleIconFiles") or []
    if info.get("CFBundleIconFile"):
        names.append(info["CFBundleIconFile"])
    files = [n for n in z.namelist() if n.startswith(app_dir) and n.count("/") == app_dir.count("/") and n.lower().endswith(".png")]
    cands = [f for f in files if any(f[len(app_dir):].startswith(n.rsplit(".png", 1)[0]) for n in names if n)]
    if not cands:
        cands = [f for f in files if "appicon" in f.lower() or "icon" in f.lower()]
    if not cands:
        return None
    best = max(cands, key=lambda f: z.getinfo(f).file_size)
    return cgbi_to_png(z.read(best))


def cgbi_to_png(data: bytes) -> bytes:
    """苹果 CgBI PNG → 标准 PNG（去 CgBI 块、raw deflate 重压、BGRA→RGBA、反预乘 alpha）。普通 PNG 原样返回。"""
    import struct
    import zlib
    if data[:8] != b"\x89PNG\r\n\x1a\n" or b"CgBI" not in data[8:40]:
        return data
    pos, chunks, idat = 8, [], b""
    w = h = 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"CgBI":
            continue
        if typ == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        if typ == b"IDAT":
            idat += body
            continue
        chunks.append((typ, body))
        if typ == b"IEND":
            break
    raw = bytearray(zlib.decompressobj(-15).decompress(idat))
    stride = w * 4
    # 先按 PNG 过滤器还原像素，再改通道顺序；改完用 filter 0 重新编码
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]; i += 1
        line = bytearray(raw[i:i + stride]); i += stride
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        px = bytearray(line)
        for x in range(0, stride, 4):
            bb, gg, rr, aa = px[x], px[x + 1], px[x + 2], px[x + 3]
            if aa:
                rr, gg, bb = min(255, rr * 255 // aa), min(255, gg * 255 // aa), min(255, bb * 255 // aa)
            px[x], px[x + 1], px[x + 2], px[x + 3] = rr, gg, bb, aa
        out += b"\x00" + px

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    res = b"\x89PNG\r\n\x1a\n"
    for t, b in chunks:
        if t == b"IEND":
            res += chunk(b"IDAT", zlib.compress(bytes(out), 9))
        res += chunk(t, b)
    return res


def _custom_dir() -> Path:
    d = data_dir() / "custom"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inspect_ipa(path: str) -> dict:
    """读 IPA 的名字 / 包名 / 版本 / 最低系统 / 图标。不是有效 IPA 抛 SignError。"""
    import zipfile
    p = Path(path)
    if not p.is_file():
        raise SignError("文件不存在")
    try:
        z = zipfile.ZipFile(p)
    except zipfile.BadZipFile:
        raise SignError("这不是有效的 IPA 文件（应为 .ipa 安装包）")
    with z:
        plists = [n for n in z.namelist() if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2]
        if not plists:
            raise SignError("IPA 里没找到 App（缺少 Payload/xxx.app）")
        info = _plist_any(z.read(plists[0]))
        app_dir = plists[0][: -len("Info.plist")]
        icon = _png_from_ipa(z, app_dir, info)
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or p.stem
    return {
        "name": str(name),
        "bundle_id": str(info.get("CFBundleIdentifier", "")),
        "version": str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""),
        "min_ios": str(info.get("MinimumOSVersion", "")),
        "size": p.stat().st_size,
        "icon": icon,
    }


def load_custom_apps() -> list[AppEntry]:
    """「我的 IPA」：保存在本机 custom/apps.json，IPA 复制一份到 custom/ 下（原文件删了也能再装）"""
    f = _custom_dir() / "apps.json"
    try:
        items = json.loads(f.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        ipa = _custom_dir() / it["file"]
        if not ipa.exists():
            continue
        icon_f = _custom_dir() / (it["id"] + ".png")
        out.append(AppEntry(
            id=it["id"], name=it["name"], subtitle="我添加的 IPA", description=it.get("bundle_id", ""),
            bundle_id=it.get("bundle_id", ""), icon="", repo="", asset_pattern="", min_ios=it.get("min_ios", ""),
            latest_version=it.get("version") or "-", asset_size=ipa.stat().st_size, local_path=str(ipa),
            icon_bytes=icon_f.read_bytes() if icon_f.exists() else None))
    return out


def _save_custom(items: list[dict]) -> None:
    (_custom_dir() / "apps.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")


def add_custom_ipa(path: str) -> AppEntry:
    """加一个本地 IPA：校验 → 复制进数据目录 → 记进列表。同包名的旧条目被替换（相当于升级）。"""
    import hashlib
    import shutil
    meta = inspect_ipa(path)
    key = meta["bundle_id"] or Path(path).name
    cid = "custom-" + hashlib.sha1(key.encode()).hexdigest()[:10]
    dst = _custom_dir() / (cid + ".ipa")
    shutil.copyfile(path, dst)
    if meta["icon"]:
        (_custom_dir() / (cid + ".png")).write_bytes(meta["icon"])
    f = _custom_dir() / "apps.json"
    try:
        items = json.loads(f.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        items = []
    items = [it for it in items if it["id"] != cid]
    items.insert(0, {"id": cid, "name": meta["name"], "bundle_id": meta["bundle_id"], "version": meta["version"],
                     "min_ios": meta["min_ios"], "file": dst.name, "added": int(time.time())})
    _save_custom(items)
    return next(a for a in load_custom_apps() if a.id == cid)


def remove_custom_ipa(cid: str) -> None:
    f = _custom_dir() / "apps.json"
    try:
        items = json.loads(f.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return
    _save_custom([it for it in items if it["id"] != cid])
    for ext in (".ipa", ".png"):
        try:
            (_custom_dir() / (cid + ext)).unlink()
        except FileNotFoundError:
            pass


def newer(a: str, b: str) -> bool:
    """a 比 b 新（按 . 分段数字比）"""
    pa = [int(x) for x in re.findall(r"\d+", a)]
    pb = [int(x) for x in re.findall(r"\d+", b)]
    return pa > pb


def system_label() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if sys.platform == "win32":
        return f"Windows {platform.release()}"
    return platform.platform()


def is_cn() -> bool:
    """中文系统默认走 Gitee 下载。Windows 上 locale 给的是 'Chinese (Simplified)_China'，要看系统界面语言 ID"""
    import locale
    if sys.platform == "win32":
        try:
            import ctypes
            # 0x04 = 中文（简/繁），低 10 位是主语言
            return ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x04
        except Exception:  # noqa: BLE001
            pass
    lang = (os.environ.get("LANG") or locale.getlocale()[0] or "").lower()
    return lang.startswith("zh") or lang.startswith("chinese")

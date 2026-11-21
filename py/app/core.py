"""一键签 · 核心逻辑（与界面无关，可单独测试）

- 应用目录：从 Gitee 拉 catalog/apps.json，新 App 只要改这个文件
- 版本 / 下载：每个 App 读自己的软件源 JSON（全能签同款格式，Gitee），不访问 GitHub
- 签名安装：调用随包附带的 plumesign（Impactor 的命令行，MIT），
  Apple ID 登录 / 证书 / 描述文件 / 注册设备 / 签名 / 装机全在它里面
"""
from __future__ import annotations

import fnmatch
import json
import logging
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

VERSION = "0.2.3"

# 应用目录：Gitee 为主，Cloudflare R2（dl.oneduo.app）兜底。2026-11-20 起不再使用 GitHub / jsDelivr
GITEE_REPO = "remon8/oneduo"
R2_BASE = "https://dl.oneduo.app"          # 镜像布局：<app id>/v<版本>/<文件名>、oneduo/catalog/apps.json、<app id>/<app id>.json
CATALOG_URLS = [
    f"https://gitee.com/{GITEE_REPO}/raw/main/catalog/apps.json",
    f"{R2_BASE}/oneduo/catalog/apps.json",
]

UA = f"OneDuo/{VERSION}"


def data_dir() -> Path:
    if os.environ.get("ONETAP_DATA_DIR"):      # 测试用：换一个数据目录，不动真实配置
        d = Path(os.environ["ONETAP_DATA_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        return d
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
    if os.environ.get("ONETAP_PLUMESIGN") and Path(os.environ["ONETAP_PLUMESIGN"]).exists():   # NAS 版 Docker
        return Path(os.environ["ONETAP_PLUMESIGN"])
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


_ssl_ctx = None


def ssl_context():
    """HTTPS 根证书。打包后的 Mac 版 OpenSSL 只认 python.org 安装目录下的 cert.pem，
    用户电脑上没有这个文件 → 所有 HTTPS 都报 CERTIFICATE_VERIFY_FAILED（Gitee / R2 全挂，
    列表一直「读取版本…」）。这里补上系统自带的证书包和随包的 certifi（工单 B15）"""
    global _ssl_ctx
    if _ssl_ctx is None:
        import ssl
        ctx = ssl.create_default_context()        # Windows 会读系统证书库
        extra = ["/etc/ssl/cert.pem"] if sys.platform == "darwin" else []
        try:
            import certifi
            extra.append(certifi.where())
        except Exception:  # noqa: BLE001
            pass
        for p in extra:
            try:
                if os.path.exists(p):
                    ctx.load_verify_locations(cafile=p)
            except Exception:  # noqa: BLE001
                pass
        _ssl_ctx = ctx
    return _ssl_ctx


def urlopen(req, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def http_get(url: str, timeout: float = 15, accept: Optional[str] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if accept:
        req.add_header("Accept", accept)
    tok = _token()
    if tok and ("api.github.com" in url or "githubusercontent.com" in url):
        req.add_header("Authorization", f"Bearer {tok}")
    with urlopen(req, timeout=timeout) as r:
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
    gitee_repo: str = ""                      # Gitee 镜像仓库（拼国内下载地址）
    sources: list = field(default_factory=list)   # 软件源 JSON 地址（全能签同款格式），版本从这里读
    source_id: str = "official"               # 来自哪个软件源（official = 一键签官方源）
    unsupported: str = ""                     # 不能装的原因（需要解锁码 / 蓝奏云网盘链接等），空 = 能装
    # 运行时补
    latest_version: Optional[str] = None
    asset_url: Optional[str] = None
    asset_api_url: Optional[str] = None
    asset_size: int = 0
    asset_urls: list = field(default_factory=list)   # 同一版本的几个下载镜像，按优先级
    latest_notes: str = ""                   # 最新版本更新了什么（软件源 versionDescription）
    latest_date: str = ""                    # 最新版本发布日期（软件源 versionDate / date）
    resolve_error: str = ""                  # 读版本失败的原因（界面显示「读取失败」而不是一直「读取版本…」）
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
    installer_assets: dict = field(default_factory=dict)   # {"windows": {name, sha256, size}, "macos": {...}}
    base_url: str = ""
    source: str = ""


def _parse_catalog(raw: bytes, base_url: str, source: str) -> Catalog:
    j = json.loads(raw.decode("utf-8"))
    keys = AppEntry.__dataclass_fields__.keys()
    apps = [AppEntry(**{k: v for k, v in a.items() if k in keys}) for a in j.get("apps", [])]
    inst = j.get("installer", {})
    return Catalog(apps=apps, installer_latest=inst.get("latest", VERSION), installer_notes=inst.get("notes", ""),
                   installer_download=inst.get("download", {}), installer_assets=inst.get("assets", {}),
                   base_url=base_url, source=source)


def load_catalog() -> Catalog:
    """几个镜像同时拉，谁先成功用谁；都失败就用本地（开发 / 离线）"""
    local = os.environ.get("ONETAP_CATALOG")
    if local:
        p = Path(local)
        return _parse_catalog(p.read_bytes(), p.parent.as_uri() + "/", "本地")

    def fetch(url):
        return url, http_get(url, timeout=10)

    errors = []
    for u in CATALOG_URLS:
        try:
            url, raw = fetch(u)
            cat = _parse_catalog(raw, url.rsplit("/", 1)[0] + "/", url.split("/")[2])
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


def sg(d: dict, *keys, default=""):
    """软件源字段名各家不统一（downloadURL / downloadUrl / down …），挨个试"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def source_versions(j: dict, bundle_id: str = "", name: str = "") -> list[dict]:
    """软件源 JSON（全能签 / ESign / AltStore 同款格式）里某个 App 的所有版本。
    bundle_id 为空时按名字匹配（有的源不写包名）。"""
    out = []
    for a in j.get("apps", []) or []:
        bid = sg(a, "bundleIdentifier", "bundleID", "bundleId")
        if bundle_id and bid != bundle_id:
            continue
        if not bundle_id and name and sg(a, "name", "appName") != name:
            continue
        vers = a.get("versions") or [a]          # AltStore 新格式把版本放 versions[]
        for v in vers:
            url = sg(v, "downloadURL", "downloadUrl", "down") or sg(a, "downloadURL", "downloadUrl", "down")
            ver = str(sg(v, "version") or sg(a, "version"))
            if not (url and ver):
                continue
            try:
                size = int(sg(v, "size", default=0) or sg(a, "size", default=0) or 0)
            except (TypeError, ValueError):
                size = 0
            lock = str(sg(v, "lock") or sg(a, "lock")) == "1"
            lanzou = str(sg(v, "isLanZouCloud") or sg(a, "isLanZouCloud")) == "1"
            out.append({"version": ver, "url": url, "size": size,
                        # 更新说明：平铺格式（全能签/ESign）是 versionDescription；
                        # AltStore 的 versions[] 里每个版本的 localizedDescription 就是该版本更新说明
                        "notes": str(sg(v, "versionDescription", "changelog", "whatsNew")
                                     or (sg(v, "localizedDescription") if v is not a else "") or ""),
                        "date": str(sg(v, "versionDate", "date") or "")[:10],
                        "unsupported": "需要解锁码，一键签不支持" if lock else ("网盘链接，不能直接下载" if lanzou else "")})
    return out


def _vkey(v: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", v)]


_src_cache: dict[str, tuple[float, dict]] = {}
_src_lock = threading.Lock()


def fetch_source(url: str, ttl: float = 120) -> dict:
    """拉一个软件源 JSON。2 分钟内同一个地址只拉一次（一个源几百个 App 时不会拉几百遍）；
    拉不到用本地上次的副本，离线也能显示列表"""
    now = time.time()
    with _src_lock:
        hit = _src_cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    import hashlib
    disk = data_dir() / "sources-cache" / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")
    try:
        raw = http_get(url, timeout=12)
        try:
            j = json.loads(raw.decode("utf-8-sig"))
        except ValueError:
            raise ValueError("这个地址返回的不是软件源（应该是 JSON 文件）") from None
        if not isinstance(j, dict) or not isinstance(j.get("apps"), list):
            raise ValueError("这个地址返回的不是软件源（缺少 apps 列表）")
        disk.parent.mkdir(exist_ok=True)
        disk.write_bytes(raw)
    except Exception:
        if disk.exists():
            j = json.loads(disk.read_bytes().decode("utf-8-sig"))
        else:
            raise
    with _src_lock:
        _src_cache[url] = (now, j)
    return j


def apply_versions(app: "AppEntry", found: list[dict]) -> bool:
    """从软件源版本列表里挑最新的能装的版本，填进 app。挑到返回 True"""
    ok = [v for v in found if not v["unsupported"]]
    if not ok:
        if found:
            app.unsupported = found[0]["unsupported"]
        return False
    best = max(ok, key=lambda v: _vkey(v["version"]))
    same = [v for v in ok if v["version"] == best["version"]]
    app.latest_version = best["version"]
    app.latest_notes = next((v["notes"] for v in same if v.get("notes")), "").strip()
    app.latest_date = next((v["date"] for v in same if v.get("date")), "")
    app.asset_size = max(v["size"] for v in same)
    urls = []
    for v in sorted(same, key=lambda v: ("gitee.com" not in v["url"]) if is_cn() else ("gitee.com" in v["url"])):
        for u in _mirror_urls(app, best["version"], v["url"]):
            if u not in urls:
                urls.append(u)
    app.asset_urls = urls
    app.asset_url = urls[0]
    app.asset_api_url = None
    app.unsupported = ""
    return True


def _latest_cache() -> Path:
    return data_dir() / "latest.json"


def _save_latest(app: AppEntry) -> None:
    try:
        d = json.loads(_latest_cache().read_text("utf-8")) if _latest_cache().exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    d[app.id] = {"version": app.latest_version, "urls": app.asset_urls, "size": app.asset_size,
                 "notes": app.latest_notes, "date": app.latest_date, "at": time.time()}
    _latest_cache().write_text(json.dumps(d, ensure_ascii=False), "utf-8")


def _mirror_urls(app: AppEntry, ver: str, first: Optional[str]) -> list[str]:
    """同一个版本的几个下载地址：源里给的 + 按命名规则拼的 Gitee 发行版 / R2 镜像地址。一个不通换下一个"""
    name = app.asset_pattern.replace("*", ver) if app.asset_pattern else ""
    ge = f"https://gitee.com/{app.gitee_repo}/releases/download/v{ver}/{name}" if app.gitee_repo and name else ""
    r2 = f"{R2_BASE}/{app.id}/v{ver}/{name}" if app.source_id == "official" and name else ""
    urls = []
    for u in [first, ge, r2]:
        if u and u not in urls:
            urls.append(u)
    return urls


def resolve_latest(app: AppEntry) -> None:
    """查这个 App 的最新版本和下载地址。

    以前直接调 GitHub API（匿名每小时 60 次，同一个出口 IP 的几台电脑一起刷就 403，列表就刷不出来）。
    现在顺序：① 软件源 JSON（Gitee + R2 镜像，几个一起拉，版本合并）
             ② 上次查到的结果（离线也能装缓存里的包）
    """
    errors = []
    srcs = list(app.sources or [])
    if srcs:
        found: list[dict] = []
        with ThreadPoolExecutor(len(srcs)) as ex:
            futs = [ex.submit(fetch_source, u) for u in srcs]
            for f in as_completed(futs):
                try:
                    found += source_versions(f.result(), app.bundle_id, "" if app.bundle_id else app.name)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"源：{e}")
        if apply_versions(app, found):
            _save_latest(app)
            return
        if app.unsupported:
            return
    # ② 上次的结果（源暂时拉不到时，离线也能装缓存里的包）
    try:
        c = json.loads(_latest_cache().read_text("utf-8")).get(app.id)
        if c and c.get("urls"):
            app.latest_version, app.asset_urls, app.asset_size = c["version"], c["urls"], c.get("size", 0)
            app.latest_notes, app.latest_date = c.get("notes", ""), c.get("date", "")
            app.asset_url = app.asset_urls[0]
            return
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError("；".join(errors) or "查不到版本")


def download_ipa(app: AppEntry, progress: Callable[[float], None], cancel: threading.Event) -> Path:
    if not app.asset_url and app.sources and not app.local_path:
        try:                             # 打开时版本没读到（网络 / 证书问题）：装之前再试一次
            resolve_latest(app)
            app.resolve_error = ""
        except Exception as e:  # noqa: BLE001
            app.resolve_error = str(e)
            raise RuntimeError(f"{app.name} 读取版本失败，请检查网络后点「⟳ 刷新」重试（{str(e)[:120]}）") from None
    if not app.asset_url:
        raise RuntimeError(f"{app.name} 还没有可下载的安装包")
    out = data_dir() / "downloads" / f"{app.id}-{app.latest_version}.ipa"
    out.parent.mkdir(exist_ok=True)
    if out.exists() and app.asset_size and out.stat().st_size == app.asset_size:
        progress(1.0)
        return out
    tok = _token()
    reqs = []
    if tok and app.asset_api_url:
        reqs.append(urllib.request.Request(app.asset_api_url, headers={
            "User-Agent": UA, "Accept": "application/octet-stream", "Authorization": f"Bearer {tok}"}))
    for u in (app.asset_urls or [app.asset_url]):
        reqs.append(urllib.request.Request(u, headers={"User-Agent": UA}))
    tmp = out.with_suffix(".part")
    errors = []
    # 一个镜像不通就换下一个；同一个镜像中途断流（大小不对 / 超时 / 连接被重置）最多再试 2 次——CDN 慢的时候常见
    for req in [r for r in reqs for _ in range(3)]:
        try:
            with urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or app.asset_size or 0)
                got = 0
                while True:
                    if cancel.is_set():
                        raise SignError("已取消")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        progress(got / total)
            if app.asset_size and got != app.asset_size:
                raise RuntimeError(f"大小不对（{got} / {app.asset_size}）")
            tmp.replace(out)
            progress(1.0)
            return out
        except SignError:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append(f"{req.full_url.split('/')[2]}：{e}")
            logging.getLogger("onetap").warning("download %s failed (%d): %s", req.full_url, len(errors), e)
    raise RuntimeError("下载失败：" + "；".join(dict.fromkeys(errors)))


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
    (r"P12_BAD_PASSWORD_OR_FORMAT", "证书密码不对，或者这不是 .p12 证书文件"),
    (r"P12_NO_PRIVATE_KEY|P12_NO_CERTIFICATE|P12_KEY_UNSUPPORTED", "这个 .p12 里没有可用的证书和私钥"),
    (r"PROV_BAD_FORMAT|ProvisioningEntitlementsUnknown", "描述文件（.mobileprovision）读不出来，确认文件没损坏"),
    (r"ApplicationVerificationFailed|0xe8008015|A valid provisioning profile", "手机拒绝安装：描述文件不包含这台手机，或者证书已被苹果吊销（掉签）"),
    (r"0xe800801c|no longer valid|revoked", "证书已被苹果吊销（掉签），换一个证书"),
    # 苹果返回 -20209「This Apple ID has been locked for security reasons」：是账号被锁，不是手机锁屏（工单 B10）
    (r"-20209|Apple ID has been (locked|disabled)|account (is |has been )?(locked|disabled)",
     "Apple ID 被苹果锁定了（安全原因），去 iforgot.apple.com 解锁或改密码后再登录"),
    (r"Incorrect|-20101|invalid.*credentials|password", "Apple ID 或密码不对"),
    (r"maximum number of apps|limit.*app ids|-7.*7.?day", "免费 Apple ID 7 天内最多注册 10 个 App ID，过几天再装或换个 Apple ID"),
    (r"3 apps|maximum.*installed|too many", "免费 Apple ID 同时最多装 3 个自签 App，先删掉一个再装"),
    # 只认 idevice 的设备锁屏错误（"device locked" / "device is locked" / DeviceLocked / PasswordProtected）
    (r"device (is )?locked|DeviceLocked|PasswordProtected", "手机锁着屏，请解锁后保持亮屏"),
    (r"Developer Mode|developer mode", "手机没打开开发者模式（设置 → 隐私与安全性 → 开发者模式）"),
    # 只认 idevice 的配对错误；以前 "pair|trust" 会被 App 里的 TrustKit.framework 之类文件名误命中（工单 B14）
    (r"pairing file|pair ?record|InvalidHostID|pairing trust|denied pairing|Pairing rejected|Pair verify|"
     r"PairingDialogResponsePending|UserDeniedPairing|not paired|NotPaired",
     "手机还没信任这台电脑：在手机上点「信任」并输入锁屏密码"),
    (r"No devices connected", "没找到手机：请用数据线连接，并解锁手机"),
    (r"2FA|two.factor|verification", "验证码不对或已过期，重新获取"),
]


_LOG_LINE = re.compile(r"^\[\S+\s+(TRACE|DEBUG|INFO|WARN)\s")


def _error_part(msg: str) -> str:
    """plumesign 的整段输出里混着 info 日志（签名时每个文件一行 copying file …），
    只拿真正的报错去匹配：anyhow 最后打的 "Error: …"（含 Caused by）；没有就去掉普通日志行（工单 B14）"""
    lines = msg.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("Error:")]
    if idx:
        return "\n".join(lines[idx[-1]:])
    kept = [ln for ln in lines if not _LOG_LINE.match(ln)]
    return "\n".join(kept) if any(ln.strip() for ln in kept) else msg


def friendly(msg: str) -> str:
    msg = _error_part(msg)
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
        self.extra_env: dict = {}

    def _env(self):
        env = dict(os.environ)
        env.update(self.extra_env)
        env["RUST_LOG"] = "info"   # 验证码提示、安装进度都靠 info 日志
        env["NO_COLOR"] = "1"
        return env

    def run(self, args: list[str], ask_2fa: Optional[Callable[[str, list], Optional[str]]] = None,
            stdin_lines: Optional[list[str]] = None) -> str:
        self.proc = subprocess.Popen([str(self.exe), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=self._env(),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = []
        for line in stdin_lines or []:
            self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        kind, phones = None, []
        for line in self.proc.stdout:
            line = re.sub(r"\x1b\[[0-9;]*m", "", line.rstrip("\n"))
            out.append(line)
            self.on_line(line)
            if line.startswith("PLUME_NEED_2FA"):
                kind, phones = line.split()[-1], []
                continue
            if line.startswith("PLUME_SMS_PHONES "):
                # "<id>:<尾号>|<id>:<尾号>"
                for item in line[len("PLUME_SMS_PHONES "):].split("|"):
                    pid, _, tail = item.partition(":")
                    if pid.strip().isdigit():
                        phones.append({"id": int(pid), "tail": tail.strip()})
                continue
            # 验证码提示那一行（plumesign 打完号码列表才打它）出现时才问用户，这样号码已经拿全了
            if kind and ask_2fa and ("Enter" in line and ("code" in line or "2FA" in line)):
                code = ask_2fa(kind, phones)
                kind = None
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


def sign_and_install_cert(ipa: Path, udid: str, p12: Path, password: str, provision: Path, on_line) -> None:
    """证书模式（全能签那种）：导入的 .p12 + 描述文件签名，不用 Apple ID。密码走环境变量，不出现在进程列表里"""
    r = PlumeRunner(on_line)
    r.extra_env = {"PLUME_P12_PASSWORD": password}
    r.run(["sign", "--p12", str(p12), "--provision", str(provision), "-p", str(ipa),
           "--register-and-install", "--udid", udid])


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
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('一键签').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
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
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "一键签.lnk"


def _migrate_old_name() -> None:
    """改名前叫「一键装」：开机自启的旧项换成新名字（Mac 的 LaunchAgent 记的是 .app 路径，也要指到新 App）"""
    try:
        if sys.platform == "win32":
            old = _win_startup_lnk().with_name("一键装.lnk")
            if old.exists():
                old.unlink()
                set_autostart(True)
        elif sys.platform == "darwin" and getattr(sys, "frozen", False):
            f = Path.home() / "Library/LaunchAgents" / f"{_AGENT_LABEL}.plist"
            if f.exists() and "一键装.app" in f.read_text("utf-8", errors="ignore"):
                set_autostart(True)
    except Exception:  # noqa: BLE001
        pass


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
        # 跟安装包里「开机自动启动」是同一个快捷方式（启动文件夹\一键签.lnk），开关互通
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

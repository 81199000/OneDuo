"""一键签自身更新：下载新版安装包 → 校验 → 退出后由外部脚本替换并重新打开。

应用列表（catalog/apps.json）里的 installer 段：
  "installer": {
    "latest": "0.1.1", "notes": "…",
    "assets": {
      "windows": {"name": "OneDuo-0.1.1-windows-x64-setup.exe", "sha256": "…", "size": 123},
      "macos":   {"name": "OneDuo-0.2.1-macos-universal.zip",   "sha256": "…", "size": 123}
    },
    "download": {"gitee": "https://gitee.com/remon8/oneduo/releases"}
  }
下载地址按 tag 拼：Gitee 发行版 v<latest>/<name>，不通再走 R2 镜像 dl.oneduo.app/oneduo/v<latest>/<name>。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import core

log = logging.getLogger("selfupdate")

GITEE_DL = "https://gitee.com/{repo}/releases/download/v{ver}/{name}"
GITEE_REPO = "remon8/oneduo"


def platform_key() -> Optional[str]:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return None


def can_self_update() -> bool:
    """只有打包好的正式版才能自己替换自己（开发时用 python main.py 跑的不行）"""
    return bool(getattr(sys, "frozen", False)) and platform_key() is not None


def asset_for(cat) -> Optional[dict]:
    k = platform_key()
    a = (cat.installer_assets or {}).get(k) if k else None
    return a if a and a.get("name") else None


def _urls(ver: str, name: str) -> list[str]:
    base = os.environ.get("ONETAP_UPDATE_BASE")   # 只用于测试：从本地目录 / 内网地址拿安装包
    if base:
        return [base.rstrip("/") + "/" + name]
    return [GITEE_DL.format(repo=GITEE_REPO, ver=ver, name=name),
            f"{core.R2_BASE}/oneduo/v{ver}/{name}"]


def download(ver: str, asset: dict, progress: Callable[[float], None]) -> Path:
    """下到数据目录 updates/，校验 sha256 和大小；失败换下一个镜像"""
    d = core.data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / asset["name"]
    want_sha = (asset.get("sha256") or "").lower()
    want_size = int(asset.get("size") or 0)
    if dest.exists() and want_sha and _sha256(dest) == want_sha:
        progress(1.0)
        return dest
    last = None
    for url in _urls(ver, asset["name"]):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            log.info("download %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": core.UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or want_size or 0)
                got = 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        progress(min(got / total, 0.999))
            if want_size and tmp.stat().st_size != want_size:
                raise IOError(f"大小不对：{tmp.stat().st_size} ≠ {want_size}")
            if want_sha and _sha256(tmp) != want_sha:
                raise IOError("文件校验没通过（sha256 不一致）")
            tmp.replace(dest)
            progress(1.0)
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("download failed %s: %s", url, e)
            last = e
            tmp.unlink(missing_ok=True)
    raise IOError(f"下载新版失败：{last}")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------- 安装：起一个独立的小脚本，等本进程退出后替换并重新打开 ----------

def launch_installer(pkg: Path) -> None:
    """调用后应尽快退出本程序（脚本会等本进程结束）"""
    if sys.platform == "win32":
        _launch_windows(pkg)
    elif sys.platform == "darwin":
        _launch_macos(pkg)
    else:
        raise RuntimeError("不支持的系统")


def _launch_windows(setup: Path) -> None:
    """等本进程退出 → 以管理员身份静默覆盖安装（会弹一次 UAC）→ 安装包装完自己用原用户身份重新打开一键签。
    UAC 点了「否」或安装失败，就重新打开旧版。"""
    pid = os.getpid()
    exe = Path(sys.executable)
    ps1 = Path(tempfile.gettempdir()) / "oneduo-update.ps1"
    q = lambda p: str(p).replace("'", "''")  # noqa: E731  PowerShell 单引号字符串转义
    ps1.write_text(
        "$ErrorActionPreference = 'SilentlyContinue'\r\n"
        f"while (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ Start-Sleep -Milliseconds 500 }}\r\n"
        "$ok = $false\r\n"
        # 只等安装包本身（Start-Process -Wait 会连它拉起的新一键签一起等，脚本就一直挂着）
        f"try {{ $si = New-Object System.Diagnostics.ProcessStartInfo '{q(setup)}'; "
        "$si.Arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOCANCEL /CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS'; "
        "$si.Verb = 'runas'; $si.UseShellExecute = $true; $p = [System.Diagnostics.Process]::Start($si); $p.WaitForExit(); "
        "$ok = ($p.ExitCode -eq 0) } catch { $ok = $false }\r\n"
        f"if (-not $ok) {{ Start-Process -FilePath '{q(exe)}' }}\r\n"
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force\r\n",
        encoding="utf-8-sig")   # 带 BOM：路径里有中文，Windows PowerShell 5.1 才按 UTF-8 读
    base = 0x00000200 | 0x08000000   # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(ps1)]
    try:
        subprocess.Popen(args, creationflags=base | 0x01000000, close_fds=True)  # + CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(args, creationflags=base, close_fds=True)


def _app_bundle() -> Path:
    # sys.executable = …/一键签.app/Contents/MacOS/OneTap
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    raise RuntimeError("找不到 .app 路径")


def _launch_macos(zip_path: Path) -> None:
    app = _app_bundle()
    work = Path(tempfile.mkdtemp(prefix="oneduo-update-"))
    # 先解压并验证签名 / 公证，验过了才动旧版
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(zip_path), str(work)], check=True)
    new = next(work.glob("*.app"), None)
    if not new:
        raise RuntimeError("安装包里没有 .app")
    chk = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(new)], capture_output=True, text=True)
    if chk.returncode != 0:
        raise RuntimeError("新版签名校验没通过：" + (chk.stderr or "").strip()[:200])
    team = subprocess.run(["/usr/bin/codesign", "-dv", str(new)], capture_output=True, text=True).stderr
    cur = subprocess.run(["/usr/bin/codesign", "-dv", str(app)], capture_output=True, text=True).stderr
    tid = lambda s: next((l.split("=", 1)[1] for l in s.splitlines() if l.startswith("TeamIdentifier=")), "")  # noqa: E731
    if tid(cur) not in ("", "not set") and tid(team) != tid(cur):
        raise RuntimeError("新版的开发者和当前版本不一致，已取消更新")
    pid = os.getpid()
    sh = work / "update.sh"
    sh.write_text(f"""#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
OLD="{app}"
BAK="{app}.old"
rm -rf "$BAK"
mv "$OLD" "$BAK" && /usr/bin/ditto "{new}" "$OLD" && rm -rf "$BAK" || {{ rm -rf "$OLD"; mv "$BAK" "$OLD"; }}
/usr/bin/xattr -dr com.apple.quarantine "$OLD" 2>/dev/null
open "$OLD"
rm -rf "{work}"
""", encoding="utf-8")
    os.chmod(sh, 0o755)
    subprocess.Popen(["/bin/sh", str(sh)], start_new_session=True, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cleanup_old() -> None:
    """启动时清掉下载过的安装包（能走到这里说明要么已经装完，要么上次没装，点更新时会重新下）"""
    for f in Path(tempfile.gettempdir()).glob("oneduo-update*"):
        try:
            f.unlink()
        except OSError:
            pass
    d = core.data_dir() / "updates"
    if d.exists():
        for f in d.iterdir():
            try:
                f.unlink() if f.is_file() else shutil.rmtree(f)
            except OSError:
                pass

"""一键签 · 搬到 NAS（桌面版用）

把这台电脑上的东西打成一个 zip 传给 NAS 版的 /api/import：
- 手机配对记录：问本机 usbmuxd 要（Mac / Windows 的 Apple 驱动都存着，和访达 / iTunes 同一份）。
  有了它 NAS 不用插数据线，手机在同一 Wi‑Fi 里就能被找到、装 App。
- Apple ID 登录状态：plumesign 的 accounts.json / state.plist / keys（是苹果给的令牌，不含密码）
- 一键签自己的数据：安装记录、软件源、证书、自己加的 IPA
用户只要填 NAS 地址点一下。
"""
from __future__ import annotations

import io
import json
import logging
import os
import plistlib
import socket
import struct
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from . import autopilot, core

log = logging.getLogger("onetap.nasmove")
PORT = 8686


# MARK: 本机 usbmuxd（只读配对记录，不改任何东西）

def _mux_sock() -> socket.socket:
    addr = os.environ.get("USBMUXD_SOCKET_ADDRESS")
    if addr and ":" in addr:
        host, port = addr.rsplit(":", 1)
        return socket.create_connection((host, int(port)), timeout=5)
    if sys.platform == "win32":
        return socket.create_connection(("127.0.0.1", 27015), timeout=5)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(addr or "/var/run/usbmuxd")
    return s


def _mux(req: dict) -> dict:
    body = plistlib.dumps({"ClientVersionString": "OneDuo", "ProgName": "OneDuo", **req})
    with _mux_sock() as s:
        s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
        hdr = b""
        while len(hdr) < 16:
            c = s.recv(16 - len(hdr))
            if not c:
                raise IOError("usbmuxd 断开了")
            hdr += c
        n = struct.unpack("<I", hdr[:4])[0] - 16
        data = b""
        while len(data) < n:
            c = s.recv(n - len(data))
            if not c:
                raise IOError("usbmuxd 断开了")
            data += c
    return plistlib.loads(data)


def pair_record(udid: str) -> Optional[bytes]:
    try:
        r = _mux({"MessageType": "ReadPairRecord", "PairRecordID": udid})
        return r.get("PairRecordData")
    except Exception as e:  # noqa: BLE001
        log.warning("read pair record %s: %s", udid, e)
        return None


def _udids() -> list[str]:
    ids = {r["udid"] for r in autopilot.load().values() if r.get("udid")}
    try:
        for d in core.list_devices():
            ids.add(d.udid)
    except Exception:  # noqa: BLE001
        pass
    return sorted(ids)


def _plume_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "PlumeImpactor"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "PlumeImpactor"


def build_bundle() -> tuple[bytes, dict]:
    buf = io.BytesIO()
    summary = {"devices": [], "apple_id": False, "installs": 0}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for u in _udids():
            rec = pair_record(u)
            if rec:
                z.writestr(f"lockdown/{u}.plist", rec)
                summary["devices"].append(u)
        pd = _plume_dir()
        for name in ("accounts.json", "state.plist"):
            if (pd / name).is_file():
                z.write(pd / name, f"plume/{name}")
                summary["apple_id"] = summary["apple_id"] or name == "accounts.json"
        if (pd / "keys").is_dir():
            for f in (pd / "keys").rglob("*"):
                if f.is_file():
                    z.write(f, "plume/" + f.relative_to(pd).as_posix())
        dd = core.data_dir()
        for name in ("installs.json", "sources.json", "certs.json", "prefs.json"):
            if (dd / name).is_file():
                z.write(dd / name, f"app/{name}")
        for sub in ("certs", "custom"):
            if (dd / sub).is_dir():
                for f in (dd / sub).rglob("*"):
                    if f.is_file():
                        z.write(f, "app/" + f.relative_to(dd).as_posix())
        summary["installs"] = len(autopilot.load())
    return buf.getvalue(), summary


def _base(addr: str) -> str:
    a = (addr or "").strip().rstrip("/")
    if not a:
        raise core.SignError("请填 NAS 地址，例如 192.168.1.10")
    if not a.startswith(("http://", "https://")):
        a = "http://" + a
    host = a.split("://", 1)[1]
    if ":" not in host.split("/")[0]:
        a += f":{PORT}"
    return a


def move(addr: str, password: str = "") -> dict:
    base = _base(addr)
    hdr = {"User-Agent": core.UA}
    if password:
        hdr["X-OneTap-Password"] = password
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/api/ping", headers=hdr), timeout=6) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise core.SignError(f"连不上 {base}：确认 NAS 上一键签已经启动、地址没填错（{str(e)[:80]}）")
    if not info.get("oneduo"):
        raise core.SignError("这个地址不是一键签 NAS 版")
    data, summary = build_bundle()
    if not summary["devices"]:
        raise core.SignError("没读到手机的配对记录：先用数据线把手机连到这台电脑、点「信任」，再搬")
    req = urllib.request.Request(base + "/api/import", data=data, method="POST",
                                 headers={**hdr, "Content-Type": "application/zip"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise core.SignError("NAS 设置了访问密码，填上密码再搬")
        raise core.SignError(f"NAS 返回错误 {e.code}")
    log.info("moved to %s: %s -> %s", base, summary, res)
    return {"ok": True, "url": base, "devices": len(summary["devices"]), "apple_id": summary["apple_id"],
            "installs": summary["installs"]}

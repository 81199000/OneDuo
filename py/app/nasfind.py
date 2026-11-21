"""一键签 · 找家里的 NAS（局域网扫描，不用 mDNS 库）

同网段 /24 并发探测：
- 8686/api/ping 是一键签 NAS 版 → 已经装好了
- 5000 / 5001 有群晖 webapi → 群晖（读型号）
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _local_nets() -> list[ipaddress.IPv4Network]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 53))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    nets = []
    for ip in ips:
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_loopback and not str(a).startswith(("198.18.", "169.254.")):
            nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
    return list(dict.fromkeys(nets))


def _open(port: int, host: str, t: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=t):
            return True
    except OSError:
        return False


def _get(url: str, t: float = 2) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(url, timeout=t, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _probe(host: str):
    out = None
    if _open(8686, host):
        try:
            if _get(f"http://{host}:8686/api/ping").get("oneduo"):
                out = {"host": host, "kind": "oneduo", "name": "一键签 NAS 版（已安装）"}
        except Exception:  # noqa: BLE001
            pass
    for scheme, port in (("http", 5000), ("https", 5001)):
        if not _open(port, host):
            continue
        try:
            r = _get(f"{scheme}://{host}:{port}/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query&query=SYNO.API.Auth,SYNO.Docker.Project")
            if r.get("success"):
                d = r.get("data") or {}
                rec = {"host": host, "kind": "synology", "name": "群晖 NAS",
                       "docker": "SYNO.Docker.Project" in d, "installed": bool(out)}
                return rec
        except Exception:  # noqa: BLE001
            pass
    return out


def scan() -> list[dict]:
    hosts = [str(h) for n in _local_nets() for h in n.hosts()]
    found = []
    with ThreadPoolExecutor(64) as ex:
        for r in ex.map(_probe, hosts):
            if r:
                found.append(r)
    return found

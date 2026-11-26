"""一键签 · 装 GuoDuo TV 到家里的安卓电视（和手机版 GuoDuo「安装到电视」同一套做法）

电脑直连电视的 adbd（TCP 5555，电视上要打开 ADB / 网络调试），不需要装 adb：
CNXN → AUTH（RSA 签名，首次电视上弹「允许调试」）→ exec:cmd package install 流式推 APK，
老电视没有 cmd 就走 sync: 推到 /data/local/tmp 再 pm install。

- 不加第三方依赖：RSA 用纯 Python（生成一次存 data_dir/adbkey，2048 位）
- 安装包：dl.oneduo.app/guoduo/tv/latest.json（带 sha256，校验过才装）
- 找电视：同网段 /24 并发探 5555；顺带探 8765（已装 GuoDuo TV 的会回 /api/pair/info）
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import struct
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import core
from .nasfind import _local_nets, _open

MANIFEST = "https://dl.oneduo.app/guoduo/tv/latest.json"
PACKAGE = "me.rrmm.guoduo.tv"


class TvError(RuntimeError):
    pass


# MARK: - 找电视

def _probe(host: str):
    if not _open(5555, host, 0.6):
        return None
    rec = {"host": host, "port": 5555, "name": "安卓电视", "installed": ""}
    if _open(8765, host, 0.4):
        try:
            with urllib.request.urlopen(f"http://{host}:8765/api/pair/info", timeout=1.5) as r:
                info = json.loads(r.read().decode("utf-8", "replace"))
            if info.get("app") == "guoduo-tv":
                rec["name"] = info.get("name") or rec["name"]
                rec["installed"] = info.get("version") or "已安装"
        except Exception:  # noqa: BLE001
            pass
    return rec


def scan() -> list[dict]:
    hosts = [str(h) for n in _local_nets() for h in n.hosts()]
    with ThreadPoolExecutor(128) as ex:
        return [r for r in ex.map(_probe, hosts) if r]


# MARK: - RSA（纯 Python，只用于 ADB 授权）

def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(40):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, e: int) -> int:
    while True:
        p = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if p % e != 1 and _is_prime(p):
            return p


def _key() -> tuple[int, int, int]:
    """(n, e, d)；第一次生成后存 data_dir/adbkey.json，电视只认同一把钥匙（免得每次都要点允许）"""
    f = core.data_dir() / "adbkey.json"
    try:
        k = json.loads(f.read_text("utf-8"))
        return int(k["n"], 16), int(k["e"]), int(k["d"], 16)
    except Exception:  # noqa: BLE001
        pass
    e = 65537
    while True:
        p, q = _prime(1024, e), _prime(1024, e)
        n = p * q
        if p != q and n.bit_length() == 2048:
            break
    d = pow(e, -1, (p - 1) * (q - 1))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"n": format(n, "x"), "e": e, "d": format(d, "x")}), "utf-8")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return n, e, d


# adbd 把 20 字节 token 当成 SHA-1 摘要：PKCS#1 v1.5 签名 = DigestInfo(SHA1) + token
_SHA1_PREFIX = bytes.fromhex("3021300906052b0e03021a05000414")


def _sign(token: bytes) -> bytes:
    n, _, d = _key()
    k = 256
    t = _SHA1_PREFIX + token
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def _pubkey_payload() -> bytes:
    """Android RSAPublicKey：len / n0inv / n[64] / rr[64] / e，全小端，base64 后加「 名字\\0」"""
    n, e, _ = _key()
    words = 64
    r32 = 1 << 32
    n0inv = (-pow(n % r32, -1, r32)) % r32
    rr = pow(2, 4096, n)
    blob = struct.pack("<II", words, n0inv)
    blob += b"".join(struct.pack("<I", (n >> (32 * i)) & 0xFFFFFFFF) for i in range(words))
    blob += b"".join(struct.pack("<I", (rr >> (32 * i)) & 0xFFFFFFFF) for i in range(words))
    blob += struct.pack("<I", e)
    return base64.b64encode(blob) + b" oneduo@desktop\x00"


# MARK: - ADB 协议

CNXN, AUTH, OPEN, OKAY, CLSE, WRTE = (0x4E584E43, 0x48545541, 0x4E45504F, 0x59414B4F, 0x45534C43, 0x45545257)


class Adb:
    def __init__(self, host: str, port: int = 5555):
        self.host, self.port = host, port
        self.sock: socket.socket | None = None
        self.max_data = 4096
        self.features: set[str] = set()
        self.model = ""
        self._local = 0

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _send(self, cmd, a0, a1, data=b""):
        assert self.sock is not None
        hdr = struct.pack("<6I", cmd, a0, a1, len(data), sum(data) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF)
        self.sock.sendall(hdr + data)

    def _recv(self, n):
        assert self.sock is not None
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise TvError("电视断开了连接")
            buf += c
        return buf

    def _read(self):
        cmd, a0, a1, ln, _, magic = struct.unpack("<6I", self._recv(24))
        if magic != cmd ^ 0xFFFFFFFF or ln > 16 << 20:
            raise TvError("电视返回了无法识别的数据，可能不是 ADB 端口")
        return cmd, a0, a1, self._recv(ln) if ln else b""

    def connect(self, on_approval=lambda: None):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=8)
        except OSError:
            raise TvError(f"连不上 {self.host}：确认电视开了 ADB 调试、和电脑在同一个网络") from None
        self._send(CNXN, 0x01000001, 256 * 1024, b"host::\x00")
        signed = key_sent = False
        while True:
            try:
                cmd, a0, a1, data = self._read()
            except socket.timeout:
                raise TvError("等电视授权超时：电视上要点「允许」（勾上「一律允许」下次就不用再点）") from None
            if cmd == CNXN:
                self.max_data = max(4096, min(a1, 1 << 20))
                self._banner(data.decode("utf-8", "replace"))
                self.sock.settimeout(30)
                return
            if cmd == AUTH and a0 == 1:
                if not signed:
                    signed = True
                    self._send(AUTH, 2, 0, _sign(data))
                elif not key_sent:
                    key_sent = True
                    self._send(AUTH, 3, 0, _pubkey_payload())
                    on_approval()
                    self.sock.settimeout(90)
                else:
                    raise TvError("电视拒绝了这台电脑的授权，请在电视上选「允许」后重试")
            elif cmd == CLSE:
                raise TvError("电视断开了连接")

    def _banner(self, s: str):
        body = s.split(":", 2)[-1]
        for part in body.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            if k == "ro.product.model":
                self.model = v.strip("\x00")
            elif k == "features":
                self.features = set(v.strip("\x00").split(","))

    # 流
    def _open(self, service: str):
        self._local += 1
        local = self._local
        self._send(OPEN, local, 0, service.encode() + b"\x00")
        while True:
            cmd, a0, a1, _ = self._read()
            if cmd == OKAY and a1 == local:
                return {"local": local, "remote": a0, "inbox": b"", "closed": False}
            if cmd == CLSE and a1 == local:
                raise TvError(f"电视拒绝了这个操作（{service[:24]}）")

    def _pump(self, s) -> bool:
        cmd, _, a1, data = self._read()
        if a1 != s["local"]:
            return False
        if cmd == OKAY:
            return True
        if cmd == WRTE:
            s["inbox"] += data
            self._send(OKAY, s["local"], s["remote"])
        elif cmd == CLSE:
            s["closed"] = True
        return False

    def _write(self, s, data: bytes):
        off = 0
        while off < len(data):
            chunk = data[off:off + self.max_data]
            self._send(WRTE, s["local"], s["remote"], chunk)
            while not self._pump(s):
                if s["closed"]:
                    raise TvError("closed")
            off += len(chunk)

    def _drain(self, s) -> str:
        while not s["closed"]:
            self._pump(s)
        return s["inbox"].decode("utf-8", "replace")

    def shell(self, cmd: str) -> str:
        return self._drain(self._open("shell:" + cmd))

    def install(self, apk: bytes, progress=lambda p: None) -> None:
        assert self.sock is not None
        self.sock.settimeout(180)
        if "cmd" in self.features:
            s = self._open(f"exec:cmd package install -r -S {len(apk)}")
            try:
                for off in range(0, len(apk), 64 * 1024):
                    self._write(s, apk[off:off + 64 * 1024])
                    progress(min(1.0, (off + 64 * 1024) / len(apk)))
                out = self._drain(s)
            except TvError:
                out = s["inbox"].decode("utf-8", "replace")
            if "Success" in out:
                return
            if "not found" not in out and "Can't find service" not in out:
                raise TvError(_friendly(out))
        path = "/data/local/tmp/guoduo-tv.apk"
        s = self._open("sync:")
        spec = f"{path},33206".encode()
        self._write(s, b"SEND" + struct.pack("<I", len(spec)) + spec)
        for off in range(0, len(apk), 64 * 1024):
            part = apk[off:off + 64 * 1024]
            self._write(s, b"DATA" + struct.pack("<I", len(part)) + part)
            progress(min(1.0, (off + len(part)) / len(apk)))
        self._write(s, b"DONE" + struct.pack("<I", int(time.time())))
        while len(s["inbox"]) < 8:
            if s["closed"]:
                raise TvError("把安装包推到电视失败")
            self._pump(s)
        if not s["inbox"].startswith(b"OKAY"):
            raise TvError("把安装包推到电视失败（电视存储空间不足或没有写入权限）")
        try:
            self._write(s, b"QUIT" + struct.pack("<I", 0))
        except TvError:
            pass
        out = self.shell(f"pm install -r {path}; rm -f {path}")
        if "Success" not in out:
            raise TvError(_friendly(out))


def _friendly(out: str) -> str:
    o = out.strip()
    if "UPDATE_INCOMPATIBLE" in o or "SIGNATURE" in o:
        return "电视上已装的 GuoDuo TV 签名不同，请先在电视上卸载旧版再安装"
    if "INSUFFICIENT_STORAGE" in o:
        return "电视存储空间不足"
    if "VERSION_DOWNGRADE" in o:
        return "电视上已经是更新的版本"
    if "NO_MATCHING_ABIS" in o:
        return "这个安装包不支持你的电视 CPU"
    if "OLDER_SDK" in o:
        return "电视系统太老，装不了这个版本"
    return "安装失败：" + o[:160] if o else "安装没有返回结果"


# MARK: - 安装包

def _manifest() -> dict:
    # 走 core 的请求（带一键签 UA + 随包证书）：dl.oneduo.app 前面的 Cloudflare 会拦 Python 默认 UA（403）
    try:
        return json.loads(core.http_get(f"{MANIFEST}?t={int(time.time())}", timeout=12).decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise TvError("获取电视版安装包信息失败，请检查网络") from None


def _apk(m: dict, progress) -> bytes:
    cache = core.data_dir() / "cache" / f"guoduo-tv-{m['version']}.apk"

    def ok(b: bytes) -> bool:
        if m.get("size") and len(b) != m["size"]:
            return False
        return hashlib.sha256(b).hexdigest() == m["sha256"].lower() if m.get("sha256") else len(b) > 100_000

    if cache.exists():
        b = cache.read_bytes()
        if ok(b):
            progress(1.0)
            return b
    try:
        req = urllib.request.Request(m["url"], headers={"User-Agent": core.UA})
        with urllib.request.urlopen(req, timeout=30, context=core.ssl_context()) as r:
            total = int(r.headers.get("Content-Length") or m.get("size") or 0)
            buf = bytearray()
            while True:
                c = r.read(256 * 1024)
                if not c:
                    break
                buf += c
                if total:
                    progress(min(1.0, len(buf) / total))
    except Exception:  # noqa: BLE001
        raise TvError("下载电视版安装包失败，请检查网络") from None
    b = bytes(buf)
    if not ok(b):
        raise TvError("安装包校验没通过，请重试")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b)
    return b


def install(host: str, port: int = 5555, step=lambda k, t: None, progress=lambda k, p: None) -> dict:
    """连接 → （首次）等电视点允许 → 查已装版本 → 下载校验 → 安装 → 拉起"""
    adb = Adb(host, port)
    try:
        step("connect", f"连接电视 {host}…")
        adb.connect(on_approval=lambda: step("approve", "电视上弹出了「允许调试」，用遥控器选「允许」（勾上「一律允许」）"))
        title = adb.model.replace("_", " ") or "安卓电视"
        before = ""
        out = adb.shell(f"dumpsys package {PACKAGE} | grep versionName")
        if "versionName=" in out:
            before = out.split("versionName=", 1)[1].split()[0]
        m = _manifest()
        step("download", f"下载 GuoDuo TV {m['version']}…")
        apk = _apk(m, lambda p: progress("download", p))
        step("install", f"装到 {title}…")
        adb.install(apk, lambda p: progress("install", p))
        step("launch", "打开 GuoDuo TV…")
        launched = "Events injected" in adb.shell(f"monkey -p {PACKAGE} -c android.intent.category.LEANBACK_LAUNCHER 1")
        core.set_pref("tv_addr", host)
        return {"ok": True, "tv": title, "version": m["version"], "before": before, "launched": launched}
    finally:
        adb.close()

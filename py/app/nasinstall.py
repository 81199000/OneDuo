"""一键签 · 装到 NAS（桌面版用，全自动）

用户只填 NAS 地址 + 账号密码，这里自动：
  登录 → 检查 Container Manager → 上传镜像（电脑先从 dl.oneduo.app 下载，NAS 不用连 Docker Hub）
  → 导入 → 建项目（host 网络）→ 构建启动 → 等网页起来 → 交给 nasmove 搬手机

目前支持：群晖 DSM 7.2+（Container Manager 的网页接口，和 Container Manager 界面同一套）。
别的 NAS 以后加 SSH 方式。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Optional

from . import core

log = logging.getLogger("onetap.nasinstall")

APP_PORT = 8686
PROJECT = "oneduo"
IMAGE_REPO = "oneduo-nas"


class NeedOTP(core.SignError):
    """账号开了两步验证：界面弹框要 6 位验证码后再调一次 install(..., otp=…)"""


COMPOSE = """# 一键签 NAS 版（由电脑版一键签自动创建）
services:
  oneduo:
    image: {image}
    container_name: oneduo
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./data:/data
    environment:
      - ONETAP_PORT={port}
"""


def _ctx() -> ssl.SSLContext:
    # NAS 的 5001 基本都是自签证书；只在局域网里用，跳过校验
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


class Synology:
    def __init__(self, host: str, port: Optional[int] = None, https: Optional[bool] = None):
        self.host = host.strip()
        self.jar = CookieJar()
        self.sid = ""
        self.syno_token = ""
        self.base = ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.HTTPSHandler(context=_ctx()))
        cands = []
        if port:
            cands = [("https" if https else "http", port)] if https is not None else [("http", port), ("https", port)]
        else:
            cands = [("http", 5000), ("https", 5001)]
        last = None
        for scheme, p in cands:
            base = f"{scheme}://{self.host}:{p}"
            try:
                info = self._raw(base, "query.cgi", {"api": "SYNO.API.Info", "version": "1", "method": "query",
                                                    "query": "SYNO.API.Auth,SYNO.Docker.Project"}, timeout=6)
                if info.get("success"):
                    self.base = base
                    self.apis = info["data"]
                    break
            except Exception as e:  # noqa: BLE001
                last = e
        if not self.base:
            raise core.SignError(f"连不上 {self.host} 的群晖管理页面（5000/5001）：{last}")

    def _raw(self, base, path, params, timeout: float = 30, data=None, headers=None):
        url = f"{base}/webapi/{path}"
        if data is None:
            req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=headers or {})
        else:
            req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), data=data, headers=headers or {}, method="POST")
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def call(self, api: str, method: str, version: int = 1, timeout: float = 60, **params):
        q = {"api": api, "method": method, "version": str(version)}
        body = {k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)) for k, v in params.items()}
        hdr = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.syno_token:
            hdr["X-SYNO-TOKEN"] = self.syno_token
        if self.sid:
            body["_sid"] = self.sid
        return self._raw(self.base, "entry.cgi", q, timeout=timeout, data=urllib.parse.urlencode(body).encode(), headers=hdr)

    def stream(self, api: str, method: str, timeout: float = 600, **params) -> str:
        """*_stream 接口返回的是纯文本日志（docker compose 输出），读完整段"""
        q = {"api": api, "method": method, "version": "1"}
        body = {k: json.dumps(v) for k, v in params.items()}
        body["_sid"] = self.sid
        req = urllib.request.Request(f"{self.base}/webapi/entry.cgi?" + urllib.parse.urlencode(q),
                                     data=urllib.parse.urlencode(body).encode(), method="POST",
                                     headers={"X-SYNO-TOKEN": self.syno_token, "Content-Type": "application/x-www-form-urlencoded"})
        with self.opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    # MARK: 登录

    def login(self, user: str, password: str, otp: str = "") -> None:
        p = {"api": "SYNO.API.Auth", "version": "7", "method": "login", "account": user, "passwd": password,
             "session": "OneDuoInstaller", "format": "sid", "enable_syno_token": "yes"}
        if otp:
            p["otp_code"] = otp
        r = self._raw(self.base, "entry.cgi", {"api": "SYNO.API.Auth", "version": "7", "method": "login"},
                      data=urllib.parse.urlencode(p).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        if not r.get("success"):
            code = (r.get("error") or {}).get("code")
            if code in (403, 406):
                raise NeedOTP("这个账号开了两步验证，请输入验证器上的 6 位验证码")
            if code == 404:
                raise core.SignError("两步验证码不对")
            if code == 400:
                raise core.SignError("NAS 账号或密码不对")
            if code in (401, 402):
                raise core.SignError("这个账号被停用了，或没有登录权限")
            if code == 407:
                raise core.SignError("登录失败次数太多，NAS 暂时封了这台电脑的 IP，过一会儿再试")
            raise core.SignError(f"NAS 登录失败（错误码 {code}）")
        self.sid = r["data"]["sid"]
        self.syno_token = r["data"].get("synotoken", "")

    def logout(self):
        try:
            self._raw(self.base, "entry.cgi", {"api": "SYNO.API.Auth", "version": "7", "method": "logout",
                                              "session": "OneDuoInstaller", "_sid": self.sid}, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # MARK: 检查

    def model(self) -> str:
        try:
            r = self.call("SYNO.Core.System", "info", 1)
            return (r.get("data") or {}).get("model", "")
        except Exception:  # noqa: BLE001
            return ""

    def check_docker(self) -> None:
        if "SYNO.Docker.Project" not in self.apis:
            raise core.SignError("这台 NAS 没装 Container Manager：打开套件中心，搜「Container Manager」安装后再试。"
                                 "（如果套件中心里没有，说明这个型号不支持 Docker）")
        r = self.call("SYNO.Docker.Project", "list")
        if not r.get("success"):
            code = (r.get("error") or {}).get("code")
            if code in (105, 119):
                raise core.SignError("这个 NAS 账号没有管理员权限，请用管理员账号登录")
            raise core.SignError(f"Container Manager 没有响应（错误码 {code}），确认它已经启动")

    def projects(self) -> dict:
        r = self.call("SYNO.Docker.Project", "list")
        return (r.get("data") or {}) if r.get("success") else {}

    def shares(self) -> list[dict]:
        r = self.call("SYNO.Core.Share", "list", 1, additional=["vol_path"])
        return ((r.get("data") or {}).get("shares") or []) if r.get("success") else []

    # MARK: 文件

    def mkdir(self, parent: str, name: str):
        self.call("SYNO.FileStation.CreateFolder", "create", 2, folder_path=json.dumps([parent]),
                  name=json.dumps([name]), force_parent="true")

    def upload(self, dest_dir: str, filename: str, data_path: Path, progress: Optional[Callable[[float], None]] = None):
        """FileStation 上传（multipart），流式发送大文件"""
        boundary = "----OneDuo" + secrets.token_hex(12)
        fields = {"path": dest_dir, "create_parents": "true", "overwrite": "true"}
        head = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = data_path.stat().st_size
        total = len(head) + size + len(tail)

        class Body:
            def __init__(self):
                self.f = open(data_path, "rb")
                self.stage = 0
                self.sent = 0

            def read(self, n=-1):
                if self.stage == 0:
                    self.stage = 1
                    return head
                if self.stage == 1:
                    b = self.f.read(1 << 20)
                    if b:
                        self.sent += len(b)
                        if progress:
                            progress(self.sent / size)
                        return b
                    self.stage = 2
                    self.f.close()
                    return tail
                return b""

        q = {"api": "SYNO.FileStation.Upload", "method": "upload", "version": "2", "_sid": self.sid}
        req = urllib.request.Request(f"{self.base}/webapi/entry.cgi?" + urllib.parse.urlencode(q), data=Body(), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                              "Content-Length": str(total), "X-SYNO-TOKEN": self.syno_token})
        with self.opener.open(req, timeout=1800) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if not res.get("success"):
            raise core.SignError(f"上传到 NAS 失败（错误码 {(res.get('error') or {}).get('code')}）")

    def delete_file(self, path: str):
        try:
            self.call("SYNO.FileStation.Delete", "start", 2, path=json.dumps([path]), recursive="true")
        except Exception:  # noqa: BLE001
            pass


def _pick_share(nas: Synology) -> str:
    """项目放哪个共享文件夹：优先 docker（Container Manager 装好会自动建），没有就第一个"""
    names = [s.get("name") for s in nas.shares()]
    for want in ("docker", "Docker"):
        if want in names:
            return "/" + want
    if names:
        return "/" + names[0]
    raise core.SignError("NAS 上没有共享文件夹：先在「控制面板 → 共享文件夹」建一个叫 docker 的")


def image_file(progress: Callable[[float], None]) -> tuple[Path, str]:
    """NAS 版镜像（docker save 出来的 tar）。先看本地缓存，没有就从 dl.oneduo.app 下载。返回 (路径, 镜像名:版本)"""
    local = os.environ.get("ONETAP_NAS_IMAGE")      # 开发测试：直接用本地打好的 tar
    if local:
        return Path(local), os.environ.get("ONETAP_NAS_IMAGE_TAG", f"{IMAGE_REPO}:latest")
    raise core.SignError("NAS 版镜像还没发布")


def install(addr: str, user: str, password: str, otp: str = "",
            step: Callable[[str, str], None] = lambda k, t: None,
            progress: Callable[[float], None] = lambda p: None) -> dict:
    """step(key, 文字)：界面显示到哪一步了。返回 {"url": NAS 版网页地址, ...}"""
    host = addr.strip().replace("http://", "").replace("https://", "").split("/")[0]
    port = None
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)
    step("connect", "连接 NAS…")
    nas = Synology(host, port)
    step("login", "登录 NAS…")
    nas.login(user, password, otp)
    try:
        model = nas.model()
        step("check", f"检查 Container Manager…（{model or '群晖'}）")
        nas.check_docker()
        share = _pick_share(nas)
        proj_dir = f"{share}/{PROJECT}"
        step("image", "准备安装包…")
        tar, tag = image_file(progress)
        step("upload", "上传到 NAS…")
        nas.mkdir(share, PROJECT)
        nas.mkdir(proj_dir, "data")         # 群晖不会自动建 bind mount 的目录，没有就 Bind mount failed
        nas.upload(proj_dir, "oneduo-nas.tar", tar, progress)
        step("import", "导入…")
        r = nas.call("SYNO.Docker.Image", "import", 1, timeout=600, path=f"{proj_dir}/oneduo-nas.tar")
        if not r.get("success"):
            raise core.SignError(f"导入镜像失败（错误码 {(r.get('error') or {}).get('code')}）")
        nas.delete_file(f"{proj_dir}/oneduo-nas.tar")
        step("project", "创建并启动…")
        existing = {p.get("name"): pid for pid, p in nas.projects().items()}
        content = COMPOSE.format(image=tag, port=APP_PORT)
        if PROJECT in existing:
            pid = existing[PROJECT]
            nas.call("SYNO.Docker.Project", "stop", 1, timeout=120, id=pid)
            r = nas.call("SYNO.Docker.Project", "update", 1, id=pid, content=content)
        else:
            r = nas.call("SYNO.Docker.Project", "create", 1, name=PROJECT, share_path=proj_dir, content=content,
                         enable_service_portal=False)
            pid = (r.get("data") or {}).get("id")
        if not r.get("success") or not pid:
            raise core.SignError(f"创建项目失败（错误码 {(r.get('error') or {}).get('code')}）")
        out = nas.stream("SYNO.Docker.Project", "build_stream", timeout=600, id=pid)
        log.info("build_stream tail: %s", out[-400:])
        if "Exit Code: 0" not in out and "Started" not in out and "Running" not in out:
            last = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
            raise core.SignError("启动失败：" + " / ".join(last)[-200:])
        step("wait", "等一键签 NAS 版启动…")
        url = f"http://{host}:{APP_PORT}"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url + "/api/ping", timeout=3) as resp:
                    if json.loads(resp.read().decode()).get("oneduo"):
                        return {"ok": True, "url": url, "model": model, "project": pid}
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        raise core.SignError("NAS 版装好了但没启动起来，到 Container Manager → 项目 → oneduo 看日志")
    finally:
        nas.logout()

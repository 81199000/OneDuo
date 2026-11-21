# PyInstaller 打包：macOS 出 一键签.app，Windows 出 一键签.exe（同一份 spec）
#   cd py && .venv/bin/python -m PyInstaller packaging/OneTap.spec --noconfirm
# 随包：网页界面、内置应用列表（离线兜底）、plumesign 签名组件
import sys
from pathlib import Path

import re as _re
ROOT = Path(SPECPATH).parent          # py/
VERSION = _re.search(r'^VERSION = "([^"]+)"', (ROOT / "app" / "core.py").read_text(encoding="utf-8"), _re.M).group(1)
REPO = ROOT.parent                    # 仓库根
exe_name = "plumesign.exe" if sys.platform == "win32" else "plumesign"
plume = REPO / "target" / "release" / exe_name
# Mac 出通用包（Apple 芯片 + Intel）：build_mac.sh 用 lipo 合好放在 target/universal/
import os as _os
TARGET_ARCH = _os.environ.get("ONETAP_TARGET_ARCH") or None
if sys.platform == "darwin" and TARGET_ARCH == "universal2":
    plume = REPO / "target" / "universal" / exe_name

datas = [
    (str(ROOT / "app" / "web"), "app/web"),
    (str(REPO / "catalog" / "apps.json"), "catalog"),
    (str(REPO / "catalog" / "icons"), "catalog/icons"),
]
binaries = [(str(plume), "bin")]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["app.webui", "app.core", "app.sources", "app.certs", "app.autopilot", "app.selfupdate", "app.nasmove"],
    excludes=["tkinter", "PIL", "app.ui", "app.server"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OneTap", console=False,
              argv_emulation=False, target_arch=TARGET_ARCH)
    coll = COLLECT(exe, a.binaries, a.datas, name="OneTap")
    app = BUNDLE(
        coll,
        name="一键签.app",
        icon=str(ROOT / "packaging" / "OneTap.icns"),
        bundle_identifier="me.rrmm.onetapinstall",
        info_plist={
            "CFBundleDisplayName": "一键签",
            "CFBundleName": "一键签",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
else:
    # Windows：onedir（启动快，不用每次解压到临时目录），Inno Setup 再打成安装包
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="一键签", console=False,
              icon=str(ROOT / "packaging" / "OneTap.ico"), version=None)
    coll = COLLECT(exe, a.binaries, a.datas, name="一键签")

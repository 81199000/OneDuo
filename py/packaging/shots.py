"""【开发用】给教程截图：打开真界面，摆好各个状态，用 WKWebView 自己的快照接口存 PNG（不需要屏幕录制权限）。
   cd py && .venv/bin/python packaging/shots.py <输出目录>
只在 macOS 上跑。界面数据是真实的（真列表、真版本号），手机名 / 账号打码成示例。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import webview  # noqa: E402

from app import webui  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots").resolve()
OUT.mkdir(parents=True, exist_ok=True)

api = webui.Api()
html = Path(__file__).resolve().parent.parent / "app" / "web" / "index.html"
win = webview.create_window("一键签 0.1.0", url=str(html), js_api=api, width=900, height=640, background_color="#111214")
api._window = win


def snap(name: str):
    """WKWebView.takeSnapshot → PNG（Retina 2x）"""
    import AppKit
    import WebKit
    done = threading.Event()
    holder = {}

    def run():
        wv = None
        stack = [win.native.contentView()]
        while stack and wv is None:
            v = stack.pop()
            if isinstance(v, WebKit.WKWebView):
                wv = v
            else:
                stack.extend(v.subviews() or [])
        if wv is None:
            holder["err"] = "no WKWebView"; done.set(); return
        cfg = WebKit.WKSnapshotConfiguration.alloc().init()

        def cb(img, err):
            if img is not None:
                rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
                holder["png"] = bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))
            holder["err"] = err
            done.set()
        wv.takeSnapshotWithConfiguration_completionHandler_(cfg, cb)

    from PyObjCTools import AppHelper
    AppHelper.callAfter(run)
    done.wait(10)
    if "png" in holder:
        (OUT / f"{name}.png").write_bytes(holder["png"])
        print("saved", name, len(holder["png"]), flush=True)
    else:
        print("FAILED", name, holder.get("err"), flush=True)


def J(js):
    return win.evaluate_js(js)


# 截图里不出现真实手机名 / 账号
FAKE = """
(() => {
  const A = window.pywebview.api;
  window.__fakeDevices = [{udid:'demo', name:'我的 iPhone', via:'USB'}];
  A.devices = async () => window.__fakeDevices;
  A.accounts = async () => window.__fakeAccounts || [];
  A.installs = async () => ({items: window.__fakeInstalls || [], enabled: true});
  A.install = () => {};
  A.autostart = async () => true;
  return 1;
})()
"""


def scene():
    print("scene start", flush=True)
    time.sleep(9)  # 等列表和版本号
    print("native:", type(win.native), flush=True)
    print("inject fake", flush=True)
    J(FAKE)
    print("poll", flush=True)
    J("window.__fakeAccounts = []; S.custom = []; renderList(); pollDevices(); 1")
    print("snap1", flush=True)
    time.sleep(3)
    snap("01-home")
    # 选 GuoDuo，手机已连接
    J("select('guoduo')")
    time.sleep(1)
    snap("02-app-selected")
    # 登录框
    J("window.onEvent('need_login'); document.getElementById('user').value='example@icloud.com'; document.getElementById('pw').value='********'")
    time.sleep(0.6)
    snap("03-login")
    J("document.getElementById('loginMask').classList.remove('show')")
    # 验证码框（两个号码）
    J("window.onEvent('need_2fa', {kind:'device', phones:[{id:1,tail:'88'}]})")
    time.sleep(0.6)
    snap("04-2fa")
    J("clearInterval(codeTimer); document.getElementById('codeMask').classList.remove('show')")
    # 安装中
    J("""S.busy=true; renderDetail(); document.getElementById('go').disabled=true; document.getElementById('go').textContent='安装中…';
        setStep('device','ok','我的 iPhone'); setStep('account','ok','example@icloud.com'); setStep('download','ok','v' + (allApps().find(a=>a.id==='guoduo').version||''));
        setStep('install','run'); msg('正在签名并安装到手机，请保持手机解锁、数据线不要拔…');
        const b=document.querySelector('.bar'); b.style.display='block'; b.firstChild.style.width='62%';""")
    time.sleep(0.6)
    snap("05-installing")
    # 完成
    J("setStep('install','ok'); finish(true)")
    time.sleep(0.6)
    snap("06-done")
    # 我的手机
    now = time.time()
    J(f"""window.__fakeAccounts=['example@icloud.com']; S.busy=false;
        window.__fakeInstalls=[
          {{key:'a',name:'GuoDuo',device:'我的 iPhone',version:'0.6.4',days_left:6.2,expires_at:{now + 6.2 * 86400},auto:true,error:'',latest:'0.6.4',running:false,app_id:'guoduo'}},
          {{key:'b',name:'DouDuo',device:'我的 iPhone',version:'1.6.3',days_left:1.4,expires_at:{now + 1.4 * 86400},auto:true,error:'',latest:'1.6.4',running:false,app_id:'douduo'}}];
        S.installs=window.__fakeInstalls; S.autostart=true; showTab('mine');""")
    time.sleep(0.8)
    snap("07-my-phones")
    # 添加自己的 IPA
    J("showTab('apps')")
    time.sleep(0.5)
    J("document.getElementById('drop').classList.add('show')")
    time.sleep(0.4)
    snap("08-drop-ipa")
    win.destroy()


threading.Thread(target=scene, daemon=True).start()
webview.start()

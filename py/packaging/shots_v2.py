"""【开发用】0.2.0 新功能的教程截图：软件源 + 证书签名。

pywebview 窗口在开着台前调度时截不到（被缩成缩略图），这里改用 Playwright 的 WebKit 渲染同一个
index.html，后面接真的 webui.Api（真源、真版本号）；手机名 / 账号 / 证书信息换成示例，不出现真实数据。

  cd py && ONETAP_DATA_DIR=<临时目录> python3 packaging/shots_v2.py ../docs/tutorial
需要：pip install playwright && playwright install webkit
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import webui  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots").resolve()
OUT.mkdir(parents=True, exist_ok=True)
HTML = Path(__file__).resolve().parent.parent / "app" / "web" / "index.html"

NOW = time.time()
DEMO_CERT = {"id": "c-demo", "name": "Apple Development: Example Co (ABCDE12345)", "team": "ABCDE12345",
             "team_name": "示例科技有限公司", "cert_expires": NOW + 300 * 86400, "prov_name": "Example Wildcard",
             "prov_expires": NOW + 300 * 86400, "app_id": "ABCDE12345.*", "all_devices": False,
             "device_count": 12, "expires_at": NOW + 300 * 86400, "wildcard": True}

# 截图里不出现真实手机名 / 账号 / 证书
FAKE = """
(() => {
  const A = window.pywebview.api;
  window.__fakeDevices = [{udid:'00008150-0000DEMO0000001C', name:'我的 iPhone', via:'USB'}];
  A.devices = async () => window.__fakeDevices;
  A.accounts = async () => ['example@icloud.com'];
  A.install = () => {};
  A.installs = async () => ({items: [], enabled: true});
  A.autostart = async () => true;
  A.add_source = () => {};
  window.__sign = { mode: 'apple_id', certs: [] };
  A.sign_modes = async () => window.__sign;
  A.set_sign_mode = async (m) => { window.__sign.mode = m; return window.__sign; };
  A.pick_file = async (k) => k === 'p12' ? '/Users/me/Downloads/证书.p12' : '/Users/me/Downloads/描述文件.mobileprovision';
  return 1;
})()
"""


async def main():
    api = webui.Api()
    api._pilot.enabled = False
    loop = asyncio.get_running_loop()
    async with async_playwright() as p:
        browser = await p.webkit.launch()
        page = await browser.new_page(viewport={"width": 900, "height": 640}, device_scale_factor=2)
        page.on("pageerror", lambda e: print("  [pageerror]", e))

        class Win:
            def evaluate_js(self, js):
                asyncio.run_coroutine_threadsafe(page.evaluate(js), loop).result(timeout=10)

            def create_file_dialog(self, *a, **k):
                return None
        api._window = Win()

        async def call(_src, name, args):
            return await loop.run_in_executor(None, lambda: getattr(api, name)(*args))

        await page.expose_binding("__pyapi", call)
        # 覆盖表优先，没覆盖的方法转给真 Api（Proxy 本身不能直接赋值）
        await page.add_init_script(
            "window.__ov = {}; window.pywebview = { api: new Proxy({}, {"
            " get: (_, n) => window.__ov[n] || ((...a) => window.__pyapi(n, a)),"
            " set: (_, n, v) => { window.__ov[n] = v; return true; } }) };")
        await page.add_init_script(FAKE.strip().removeprefix("(") .removesuffix(")()") and f"window.addEventListener('DOMContentLoaded', () => ({FAKE.strip()}))")
        await page.goto(HTML.as_uri())
        await page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
        await page.wait_for_function("document.querySelectorAll('.item').length >= 1", timeout=60000)
        await page.wait_for_timeout(5000)
        J = page.evaluate

        async def snap(name):
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(OUT / f"{name}.png"))
            print("saved", name, flush=True)

        await J("S.custom = []; renderList(); 1")
        await page.wait_for_timeout(2500)          # 等 pollDevices 把示例手机显示出来
        await snap("01-home")

        # ---- 原教程 02–08（只有 DouDuo）----
        await J("select('douduo'); 1")
        await snap("02-app-selected")
        await J("window.onEvent('need_login'); $('user').value='example@icloud.com'; $('pw').value='********'; 1")
        await snap("03-login")
        await J("$('loginMask').classList.remove('show'); window.onEvent('need_2fa', {kind:'device', phones:[{id:1,tail:'88'}]}); 1")
        await snap("04-2fa")
        await J("""clearInterval(codeTimer); $('codeMask').classList.remove('show');
            S.busy=true; renderDetail(); $('go').disabled=true; $('go').textContent='安装中…';
            setStep('device','ok','我的 iPhone'); setStep('account','ok','example@icloud.com');
            setStep('download','ok','v' + (allApps().find(a=>a.id==='douduo').version||''));
            setStep('install','run'); msg('正在签名并安装到手机，请保持手机解锁、数据线不要拔…');
            const b=document.querySelector('.bar'); b.style.display='block'; b.firstChild.style.width='62%'; 1""")
        await snap("05-installing")
        await J("setStep('install','ok'); finish(true); 1")
        await snap("06-done")
        ver = await J("allApps().find(a=>a.id==='douduo').version || '1.6.4'")
        await J(f"""S.busy=false; S.installs=[
            {{key:'a',name:'DouDuo',device:'我的 iPhone',version:'{ver}',days_left:6.2,expires_at:{NOW + 6.2 * 86400},auto:true,error:'',latest:'{ver}',running:false,app_id:'douduo',sign_mode:'apple_id'}},
            {{key:'b',name:'DouDuo',device:'我的 iPad',version:'{ver}',days_left:1.4,expires_at:{NOW + 1.4 * 86400},auto:true,error:'',latest:'{ver}',running:false,app_id:'douduo',sign_mode:'apple_id'}}];
            S.autostart=true; showTab('mine'); 1""")
        await snap("07-my-phones")
        await J("showTab('apps'); S.sel=null; renderList(); $('detail').innerHTML='<div class=\"empty\">← 选一个 App</div>'; $('drop').classList.add('show'); 1")
        await snap("08-drop-ipa")
        await J("$('drop').classList.remove('show'); 1")

        # 软件源与证书页（只有官方源）
        await J("showTab('set')")
        await snap("09-sources")
        # 正在添加一个源：输入框里有地址
        await page.fill("#srcUrl", "https://example.com/apps/source.json")
        await snap("10-add-source")

        # 导入证书弹窗：选好文件、填好密码的样子
        await J("""openCert(); S.cert = {p12: '/Users/me/Downloads/证书.p12', prov: '/Users/me/Downloads/描述文件.mobileprovision'};
                   $('certP12').textContent = '证书.p12'; $('certP12').className = 'filebtn set';
                   $('certProv').textContent = '描述文件.mobileprovision'; $('certProv').className = 'filebtn set';
                   $('certPw').value = '********'; 1""")
        await snap("11-import-cert")
        await J("closeCert(); 1")

        # 导入成功、选中证书后的签名方式
        await J(f"window.__sign = {{ mode: 'c-demo', certs: [{__import__('json').dumps(DEMO_CERT, ensure_ascii=False)}] }}; "
                "S.sign = window.__sign; setSlogan(); renderSet(); 1")
        await snap("12-cert-selected")

        # 用证书装 App 的详情页
        await J("showTab('apps'); select('douduo'); 1")
        await snap("13-install-with-cert")
        await browser.close()


asyncio.run(main())

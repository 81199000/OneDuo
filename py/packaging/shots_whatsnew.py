"""【开发用】「更新内容」功能截图。

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
        await page.wait_for_timeout(2500)
        v = await J("(allApps().find(a=>a.id==='douduo')||{}).version")
        # ① 详情页：更新内容（默认收起 → 展开）
        await J("select('douduo'); 1")
        await snap("14-whatsnew-collapsed")
        await J("document.querySelector('.whatsnew').click(); 1")
        await snap("15-whatsnew-open")
        await J("$('updLater').click(); 1")
        # ② 我的手机：装的是旧版、源里有新版 → 「看更新内容」
        await J("""S.installs = [{key:'k1', name:'DouDuo', device:'我的 iPhone', version:'1.6.4', latest:'%s',
            days_left:5.2, expires_at:Date.now()/1000+5.2*86400, auto:true, error:'', running:false,
            app_id:'douduo', sign_mode:'apple_id'}]; showTab('mine'); renderMine(); 1""" % v)
        await snap("16-mine-has-update")
        await J("document.querySelector('.updlink').click(); 1")
        await snap("17-whatsnew-dialog")
        await browser.close()


asyncio.run(main())

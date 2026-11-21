"""【开发用】「装到 NAS」教程截图（示例数据：NAS 地址、账号、手机名都是假的）

  cd py && ONETAP_DATA_DIR=<临时目录> /usr/local/bin/python3 packaging/shots_nas.py ../docs/tutorial
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import webui  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots").resolve()
OUT.mkdir(parents=True, exist_ok=True)
HTML = Path(__file__).resolve().parent.parent / "app" / "web" / "index.html"

FAKE = """
(() => {
  const A = window.pywebview.api;
  A.devices = async () => [{udid:'00008150-0000DEMO0000001C', name:'我的 iPhone', via:'USB'}];
  A.accounts = async () => ['example@icloud.com'];
  A.installs = async () => ({items: [], enabled: true});
  A.autostart = async () => true;
  A.nas_addr = async () => '';
  A.find_nas = async () => [{host:'192.168.1.20', kind:'synology', name:'群晖 NAS', docker:true, installed:false}];
  A.install_to_nas = () => {};
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
        api._window = Win()

        async def call(_src, name, args):
            return await loop.run_in_executor(None, lambda: getattr(api, name)(*args))
        await page.expose_binding("__pyapi", call)
        await page.add_init_script(
            "window.__ov = {}; window.pywebview = { api: new Proxy({}, {"
            " get: (_, n) => window.__ov[n] || ((...a) => window.__pyapi(n, a)),"
            " set: (_, n, v) => { window.__ov[n] = v; return true; } }) };")
        await page.add_init_script(f"window.addEventListener('DOMContentLoaded', () => ({FAKE.strip()}))")
        await page.goto(HTML.as_uri())
        await page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
        await page.wait_for_function("document.querySelectorAll('.item').length >= 1", timeout=60000)
        await page.wait_for_timeout(3000)
        J = page.evaluate

        async def snap(name):
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(OUT / f"{name}.png"))
            print("saved", name, flush=True)

        await J("S.custom = []; S.installs = []; S.autostart = true; showTab('mine'); 1")
        await J("document.getElementById('mineview').scrollTop = 9999; 1")
        await snap("14-nas-entry")
        await J("openMove(); 1")
        await page.wait_for_timeout(800)
        await page.fill("#nasUser", "admin")
        await page.fill("#nasPw", "********")
        await snap("15-nas-login")
        await J("""$('moveGo').disabled = true; $('moveGo').textContent = '安装中…'; $('nasCred').style.display = 'none';
                   $('nasSteps').innerHTML = ''; nasStep('upload', ''); const b = $('nasBar'); b.style.display = 'block'; b.firstChild.style.width = '64%'; 1""")
        await snap("16-nas-installing")
        await browser.close()

asyncio.run(main())

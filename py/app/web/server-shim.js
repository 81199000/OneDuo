// 一键签 NAS 版：页面在浏览器里打开时，用这个顶替 pywebview 的 window.pywebview.api
// - api().xxx(...) → POST /api/xxx（参数 JSON 数组），返回 Promise，和桌面版一样
// - 后台推送 → /events（SSE）→ window.onEvent
// - 选文件 → 浏览器上传到 NAS，再把 NAS 上的路径交给原来的方法
window.ONETAP_SERVER = true;
(function () {
  const call = async (name, args) => {
    const r = await fetch("/api/" + name, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args) });
    if (r.status === 401) { location.reload(); throw new Error("需要密码"); }
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    return j.r;
  };
  const pick = (accept, multiple) => new Promise(res => {
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = accept; inp.multiple = !!multiple; inp.style.display = "none";
    inp.onchange = () => { res(Array.from(inp.files || [])); inp.remove(); };
    document.body.appendChild(inp); inp.click();
  });
  const upload = (file, kind, onp) => new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/upload?kind=${kind}&name=${encodeURIComponent(file.name)}`);
    x.upload.onprogress = e => e.lengthComputable && onp && onp(e.loaded / e.total);
    x.onload = () => { try { const j = JSON.parse(x.responseText); j.path ? res(j.path) : rej(new Error(j.error || "上传失败")); } catch (e) { rej(e); } };
    x.onerror = () => rej(new Error("上传失败，检查网络"));
    x.send(file);
  });
  const uploadIpas = async files => {
    const ipas = files.filter(f => f.name.toLowerCase().endsWith(".ipa"));
    if (!ipas.length) { window.toast && toast("只能添加 .ipa 安装包"); return; }
    const paths = [];
    for (const f of ipas) {
      try { paths.push(await upload(f, "ipa", p => window.toast && toast(`正在上传 ${f.name} … ${Math.round(p * 100)}%`))); }
      catch (e) { window.toast && toast(`${f.name} 没传上：${e.message}`); }
    }
    if (paths.length) await call("add_ipas", [paths]);
  };
  const local = {
    pick_ipa: async () => uploadIpas(await pick(".ipa", true)),
    pick_file: async kind => {
      const [f] = await pick(kind === "p12" ? ".p12,.pfx" : ".mobileprovision", false);
      return f ? upload(f, kind) : "";
    },
    open_url: async url => { window.open(url, "_blank", "noopener"); },
  };
  window.pywebview = { api: new Proxy({}, { get: (_, name) => local[name] || ((...args) => call(name, args)) }) };

  // 拖进页面的 IPA
  window.addEventListener("drop", e => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length) uploadIpas(files);
  });

  // 推送：断了自动重连（NAS 重启、Wi‑Fi 切换）
  const listen = () => {
    const es = new EventSource("/events");
    es.onmessage = m => { try { const [ev, d] = JSON.parse(m.data); window.onEvent && window.onEvent(ev, d); } catch (e) {} };
    es.onerror = () => { es.close(); setTimeout(listen, 3000); };
  };
  document.addEventListener("DOMContentLoaded", () => {
    document.body.classList.add("server");
    listen();
    window.dispatchEvent(new Event("pywebviewready"));
  });
})();

"""一键签 · 界面（tkinter，Python 自带，Mac / Windows 通用）

首页：从 GitHub 列表读出的 App 卡片（DouDuo、GuoDuo……以后新 App 改列表就自动出现）
安装：选 App → 连手机（屏幕上一步步教）→ Apple ID 登录（验证码弹窗）→ 下载 → 签名安装 → 告诉用户手机上信任
"""
from __future__ import annotations

import io
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk

from . import core

try:
    from PIL import Image, ImageTk  # 图标缩放（没有也能跑，只是不显示图标）
except Exception:  # noqa: BLE001
    Image = ImageTk = None

BG = "#111214"
CARD = "#1C1D21"
CARD_HI = "#26272C"
TEXT = "#F2F2F3"
SUB = "#A0A1A8"
DIM = "#6C6D74"
ACCENT = "#FF3B30"
OK = "#34C759"
FONT = "PingFang SC" if core.sys.platform == "darwin" else "Microsoft YaHei UI"


def F(size, weight="normal"):
    return (FONT, size, weight)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"一键签 {core.VERSION}")
        self.geometry("820x600+120+120")
        self.minsize(760, 540)
        self.configure(bg=BG)
        self.q: queue.Queue = queue.Queue()
        self.catalog: core.Catalog | None = None
        self.icons: dict[str, object] = {}
        self.selected: core.AppEntry | None = None
        self.device: core.Device | None = None
        self.cancel = threading.Event()
        self._build()
        self.after(50, self._pump)
        self._bg(self._load_catalog)
        self._poll_devices()

    # MARK: 线程 → 界面

    def _bg(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()

    def ui(self, fn, *a):
        self.q.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def ask_main(self, fn):
        """后台线程里同步问用户（验证码等）"""
        box: dict = {}
        done = threading.Event()

        def run():
            box["v"] = fn()
            done.set()

        self.ui(run)
        done.wait()
        return box.get("v")

    # MARK: 布局

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=24, pady=(20, 8))
        tk.Label(top, text="一键签", font=F(22, "bold"), fg=TEXT, bg=BG).pack(side="left")
        tk.Label(top, text="  用自己的 Apple ID 免费签名，直接装到 iPhone / iPad", font=F(12), fg=SUB, bg=BG).pack(
            side="left", pady=(8, 0))
        self.update_btn = tk.Label(top, text="", font=F(11), fg=ACCENT, bg=BG, cursor="hand2")
        self.update_btn.pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=8)
        # 左：App 列表
        self.list_frame = tk.Frame(body, bg=BG, width=300)
        self.list_frame.pack(side="left", fill="y")
        self.list_frame.pack_propagate(False)
        self.list_status = tk.Label(self.list_frame, text="正在读取应用列表…", font=F(12), fg=SUB, bg=BG)
        self.list_status.pack(anchor="w", pady=8)
        # 右：详情 + 步骤
        self.detail = tk.Frame(body, bg=CARD, highlightthickness=0)
        self.detail.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self._empty_detail()

        # 底：手机状态
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=24, pady=(4, 16))
        self.dev_dot = tk.Label(bottom, text="●", font=F(13), fg=DIM, bg=BG)
        self.dev_dot.pack(side="left")
        self.dev_label = tk.Label(bottom, text="未连接手机：用数据线把 iPhone 连到这台电脑", font=F(12), fg=SUB, bg=BG)
        self.dev_label.pack(side="left", padx=6)
        self.acct_label = tk.Label(bottom, text="", font=F(11), fg=DIM, bg=BG)
        self.acct_label.pack(side="right")

    def _empty_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()
        tk.Label(self.detail, text="← 选一个 App", font=F(15), fg=SUB, bg=CARD).place(relx=0.5, rely=0.45, anchor="center")

    # MARK: 应用列表

    def _load_catalog(self):
        try:
            cat = core.load_catalog()
        except Exception as e:  # noqa: BLE001
            self.ui(self.list_status.config, {"text": f"应用列表加载失败\n{e}", "fg": ACCENT})
            return
        for a in cat.apps:
            a.icon_bytes = core.load_icon(cat, a)
        self.ui(self._show_catalog, cat)
        for a in cat.apps:
            try:
                core.resolve_latest(a)
            except Exception:  # noqa: BLE001
                pass
            self.ui(self._refresh_card, a)
        if core.newer(cat.installer_latest, core.VERSION):
            self.ui(self._offer_self_update, cat)

    def _icon(self, a: core.AppEntry, size: int):
        key = f"{a.id}@{size}"
        if key in self.icons:
            return self.icons[key]
        img = None
        if a.icon_bytes and Image:
            try:
                im = Image.open(io.BytesIO(a.icon_bytes)).convert("RGBA").resize((size, size), Image.LANCZOS)
                mask = Image.new("L", (size, size), 0)
                from PIL import ImageDraw
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 4.5, fill=255)
                im.putalpha(mask)
                img = ImageTk.PhotoImage(im)
            except Exception:  # noqa: BLE001
                img = None
        self.icons[key] = img
        return img

    def _show_catalog(self, cat: core.Catalog):
        self.catalog = cat
        self.list_status.config(text=f"共 {len(cat.apps)} 个 App · 列表来自 {cat.source}", fg=DIM, font=F(11))
        self.cards = {}
        for a in cat.apps:
            card = tk.Frame(self.list_frame, bg=CARD, cursor="hand2")
            card.pack(fill="x", pady=5)
            ic = self._icon(a, 52)
            il = tk.Label(card, image=ic, bg=CARD) if ic else tk.Label(card, text="", width=6, bg=CARD)
            il.pack(side="left", padx=12, pady=12)
            txt = tk.Frame(card, bg=CARD)
            txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=a.name, font=F(14, "bold"), fg=TEXT, bg=CARD, anchor="w").pack(fill="x")
            tk.Label(txt, text=a.subtitle, font=F(11), fg=SUB, bg=CARD, anchor="w").pack(fill="x")
            ver = tk.Label(txt, text="读取版本…", font=F(10), fg=DIM, bg=CARD, anchor="w")
            ver.pack(fill="x")
            self.cards[a.id] = (card, ver)
            for w in (card, il, txt, *txt.winfo_children()):
                w.bind("<Button-1>", lambda _e, app=a: self.select(app))

    def _refresh_card(self, a: core.AppEntry):
        card, ver = self.cards[a.id]
        ver.config(text=f"最新 v{a.latest_version}" if a.latest_version else "暂无安装包")
        if self.selected is a:
            self.select(a)

    def select(self, a: core.AppEntry):
        self.selected = a
        for aid, (card, _v) in self.cards.items():
            bg = CARD_HI if aid == a.id else CARD
            card.config(bg=bg)
            for w in card.winfo_children():
                w.config(bg=bg)
                for ww in w.winfo_children():
                    ww.config(bg=bg)
        self._render_detail()

    # MARK: 详情 / 安装步骤

    def _render_detail(self):
        a = self.selected
        for w in self.detail.winfo_children():
            w.destroy()
        head = tk.Frame(self.detail, bg=CARD)
        head.pack(fill="x", padx=22, pady=(22, 10))
        ic = self._icon(a, 72)
        if ic:
            tk.Label(head, image=ic, bg=CARD).pack(side="left")
        t = tk.Frame(head, bg=CARD)
        t.pack(side="left", padx=14)
        tk.Label(t, text=a.name, font=F(20, "bold"), fg=TEXT, bg=CARD, anchor="w").pack(fill="x")
        v = f"v{a.latest_version}" if a.latest_version else "暂无安装包"
        size = f" · {a.asset_size / 1048576:.1f} MB" if a.asset_size else ""
        tk.Label(t, text=f"{v}{size} · 需要 iOS {a.min_ios}+", font=F(11), fg=SUB, bg=CARD, anchor="w").pack(fill="x")
        link = tk.Label(t, text="项目主页", font=F(11), fg=ACCENT, bg=CARD, cursor="hand2", anchor="w")
        link.pack(fill="x")
        link.bind("<Button-1>", lambda _e: webbrowser.open(a.homepage))

        tk.Label(self.detail, text=a.description, font=F(12), fg=SUB, bg=CARD, wraplength=420, justify="left",
                 anchor="w").pack(fill="x", padx=22)

        # 步骤
        self.steps_frame = tk.Frame(self.detail, bg=CARD)
        self.steps_frame.pack(fill="x", padx=22, pady=16)
        self.step_rows = {}
        for key, title in [("device", "连接手机"), ("account", "登录 Apple ID"), ("download", "下载安装包"),
                           ("install", "签名并安装"), ("trust", "在手机上信任")]:
            row = tk.Frame(self.steps_frame, bg=CARD)
            row.pack(fill="x", pady=3)
            dot = tk.Label(row, text="○", font=F(13), fg=DIM, bg=CARD, width=2)
            dot.pack(side="left")
            lab = tk.Label(row, text=title, font=F(12), fg=SUB, bg=CARD, anchor="w")
            lab.pack(side="left")
            note = tk.Label(row, text="", font=F(11), fg=DIM, bg=CARD, anchor="w")
            note.pack(side="left", padx=8)
            self.step_rows[key] = (dot, lab, note)

        self.progress = ttk.Progressbar(self.detail, mode="determinate", maximum=1.0)
        self.hint = tk.Label(self.detail, text="", font=F(12), fg=TEXT, bg=CARD, wraplength=440, justify="left",
                             anchor="w")
        self.hint.pack(fill="x", padx=22)

        btns = tk.Frame(self.detail, bg=CARD)
        btns.pack(side="bottom", fill="x", padx=22, pady=20)
        self.go = tk.Button(btns, text="一键安装", font=F(14, "bold"), command=self.start_install,
                            bg=ACCENT, fg="white", activebackground=ACCENT, relief="flat", padx=26, pady=8,
                            highlightthickness=0, borderwidth=0)
        self.go.pack(side="right")
        if not a.asset_url:
            self.go.config(state="disabled", text="暂无安装包")
        self._mark("device", "ok" if self.device else "wait", self.device.name if self.device else "")

    def _mark(self, key, state, note=""):
        if not getattr(self, "step_rows", None) or key not in self.step_rows:
            return
        dot, lab, n = self.step_rows[key]
        sym, col = {"wait": ("○", DIM), "run": ("◐", ACCENT), "ok": ("●", OK), "err": ("✕", ACCENT)}[state]
        dot.config(text=sym, fg=col)
        lab.config(fg=TEXT if state != "wait" else SUB)
        n.config(text=note, fg=ACCENT if state == "err" else DIM)

    def _hint(self, text, color=TEXT):
        if getattr(self, "hint", None) and self.hint.winfo_exists():
            self.hint.config(text=text, fg=color)

    # MARK: 手机检测（每 2 秒）

    def _poll_devices(self):
        def work():
            devs = core.list_devices()
            self.ui(self._set_devices, devs)
        self._bg(work)
        self.after(2500, self._poll_devices)

    def _set_devices(self, devs):
        self.device = devs[0] if devs else None
        if self.device:
            via = "数据线" if self.device.model == "USB" else "Wi‑Fi"
            self.dev_dot.config(fg=OK)
            self.dev_label.config(text=f"已连接：{self.device.name}（{via}）", fg=TEXT)
        else:
            self.dev_dot.config(fg=DIM)
            self.dev_label.config(text="未连接手机：用数据线把 iPhone 连到这台电脑，解锁后点「信任」", fg=SUB)
        if self.selected and not getattr(self, "busy", False):
            self._mark("device", "ok" if self.device else "wait", self.device.name if self.device else "")
        accts = core.saved_accounts()
        self.acct_label.config(text=f"Apple ID：{accts[0]}" if accts else "")

    # MARK: 安装流程

    def start_install(self):
        a = self.selected
        if not self.device:
            self._mark("device", "err", "没找到手机")
            self._hint("① 用数据线把 iPhone 连到这台电脑\n② 解锁手机，弹出「要信任此电脑吗？」点「信任」，输入锁屏密码\n"
                       "连上后下面会显示手机名字，再点一键安装。", SUB)
            return
        self.busy = True
        self.cancel.clear()
        self.go.config(state="disabled", text="安装中…")
        self._bg(self._install_flow, a, self.device)

    def _ask_login(self):
        d = LoginDialog(self)
        return d.result

    def _ask_2fa(self, kind):
        msg = ("Apple 已向你的手机发送了 6 位验证码，请输入：" if kind == "device"
               else "Apple 已通过短信发送 6 位验证码，请输入：")
        return self.ask_main(lambda: simpledialog.askstring("双重认证验证码", msg, parent=self))

    def _install_flow(self, a: core.AppEntry, dev: core.Device):
        def log(line):
            if "PLUME_" in line:
                return
            short = line.split("] ")[-1].strip()
            if short:
                self.ui(self._hint, short[:120], DIM)

        try:
            self.ui(self._mark, "device", "ok", dev.name)
            # 账号
            self.ui(self._mark, "account", "run")
            accts = core.saved_accounts()
            if not accts:
                creds = self.ask_main(self._ask_login)
                if not creds:
                    raise core.SignError("已取消登录")
                self.ui(self._hint, "正在登录 Apple ID…（只发给苹果，本软件不保存密码）", SUB)
                core.login(creds[0], creds[1], log, self._ask_2fa)
                accts = core.saved_accounts()
            self.ui(self._mark, "account", "ok", accts[0] if accts else "")
            # 下载
            self.ui(self._mark, "download", "run")
            self.ui(self.progress.pack, {"fill": "x", "padx": 22, "pady": (0, 8)})
            ipa = core.download_ipa(a, lambda p: self.ui(self.progress.config, {"value": p}), self.cancel)
            self.ui(self.progress.pack_forget)
            self.ui(self._mark, "download", "ok", f"v{a.latest_version}")
            # 签名安装
            self.ui(self._mark, "install", "run")
            self.ui(self._hint, "正在签名并安装到手机，请保持手机解锁、数据线不要拔…", SUB)
            core.sign_and_install(ipa, dev.udid, log)
            self.ui(self._mark, "install", "ok")
            # 信任
            self.ui(self._mark, "trust", "run")
            self.ui(self._hint,
                    "✅ 已装到手机！第一次打开前，在手机上：\n"
                    "① 设置 → 通用 → VPN 与设备管理 → 点你的 Apple ID → 信任\n"
                    "② 如果提示需要开发者模式：设置 → 隐私与安全性 → 开发者模式 → 打开，按提示重启\n"
                    "免费 Apple ID 签的 App 7 天后会过期，到时打开本软件再装一次即可（数据不丢）。", OK)
            self.ui(self._mark, "trust", "ok", "按上面提示操作")
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, core.SignError) else core.friendly(str(e))
            self.ui(self._hint, f"没装上：{msg}", ACCENT)
            for k in ("account", "download", "install"):
                dot = self.step_rows.get(k, (None,))[0] if getattr(self, "step_rows", None) else None
                if dot is not None and dot.cget("text") == "◐":
                    self.ui(self._mark, k, "err")
            self.ui(self.progress.pack_forget)
        finally:
            self.busy = False
            self.ui(self.go.config, {"state": "normal", "text": "再装一次"})

    # MARK: 自身更新

    def _offer_self_update(self, cat: core.Catalog):
        self.update_btn.config(text=f"有新版本 v{cat.installer_latest} ↗")
        url = cat.installer_download.get("gitee" if core.is_cn() else "github") or cat.installer_download.get("github")

        def go(_e=None):
            if messagebox.askyesno("更新一键签", f"新版本 v{cat.installer_latest}\n\n{cat.installer_notes}\n\n打开下载页？"):
                webbrowser.open(url)
        self.update_btn.bind("<Button-1>", go)


class LoginDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("登录 Apple ID")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.result = None
        tk.Label(self, text="用你自己的 Apple ID 签名", font=F(14, "bold"), fg=TEXT, bg=CARD).pack(padx=24, pady=(20, 4),
                                                                                         anchor="w")
        tk.Label(self, text="账号密码直接发给苹果，本软件不保存密码。建议用一个小号。", font=F(11), fg=SUB, bg=CARD,
                 wraplength=340, justify="left").pack(padx=24, anchor="w")
        self.user = tk.Entry(self, font=F(13), width=30)
        self.pw = tk.Entry(self, font=F(13), width=30, show="•")
        for lab, ent in (("Apple ID（邮箱或手机号）", self.user), ("密码", self.pw)):
            tk.Label(self, text=lab, font=F(11), fg=SUB, bg=CARD).pack(padx=24, pady=(12, 2), anchor="w")
            ent.pack(padx=24, anchor="w")
        row = tk.Frame(self, bg=CARD)
        row.pack(fill="x", padx=24, pady=20)
        tk.Button(row, text="登录", font=F(12, "bold"), command=self._ok, bg=ACCENT, fg="white", relief="flat",
                  padx=18).pack(side="right")
        tk.Button(row, text="取消", font=F(12), command=self.destroy, relief="flat").pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())
        self.user.focus_set()
        self.transient(parent)
        self.grab_set()
        parent.wait_window(self)

    def _ok(self):
        u, p = self.user.get().strip(), self.pw.get()
        if u and p:
            self.result = (u, p)
            self.destroy()


def main():
    App().mainloop()

"""校园网自动登录 —— 一键设置（图形界面）。

双击「1-一键安装配置.bat」就会打开这个窗口。填账号密码，点一个按钮，完事。

两条设计约定：
1. 全程不使用任何 messagebox / 系统对话框，所有提示都写在窗口内的状态文字里，
   这样"不弹窗"这条要求连设置阶段也不会被破坏。
2. 后台线程绝不直接碰 Tk（跨线程调 root.after 不是线程安全的，会抛
   "main thread is not in main loop"）。统一走 queue.Queue，主线程定时取。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

from campusnet import crypto  # noqa: E402
from campusnet.checker import check  # noqa: E402
from campusnet.config import DEFAULT_CONFIG_PATH, Config  # noqa: E402
from campusnet.daemon import build_client, make_verifier  # noqa: E402
from campusnet.drcom import looks_like_drcom  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402
from campusnet.portal import PortalPage  # noqa: E402
from gui_common import install_task, whitelist_status  # noqa: E402

DEFAULT_PORTAL = "http://111.17.200.130/"
CARRIERS = [
    ("校园用户（不带后缀）", ""),
    ("中国电信  @dx", "@dx"),
    ("中国联通  @lt", "@lt"),
]

GREEN = "#1a6b1a"
RED = "#b00020"
ORANGE = "#b06a00"
GREY = "#333333"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.portal_url = ""
        self.detected_account = ""
        self.detected_suffix = ""
        self.busy = False
        self.tk_error = ""

        root.title("校园网自动登录 - 一键设置")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            frame,
            text="填好账号密码，点下面的按钮，以后就不用管了。",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        ttk.Label(
            frame,
            text="设置完成后它会自动在后台运行，掉线自动重连，全程不会弹任何窗口。",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        pad = {"padx": 14, "pady": 6}

        ttk.Label(frame, text="上网账号").grid(row=2, column=0, sticky="w", **pad)
        self.var_account = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_account, width=30, font=("Consolas", 10)).grid(
            row=2, column=1, sticky="w", **pad
        )

        ttk.Label(frame, text="上网密码").grid(row=3, column=0, sticky="w", **pad)
        self.var_password = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_password, width=30, show="*", font=("Consolas", 10)).grid(
            row=3, column=1, sticky="w", **pad
        )

        ttk.Label(frame, text="运营商").grid(row=4, column=0, sticky="w", **pad)
        self.var_carrier = tk.StringVar(value=CARRIERS[0][0])
        ttk.Combobox(
            frame, textvariable=self.var_carrier, width=27, state="readonly",
            values=[c[0] for c in CARRIERS],
        ).grid(row=4, column=1, sticky="w", **pad)

        self.var_advanced = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="显示高级设置", variable=self.var_advanced, command=self.toggle_advanced
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=14, pady=(8, 0))

        self.adv = ttk.Frame(frame)
        self.adv.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Label(self.adv, text="门户地址").grid(row=0, column=0, sticky="w", **pad)
        self.var_portal = tk.StringVar(value=DEFAULT_PORTAL)
        ttk.Entry(self.adv, textvariable=self.var_portal, width=30, font=("Consolas", 9)).grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Label(self.adv, text="通知webhook").grid(row=1, column=0, sticky="w", **pad)
        self.var_webhook = tk.StringVar()
        ttk.Entry(self.adv, textvariable=self.var_webhook, width=30, font=("Consolas", 9)).grid(
            row=1, column=1, sticky="w", **pad
        )
        ttk.Label(
            self.adv,
            text="（可留空。填企业微信/钉钉群机器人地址，\n登录失败时推消息到你手机）",
            foreground="#777777", justify="left",
        ).grid(row=2, column=1, sticky="w", padx=14)
        self.adv.grid_remove()

        self.btn = tk.Button(
            frame, text="  保存并启用自动登录  ", command=self.on_install,
            font=("Microsoft YaHei UI", 11, "bold"), bg="#2d7ff9", fg="white",
            activebackground="#1b5fc1", activeforeground="white",
            relief="flat", padx=10, pady=8, cursor="hand2",
        )
        self.btn.grid(row=7, column=0, columnspan=2, pady=(18, 6))

        self.var_status = tk.StringVar(value="正在检测你的校园网 ...")
        self.lbl_status = ttk.Label(
            frame, textvariable=self.var_status, wraplength=390, justify="left", foreground=GREY
        )
        self.lbl_status.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=390)
        self.progress.grid(row=9, column=0, columnspan=2, pady=(10, 0))
        self.progress.grid_remove()

        self.load_existing()
        # 关窗口时先停掉轮询，避免 Tcl 报"invalid command name"后台错误 ——
        # Tcl 的后台错误在 Windows 上有弹错误对话框的风险，必须堵掉。
        self._closed = False
        self._after_id = None
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.report_exception
        self._after_id = self.root.after(80, self._drain)
        self.root.after(200, self.detect_async)

    # ---------- 跨线程安全投递 ----------

    def post(self, fn) -> None:
        """从后台线程投递一个 UI 更新函数，由主线程执行。"""
        if fn is not None:
            self.q.put(fn)

    def on_close(self) -> None:
        self._closed = True
        # 必须先取消挂起的定时回调再销毁窗口：否则 Tcl 会报
        # "invalid command name ..._drain" 后台错误，而 Tcl 的后台错误
        # 在 Windows 上有弹错误对话框的风险 —— 绝不能让"不弹窗"破功。
        try:
            if self._after_id is not None:
                self.root.after_cancel(self._after_id)
                self._after_id = None
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def report_exception(self, exc, val, tb) -> None:
        """顶掉 Tkinter 默认的异常输出：写文件，绝不弹任何对话框。"""
        try:
            import traceback as _tb

            (ROOT / "logs").mkdir(parents=True, exist_ok=True)
            with open(ROOT / "logs" / "gui_error.log", "a", encoding="utf-8") as fh:
                fh.write("".join(_tb.format_exception(exc, val, tb)))
        except Exception:  # noqa: BLE001
            pass

    def _drain(self) -> None:
        if self._closed:
            return
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.tk_error = traceback.format_exc()
        if self._closed:
            return
        try:
            self._after_id = self.root.after(80, self._drain)
        except Exception:  # noqa: BLE001
            pass

    # ---------- UI ----------

    def toggle_advanced(self) -> None:
        if self.var_advanced.get():
            self.adv.grid()
        else:
            self.adv.grid_remove()

    def say(self, text: str, color: str = GREY) -> None:
        self.var_status.set(text)
        self.lbl_status.configure(foreground=color)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.btn.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    # ---------- 数据 ----------

    def load_existing(self) -> None:
        try:
            cfg = Config.load(DEFAULT_CONFIG_PATH)
        except Exception:  # noqa: BLE001
            return
        if cfg.username:
            self.var_account.set(cfg.username)
        if cfg.portal_url:
            self.portal_url = cfg.portal_url
            self.var_portal.set(cfg.portal_url)
        if cfg.drcom.account_suffix:
            for label, suf in CARRIERS:
                if suf == cfg.drcom.account_suffix:
                    self.var_carrier.set(label)
                    break
        if getattr(cfg, "notify_webhook", ""):
            self.var_webhook.set(cfg.notify_webhook)

    def detect_async(self) -> None:
        threading.Thread(target=self._detect, daemon=True).start()

    def _detect(self) -> None:
        try:
            cfg = Config.load(DEFAULT_CONFIG_PATH)
        except Exception:  # noqa: BLE001
            cfg = Config()
        client = build_client(cfg)
        msgs: list[str] = []
        portal = cfg.portal_url
        account = ""
        suffix = ""

        try:
            state = check(client, cfg)
            if state.portal_url:
                portal = state.portal_url
                msgs.append("已找到校园网登录页")
            elif state.online:
                msgs.append("你现在能上网")
        except Exception:  # noqa: BLE001
            pass
        if not portal:
            portal = DEFAULT_PORTAL

        try:
            resp = client.get(portal, follow_redirects=True)
            page = PortalPage(resp.text, resp.url)
            uid = ""
            for rx in (r"uid\s*=\s*'([^']+)'", r'uid\s*=\s*"([^"]+)"'):
                m = re.search(rx, page.html or "")
                if m:
                    uid = m.group(1)
                    break
            if uid:
                # 注意：uid 里的 "@cmcc" 是 AC 内部的运营商标记，不是登录时要加的后缀。
                # 登录账号就是裸手机号 + ",0," 前缀；校园用户不加任何后缀。
                account = uid.split("@", 1)[0]
                suffix = ""
                msgs.append(f"已读到你的账号 {_mask(account)}")
            if looks_like_drcom(resp.url, page):
                msgs.append("认证系统是城市热点 Dr.COM")
        except Exception:  # noqa: BLE001
            pass

        tail = "填好密码，点下面的按钮就完成了。"
        text = ("检测完成：" + "、".join(msgs) + "\n" + tail) if msgs else ("检测完成。\n" + tail)
        self.post(lambda: self._apply_detect(portal, account, suffix, text))

    def _apply_detect(self, portal: str, account: str, suffix: str, text: str) -> None:
        self.portal_url = portal
        self.detected_account = account
        self.detected_suffix = suffix
        if portal:
            self.var_portal.set(portal)
        if account and not self.var_account.get():
            self.var_account.set(account)
        if suffix:
            for label, suf in CARRIERS:
                if suf == suffix:
                    self.var_carrier.set(label)
                    break
            else:
                text += f"\n（检测到后缀 {suffix}，保存时会自动使用）"
        self.say(text, GREEN)

    def current_suffix(self) -> str:
        label = self.var_carrier.get()
        for lab, suf in CARRIERS:
            if lab == label:
                if suf == "" and self.detected_suffix:
                    return self.detected_suffix
                return suf
        return ""

    # ---------- 安装 ----------

    def on_install(self) -> None:
        if self.busy:
            return
        account = self.var_account.get().strip()
        password = self.var_password.get()
        if not account:
            self.say("请先填写上网账号。", RED)
            return
        if not password:
            self.say("请先填写上网密码。", RED)
            return
        self.set_busy(True)
        self.say("正在保存并启用 ...")
        threading.Thread(target=self._install, args=(account, password), daemon=True).start()

    def _install(self, account: str, password: str) -> None:
        try:
            cfg = Config.load(DEFAULT_CONFIG_PATH)
        except Exception:  # noqa: BLE001
            cfg = Config()

        portal = (self.var_portal.get() or self.portal_url or DEFAULT_PORTAL).strip()
        suffix = self.current_suffix()
        webhook = self.var_webhook.get().strip()

        cfg.portal_url = portal
        cfg.username = account
        cfg.drcom.enabled = True
        cfg.drcom.account_suffix = suffix
        if webhook:
            cfg.notify_webhook = webhook
        if not cfg.drcom.host:
            from urllib.parse import urlsplit

            cfg.drcom.host = urlsplit(portal).hostname or ""

        try:
            cfg.password_enc = crypto.protect(password)
            cfg.save(DEFAULT_CONFIG_PATH)
        except Exception as exc:  # noqa: BLE001
            self.post(lambda: self._fail(f"保存失败：{exc}"))
            return

        # 收紧配置文件权限（失败不影响功能，密码本身已加密）
        try:
            import subprocess

            subprocess.run(
                ["icacls", str(DEFAULT_CONFIG_PATH), "/inheritance:r", "/grant:r",
                 f"{os.environ.get('USERNAME', '')}:F"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001
            pass

        self.post(lambda: self.say("配置已保存，正在设置开机自启 ..."))
        ok_task, info = install_task(ROOT / "tools")

        # 顺带检查杀毒软件白名单：没加入的话，在结果里提醒一句
        # （Windows Defender 可以自动加，第三方杀毒只能给手动步骤）
        wl_note = ""
        try:
            wl = whitelist_status()
            if wl.get("defender_available"):
                if wl.get("missing_processes") or wl.get("missing_paths"):
                    wl_note = (
                        "\n\n⚠ 小提示：还没加入杀毒软件白名单。建议双击「4-加入白名单.bat」"
                        "（或打开「2-检查一下.bat」点「加入白名单」），"
                        "防止杀毒软件误拦导致偶尔不生效。"
                    )
            elif wl.get("third_party_av"):
                wl_note = (
                    "\n\n⚠ 检测到杀毒软件：" + "、".join(wl["third_party_av"]) +
                    "。建议双击「4-加入白名单.bat」，按提示把程序加入白名单，防止误拦。"
                )
        except Exception:  # noqa: BLE001
            pass

        head = (
            f"设置完成\n\n"
            f"账号：{_mask(account)}{suffix}\n"
            f"密码：已用系统加密保存到本机（换电脑或换 Windows 用户都解不开）\n"
            f"开机自启：{'已启用' if ok_task else '失败'}"
        )
        note = ""
        try:
            logger = setup_logging(cfg, console=False)
            client = build_client(cfg)
            state = check(client, cfg)
            if state.online:
                note = "\n\n当前网络已经通了，说明一切正常。"
            else:
                from campusnet.authenticator import PortalAuthenticator

                auth = PortalAuthenticator(client, cfg, logger)
                result = auth.login(
                    cfg.portal_url or state.portal_url, account, password,
                    make_verifier(client, cfg, logger, tries=1),
                )
                note = "\n\n实测登录：" + ("成功" if result.ok else f"没成功（{result.reason}）")
        except Exception:  # noqa: BLE001
            pass

        if ok_task:
            text = head + "\n\n以后它会在后台自己跑，不用再管它了。" + wl_note + note
            color = GREEN
        else:
            text = head + f"\n\n设置没完全成功，提示：{info[:300]}" + wl_note + note
            color = ORANGE
        self.post(lambda: self._done(text, color))

    def _fail(self, text: str) -> None:
        self.say(text, RED)
        self.set_busy(False)

    def _done(self, text: str, color: str) -> None:
        self.say(text, color)
        self.set_busy(False)


def _mask(account: str) -> str:
    a = account or ""
    if len(a) <= 6:
        return a
    return a[:3] + "****" + a[-4:]


def main() -> int:
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return 1
    app = None
    try:
        app = App(root)
        root.mainloop()
    except Exception:  # noqa: BLE001
        try:
            (ROOT / "logs").mkdir(parents=True, exist_ok=True)
            (ROOT / "logs" / "gui_setup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        if app is not None and app.tk_error:
            try:
                (ROOT / "logs").mkdir(parents=True, exist_ok=True)
                (ROOT / "logs" / "gui_setup_error.log").write_text(app.tk_error, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

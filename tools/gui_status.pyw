"""校园网自动登录 —— 状态查看（图形界面）。

双击「2-检查一下.bat」打开。用大白话告诉你现在是什么情况、出问题该点哪里。

因为程序按要求不弹任何窗口，这个窗口就是你的"仪表盘"。
后台线程绝不直接碰 Tk，统一走 queue.Queue 交给主线程。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

from campusnet.checker import check  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.daemon import build_client, make_verifier  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402
from gui_common import (  # noqa: E402
    apply_whitelist,
    daemon_running,
    last_success_time,
    recent_log_lines,
    task_registered,
    uninstall_task,
    whitelist_status,
)

GREEN = "#1a6b1a"
RED = "#b00020"
ORANGE = "#b06a00"
GREY = "#444444"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.tk_error = ""

        root.title("校园网自动登录 - 状态")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frame, text="现在是什么情况", font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )

        self.rows: dict = {}
        for i, (key, title) in enumerate(
            [
                ("net", "网络"),
                ("proc", "后台程序"),
                ("task", "开机自启"),
                ("av", "杀毒白名单"),
                ("last", "上次自动登录"),
            ],
            start=1,
        ):
            ttk.Label(frame, text=title, width=12).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value="检查中 ...")
            lbl = ttk.Label(frame, textvariable=var, foreground=GREY)
            lbl.grid(row=i, column=1, columnspan=2, sticky="w", pady=3)
            self.rows[key] = (var, lbl)

        ttk.Separator(frame, orient="horizontal").grid(row=10, column=0, columnspan=3, sticky="ew", pady=12)

        btns = ttk.Frame(frame)
        btns.grid(row=11, column=0, columnspan=3, sticky="ew")
        self.btn_test = tk.Button(
            btns, text="立即测试登录", command=self.on_test,
            font=("Microsoft YaHei UI", 10, "bold"), bg="#2d7ff9", fg="white",
            relief="flat", padx=12, pady=6, cursor="hand2",
            activebackground="#1b5fc1", activeforeground="white",
        )
        self.btn_test.grid(row=0, column=0, padx=(0, 8))
        self.btn_wl = tk.Button(
            btns, text="加入白名单", command=self.on_whitelist,
            font=("Microsoft YaHei UI", 9), bg="#ffd966", fg="#4a3800",
            relief="flat", padx=10, pady=6, cursor="hand2",
            activebackground="#f2c94c", activeforeground="#4a3800",
        )
        self.btn_wl.grid(row=0, column=1, padx=(0, 8))
        tk.Button(btns, text="刷新", command=self.refresh, relief="flat", padx=12, pady=6,
                  bg="#e8e8e8", cursor="hand2").grid(row=0, column=2, padx=(0, 8))
        tk.Button(btns, text="打开日志文件夹", command=self.open_logs, relief="flat", padx=12, pady=6,
                  bg="#e8e8e8", cursor="hand2").grid(row=0, column=3, padx=(0, 8))
        tk.Button(btns, text="卸载", command=self.on_uninstall, relief="flat", padx=12, pady=6,
                  bg="#f0dede", cursor="hand2").grid(row=0, column=4)

        ttk.Label(frame, text="最近的运行记录", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(16, 6)
        )
        self.txt = tk.Text(frame, width=76, height=12, font=("Consolas", 9), wrap="none",
                           bg="#fbfbfb", relief="solid", borderwidth=1)
        self.txt.grid(row=13, column=0, columnspan=3, sticky="nsew")
        self.txt.configure(state="disabled")

        self.var_hint = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.var_hint, wraplength=560, justify="left",
                  foreground=GREY).grid(row=14, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self._closed = False
        self._after_id = None
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.report_exception
        self._after_id = self.root.after(80, self._drain)
        self.root.after(150, self.refresh)

    # ---------- 跨线程安全投递 ----------

    def post(self, fn) -> None:
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

    # ---------- 工具 ----------

    def set_row(self, key: str, text: str, color: str) -> None:
        var, lbl = self.rows[key]
        var.set(text)
        lbl.configure(foreground=color)

    def hint(self, text: str, color: str = GREY) -> None:
        self.var_hint.set(text)

    def fill_log(self) -> None:
        lines = recent_log_lines(20)
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert(
            "end",
            "\n".join(lines) if lines else "（还没有运行记录。设置好之后，登录一次 Windows 就会出现。）",
        )
        self.txt.see("end")
        self.txt.configure(state="disabled")
        return lines

    def open_logs(self) -> None:
        for target in (ROOT / "logs", ROOT):
            try:
                os.startfile(str(target))  # noqa: S606
                return
            except Exception:  # noqa: BLE001
                continue

    # ---------- 状态刷新 ----------

    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.hint("正在检查 ...")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            cfg = Config.load()
        except Exception:  # noqa: BLE001
            cfg = Config()

        data = {
            "configured": cfg.is_configured(),
            "running": daemon_running(),
            "registered": task_registered(),
            "last": last_success_time(),
            "online": None,
            "detail": "",
        }
        try:
            state = check(build_client(cfg), cfg)
            data["online"] = state.online
            data["detail"] = state.detail
        except Exception as exc:  # noqa: BLE001
            data["detail"] = str(exc)

        try:
            data["wl"] = whitelist_status()
        except Exception as exc:  # noqa: BLE001
            data["wl"] = {"defender_error": str(exc)}

        self.post(lambda: self._apply_refresh(data))

    def _apply_refresh(self, d: dict) -> None:
        if not d["configured"]:
            self.set_row("net", "还没有设置过，请先双击「1-一键安装配置.bat」", ORANGE)
            self.set_row("proc", "—", GREY)
            self.set_row("task", "—", GREY)
            self.set_row("av", "—", GREY)
            self.set_row("last", "—", GREY)
            self.hint("还没有设置。双击「1-一键安装配置.bat」，填账号密码，点一次按钮就完成了。", ORANGE)
        else:
            if d["online"] is True:
                self.set_row("net", "能上网 ✅", GREEN)
            elif d["online"] is False:
                self.set_row("net", f"现在上不了网 ❌（{d['detail']}）", RED)
            else:
                self.set_row("net", f"没测出来（{d['detail']}）", ORANGE)

            self.set_row(
                "proc",
                "正在后台运行 ✅" if d["running"] else "没有在运行 ❌（双击「1」重新设置一次即可）",
                GREEN if d["running"] else RED,
            )
            self.set_row(
                "task",
                "已设置，登录 Windows 后自动启动 ✅" if d["registered"] else "未设置 ❌",
                GREEN if d["registered"] else RED,
            )
            self.set_row("last", d["last"] or "（还没有成功记录）", GREEN if d["last"] else GREY)

            wl = d.get("wl") or {}
            third = wl.get("third_party_av") or []
            wl_hint = ""
            if wl.get("defender_available"):
                missing = (wl.get("missing_processes") or []) + (wl.get("missing_paths") or [])
                if not missing and not third:
                    self.set_row("av", "已加入 ✅", GREEN)
                elif not missing and third:
                    names = "、".join(third[:2])
                    self.set_row("av", f"Defender 已加入；还有 {names}，建议也添加", ORANGE)
                else:
                    self.set_row("av", "未加入 ❌（点下方「加入白名单」一键处理）", ORANGE)
            elif third:
                names = "、".join(third[:2])
                self.set_row("av", f"检测到 {names}（需手动添加）", ORANGE)
            else:
                self.set_row("av", "没检测到（无法读取 Defender 状态）", GREY)

            if third and wl.get("third_party_hint"):
                # 第三方杀毒没有自动接口，只要装了就一直给出手动步骤
                wl_hint = wl["third_party_hint"]

            if d["online"] is True:
                self.hint("一切正常，不用管它。", GREEN)
            elif d["online"] is False and d["running"]:
                self.hint(
                    "它已经发现断网，正在自动重连。等 1~2 分钟再看一次；\n"
                    "如果一直是红的，点上面的「立即测试登录」。",
                    ORANGE,
                )
            elif d["online"] is False and not d["running"]:
                self.hint("后台程序没在运行。双击「1-一键安装配置.bat」重新设置一次即可。", ORANGE)
            else:
                self.hint("")

        lines = self.fill_log()
        if lines and not self.var_hint.get():
            self.hint(f"最近一条记录：{lines[-1]}", GREY)
        wl_hint = locals().get("wl_hint", "")
        if wl_hint:
            cur = self.var_hint.get()
            self.hint((cur + "\n\n" + wl_hint).strip() if cur else wl_hint, ORANGE)
        self.busy = False

    # ---------- 测试登录 ----------

    def on_test(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.btn_test.configure(state="disabled")
        self.hint("正在测试登录，请稍等 ...")
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self) -> None:
        from campusnet import crypto
        from campusnet.authenticator import PortalAuthenticator

        try:
            cfg = Config.load()
        except Exception as exc:  # noqa: BLE001
            self._test_done(f"读取配置失败：{exc}", RED)
            return
        if not cfg.is_configured():
            self._test_done("还没有设置，请先双击「1-一键安装配置.bat」。", ORANGE)
            return
        try:
            password = crypto.unprotect(cfg.password_enc)
        except Exception as exc:  # noqa: BLE001
            self._test_done(
                f"密码解不开（是不是换过电脑或 Windows 用户？）：{exc}\n请重新双击「1」设置一次。", RED
            )
            return

        try:
            logger = setup_logging(cfg, console=False)
            client = build_client(cfg)
            state = check(client, cfg)
            if state.online:
                self._test_done("当前网络已经通了，不需要登录 —— 说明工作正常 ✅", GREEN)
                return
            auth = PortalAuthenticator(client, cfg, logger)
            result = auth.login(
                cfg.portal_url or state.portal_url, cfg.username, password,
                make_verifier(client, cfg, logger, tries=2),
            )
            if result.ok:
                self._test_done(f"登录成功 ✅（{result.reason}）", GREEN)
            elif result.bad_credentials:
                self._test_done("登录失败：账号或密码不对 ❌\n请双击「1-一键安装配置.bat」重新填一次。", RED)
            else:
                self._test_done(f"登录没成功 ❌\n原因：{result.reason}", RED)
        except Exception as exc:  # noqa: BLE001
            self._test_done(f"测试过程出错：{exc}", RED)

    def _test_done(self, text: str, color: str) -> None:
        def apply() -> None:
            self.hint(text, color)
            self.btn_test.configure(state="normal")
            self.busy = False
            self.fill_log()

        self.post(apply)

    # ---------- 杀毒白名单 ----------

    def on_whitelist(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.btn_wl.configure(state="disabled")
        self.hint(
            "正在加入白名单。Windows 会弹出「用户账户控制」窗口，请点「是」 ...\n"
            "（360 / 火绒 / 电脑管家等第三方杀毒需要按提示手动点几下）",
        )
        threading.Thread(target=self._whitelist_worker, daemon=True).start()

    def _whitelist_worker(self) -> None:
        try:
            ok, info = apply_whitelist()
        except Exception as exc:  # noqa: BLE001
            ok, info = False, f"调用失败：{exc}"
        text = (info or "").strip()[:900] or ("已完成" if ok else "没有完成，请看窗口里的提示")

        def done() -> None:
            self.btn_wl.configure(state="normal")
            self.busy = False
            self.hint(text, GREEN if ok else ORANGE)
            self.root.after(1200, self.refresh)

        self.post(done)

    # ---------- 卸载 ----------

    def on_uninstall(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.hint("正在卸载 ...")
        threading.Thread(target=self._uninstall_worker, daemon=True).start()

    def _uninstall_worker(self) -> None:
        ok, info = uninstall_task(ROOT / "tools")
        text = "已卸载：开机自启和后台进程都清掉了。" if ok else f"卸载可能没完全成功：{info[:300]}"
        self.post(lambda: self.hint(text, GREEN if ok else ORANGE))
        self.busy = False
        self.root.after(400, self.refresh)


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
            (ROOT / "logs" / "gui_status_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        if app is not None and app.tk_error:
            try:
                (ROOT / "logs").mkdir(parents=True, exist_ok=True)
                (ROOT / "logs" / "gui_status_error.log").write_text(app.tk_error, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

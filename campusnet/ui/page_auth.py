"""认证页：顶部工具条 + 结果横幅 + 深蓝墨色控制台。

没有内嵌浏览器：纯标准库做不到（要 WebView2 运行时 + 第三方绑定）。
改成用已保存的凭据直接向门户提交认证，这一步就能让网通；
想看页面本身点「在浏览器中打开」。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk

from . import gui_service as svc
from .. import status
from ..security import keeps_portal_query
from .ui_kit import (
    BAD, BG, BORDER, CARD, CONSOLE_BG, CONSOLE_DIM, CONSOLE_FG, FAINT, GOOD,
    MONO, MUTED, PRIMARY, TEXT, WARN,
    Card, FlatButton, dot, f, mono, severity_color,
)


class AuthPage(tk.Frame):
    def __init__(self, parent: tk.Misc, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self._stop = threading.Event()
        self._busy = False
        self._portal_url = ""
        self._build()

    # ------------------------------------------------ 结构

    def _build(self) -> None:
        # ---- 工具条 ----
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", pady=(0, 12))
        self.btn_browser_login = FlatButton(
            bar, "打开认证页面并填写", self.on_browser_login, kind="primary", font=f(10, "bold"),
            padx=18, pady=9, parent_bg=BG,
        )
        self.btn_browser_login.pack(side="left")
        self.btn_login = FlatButton(bar, "直接提交认证", self.on_login, kind="ghost",
                                    padx=14, pady=9, parent_bg=BG)
        self.btn_login.pack(side="left", padx=(10, 0))
        self.btn_probe = FlatButton(bar, "探测认证地址", self.on_probe, kind="ghost",
                                    padx=14, pady=9, parent_bg=BG)
        self.btn_probe.pack(side="left", padx=(10, 0))
        self.btn_check = FlatButton(bar, "检查上网状态", self.on_check, kind="ghost",
                                    padx=14, pady=9, parent_bg=BG)
        self.btn_check.pack(side="left", padx=(10, 0))
        self.btn_stop = FlatButton(bar, "停止", self.on_stop, kind="danger",
                                   padx=16, pady=9, parent_bg=BG)
        self.btn_stop.pack(side="right")
        self.btn_stop.set_enabled(False)

        # ---- 结果横幅 ----
        banner = tk.Frame(self, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        banner.pack(fill="x", pady=(0, 12))
        self._strip = tk.Frame(banner, bg=PRIMARY, width=3)
        self._strip.pack(side="left", fill="y")
        banner_body = tk.Frame(banner, bg=CARD)
        banner_body.pack(side="left", fill="x", expand=True, padx=16, pady=12)

        line = tk.Frame(banner_body, bg=CARD)
        line.pack(fill="x")
        self._state_dot = dot(line, MUTED, CARD, size=10)
        self._state_dot.pack(side="left", padx=(0, 8), pady=(4, 0))
        self.lbl_status = tk.Label(
            line, text="尚未开始认证。", font=f(10.5, "bold"), bg=CARD, fg=TEXT,
            anchor="w", justify="left", wraplength=820,
        )
        self.lbl_status.pack(side="left", fill="x", expand=True)
        self.lbl_portal = tk.Label(
            banner_body, text="认证页面地址：未设置", font=f(8.5), bg=CARD, fg=MUTED,
            anchor="w", justify="left", wraplength=820,
        )
        self.lbl_portal.pack(fill="x", pady=(4, 0))

        # ---- 控制台 ----
        console_card = Card(self, padding=0)
        console_card.pack(fill="both", expand=True)
        head = tk.Frame(console_card.body, bg=CARD)
        head.pack(fill="x", padx=16, pady=(12, 8))
        SectionTitle_inline = tk.Label(head, text="认证过程", font=f(10.5, "bold"), bg=CARD, fg=TEXT)
        SectionTitle_inline.pack(side="left")
        self.btn_browser = FlatButton(head, "在系统浏览器中打开", self.on_open_browser,
                                      kind="link", padx=4, pady=0)
        self.btn_browser.pack(side="right")
        FlatButton(head, "清空", self.on_clear, kind="link", padx=6, pady=0).pack(
            side="right", padx=(0, 8)
        )
        tk.Label(head, text="过程记录", font=f(8), bg=CARD, fg=FAINT).pack(
            side="left", padx=(8, 0)
        )

        wrap = tk.Frame(console_card.body, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.console = tk.Text(
            wrap, font=mono(9), bg=CONSOLE_BG, fg=CONSOLE_FG, relief="flat", bd=0,
            highlightthickness=0, wrap="word", padx=12, pady=10, height=8,
            insertwidth=0, cursor="arrow", selectbackground="#1e3a63",
            selectforeground="#ffffff",
        )
        self.console.pack(fill="both", expand=True)
        self.console.configure(state="disabled")
        self.console.tag_configure("good", foreground="#6ee7a8")
        self.console.tag_configure("bad", foreground="#ff9a9a")
        self.console.tag_configure("warn", foreground="#ffd479")
        self.console.tag_configure("muted", foreground=CONSOLE_DIM)
        self.console.tag_configure("time", foreground="#4f6b96", font=(MONO, 8))

    # ------------------------------------------------ 生命周期

    def on_show(self) -> None:
        cfg = self.app.reload_cfg()
        self._portal_url = cfg.portal_url or ""
        self._refresh_portal_label()
        if not self.console.get("1.0", "end").strip():
            self._append("准备就绪。", "muted")

    def _refresh_portal_label(self) -> None:
        cfg = self.app.reload_cfg()
        url = self._portal_url or cfg.portal_url
        if url:
            self.lbl_portal.configure(text=f"认证页面地址：{url}", fg=MUTED)
        else:
            self.lbl_portal.configure(
                text="认证页面地址：未设置。请点击「探测认证地址」，或在「设置」中填写。", fg=FAINT,
            )

    # ------------------------------------------------ 控制台

    def _append(self, text: str, tag: str | None = None) -> None:
        try:
            self.console.configure(state="normal")
            self.console.insert("end", time.strftime("%H:%M:%S "), "time")
            self.console.insert("end", text + "\n", tag or "")
            self.console.see("end")
            self.console.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _append_threadsafe(self, text: str, tag: str | None = None) -> None:
        self.app.post(lambda: self._append(text, tag))

    def on_clear(self) -> None:
        try:
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, text: str, color: str = TEXT) -> None:
        try:
            self.lbl_status.configure(text=text, fg=color)
            self._strip.configure(bg=color if color in (GOOD, BAD, WARN) else PRIMARY)
            self._state_dot.delete("all")
            dot_color = color if color in (GOOD, BAD, WARN) else MUTED
            self._state_dot.create_oval(1, 1, 9, 9, fill=dot_color, outline=dot_color)
        except Exception:  # noqa: BLE001
            pass

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for btn in (self.btn_browser_login, self.btn_login, self.btn_probe, self.btn_check):
            btn.set_enabled(not busy)
        self.btn_stop.set_enabled(busy)

    # ------------------------------------------------ 动作

    def on_browser_login(self) -> None:
        """浏览器模式：开一个无地址栏窗口，注入脚本填账号密码（学 CampusConnect）。"""
        if self._busy:
            return
        cfg = self.app.reload_cfg()
        if not cfg.portal_auth_enabled:
            self._append("「连接后进行网页认证」已关闭。", "warn")
            self._set_status("网页认证已关闭。请在「设置」中开启「连接后进行网页认证」。", WARN)
            return
        if not cfg.is_configured():
            self._append("尚未配置学号 / 上网账号与上网密码，请在「设置」中填写。", "warn")
            self._set_status("账号或密码未配置。", WARN)
            return
        self._stop.clear()
        self._set_busy(True)
        self._set_status("正在打开认证页面…", PRIMARY)
        self._append("浏览器模式：打开认证页面并填写已保存的账号。", "muted")

        def progress(text: str) -> None:
            self._append_threadsafe(text)

        def work():
            return svc.browser_login(progress=progress, stop=self._stop)

        def done(result: dict) -> None:
            self._set_busy(False)
            state = result.get("state") or ""
            if result.get("ok"):
                self._append(f"完成：{result.get('reason', '网络已通')}", "good")
                self._set_status("认证完成。", GOOD)
                try:
                    self.app.pages["overview"].refresh_light()
                except Exception:  # noqa: BLE001
                    pass
                return
            reason = result.get("reason") or "未知原因"
            if result.get("needs_user"):
                self._append(f"需要人工处理：{reason}", "warn")
                self._set_status(f"需要人工处理：{reason}", WARN)
            else:
                self._append(f"认证失败：{reason}", "bad")
                self._set_status(f"认证失败：{reason}", BAD)
            if state:
                self._append(f"页面状态：{state}", "muted")
            try:
                self.app.pages["overview"].refresh_light()
            except Exception:  # noqa: BLE001
                pass

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self._append(f"异常：{exc}", "bad")
            self._set_status(f"浏览器认证出错：{exc}", BAD)

        self.app.run_bg(work, done, failed)

    def on_login(self) -> None:
        if self._busy:
            return
        cfg = self.app.reload_cfg()
        if not cfg.portal_auth_enabled:
            self._append("「连接后进行网页认证」已关闭。", "warn")
            self._set_status("网页认证已关闭。请在「设置」中开启「连接后进行网页认证」。", WARN)
            return
        self._stop.clear()
        self._set_busy(True)
        self._set_status("正在认证…", PRIMARY)
        self._append("开始认证。")
        if cfg.autofill_click:
            self._append("自动提交已开启：使用已保存的账号与密码提交。", "muted")

        def progress(text: str) -> None:
            self._append_threadsafe(text)

        def work():
            return svc.login(progress=progress, stop=self._stop)

        def done(result: dict) -> None:
            self._set_busy(False)
            if result.get("ok"):
                state = status.from_auth(result)
                self._append(f"认证完成：{result.get('reason', '成功')}", "good")
                self._set_status(state.describe(), GOOD)
                try:
                    self.app.set_connection_state(state)
                    svc.record_last_result(result)
                    self.app.pages["overview"].refresh_light()
                except Exception:  # noqa: BLE001
                    pass
                return
            if result.get("stopped"):
                self._append("已停止。", "warn")
                self._set_status("已停止认证。", WARN)
                return
            # 用统一状态机区分"密码错 / 网络故障 / 认证服务器无响应"，
            # 不再把一切都写成"认证失败"，也不再一律引导用户去改密码。
            state = status.from_auth(result)
            reason = result.get("reason") or ""
            self._append(f"{state.label}：{reason or '未提供原因'}", "bad")
            self._set_status(state.describe(), severity_color(state.severity))
            if state.advice:
                self._append(f"建议：{state.advice}", "warn")
            try:
                self.app.set_connection_state(state)
                svc.record_last_result(result)
            except Exception:  # noqa: BLE001
                pass

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self._append(f"异常：{exc}", "bad")
            self._set_status(f"认证过程出错：{exc}", BAD)

        self.app.run_bg(work, done, failed)

    def on_probe(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._set_status("正在探测认证页面…", PRIMARY)
        self._append("正在探测认证页面地址…")

        def done(result: dict) -> None:
            self._set_busy(False)
            if not result.get("ok"):
                # 三态渲染：ONLINE（网络正常、当前不需要门户）与 UNKNOWN（探测失败
                # 或证据不足）必须区分开。以前这里一律 `探测失败：…` + `未探测到认证
                # 页面：…`，把"已经能上网"报成了程序故障（用户实际反馈的现象）。
                state = status.from_probe(result)
                self._append(state.describe(), state.severity)
                self._set_status(state.describe(), severity_color(state.severity))
                if result.get("needs_confirm"):
                    self._append(
                        "候选地址（已脱敏，未经确认不会用于认证）："
                        f"{result.get('candidate_url') or '(无)'}",
                        "muted",
                    )
                if result.get("manual_entry"):
                    self._append("可在「设置」里手工填写认证页面地址。", "muted")
                self._refresh_portal_label()
                return
            url = result.get("url") or ""
            self._portal_url = url
            self._append(f"认证页面：{url}", "good")
            if result.get("title"):
                self._append(f"页面标题：{result['title']}", "muted")
            self._append(
                f"类型：{result.get('portal_type') or '未知'}，表单数量 {result.get('form_count', 0)}",
                "muted",
            )
            for reason in (result.get("evidence") or [])[:4]:
                self._append(f"判断依据：{reason}", "muted")
            self._set_status("探测完成。可点击「打开认证页面并填写」继续。", GOOD)
            if result.get("persistable", True):
                self._persist_portal(url)
            self._refresh_portal_label()

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self._append(f"探测异常：{exc}", "bad")
            self._set_status(f"探测异常：{exc}", BAD)

        self.app.run_bg(svc.probe_portal, done, failed)

    def _persist_portal(self, url: str) -> None:
        if not url:
            return
        cfg = self.app.reload_cfg()
        current = (cfg.portal_url or "").strip()
        if current == url:
            return
        # 防"参数退化"：探测跟随重定向后拿到的是门户的"最终页地址"，常常不带
        # wlanuserip / wlanacname / nasip 等网关参数；用它覆盖发现阶段拿到的带参数
        # 地址会让后续认证丢参数（本机日志里配置的门户地址就这样从带参数 URL
        # 变成了裸 a79.htm）。
        if not keeps_portal_query(current, url):
            self._append(
                f"新探测到的地址比已保存的少参数，已保留原地址：{current}", "warn",
            )
            return
        cfg.portal_url = url
        ok, msg = self.app.save_cfg(cfg)
        if ok:
            self._append("认证页面地址已写入配置。", "muted")
        else:
            # 以前这里是静默失败：用户以为地址记住了，下次还得重填
            self._append(f"认证页面地址未能写入配置：{msg}", "warn")
            self._set_status(f"认证页面地址保存失败：{msg}", WARN)

    def on_check(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._set_status("正在检查上网状态…", PRIMARY)
        self.app.run_bg(svc.online_status, self._check_done, self._check_failed)

    def _check_done(self, result: dict) -> None:
        self._set_busy(False)
        state = status.from_check(result)
        self._append(f"{state.label}：{state.detail}", "good" if state.ok else "bad")
        self._set_status(state.describe(), severity_color(state.severity))
        try:
            self.app.set_connection_state(state)
        except Exception:  # noqa: BLE001
            pass
        portal = result.get("portal_url") or ""
        if not state.ok and portal:
            self._append(f"跳转至认证页面：{portal}", "muted")
            self._portal_url = portal
            self._refresh_portal_label()
        elif not state.ok and state.advice:
            self._append(f"建议：{state.advice}", "warn")

    def _check_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        state = status.internal_error(f"检查上网状态时发生异常：{exc}")
        self._append(f"{state.label}：{exc}", "bad")
        self._set_status(state.describe(), BAD)

    def on_stop(self) -> None:
        self._stop.set()
        self.btn_stop.set_enabled(False)
        self._append("已请求停止，等待当前请求完成。", "warn")
        # 浏览器模式留下的窗口也一起关掉。注意：关闭会话可能要 kill 进程并重试删除
        # 临时目录（最长约 3 秒），必须放后台，否则界面会假死。
        self.app.run_bg(
            svc.close_browser,
            lambda _ok: self._append("认证页面窗口已关闭。", "muted"),
            lambda exc: self._append(f"关闭认证页面窗口失败：{exc}", "warn"),
        )

    def on_open_browser(self) -> None:
        cfg = self.app.reload_cfg()
        url = self._portal_url or cfg.portal_url
        if not url:
            self._append("尚未配置认证页面地址。请点击「探测认证地址」。", "warn")
            self._set_status("尚未配置认证页面地址。", WARN)
            return
        if svc.open_in_browser(url):
            self._append(f"已在系统浏览器中打开：{url}", "muted")
        else:
            self._append("无法打开系统浏览器，可复制地址手动打开。", "bad")

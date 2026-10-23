"""设置页：左「目标网络」/ 右「上网账号」双栏，底部一条保存操作。"""

from __future__ import annotations

import tkinter as tk

from . import gui_service as svc
from .. import status
from ..logs import get_logger
from .ui_kit import (
    BAD, BG, BORDER, CARD, FAINT, GOOD, MUTED, PRIMARY, TEXT, WARN,
    Card, FlatButton, ScrollArea, Segmented, Toggle, bind_mousewheel, f,
    severity_color,
)

MODES = [
    ("开放网络", "open"),
    ("使用 Windows 已保存的配置", "saved"),
]


def _entry(parent: tk.Misc, variable: tk.Variable, *, show: str | None = None,
           width: int | None = None) -> tk.Entry:
    entry = tk.Entry(
        parent, textvariable=variable, font=f(10.5), bg="#ffffff", fg=TEXT,
        relief="flat", bd=0, highlightthickness=1,
        highlightbackground=BORDER, highlightcolor=PRIMARY,
        insertbackground=TEXT, show=show or "",
    )
    if width:
        entry.configure(width=width)
    entry.bind("<FocusIn>", lambda _e: entry.configure(highlightbackground=PRIMARY))
    entry.bind("<FocusOut>", lambda _e: entry.configure(highlightbackground=BORDER))
    return entry


class SettingsPage(tk.Frame):
    def __init__(self, parent: tk.Misc, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.var_ssid = tk.StringVar()
        self.var_mode = tk.StringVar(value="open")
        self.var_portal_auth = tk.BooleanVar(value=True)
        self.var_browser_fallback = tk.BooleanVar(value=False)
        self.var_portal = tk.StringVar()
        self.var_account = tk.StringVar()
        self.var_service = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_autofill = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=False)
        self._autostart_guard = False
        self._autostart_busy = False
        self._saving = False
        self._testing = False
        self._build()

    # ------------------------------------------------ 结构

    def _build(self) -> None:
        # ---- 底部操作（必须先占好底部位置：后面那双栏是 expand 的，会把空间吃光） ----
        footer = tk.Frame(self, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.btn_save_enable = FlatButton(
            footer, "保存并开启自动连接", lambda: self.on_save(enable=True), kind="primary",
            font=f(10, "bold"), padx=20, pady=9, parent_bg=BG,
        )
        self.btn_save_enable.pack(side="left")
        self.btn_save_only = FlatButton(
            footer, "保存设置", lambda: self.on_save(enable=False), kind="ghost",
            padx=16, pady=9, parent_bg=BG,
        )
        self.btn_save_only.pack(side="left", padx=(10, 0))
        # 「测试连接」：用已保存的凭据走一遍完整链路，回答"这套配置能不能上网"。
        # 与"保存"严格区分：保存只证明写盘成功，不代表认证成功。
        self.btn_test = FlatButton(
            footer, "测试连接", lambda: self.on_test_connection(), kind="soft",
            padx=16, pady=9, parent_bg=BG,
        )
        self.btn_test.pack(side="left", padx=(10, 0))
        tk.Label(footer, text="账号密码加密保存在本机；更换电脑后需重新填写。",
                 font=f(8.5), bg=BG, fg=FAINT).pack(side="right")

        # ---- 双栏（放进可滚动容器：不同 DPI/字体下内容变高也不会被窗口切掉） ----
        self.scroll = ScrollArea(self, bg=BG, height=320)
        self.scroll.pack(fill="both", expand=True)
        cols = tk.Frame(self.scroll.inner, bg=BG)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="col")
        cols.columnconfigure(1, weight=1, uniform="col")
        cols.rowconfigure(0, weight=1)

        # ======== 左：目标网络 ========
        net_card = Card(cols, padding=18)
        net_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        tk.Label(net_card.body, text="目标网络", font=f(11, "bold"), bg=CARD, fg=TEXT).pack(anchor="w")

        tk.Label(net_card.body, text="Wi-Fi 名称（SSID，精确匹配）", font=f(9), bg=CARD,
                 fg=MUTED).pack(anchor="w", pady=(14, 0))
        self.entry_ssid = _entry(net_card.body, self.var_ssid)
        self.entry_ssid.pack(fill="x", pady=(4, 4))
        tk.Label(net_card.body, text="与 Windows Wi-Fi 列表中的名称完全一致；后缀（如 -5G）不同视为不同网络。",
                 font=f(8.5), bg=CARD, fg=FAINT, wraplength=380, justify="left").pack(anchor="w")

        tk.Label(net_card.body, text="连接方式", font=f(9), bg=CARD, fg=MUTED).pack(
            anchor="w", pady=(16, 6)
        )
        Segmented(net_card.body, MODES, self.var_mode, bg=CARD).pack(anchor="w")
        tk.Label(
            net_card.body,
            text="开放网络：无已保存配置时自动创建配置文件。需密码或企业认证的网络，"
                 "请先在 Windows 中连接一次并保存。",
            font=f(8.5), bg=CARD, fg=FAINT, wraplength=380, justify="left",
        ).pack(anchor="w", pady=(8, 6))
        FlatButton(net_card.body, "打开 Windows Wi-Fi 设置", svc.open_wifi_settings,
                   kind="link", padx=2, pady=2).pack(anchor="w")

        self.auth_switch_row = tk.Frame(net_card.body, bg=CARD)
        self.auth_switch_row.pack(fill="x", pady=(18, 0))
        tk.Frame(self.auth_switch_row, bg=BORDER, height=1).pack(fill="x", pady=(0, 12))
        switch_text = tk.Frame(self.auth_switch_row, bg=CARD)
        switch_text.pack(side="left", fill="x", expand=True)
        tk.Label(switch_text, text="连接后进行网页认证", font=f(10, "bold"),
                 bg=CARD, fg=TEXT).pack(anchor="w")
        tk.Label(switch_text, text="关闭后仅连接 Wi-Fi，不提交认证。",
                 font=f(8.5), bg=CARD, fg=MUTED, wraplength=280, justify="left").pack(anchor="w", pady=(2, 0))
        self.toggle_auth = Toggle(self.auth_switch_row, self.var_portal_auth,
                                  command=self._apply_auth_state, bg=CARD)
        self.toggle_auth.pack(side="right", anchor="n")

        fb_row = tk.Frame(net_card.body, bg=CARD)
        fb_row.pack(fill="x", pady=(14, 0))
        fb_text = tk.Frame(fb_row, bg=CARD)
        fb_text.pack(side="left", fill="x", expand=True)
        tk.Label(fb_text, text="认证失败时用浏览器重试", font=f(10, "bold"),
                 bg=CARD, fg=TEXT).pack(anchor="w")
        tk.Label(fb_text, text="后台认证失败后打开浏览器重新认证。会弹出窗口，仅在需要时开启。",
                 font=f(8.5), bg=CARD, fg=MUTED, wraplength=280, justify="left").pack(
            anchor="w", pady=(2, 0))
        self.toggle_fallback = Toggle(fb_row, self.var_browser_fallback, bg=CARD)
        self.toggle_fallback.pack(side="right", anchor="n")

        # ======== 右：上网账号 ========
        acc_card = Card(cols, padding=18)
        acc_card.grid(row=0, column=1, sticky="nsew")

        tk.Label(acc_card.body, text="上网账号", font=f(11, "bold"), bg=CARD, fg=TEXT).pack(anchor="w")

        self.auth_box = tk.Frame(acc_card.body, bg=CARD)
        self.auth_box.pack(fill="x", pady=(14, 0))

        portal_row = tk.Frame(self.auth_box, bg=CARD)
        portal_row.pack(fill="x")
        tk.Label(portal_row, text="认证页面地址", font=f(9), bg=CARD, fg=MUTED).pack(side="left")
        FlatButton(portal_row, "探测", self.on_probe_portal, kind="link", padx=4, pady=0).pack(
            side="right"
        )
        self.entry_portal = _entry(self.auth_box, self.var_portal)
        self.entry_portal.pack(fill="x", pady=(4, 4))
        tk.Label(self.auth_box, text="填写认证网页地址；留空时每次自动探测。",
                 font=f(8.5), bg=CARD, fg=FAINT).pack(anchor="w", pady=(0, 12))

        tk.Label(self.auth_box, text="学号 / 上网账号", font=f(9), bg=CARD, fg=MUTED).pack(anchor="w")
        self.entry_account = _entry(self.auth_box, self.var_account)
        self.entry_account.pack(fill="x", pady=(4, 12))

        tk.Label(self.auth_box, text="运营商 / 服务选项", font=f(9), bg=CARD, fg=MUTED).pack(anchor="w")
        self.entry_service = _entry(self.auth_box, self.var_service)
        self.entry_service.pack(fill="x", pady=(4, 4))
        tk.Label(self.auth_box,
                 text="填写认证网页中的完整选项名称，例如中国移动、中国联通、中国电信。网页无此选项时留空。",
                 font=f(8.5), bg=CARD, fg=FAINT, wraplength=380, justify="left").pack(
            anchor="w", pady=(0, 12))

        tk.Label(self.auth_box, text="上网密码", font=f(9), bg=CARD, fg=MUTED).pack(anchor="w")
        pwd_row = tk.Frame(self.auth_box, bg=CARD)
        pwd_row.pack(fill="x", pady=(4, 4))
        self.entry_password = _entry(pwd_row, self.var_password, show="*")
        self.entry_password.pack(side="left", fill="x", expand=True)
        # 显示/隐藏切换：掩码输入看不清打错的字符，这里给一个显式开关
        self._password_hidden = True
        self.btn_show_password = FlatButton(
            pwd_row, "显示", self.on_toggle_password, kind="link", padx=8, pady=6,
        )
        self.btn_show_password.pack(side="right", padx=(8, 0))
        self.lbl_password_hint = tk.Label(self.auth_box, text="", font=f(8.5), bg=CARD, fg=FAINT,
                                          wraplength=380, justify="left")
        self.lbl_password_hint.pack(anchor="w")

        self.chk_autofill = tk.Checkbutton(
            self.auth_box, text="自动填写后点击登录", variable=self.var_autofill,
            font=f(9.5), bg=CARD, fg=TEXT, activebackground=CARD, activeforeground=TEXT,
            selectcolor="#ffffff", bd=0, highlightthickness=0, cursor="hand2",
        )
        self.chk_autofill.pack(anchor="w", pady=(4, 8))

        # 自定义接口（折叠）
        self.btn_advanced = FlatButton(
            self.auth_box, "高级设置 · 特殊认证页面", self.on_toggle_advanced,
            kind="link", padx=2, pady=2,
        )
        self.btn_advanced.pack(anchor="w")
        self.adv_box = tk.Frame(acc_card.body, bg=CARD)
        tk.Label(
            self.adv_box,
            text="留空时自动识别。",
            font=f(8.5), bg=CARD, fg=FAINT, wraplength=380, justify="left",
        ).pack(anchor="w", pady=(6, 8))
        self.var_custom_url = tk.StringVar()
        self.var_custom_user = tk.StringVar()
        self.var_custom_pass = tk.StringVar()
        for label, var in (
            ("提交地址", self.var_custom_url),
            ("账号字段名", self.var_custom_user),
            ("密码字段名", self.var_custom_pass),
        ):
            tk.Label(self.adv_box, text=label, font=f(9), bg=CARD, fg=MUTED).pack(anchor="w")
            _entry(self.adv_box, var).pack(fill="x", pady=(3, 8))

        tk.Frame(self.adv_box, bg=BORDER, height=1).pack(fill="x", pady=(6, 8))
        tk.Label(self.adv_box, text="CSS 选择器（可选）", font=f(9), bg=CARD,
                 fg=MUTED).pack(anchor="w")
        tk.Label(self.adv_box, text="留空时自动识别。可指定具体控件，例如 #username。",
                 font=f(8.5), bg=CARD, fg=FAINT, wraplength=380, justify="left").pack(
            anchor="w", pady=(3, 8))
        self.var_user_sel = tk.StringVar()
        self.var_pass_sel = tk.StringVar()
        self.var_submit_sel = tk.StringVar()
        for label, var in (
            ("账号输入框", self.var_user_sel),
            ("密码输入框", self.var_pass_sel),
            ("登录按钮", self.var_submit_sel),
        ):
            tk.Label(self.adv_box, text=label, font=f(9), bg=CARD, fg=MUTED).pack(anchor="w")
            _entry(self.adv_box, var).pack(fill="x", pady=(3, 8))

        # 滚轮要绑到后加进来的子控件上，否则鼠标在卡片上滚不动
        bind_mousewheel(self.scroll.inner, self.scroll.canvas)

        # ======== 下：启动与安全（V3.0 新增） ========
        start_card = Card(self.scroll.inner, padding=18)
        start_card.pack(fill="x", pady=(12, 4))
        body = start_card.body

        tk.Label(body, text="启动与安全", font=f(11, "bold"), bg=CARD, fg=TEXT).pack(anchor="w")

        self.autostart_row = tk.Frame(body, bg=CARD)
        self.autostart_row.pack(fill="x", pady=(12, 0))
        as_text = tk.Frame(self.autostart_row, bg=CARD)
        as_text.pack(side="left", fill="x", expand=True)
        tk.Label(as_text, text="开机自动启动后台守护", font=f(10, "bold"),
                 bg=CARD, fg=TEXT).pack(anchor="w")
        self.lbl_autostart = tk.Label(
            as_text, text="", font=f(8.5), bg=CARD, fg=FAINT, wraplength=560, justify="left",
        )
        self.lbl_autostart.pack(anchor="w", pady=(2, 0))
        self.toggle_autostart = Toggle(self.autostart_row, self.var_autostart,
                                       command=self.on_toggle_autostart, bg=CARD)
        self.toggle_autostart.pack(side="right", anchor="n")

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(14, 10))

        self.lbl_paths = tk.Label(
            body, text="", font=f(8.5), bg=CARD, fg=FAINT, wraplength=760, justify="left",
        )
        self.lbl_paths.pack(anchor="w")

        btn_row = tk.Frame(body, bg=CARD)
        btn_row.pack(fill="x", pady=(10, 0))
        self.btn_data_dir = FlatButton(btn_row, "打开数据目录", self.on_open_data_dir,
                                       kind="ghost", padx=12, pady=6, parent_bg=CARD)
        self.btn_data_dir.pack(side="left")
        self.btn_security = FlatButton(btn_row, "安全体检（只读）", self.on_security_check,
                                       kind="ghost", padx=12, pady=6, parent_bg=CARD)
        self.btn_security.pack(side="left", padx=(8, 0))
        self.btn_diag = FlatButton(btn_row, "导出脱敏诊断包", self.on_export_diagnostics,
                                   kind="ghost", padx=12, pady=6, parent_bg=CARD)
        self.btn_diag.pack(side="left", padx=(8, 0))
        self.btn_legacy = FlatButton(btn_row, "清理旧版自启动", self.on_cleanup_legacy,
                                     kind="ghost", padx=12, pady=6, parent_bg=CARD)
        self.btn_legacy.pack(side="left", padx=(8, 0))

        bind_mousewheel(start_card.body, self.scroll.canvas)

    # ------------------------------------------------ 生命周期

    def on_show(self) -> None:
        self.load_from_config()
        self._verify_saved_password_async()

    def _verify_saved_password_async(self) -> None:
        """后台复查"已保存的密码在本机能不能解开"。

        DPAPI 解密可能慢，所以放后台；结果只更新一行提示文字。
        这一步很有价值：换过 Windows 用户/电脑后，密文还在但解不开，
        如果不查就会一直显示"已保存密码"，而守护进程一启动就退出。
        """

        def done(result: dict) -> None:
            try:
                if not isinstance(result, dict):
                    return
                if result.get("has") and not result.get("ok"):
                    self.lbl_password_hint.configure(
                        text="已保存的密码在本机解不开（换过 Windows 用户或电脑），"
                             "请重新输入一次上网密码。",
                        fg=BAD,
                    )
                elif result.get("has"):
                    self.lbl_password_hint.configure(text="已保存密码；留空表示不修改。", fg=FAINT)
            except Exception:  # noqa: BLE001
                pass

        self.app.run_bg(svc.password_status, done, lambda _exc: None)

    def load_from_config(self, *, password_ok: bool | None = None) -> None:
        """把配置回填到控件。

        ``password_ok`` 为 None 时**不做 DPAPI 解密**（那一步可能慢到几秒，
        放在 UI 线程会让窗口卡住）；只有刚从后台校验拿回结果的调用点才传值。
        """
        cfg = self.app.reload_cfg()
        self.var_ssid.set(cfg.wifi_ssid or "")
        self.var_mode.set("saved" if cfg.wifi_connect_mode == "saved" else "open")
        self.var_portal_auth.set(bool(getattr(cfg, "portal_auth_enabled", True)))
        self.var_browser_fallback.set(bool(getattr(cfg, "browser_fallback", False)))
        self.var_portal.set(cfg.portal_url or "")
        self.var_account.set(cfg.username or "")
        self.var_service.set(getattr(cfg, "service_option", "") or "")
        self.var_password.set("")
        self.var_autofill.set(bool(cfg.autofill_click))
        self.var_user_sel.set(getattr(cfg, "user_selector", "") or "")
        self.var_pass_sel.set(getattr(cfg, "password_selector", "") or "")
        self.var_submit_sel.set(getattr(cfg, "submit_selector", "") or "")
        self.var_custom_url.set(cfg.custom.url or "")
        self.var_custom_user.set(cfg.custom.username_field or "")
        self.var_custom_pass.set(cfg.custom.password_field or "")
        if password_ok is False:
            self.lbl_password_hint.configure(
                text="已保存的密码无法在本机解密，请重新输入。", fg=BAD)
        elif cfg.password_enc:
            self.lbl_password_hint.configure(text="已保存密码；留空表示不修改。", fg=FAINT)
        else:
            self.lbl_password_hint.configure(text="尚未保存密码。", fg=FAINT)
        self.toggle_auth.refresh()
        self.toggle_fallback.refresh()
        self._apply_auth_state()
        self.refresh_system_state()

    # ------------------------------------------------ 启动与安全

    def refresh_system_state(self) -> None:
        """刷新自启动状态与数据目录说明（只读，不修改任何设置）。"""
        try:
            info = svc.autostart_status()
        except Exception as exc:  # noqa: BLE001
            info = {"enabled": False, "error": str(exc)}
        self._autostart_guard = True
        try:
            self.var_autostart.set(bool(info.get("enabled")))
            self.toggle_autostart.refresh()
        finally:
            self._autostart_guard = False

        if info.get("error"):
            hint = f"无法读取自启动设置：{info['error']}"
            fg = WARN
        elif info.get("stale"):
            hint = "自启动项指向的程序位置和当前不一致（可能搬过目录），建议重新开启一次。"
            fg = WARN
        elif info.get("enabled"):
            hint = "已开启：登录 Windows 后自动在后台守护，无需手动打开本程序。"
            fg = FAINT
        else:
            hint = "未开启：需要手动打开本程序才会自动认证。"
            fg = FAINT
        try:
            self.lbl_autostart.configure(text=hint, fg=fg)
        except Exception:  # noqa: BLE001
            pass

        try:
            self.lbl_paths.configure(
                text="配置、日志与备份都保存在：" + svc.data_dir()
                + "（安装目录只读，程序不会把个人数据写进去）"
            )
        except Exception:  # noqa: BLE001
            pass

    def on_toggle_autostart(self) -> None:
        """开机自启动开关。

        必须异步：打开时要写注册表**并拉起后台守护**，而
        ``start_daemon()`` 会轮询最多几秒等它就绪。以前这些都在 UI 线程里，
        表现为"点一下开关，窗口卡住几秒"—— 和保存按钮是同一类问题。
        """
        if self._autostart_guard or self._autostart_busy:
            return
        wanted = bool(self.var_autostart.get())
        self._autostart_busy = True
        try:
            self.toggle_autostart.set_enabled(False)
        except Exception:  # noqa: BLE001
            pass
        self.app.set_status("正在" + ("开启" if wanted else "关闭") + "开机自启动…", MUTED)

        def work() -> dict:
            ok, msg = svc.set_autostart(wanted)
            started = True
            start_msg = ""
            if ok and wanted:
                started, start_msg = svc.start_daemon()
            return {"ok": ok, "message": msg, "started": started, "start_message": start_msg}

        def done(result: dict) -> None:
            self._autostart_busy = False
            try:
                self.toggle_autostart.set_enabled(True)
            except Exception:  # noqa: BLE001
                pass
            if not result.get("ok"):
                self.app.set_status(result.get("message") or "设置失败。", BAD)
            elif wanted and not result.get("started", True):
                self.app.set_status(
                    f"{result.get('message')} 但后台守护没能启动：{result.get('start_message')}",
                    WARN,
                )
            else:
                self.app.set_status(result.get("message") or "已设置。", GOOD)
            self.refresh_system_state()

        def failed(exc: Exception) -> None:
            self._autostart_busy = False
            try:
                self.toggle_autostart.set_enabled(True)
            except Exception:  # noqa: BLE001
                pass
            self.app.set_status(f"设置失败：{exc}", BAD)
            self.refresh_system_state()

        self.app.run_bg(work, done, failed)

    def on_open_data_dir(self) -> None:
        if not svc.open_path(svc.data_dir()):
            self.app.set_status("无法打开数据目录：" + svc.data_dir(), WARN)

    def on_security_check(self) -> None:
        self.app.set_status("正在读取安全现状（只读，不会修改任何设置）…")
        self.btn_security.set_enabled(False)

        def done(report: dict) -> None:
            self.btn_security.set_enabled(True)
            products = "、".join(report.get("products") or []) or "未读取到"
            defender = report.get("defender") or {}
            if defender.get("ok"):
                state = "实时保护开启" if defender.get("realtime") else "实时保护未开启"
            else:
                state = f"状态不可用（{defender.get('error') or '未知'}）"
            self.app.set_status(
                f"杀毒软件：{products}；Defender {state}。"
                "本程序不需要任何排除项；详细建议见日志或诊断包。",
                GOOD,
            )
            try:
                log = svc.get_logger()
                for line in (report.get("advice") or []):
                    log.info("安全体检：%s", line)
            except Exception:  # noqa: BLE001
                pass

        def failed(exc: Exception) -> None:
            self.btn_security.set_enabled(True)
            self.app.set_status(f"安全体检失败：{exc}", BAD)

        self.app.run_bg(svc.security_report, done, failed)

    def on_export_diagnostics(self) -> None:
        self.app.set_status("正在导出脱敏诊断包…")
        self.btn_diag.set_enabled(False)

        def done(result) -> None:
            self.btn_diag.set_enabled(True)
            ok, info = result
            if ok:
                self.app.set_status(f"诊断包已导出：{info}（账号、密码、内网地址已打码）", GOOD)
                svc.open_path(info)
            else:
                self.app.set_status(info, BAD)

        def failed(exc: Exception) -> None:
            # 失败分支必须把按钮恢复，否则按钮会永久灰着（用户只能重启程序）
            try:
                self.btn_diag.set_enabled(True)
            except Exception:  # noqa: BLE001
                pass
            self.app.set_status(f"导出诊断包失败：{exc}", BAD)

        self.app.run_bg(svc.export_diagnostics, done, failed)

    def on_cleanup_legacy(self) -> None:
        self.app.set_status("正在清理旧版（V2）留下的自启动项…")
        self.btn_legacy.set_enabled(False)

        def work():
            from .. import autostart

            return autostart.cleanup_legacy()

        def done(done_list) -> None:
            self.btn_legacy.set_enabled(True)
            if not done_list:
                self.app.set_status("没有发现旧版自启动项，不需要清理。", GOOD)
                return
            self.app.set_status("；".join(done_list), GOOD)

        def failed(exc: Exception) -> None:
            try:
                self.btn_legacy.set_enabled(True)
            except Exception:  # noqa: BLE001
                pass
            self.app.set_status(f"清理旧版残留失败：{exc}", BAD)

        self.app.run_bg(work, done, failed)

    # ------------------------------------------------ 交互

    def _apply_auth_state(self) -> None:
        on = bool(self.var_portal_auth.get())
        try:
            for child in self.auth_box.winfo_children():
                _enable_recursive(child, on)
        except Exception:  # noqa: BLE001
            pass

    def on_toggle_advanced(self) -> None:
        if self.adv_box.winfo_ismapped():
            self.adv_box.pack_forget()
            self.btn_advanced.set_text("高级设置 · 特殊认证页面")
        else:
            self.adv_box.pack(fill="x")
            self.btn_advanced.set_text("收起高级设置")

    def on_probe_portal(self) -> None:
        self.app.set_status("正在探测认证页面…")
        self.btn_save_enable.set_enabled(False)

        def done(result: dict) -> None:
            self.btn_save_enable.set_enabled(True)
            # 三态渲染：ONLINE（网络正常，本来就不该有门户）/ UNKNOWN（探测失败或
            # 证据不足）/ PORTAL_SUSPECTED（确实发现了门户）。以前这里一律写
            # "探测失败"，把"已经能上网"报成了程序故障。
            state = status.from_probe(result)
            if not result.get("ok"):
                self.app.set_status(state.describe(), severity_color(state.severity))
                return
            url = result.get("url") or ""
            if url:
                self.var_portal.set(url)
            self.app.set_status(
                f"{state.label}：{url}"
                + (f"（{result.get('portal_type')}）" if result.get("portal_type") else ""),
                GOOD,
            )

        def failed(exc: Exception) -> None:
            self.btn_save_enable.set_enabled(True)
            self.app.set_status(f"探测异常：{exc}", BAD)

        self.app.run_bg(svc.probe_portal, done, failed)

    def on_save(self, *, enable: bool) -> None:
        """保存设置。

        **必须整段异步**（历史教训：点完按钮窗口卡成"未响应"）。
        保存要做的三件事都可能慢到几秒，而且都不是我们能控制的：

        1. ``CryptProtectData``（DPAPI 加密密码）—— 域账户/漫游主密钥、
           或杀软挂钩 DPAPI 时可能往返网络；
        2. 写 ``%LOCALAPPDATA%`` 下的配置文件 —— 实时防护会先扫一遍；
        3. 回读校验（DPAPI 解密）—— 同上。

        这些以前全在主线程做，用户看到的就是"点保存 → 未响应"。
        现在：UI 线程只读控件、做纯文本校验，然后把加密+写盘+回读交给后台线程，
        期间按钮置灰并显示"正在保存…"。
        """
        # ---- 1. UI 线程：读控件 + 纯校验（不碰 DPAPI、不写盘）----
        ssid = self.var_ssid.get().strip()
        account = self.var_account.get().strip()
        password = self.var_password.get()
        portal_auth = bool(self.var_portal_auth.get())

        if enable and not ssid:
            self.app.set_status("请填写 Wi-Fi 名称。", WARN)
            self.entry_ssid.focus_set()
            return
        if portal_auth and not account:
            self.app.set_status("请填写学号 / 上网账号。", WARN)
            self.entry_account.focus_set()
            return
        if portal_auth and not password and not bool(self.app.cfg.password_enc):
            self.app.set_status("请填写上网密码。", WARN)
            self.entry_password.focus_set()
            return
        if self._saving:
            self.app.set_status("正在保存，请稍候…", MUTED)
            return

        # 把控件里的值一次性取出来（后台线程里不能再碰 Tk 控件）
        values = {
            "wifi_ssid": ssid,
            "wifi_connect_mode": "saved" if self.var_mode.get() == "saved" else "open",
            "portal_auth_enabled": portal_auth,
            "browser_fallback": bool(self.var_browser_fallback.get()),
            "portal_url": self.var_portal.get().strip(),
            "username": account,
            "service_option": self.var_service.get().strip(),
            "autofill_click": bool(self.var_autofill.get()),
            "user_selector": self.var_user_sel.get().strip(),
            "password_selector": self.var_pass_sel.get().strip(),
            "submit_selector": self.var_submit_sel.get().strip(),
            "custom_url": self.var_custom_url.get().strip(),
            "custom_user": self.var_custom_user.get().strip(),
            "custom_password": self.var_custom_pass.get().strip(),
            "auto_enabled": True if enable else None,
        }

        # ---- 2. 后台线程：加密 + 原子写 + 回读校验 ----
        def work() -> dict:
            return svc.save_settings(values, password, enable=enable)

        self._begin_saving()
        self.app.run_bg(work, self._save_done, self._save_failed)

    def _begin_saving(self) -> None:
        self._saving = True
        try:
            self.btn_save_enable.set_enabled(False)
            self.btn_save_only.set_enabled(False)
        except Exception:  # noqa: BLE001
            pass
        self.app.set_status("正在保存…（加密密码并写入配置）", MUTED)

    def _end_saving(self) -> None:
        self._saving = False
        try:
            self.btn_save_enable.set_enabled(True)
            self.btn_save_only.set_enabled(True)
        except Exception:  # noqa: BLE001
            pass

    def _save_done(self, result: dict) -> None:
        self._end_saving()
        if not result.get("ok"):
            self.app.set_status(result.get("message") or "保存失败。", BAD)
            return

        cfg = result.get("config")
        if cfg is not None:
            self.app.apply_saved_config(cfg)
        self.var_password.set("")
        self.load_from_config(password_ok=result.get("password_ok"))
        try:
            overview = self.app.pages["overview"]
            overview.sync_from_config()
            overview.refresh_all()
        except Exception:  # noqa: BLE001
            pass

        if not result.get("password_ok", True):
            self.app.set_status(
                "配置已保存，但本机无法解密刚保存的密码（更换 Windows 用户或电脑后会出现这种情况）。"
                "请重新输入一次上网密码。",
                WARN,
            )
            return

        if result.get("enable"):
            # 关键：写盘成功 ≠ 自动连接已生效。
            # 必须确认后台守护真的跑起来，否则界面会同时显示"自动连接已开启"和"守护未运行"。
            self.app.set_status("配置已保存。正在启动后台守护…", MUTED)
            self._start_daemon_then_report()
        else:
            # 文案严格区分：这里只证明配置写入成功，不代表认证或联网成功。
            self.app.set_status("配置保存成功。账号信息已保存，可以进行连接测试。", GOOD)

    def _save_failed(self, exc: Exception) -> None:
        self._end_saving()
        self.app.set_status(f"保存失败：{exc}", BAD)

    def _start_daemon_then_report(self) -> None:
        """保存并开启自动连接后，真的把后台守护拉起来，并按实际结果报告。"""

        def done(payload) -> None:
            try:
                started, message = payload
            except Exception:  # noqa: BLE001
                started, message = False, "返回值异常"
            if started:
                self.app.set_status("配置保存成功，自动连接已开启（后台守护运行中）。", GOOD)
            else:
                self.app.set_status(
                    f"配置保存成功，但后台守护未能启动：{message} "
                    "自动连接暂时不会生效，可点「立即连接」手动认证，或稍后在「启动与安全」重试。",
                    WARN,
                )
            try:
                self.app.pages["overview"].refresh_all()
            except Exception:  # noqa: BLE001
                pass
            # 只在用户还停留在设置页时才跳转，避免把正在看别的页面的人强行拉走
            try:
                if self.app.current == "settings":
                    self.app.navigate("overview")
            except Exception:  # noqa: BLE001
                pass

        def failed(exc: Exception) -> None:
            self.app.set_status(
                f"配置保存成功，但启动后台守护失败：{exc}。可点「立即连接」手动认证。", WARN
            )

        self.app.run_bg(svc.start_daemon, done, failed)

    def on_toggle_password(self) -> None:
        """显示/隐藏密码。"""
        try:
            self._password_hidden = not self._password_hidden
            self.entry_password.configure(show="*" if self._password_hidden else "")
            self.btn_show_password.set_text("显示" if self._password_hidden else "隐藏")
            self.entry_password.focus_set()
        except Exception as exc:  # noqa: BLE001
            self.app.set_status(f"切换密码显示失败：{exc}", WARN)

    def on_test_connection(self) -> None:
        """测试连接：用已保存的凭据做一次真实探测 + 认证 + 复核，不改配置。"""
        if self._testing:
            return
        self._testing = True
        try:
            self.btn_test.set_enabled(False)
            self.btn_test.set_text("测试中…")
        except Exception:  # noqa: BLE001
            pass
        self.app.set_status("正在测试连接（检查网络 → 认证 → 复核外网），请稍候…", MUTED)

        def done(result: dict) -> None:
            self._finish_test()
            state = status.from_auth(result)
            self.app.set_status(state.describe(), severity_color(state.severity))
            self.app.set_connection_state(state)
            try:
                self.app.pages["overview"].refresh_all()
            except Exception:  # noqa: BLE001
                pass

        def failed(exc: Exception) -> None:
            self._finish_test()
            state = status.internal_error(f"测试连接时发生异常：{exc}")
            self.app.set_status(state.describe(), BAD)
            try:
                get_logger().warning("测试连接失败：%s", exc)
            except Exception:  # noqa: BLE001
                pass

        self.app.run_bg(svc.test_connection, done, failed)

    def _finish_test(self) -> None:
        self._testing = False
        try:
            self.btn_test.set_enabled(True)
            self.btn_test.set_text("测试连接")
        except Exception:  # noqa: BLE001
            pass


def _enable_recursive(widget: tk.Misc, enabled: bool) -> None:
    """整块启用/禁用（tk 没有容器级禁用，只能逐个设置）。"""
    state = "normal" if enabled else "disabled"
    try:
        if isinstance(widget, FlatButton):
            widget.set_enabled(enabled)
        elif isinstance(widget, (tk.Entry, tk.Checkbutton)):
            widget.configure(state=state)
        elif isinstance(widget, tk.Label):
            widget.configure(fg=MUTED if enabled else FAINT)
    except Exception:  # noqa: BLE001
        pass
    for child in widget.winfo_children():
        _enable_recursive(child, enabled)

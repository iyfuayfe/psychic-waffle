"""概览页：一张带主色边的状态卡 + 三块指标 + 左列表右开关的双栏。

和"大标题 + 单列卡片"的常见布局不同，这里的信息层级是：
    状态卡（现在怎么样） → 指标带（网络 / 信号 / 认证） → 双栏（附近网络 / 自动连接）
"""

from __future__ import annotations

import time
import tkinter as tk

from . import gui_service as svc
from .. import paths, status
from .ui_kit import (
    BAD, BG, BORDER, CARD, FAINT, GOOD, MUTED, PRIMARY, TEXT, WARN,
    Badge, Card, FlatButton, ScrollArea, SectionTitle, StatTile, Toggle, dot, f,
    severity_color,
)

REFRESH_WIFI_MS = 15_000
REFRESH_SCAN_MS = 45_000


class OverviewPage(tk.Frame):
    def __init__(self, parent: tk.Misc, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self._networks: list[dict] = []
        self._scan_error: str = ""
        self._wifi: dict = {}
        self._online: bool | None = None
        self._scan_busy = False
        self._light_busy = False
        self._connect_busy = False
        self._check_busy = False
        self._last_state_text = ""
        self._wifi_timer = None
        self._scan_timer = None
        self._active = False
        self._build()
        self.refresh_all()

    # ------------------------------------------------ 结构

    def _build(self) -> None:
        # ---- 状态卡 ----
        hero = Card(self, padding=18, accent=PRIMARY)
        hero.pack(fill="x", pady=(0, 12))
        hero_row = hero.body

        info = tk.Frame(hero_row, bg=CARD)
        info.pack(side="left", fill="x", expand=True)

        head = tk.Frame(info, bg=CARD)
        head.pack(anchor="w")
        self._state_dot = dot(head, WARN, CARD, size=10)
        self._state_dot.pack(side="left", padx=(0, 8), pady=(4, 0))
        self.lbl_state = tk.Label(head, text="正在检测…", font=f(13, "bold"), bg=CARD, fg=TEXT)
        self.lbl_state.pack(side="left")
        self.badge_online = Badge(head, "检测中", fg=MUTED, bg="#eef2f8")
        self.badge_online.pack(side="left", padx=(10, 0), pady=(3, 0))

        tiles = tk.Frame(info, bg=CARD)
        tiles.pack(fill="x", pady=(12, 0))
        self.tile_net = StatTile(tiles, "无线网络", "—")
        self.tile_net.pack(side="left")
        tk.Frame(tiles, bg=BORDER, width=1, height=30).pack(side="left", padx=16)
        self.tile_signal = StatTile(tiles, "信号强度", "—")
        self.tile_signal.pack(side="left")
        tk.Frame(tiles, bg=BORDER, width=1, height=30).pack(side="left", padx=16)
        self.tile_auth = StatTile(tiles, "上网认证", "—")
        self.tile_auth.pack(side="left")
        tk.Frame(tiles, bg=BORDER, width=1, height=30).pack(side="left", padx=16)
        # 账号配置状态：解决"换了电脑还显示自动连接已开启、但守护其实 need-user"
        self.tile_account = StatTile(tiles, "账号", "—")
        self.tile_account.pack(side="left")

        # ---- 状态面板：后台守护 / 最近一次结果 / 最近一次时间 ----
        panel = tk.Frame(info, bg=CARD)
        panel.pack(fill="x", pady=(10, 0))
        self.lbl_daemon_row = tk.Label(
            panel, text="后台守护：检测中", font=f(8.5), bg=CARD, fg=FAINT, anchor="w",
        )
        self.lbl_daemon_row.pack(anchor="w")
        self.lbl_last_row = tk.Label(
            panel, text="最近一次：还没有连接记录", font=f(8.5), bg=CARD, fg=FAINT, anchor="w",
            wraplength=430, justify="left",
        )
        self.lbl_last_row.pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(hero_row, bg=CARD)
        actions.pack(side="right", anchor="n")
        self.btn_connect = FlatButton(actions, "立即连接", self.on_connect_now, kind="primary",
                                      font=f(10, "bold"), padx=18, pady=9)
        self.btn_connect.pack(side="left", padx=(0, 8))
        FlatButton(actions, "去设置", lambda: self.app.navigate("settings"), kind="ghost",
                   padx=16, pady=9).pack(side="left")

        # ---- 双栏 ----
        cols = tk.Frame(self, bg=BG)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=0, minsize=306)
        cols.rowconfigure(0, weight=1)

        # 左：附近网络
        net_card = Card(cols, padding=0)
        net_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        net_head = tk.Frame(net_card.body, bg=CARD)
        net_head.pack(fill="x", padx=16, pady=(14, 8))
        titles = tk.Frame(net_head, bg=CARD)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text="附近的无线网络", font=f(10.5, "bold"), bg=CARD, fg=TEXT).pack(anchor="w")
        self.lbl_updated = tk.Label(titles, text="尚未扫描", font=f(8.5), bg=CARD, fg=FAINT)
        self.lbl_updated.pack(anchor="w", pady=(2, 0))
        self.btn_refresh = FlatButton(net_head, "重新扫描", self.refresh_scan, kind="soft",
                                      padx=12, pady=5)
        self.btn_refresh.pack(side="right", anchor="n")

        self.list = ScrollArea(net_card.body, bg=CARD, height=200)
        self.list.pack(fill="both", expand=True, padx=6, pady=(0, 10))
        self._render_networks()

        # 右：自动连接 + 快捷操作
        side = tk.Frame(cols, bg=BG)
        side.grid(row=0, column=1, sticky="nsew")

        auto_card = Card(side, padding=16)
        auto_card.pack(fill="x")
        auto_head = tk.Frame(auto_card.body, bg=CARD)
        auto_head.pack(fill="x")
        auto_text = tk.Frame(auto_head, bg=CARD)
        auto_text.pack(side="left", fill="x", expand=True)
        tk.Label(auto_text, text="自动连接并认证", font=f(10.5, "bold"), bg=CARD, fg=TEXT).pack(anchor="w")
        self.lbl_auto_desc = tk.Label(
            auto_text, text="发现目标网络后连接，并使用已保存的账号完成认证。", font=f(8.5), bg=CARD, fg=MUTED,
            wraplength=210, justify="left", anchor="w",
        )
        self.lbl_auto_desc.pack(anchor="w", pady=(3, 0))
        self.toggle_auto = Toggle(auto_head, self.app.var_auto, command=self.on_toggle_auto, bg=CARD)
        self.toggle_auto.pack(side="right", anchor="n")

        quick_card = Card(side, padding=16)
        quick_card.pack(fill="both", expand=True, pady=(12, 0))
        SectionTitle(quick_card.body, "快捷操作").pack(fill="x", pady=(0, 10))
        for label, command in (
            ("检查上网状态", self.on_check_online),
            ("在系统浏览器中打开认证页面", self.on_open_browser),
            ("查看日志", self.on_open_log),
            ("打开 Windows Wi-Fi 设置", svc.open_wifi_settings),
        ):
            FlatButton(quick_card.body, "▸  " + label, command, kind="link", padx=2, pady=4).pack(
                anchor="w", pady=1
            )

            tk.Frame(quick_card.body, bg=BORDER, height=1).pack(fill="x")

    # ------------------------------------------------ 生命周期

    def on_show(self) -> None:
        self._active = True
        self.sync_from_config()
        self.refresh_light()
        self._schedule()

    def on_hide(self) -> None:
        self._active = False
        for timer in (self._wifi_timer, self._scan_timer):
            if timer is not None:
                try:
                    self.after_cancel(timer)
                except Exception:  # noqa: BLE001
                    pass
        self._wifi_timer = self._scan_timer = None

    def _schedule(self) -> None:
        if not self._active:
            return
        # 先取消旧定时器：重复进入本页（例如再点一次当前页签）会再调 _schedule，
        # 不取消就会叠加出多条轮询链（实测每点一次多一条 15s/45s 链）。
        self._cancel_timers()
        try:
            self._wifi_timer = self.after(REFRESH_WIFI_MS, self._tick_light)
            self._scan_timer = self.after(REFRESH_SCAN_MS, self._tick_scan)
        except Exception:  # noqa: BLE001
            pass

    def _cancel_timers(self) -> None:
        for timer in (self._wifi_timer, self._scan_timer):
            if timer is not None:
                try:
                    self.after_cancel(timer)
                except Exception:  # noqa: BLE001
                    pass
        self._wifi_timer = self._scan_timer = None

    def _tick_light(self) -> None:
        if not self._active:
            return
        self.refresh_light()
        self._wifi_timer = self.after(REFRESH_WIFI_MS, self._tick_light)

    def _tick_scan(self) -> None:
        if not self._active:
            return
        self.refresh_scan()
        self._scan_timer = self.after(REFRESH_SCAN_MS, self._tick_scan)

    # ------------------------------------------------ 配置同步

    def sync_from_config(self) -> None:
        cfg = self.app.reload_cfg()
        try:
            self.app.var_auto.set(bool(cfg.auto_enabled) and cfg.is_configured())
        except Exception:  # noqa: BLE001
            pass
        self.toggle_auto.refresh()
        target = (cfg.wifi_ssid or "").strip()
        if target:
            self.lbl_auto_desc.configure(text=f"发现 {target} 后连接，并使用已保存的账号完成认证。")
        else:
            self.lbl_auto_desc.configure(text="发现目标网络后连接，并使用已保存的账号完成认证。")

    # ------------------------------------------------ 刷新

    def refresh_all(self) -> None:
        self.sync_from_config()
        self.refresh_light()
        self.refresh_scan()

    def refresh_light(self) -> None:
        if self._light_busy:
            return
        self._light_busy = True

        def work():
            return {"wifi": svc.wifi_snapshot(), "online": svc.online_status()}

        self.app.run_bg(work, self._apply_light, self._light_failed)

    def _light_failed(self, exc: Exception) -> None:
        """轻量刷新失败：hero 与徽标也要更新，否则会永久停在"正在检测…"。"""
        self._light_busy = False
        self.tile_auth.set("检测失败", WARN)
        try:
            self.lbl_state.configure(text="网络状态未知", fg=WARN)
            self._paint_dot(WARN)
            self._set_badge("检测失败", WARN, "#fdf3e7")
            self.app.set_status(f"网络状态检测失败：{exc}", WARN)
            self.btn_connect.set_enabled(not self._connect_busy)
        except Exception:  # noqa: BLE001
            pass

    def _apply_light(self, data: dict) -> None:
        self._light_busy = False
        wifi = data.get("wifi") or {}
        online = data.get("online") or {}
        self._wifi = wifi
        self._online = bool(online.get("online"))
        cfg = self.app.reload_cfg()
        target = (cfg.wifi_ssid or "").strip()

        if wifi.get("connected"):
            ssid = wifi.get("ssid") or ""
            self.lbl_state.configure(text=f"已连接 {ssid}".strip(), fg=TEXT)
            self._paint_dot(GOOD)
            self.tile_net.set(ssid or "—")
            self.tile_signal.set(f"{wifi.get('signal', 0)}%")
            self.btn_connect.set_enabled(not self._connect_busy)
        elif wifi.get("available"):
            self.lbl_state.configure(text="无线网络未连接", fg=TEXT)
            self._paint_dot(WARN)
            self.tile_net.set(target or "未设置")
            self.tile_signal.set("—")
            self.btn_connect.set_enabled(bool(target) and not self._connect_busy)
        else:
            self.lbl_state.configure(text="未检测到无线网卡", fg=TEXT)
            self._paint_dot(BAD)
            self.tile_net.set("不可用")
            self.tile_signal.set("—")
            self.btn_connect.set_enabled(False)

        # 用统一状态机渲染"联网"结论：区分"需要认证 / 网络不可用 / 已连接"，
        # 不再把一切 online=False 都写成"未认证"。
        state = status.from_check(online)
        self.apply_connection_state(state)
        self._refresh_panel()

    def apply_connection_state(self, state) -> None:
        """把统一状态机的结论渲染到 hero / 徽标 / 认证格 / 账号格。

        界面只负责显示，判定都在 ``campusnet/status.py`` 里做，
        因此不会出现"接口在就显示已连接""失败还显示成功"这类不一致。
        """
        try:
            color = severity_color(state.severity)
            self.lbl_state.configure(text=state.label, fg=color)
            self._paint_dot(color)
            badge_bg = {"good": "#e8f6ee", "warn": "#fdf3e7", "bad": "#fdeceb"}.get(
                state.severity, "#eef2f8")
            self._set_badge(state.label, color, badge_bg)
            self.tile_auth.set(state.label, color)
            self._last_state_text = state.describe()
            if state.kind == status.KIND_ONLINE:
                self.tile_auth.set("已认证", GOOD)
        except Exception:  # noqa: BLE001  渲染失败不影响业务结果
            pass

    def _refresh_panel(self) -> None:
        """刷新"账号 / 后台守护 / 最近一次结果"三行留痕信息。"""
        try:
            cfg = self.app.cfg
            if not cfg.username or not cfg.password_enc:
                self.tile_account.set("未配置", WARN)
            elif svc.has_saved_password(cfg):
                self.tile_account.set("已配置", GOOD)
            else:
                self.tile_account.set("待重设密码", WARN)
        except Exception:  # noqa: BLE001
            self.tile_account.set("—")

        try:
            health = self.app.daemon_health()
            label = str(health.get("label") or "状态未知")
            if health.get("healthy"):
                color = GOOD
            elif health.get("running"):
                color = BAD
            else:
                color = FAINT
            detail = str(health.get("detail") or health.get("reason") or "")
            text = f"后台守护：{label}"
            if detail:
                text += f" · {detail[:60]}"
            self.lbl_daemon_row.configure(text=text, fg=color)
        except Exception:  # noqa: BLE001
            self.lbl_daemon_row.configure(text="后台守护：状态未知", fg=FAINT)

        try:
            summary, stamp = svc.last_result_text()
            when = time.strftime("%m-%d %H:%M", time.localtime(stamp)) if stamp else ""
            text = f"最近一次：{summary}"
            if when:
                text += f"（{when}）"
            if self._last_state_text:
                text += f" · 当前：{self._last_state_text[:70]}"
            self.lbl_last_row.configure(text=text, fg=FAINT)
        except Exception:  # noqa: BLE001
            pass

    def on_open_log(self) -> None:
        """打开日志文件（在系统默认程序里），并把最近几行写进状态栏。"""
        try:
            tail = svc.log_tail(3)
            if tail:
                self.app.set_status("最近日志：" + " / ".join(line.strip()[:60] for line in tail), MUTED)
        except Exception:  # noqa: BLE001
            pass
        target = ""
        try:
            target = str(paths.log_file())
        except Exception:  # noqa: BLE001
            target = ""
        if not target:
            self.app.set_status("找不到日志文件位置。", WARN)
            return
        # ShellExecute 可能阻塞上百毫秒，放后台
        self.app.run_bg(
            lambda: svc.open_path(target),
            lambda ok: None if ok else self.app.set_status(f"无法打开日志文件：{target}", WARN),
        )

    def _paint_dot(self, color: str) -> None:
        try:
            self._state_dot.delete("all")
            self._state_dot.create_oval(1, 1, 9, 9, fill=color, outline=color)
        except Exception:  # noqa: BLE001
            pass

    def _set_badge(self, text: str, fg: str, bg: str) -> None:
        try:
            self.badge_online.configure(text=text, fg=fg, bg=bg)
        except Exception:  # noqa: BLE001
            pass

    def refresh_scan(self) -> None:
        if self._scan_busy:
            return
        self._scan_busy = True
        self.lbl_updated.configure(text="正在扫描…")
        self.app.run_bg(svc.scan_networks, self._apply_scan, self._scan_failed)

    def _scan_failed(self, exc: Exception) -> None:
        self._scan_busy = False
        self.lbl_updated.configure(text=f"扫描失败：{exc}")

    def _apply_scan(self, result) -> None:
        self._scan_busy = False
        nets, error = result if isinstance(result, tuple) else ([], "")
        self._networks = nets
        self._scan_error = error
        if error:
            # 扫描失败的原因要留在界面上（例如"需要开启位置权限"），
            # 而不是只显示"0 个网络"让用户以为附近没有 Wi-Fi。
            self.lbl_updated.configure(text=f"扫描失败：{error[:60]}")
        else:
            self.lbl_updated.configure(text=f"{len(nets)} 个网络 · 更新于 {time.strftime('%H:%M')}")
        self._render_networks()

    # ------------------------------------------------ 列表渲染

    def _render_networks(self) -> None:
        self.list.clear()
        cfg = self.app.reload_cfg()
        target = (cfg.wifi_ssid or "").strip()
        current = (self._wifi or {}).get("ssid") or ""

        if not self._networks:
            error = getattr(self, "_scan_error", "") or ""
            if error:
                # 说清楚"为什么没有列表"，并给出下一步（手动填 SSID 仍然可用）
                tk.Label(
                    self.list.inner, text="无法读取附近的 Wi-Fi 列表",
                    font=f(10, "bold"), bg=CARD, fg=WARN, pady=(16, 2),
                ).pack(fill="x")
                tk.Label(
                    self.list.inner, text=error, font=f(8.5), bg=CARD, fg=MUTED,
                    wraplength=520, justify="left",
                ).pack(fill="x", pady=(0, 6))
                tk.Label(
                    self.list.inner,
                    text="不影响使用：可以直接在「设置」里手动填写 Wi-Fi 名称（SSID）。",
                    font=f(8.5), bg=CARD, fg=FAINT, wraplength=520, justify="left",
                ).pack(fill="x", pady=(0, 16))
            else:
                tk.Label(
                    self.list.inner, text="未扫描到网络。请点击「重新扫描」。",
                    font=f(9.5), bg=CARD, fg=FAINT, pady=20,
                ).pack(fill="x")
            return

        for net in self._networks:
            ssid = net.get("ssid") or ""
            row = tk.Frame(self.list.inner, bg=CARD)
            row.pack(fill="x")
            inner = tk.Frame(row, bg=CARD)
            inner.pack(fill="x", padx=10, pady=8)

            left = tk.Frame(inner, bg=CARD)
            left.pack(side="left", fill="x", expand=True)
            title_row = tk.Frame(left, bg=CARD)
            title_row.pack(anchor="w")
            tk.Label(title_row, text=ssid, font=f(10.5, "bold"), bg=CARD, fg=TEXT).pack(side="left")
            if ssid == target:
                Badge(title_row, "目标").pack(side="left", padx=(8, 0))
            tk.Label(left, text=net.get("label", ""), font=f(9), bg=CARD, fg=MUTED).pack(
                anchor="w", pady=(2, 0)
            )

            right = tk.Frame(inner, bg=CARD)
            right.pack(side="right")
            if ssid == current:
                tk.Label(right, text="已连接", font=f(9), bg=CARD, fg=GOOD).pack(side="left")
            else:
                FlatButton(right, "设为目标", lambda s=ssid: self.set_target(s),
                           kind="link", padx=6, pady=2).pack(side="left")

            tk.Frame(self.list.inner, bg=BORDER, height=1).pack(fill="x", padx=10)

    # ------------------------------------------------ 动作

    def set_target(self, ssid: str) -> None:
        cfg = self.app.reload_cfg()
        cfg.wifi_ssid = ssid
        ok, msg = self.app.save_cfg(cfg)
        if ok:
            self.app.set_status(f"已将 {ssid} 设为目标网络。", GOOD)
            self.sync_from_config()
            self._render_networks()
        else:
            self.app.set_status(msg, BAD)

    def on_toggle_auto(self) -> None:
        cfg = self.app.reload_cfg()
        enabled = bool(self.app.var_auto.get())
        if enabled and not cfg.is_configured():
            self.app.var_auto.set(False)
            self.toggle_auto.refresh()
            self.app.set_status("请先在「设置」中填写学号 / 上网账号与上网密码。", WARN)
            self.app.navigate("settings")
            return
        cfg.auto_enabled = enabled
        ok, msg = self.app.save_cfg(cfg)
        if ok:
            self.app.set_status("自动连接已开启。" if enabled else "自动连接已关闭：后台仅检查网络状态。",
                                GOOD if enabled else MUTED)
        else:
            self.app.set_status(msg, BAD)
            self.app.var_auto.set(not enabled)
            self.toggle_auto.refresh()

    def on_connect_now(self) -> None:
        if self._connect_busy:
            self.app.set_status("正在连接中，请稍候…", MUTED)
            return
        cfg = self.app.reload_cfg()
        target = (cfg.wifi_ssid or "").strip()
        self._connect_busy = True
        self.btn_connect.set_enabled(False)
        self.btn_connect.set_text("连接中…")
        state = status.checking("正在连接无线网络并认证…")
        self.apply_connection_state(state)
        self.app.set_status("正在连接并认证…", MUTED)

        def work():
            return svc.connect_and_login(target, cfg.wifi_connect_mode,
                                         progress=self.app.progress_to_status)

        def finish(result: dict) -> None:
            self._connect_busy = False
            self.btn_connect.set_text("立即连接")
            self.btn_connect.set_enabled(True)
            state = status.from_auth(result)
            self.apply_connection_state(state)
            self.app.set_connection_state(state)
            self.app.set_status(state.describe(), severity_color(state.severity))
            # 留痕：最近一次结果与时间（面板会显示）
            try:
                svc.record_last_result(result)
            except Exception:  # noqa: BLE001
                pass
            self.refresh_all()

        def done(result: dict) -> None:
            finish(result)

        def failed(exc: Exception) -> None:
            finish({"ok": False, "status": "server-error", "reason": f"连接过程异常：{exc}"})

        self.app.run_bg(work, done, failed)

    def on_check_online(self) -> None:
        if self._check_busy:
            return
        self._check_busy = True
        self.app.set_status("正在检查上网状态…", MUTED)

        def done(result: dict) -> None:
            self._check_busy = False
            state = status.from_check(result)
            self.apply_connection_state(state)
            self.app.set_connection_state(state)
            self.app.set_status(state.describe(), severity_color(state.severity))
            self.refresh_light()

        def failed(exc: Exception) -> None:
            self._check_busy = False
            self.app.set_status(f"检查异常：{exc}", BAD)

        self.app.run_bg(svc.online_status, done, failed)

    def on_open_browser(self) -> None:
        cfg = self.app.reload_cfg()

        def work():
            url = (cfg.portal_url or "").strip()
            if url:
                return url
            probe = svc.online_status()
            return (probe.get("portal_url") or "").strip()

        def done(url: str) -> None:
            if not url:
                self.app.set_status(
                    "尚未配置认证页面地址。请在「设置」中点击「探测」或手工填写。", WARN
                )
                return
            # 打开外部浏览器可能阻塞 1~2 秒，放到后台线程
            self.app.run_bg(
                lambda: svc.open_in_browser(url),
                lambda ok: self.app.set_status(
                    f"已在系统浏览器中打开：{url}" if ok else f"无法打开系统浏览器：{url}",
                    GOOD if ok else BAD,
                ),
            )

        self.app.run_bg(work, done, lambda exc: self.app.set_status(f"异常：{exc}", BAD))

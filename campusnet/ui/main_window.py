"""校园网自动登录 · 控制台 —— 主窗口（V3.0 唯一界面）。

界面结构：

    顶部应用栏（品牌 + 后台守护状态）
    横向页签（概览 / 设置 / 认证）
    内容区（每页自己排版：指标带、双栏、控制台）
    底部状态栏（一行小字）

三条底线不变：不弹任何 messagebox；界面绝不阻塞（网络都在后台线程）；
关掉窗口不影响开机自启的后台自动登录。

V3.0 的变化：

* 入口统一到本模块，``CampusNet.exe``（正常启动）与 ``CampusNetSetup.exe``
  （``--setup`` 引导模式）都走这里；旧的 ``gui_setup.pyw`` / ``gui_status.pyw``
  退化成转调本模块的薄壳。
* 版本号、日志目录、配置文件路径全部来自 :mod:`campusnet.paths` 与
  :mod:`campusnet.version`，打包后指向用户数据目录而不是安装目录。
* 首次启动（还没配置账号）自动落到「设置」页并给出引导文案。
* 后台守护状态改为读**心跳文件 + 互斥体**，能显示"现在在干什么"，而不是只有
  "运行中 / 未运行"。
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback

import tkinter as tk

from .. import paths
from ..autostart import status_dict as autostart_status
from ..conflicts import describe as describe_conflicts
from ..conflicts import detect as detect_conflicts
from .. import status
from ..logs import get_logger
from ..session import begin as begin_session
from ..session import describe_previous, end as end_session
from ..session import refresh as refresh_session
from ..session import take_previous
from ..version import APP_NAME_CN as APP_NAME
from ..version import APP_SUBTITLE as APP_SUB
from ..version import __version__ as VERSION
from . import gui_service as svc
from .page_auth import AuthPage
from .page_overview import OverviewPage
from .page_settings import SettingsPage
from .ui_kit import (
    BAD, BG, BORDER, CARD, FAINT, GOOD, MUTED, PRIMARY, PRIMARY_SOFT, TEXT, WARN,
    apply_theme, f, rounded_rect,
)

WINDOW_W, WINDOW_H = 1020, 700

TABS = [
    ("overview", "概览"),
    ("settings", "设置"),
    ("auth", "认证"),
]

# 心跳状态名 → 界面文案统一由 campusnet.daemon.STATE_TEXT 提供，
# 这里不再维护第二份（两处各写一份最容易漂移）。


class TabItem(tk.Label):
    """横向页签：选中时蓝底白字，未选中时透明底灰字。"""

    def __init__(self, parent: tk.Misc, text: str, command):
        super().__init__(
            parent, text=text, font=f(10), bg=CARD, fg=MUTED,
            padx=16, pady=6, bd=0, cursor="hand2",
        )
        self._command = command
        self._active = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.set_active(False)

    def _click(self, _event=None):
        try:
            self._command()
        except Exception:  # noqa: BLE001
            pass

    def _enter(self, _event=None):
        if not self._active:
            try:
                self.configure(bg=PRIMARY_SOFT, fg=PRIMARY)
            except Exception:  # noqa: BLE001
                pass

    def _leave(self, _event=None):
        if not self._active:
            try:
                self.configure(bg=CARD, fg=MUTED)
            except Exception:  # noqa: BLE001
                pass

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        try:
            if self._active:
                self.configure(bg=PRIMARY, fg="#ffffff", font=f(10, "bold"))
            else:
                self.configure(bg=CARD, fg=MUTED, font=f(10))
        except Exception:  # noqa: BLE001
            pass


def _logo(parent: tk.Misc, size: int = 28) -> tk.Canvas:
    """品牌小方块：蓝色圆角方块 + 白色无线信号弧。"""
    canvas = tk.Canvas(parent, width=size, height=size, bg=CARD, highlightthickness=0, bd=0)
    rounded_rect(canvas, 0, 0, size, size, 8, fill=PRIMARY, outline=PRIMARY)
    cx = size / 2
    base = size * 0.70
    for radius in (size * 0.16, size * 0.28, size * 0.40):
        canvas.create_arc(
            cx - radius, base - radius, cx + radius, base + radius,
            start=38, extent=104, style="arc", outline="#ffffff", width=2,
        )
    canvas.create_oval(cx - 2, base - 2, cx + 2, base + 2, fill="#ffffff", outline="#ffffff")
    return canvas


class App:
    def __init__(self, root: tk.Tk, start_page: str = "overview",
                 previous_session: dict | None = None):
        self.root = root
        self._previous_session = previous_session
        self.q: queue.Queue = queue.Queue()
        self._closed = False
        self._after_id = None
        self._status_timer = None
        self._conflict_timer = None
        self._seen_conflicts: set = set()
        self._daemon_health_cache: dict = {}
        self._conn_state = None
        self._tk_error = ""
        self.cfg = svc.load_config()
        self.var_auto = tk.BooleanVar(value=bool(self.cfg.auto_enabled) and self.cfg.is_configured())
        self.pages: dict[str, tk.Frame] = {}
        self.tabs: dict[str, TabItem] = {}
        self.current = ""

        root.title(f"{APP_NAME} · 控制台")
        root.configure(bg=BG)
        root.minsize(940, 640)
        _center(root, WINDOW_W, WINDOW_H)
        apply_theme(root)

        # ---------- 顶部应用栏 ----------
        appbar = tk.Frame(root, bg=CARD)
        appbar.pack(side="top", fill="x")
        bar = tk.Frame(appbar, bg=CARD)
        bar.pack(fill="x", padx=20, pady=(12, 10))

        brand = tk.Frame(bar, bg=CARD)
        brand.pack(side="left")
        _logo(brand).pack(side="left", padx=(0, 10))
        brand_text = tk.Frame(brand, bg=CARD)
        brand_text.pack(side="left")
        tk.Label(brand_text, text=APP_NAME, font=f(12, "bold"), bg=CARD, fg=TEXT).pack(anchor="w")
        tk.Label(brand_text, text=APP_SUB, font=f(8), bg=CARD, fg=FAINT).pack(anchor="w")

        self.var_daemon = tk.StringVar(value="后台守护：检测中")
        self.lbl_daemon = tk.Label(
            bar, textvariable=self.var_daemon, font=f(8.5), bg=CARD, fg=FAINT,
        )
        self.lbl_daemon.pack(side="right")
        tk.Frame(appbar, bg=BORDER, height=1).pack(fill="x")

        # ---------- 横向页签 ----------
        tabbar = tk.Frame(root, bg=CARD)
        tabbar.pack(side="top", fill="x")
        tabs_inner = tk.Frame(tabbar, bg=CARD)
        tabs_inner.pack(fill="x", padx=16, pady=(4, 6))
        for key, label in TABS:
            item = TabItem(tabs_inner, label, lambda k=key: self.navigate(k))
            item.pack(side="left", padx=2)
            self.tabs[key] = item
        tk.Frame(tabbar, bg=BORDER, height=1).pack(fill="x")

        # ---------- 底部状态栏 ----------
        statusbar = tk.Frame(root, bg=CARD)
        statusbar.pack(side="bottom", fill="x")
        tk.Frame(statusbar, bg=BORDER, height=1).pack(fill="x")
        status_inner = tk.Frame(statusbar, bg=CARD)
        status_inner.pack(fill="x", padx=20, pady=6)
        self.var_status = tk.StringVar(value="就绪")
        self.lbl_status = tk.Label(
            status_inner, textvariable=self.var_status, font=f(9), bg=CARD, fg=MUTED,
            anchor="w", justify="left",
        )
        self.lbl_status.pack(side="left", fill="x", expand=True)
        tk.Label(
            status_inner, text=f"账号密码加密保存在本机 · v{VERSION}",
            font=f(8.5), bg=CARD, fg=FAINT,
        ).pack(side="right")

        # ---------- 内容区 ----------
        self.host = tk.Frame(root, bg=BG)
        self.host.pack(side="top", fill="both", expand=True, padx=24, pady=(16, 14))

        self.pages["overview"] = OverviewPage(self.host, self)
        self.pages["settings"] = SettingsPage(self.host, self)
        self.pages["auth"] = AuthPage(self.host, self)

        self.navigate(start_page if start_page in self.pages else "overview")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.report_exception
        self._after_id = self.root.after(80, self._drain)
        self._status_timer = self.root.after(500, self._tick_daemon)
        self._first_run_hint()
        self._report_config_problem()
        self._check_environment_conflicts()

    # ------------------------------------------------ 线程 / 队列

    def post(self, fn) -> None:
        if fn is not None and not self._closed:
            self.q.put(fn)

    def run_bg(self, work, done=None, failed=None) -> None:
        def runner():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                if failed is not None:
                    self.post(lambda e=exc: failed(e))
                return
            if done is not None:
                self.post(lambda r=result: done(r))

        threading.Thread(target=runner, daemon=True).start()

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
                self._tk_error = traceback.format_exc()
        if self._closed:
            return
        try:
            self._after_id = self.root.after(80, self._drain)
        except Exception:  # noqa: BLE001
            pass

    def report_exception(self, exc, val, tb) -> None:
        try:
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            with open(paths.log_dir() / "gui_error.log", "a", encoding="utf-8") as fh:
                fh.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------ 状态

    def set_status(self, text: str, color: str = MUTED) -> None:
        def apply():
            try:
                self.var_status.set(text)
                self.lbl_status.configure(fg=color)
            except Exception:  # noqa: BLE001
                pass

        self.post(apply)

    def progress_to_status(self, text: str) -> None:
        self.set_status(text, MUTED)

    def set_connection_state(self, state) -> None:
        """记录并渲染"当前联网/认证状态"（唯一权威来源，见 campusnet/status.py）。

        各页面不再自己拼状态文字：谁拿到真实结果，就调这里更新一次。
        """
        self._conn_state = state
        try:
            overview = self.pages.get("overview")
            if overview is not None and hasattr(overview, "apply_connection_state"):
                self.post(lambda s=state: overview.apply_connection_state(s))
        except Exception:  # noqa: BLE001  渲染失败不能影响业务结果
            pass

    def connection_state(self):
        return self._conn_state

    def daemon_health(self) -> dict:
        """最近一次探测到的后台守护健康状态（由 _tick_daemon 每 10 秒更新）。"""
        return dict(self._daemon_health_cache or {})

    def _report_config_problem(self) -> None:
        """启动时若配置文件读不出来，必须让用户看到（以前是静默降级成默认配置）。"""
        try:
            problem = svc.last_config_error()
            if not problem:
                return
            state = status.config_error(problem)
            self.set_connection_state(state)
            self.set_status(state.describe(), BAD)
            get_logger().error("配置读取失败：%s", problem)
        except Exception:  # noqa: BLE001
            pass

    def _first_run_hint(self) -> None:
        """首次启动：把用户直接带到该填的地方，并说明下一步。"""
        try:
            cfg = self.cfg
            if cfg.is_configured():
                return
            self.navigate("settings")
            self.set_status(
                "欢迎使用。请先填写 Wi-Fi 名称与上网账号密码，然后点「保存并开启自动连接」。",
                WARN,
            )
        except Exception:  # noqa: BLE001
            pass

    def _check_environment_conflicts(self, *, first: bool = True) -> None:
        """启动自检：有没有会注入本进程、害得界面闪退的第三方叠加软件。

        真实事故：游戏加加注入的 GPP64.dll 会让任何 tkinter 程序在 3 秒左右
        崩溃（进程直接消失，连日志都留不下）。

        关键点：这类叠加软件往往是**看到窗口才注入**的（实测在窗口创建前
        枚举模块看不到它），所以这里不能只在启动时查一次 —— 启动时查一次，
        之后每 2 秒再看一次，一旦发现就把模块名与来源写进会话标记，
        这样即便进程随后被打崩，下次启动也能说清楚是谁干的。
        """
        try:
            log = get_logger()
            hits = detect_conflicts()
            new = [hit for hit in hits if hit.module not in self._seen_conflicts]
            for hit in new:
                self._seen_conflicts.add(hit.module)
                log.error(
                    "自检发现注入模块：%s（%s）路径=%s。%s",
                    hit.module, hit.vendor, hit.path, hit.advice,
                )
            if new:
                message = describe_conflicts(hits)
                log.error("自检结论：%s", message)
                self.set_status(message, BAD)
                # 把线索写进会话标记：万一进程随后崩溃，下次启动能读到
                refresh_session(extra={"conflicts": [hit.module for hit in hits],
                                       "conflict_vendor": [hit.vendor for hit in hits]})
            elif first:
                # 还没被注入，但如果叠加软件在跑，就要提前告知风险
                # （它们的注入是条件触发的：可能这次没注入，下次就注入了）
                try:
                    from .. import hardening

                    overlays = hardening.status().get("overlay_processes") or []
                except Exception:  # noqa: BLE001
                    overlays = []
                if overlays:
                    vendors = "、".join(item["vendor"] for item in overlays)
                    log.warning("检测到叠加类软件正在运行：%s", vendors)
                    self.set_status(
                        f"检测到 {vendors} 正在运行。它会向本程序注入 DLL，可能导致界面几秒后消失；"
                        "已尝试进程加固，若仍闪退请退出该软件或在其中排除 CampusNet.exe。",
                        WARN,
                    )

            if first:
                previous = self._previous_session
                if previous:
                    text = describe_previous(previous)
                    log.warning("自检：%s", text)
                    if not new:
                        self.set_status(text, WARN)
        except Exception:  # noqa: BLE001  自检失败绝不能影响使用
            pass
        finally:
            if not self._closed:
                try:
                    self._conflict_timer = self.root.after(
                        2000, lambda: self._check_environment_conflicts(first=False)
                    )
                except Exception:  # noqa: BLE001
                    pass

    def _tick_daemon(self) -> None:
        """每 10 秒刷新一次"后台守护"的真实状态。

        判定依据是**心跳新鲜度**（见 daemon.daemon_health），不是"进程还在"：
        后台卡死时界面会明确显示"运行中但无响应"，而不是继续显示正常运行。
        """

        def work():
            try:
                return {"health": svc.daemon_status(), "autostart": autostart_status()}
            except Exception as exc:  # noqa: BLE001  状态查询失败也要能显示原因
                return {"error": str(exc)}

        def done(info) -> None:
            try:
                if not info or info.get("error"):
                    self.var_daemon.set("后台守护：状态未知")
                    self.lbl_daemon.configure(fg=FAINT)
                    if info and info.get("error"):
                        get_logger().warning("查询后台守护状态失败：%s", info["error"])
                    return
                health = info.get("health") or {}
                label = str(health.get("label") or "状态未知")
                text = "后台守护：" + label
                if health.get("healthy"):
                    color = GOOD
                elif health.get("running"):
                    color = BAD          # 进程在但不响应：这是异常，必须显眼
                else:
                    color = WARN
                    if info.get("autostart", {}).get("enabled"):
                        text += "（已设为开机启动）"
                self.var_daemon.set(text)
                self.lbl_daemon.configure(fg=color)
                self._daemon_health_cache = health
            except Exception:  # noqa: BLE001
                pass

        self.run_bg(work, done)
        if not self._closed:
            try:
                self._status_timer = self.root.after(10_000, self._tick_daemon)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------ 配置

    def reload_cfg(self):
        self.cfg = svc.load_config()
        return self.cfg

    def save_cfg(self, cfg) -> tuple[bool, str]:
        ok, msg = svc.save_config(cfg)
        if ok:
            self.apply_saved_config(cfg)
        return ok, msg

    def apply_saved_config(self, cfg) -> None:
        """把"已经写盘的配置"同步到界面状态（只改内存与控件，不做 IO）。"""
        try:
            self.cfg = cfg
            self.var_auto.set(bool(cfg.auto_enabled) and cfg.is_configured())
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------ 页面切换

    def navigate(self, key: str) -> None:
        if key not in self.pages:
            return
        if key == self.current:
            try:
                self.pages[key].on_show()
            except Exception:  # noqa: BLE001
                pass
            return
        for name, tab in self.tabs.items():
            tab.set_active(name == key)
        if self.current:
            try:
                self.pages[self.current].on_hide()
            except Exception:  # noqa: BLE001
                pass
            self.pages[self.current].pack_forget()
        self.current = key
        page = self.pages[key]
        page.pack(fill="both", expand=True)
        try:
            page.on_show()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------ 关闭

    def on_close(self) -> None:
        self._closed = True
        for timer in (self._after_id, self._status_timer, self._conflict_timer):
            if timer is not None:
                try:
                    self.root.after_cancel(timer)
                except Exception:  # noqa: BLE001
                    pass
        self._after_id = self._status_timer = self._conflict_timer = None
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _center(root: tk.Tk, width: int, height: int) -> None:
    try:
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = max(0, int((screen_w - width) / 2))
        y = max(0, int((screen_h - height) / 2) - 24)
        root.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:  # noqa: BLE001
        root.geometry(f"{width}x{height}")


def _parse_start_page(argv: list) -> str:
    """支持 ``--page overview|settings|auth`` 与 ``--setup``。"""
    if "--setup" in argv:
        return "settings"
    if "--page" in argv:
        index = argv.index("--page")
        if index + 1 < len(argv) and argv[index + 1] in dict(TABS):
            return argv[index + 1]
    return "overview"


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    start_page = _parse_start_page(argv)

    # **先读**上一次的异常退出记录，再写本次标记（顺序不能反，否则会自己抹掉线索）
    try:
        previous = take_previous()
    except Exception:  # noqa: BLE001
        previous = None

    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return 1

    # 进程加固是**可选功能**（3.1.0 起默认关闭，理由见 campusnet/hardening.py）：
    # 只有用户显式设置 CAMPUSNET_HARDEN=1 才施加进程缓解策略。默认路径不做任何
    # 改变进程安全属性的操作，也就不需要"先隐藏窗口再显示"这套动作。
    hardening_report: dict = {}
    try:
        from .. import hardening

        if hardening.requested_mode() == hardening.MODE_OPT_IN:
            # 显式选择加固：此刻 Tk 的 DLL 已全部加载完，先藏窗口再上锁，
            # 免得窗口一出现就被注入（这是该策略唯一有意义的施加时机）。
            root.withdraw()
            hardening_report = hardening.self_protect()
            get_logger().info("进程加固（可选，用户已开启）：%s",
                              hardening.describe(hardening_report))
        else:
            # 默认路径：bootstrap 里的 early_protect() 已经记录了决策，这里只写日志
            get_logger().info("进程加固：%s", hardening.describe())
    except Exception as exc:  # noqa: BLE001  加固失败绝不能导致界面打不开
        try:
            get_logger().warning("进程加固检查失败（已忽略，不影响使用）：%s", exc)
        except Exception:  # noqa: BLE001
            pass

    # 会话标记：正常关闭会删掉它；如果进程被第三方 DLL 打崩，下次启动
    # 就能识别出"上次界面异常退出"并提示（见 session.py 的说明）。
    begin_session("gui")
    app = None
    try:
        app = App(root, start_page=start_page, previous_session=previous)
        try:
            root.deiconify()          # 可选加固时窗口曾被隐藏，这里统一恢复显示
        except Exception:  # noqa: BLE001
            pass
        root.mainloop()
    except Exception:  # noqa: BLE001
        try:
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            (paths.log_dir() / "gui_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        if app is not None and app._tk_error:
            try:
                paths.log_dir().mkdir(parents=True, exist_ok=True)
                (paths.log_dir() / "gui_error.log").write_text(app._tk_error, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        if app is not None and app._closed:
            end_session()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""界面基础件：蓝白配色 + 卡片 / 按钮 / 开关 / 指标块 / 深色控制台。

设计取向（刻意和常见"左侧导航 + 白卡片"的同类工具拉开距离）：
- **顶部应用栏 + 横向页签**，没有左侧导航栏；
- 主色是蓝（#1a6fe0），大面积留白，描边极淡（#dbe4f0），不用阴影；
- 数据用"指标块（StatTile）"横向排布，而不是一整块大标题；
- 认证过程用**深蓝墨色控制台**，和浅色卡片形成对比。

只用 tkinter/ttk（标准库），不引入第三方依赖。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ---------- 蓝白配色 ----------
BG = "#f2f6fc"            # 页面底色（很浅的蓝灰）
CARD = "#ffffff"
BORDER = "#dbe4f0"        # 卡片描边
BORDER_STRONG = "#c3d2e6"
TEXT = "#14213d"          # 主文字（深藏青）
MUTED = "#4d5d76"
FAINT = "#71809a"

PRIMARY = "#1a6fe0"       # 主蓝
PRIMARY_DARK = "#155bb8"
PRIMARY_SOFT = "#e7f0ff"  # 浅蓝底（选中/标签）

GOOD = "#0b8a43"
BAD = "#d92d20"
WARN = "#b54708"

# 认证控制台（深蓝墨色）
CONSOLE_BG = "#0b1b33"
CONSOLE_FG = "#d7e3f7"
CONSOLE_DIM = "#7d93b8"

# 兼容旧名字（页面里还在用 ACCENT）
ACCENT = PRIMARY
ACCENT_DARK = PRIMARY_DARK
ACCENT_SOFT = PRIMARY_SOFT
SIDEBAR = CARD

FONT = "Microsoft YaHei UI"
MONO = "Consolas"


def f(size: float = 9.5, weight: str = "normal") -> tuple:
    """字体助手。Tk 只接受整数字号，9.5 这种会直接抛 TclError，所以统一取整。"""
    px = int(size + 0.5) if isinstance(size, float) else int(size)
    if weight == "normal":
        return (FONT, px)
    return (FONT, px, weight)


def mono(size: float = 9) -> tuple:
    px = int(size + 0.5) if isinstance(size, float) else int(size)
    return (MONO, px)


# ---------- 主题 ----------

def apply_theme(root: tk.Misc) -> None:
    try:
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure(
            "TScrollbar", gripcount=0, background="#c9d6e8", darkcolor="#c9d6e8",
            lightcolor="#c9d6e8", troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
            arrowsize=12, relief="flat",
        )
        style.map("TScrollbar", background=[("active", "#aebfd6")])
    except Exception:  # noqa: BLE001
        pass


# ---------- 基础小工具 ----------

def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)


def hline(parent: tk.Misc, bg: str = BORDER, **pack_kw) -> tk.Frame:
    line = tk.Frame(parent, bg=bg, height=1)
    line.pack(fill="x", **pack_kw)
    return line


def dot(parent: tk.Misc, color: str = GOOD, bg: str = CARD, size: int = 9) -> tk.Canvas:
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
    c.create_oval(1, 1, size - 1, size - 1, fill=color, outline=color)
    return c


def severity_color(severity: str) -> str:
    """把 ``campusnet.status`` 的严重程度翻译成界面颜色。

    统一在这里转换，避免每个页面各写一份 ``good->GOOD`` 的映射（写漏一处就会出现
    "失败显示成灰字"这种不一致）。
    """
    from .. import status

    return {
        status.SEV_GOOD: GOOD,
        status.SEV_WARN: WARN,
        status.SEV_BAD: BAD,
    }.get(severity, MUTED)


def bind_mousewheel(widget: tk.Misc, canvas: tk.Canvas) -> None:
    def on_wheel(event):
        try:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:  # noqa: BLE001
            pass

    def walk(w):
        try:
            w.bind("<MouseWheel>", on_wheel)
        except Exception:  # noqa: BLE001
            pass
        for child in w.winfo_children():
            walk(child)

    walk(widget)


def _bg_of(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except Exception:  # noqa: BLE001
        return CARD


# ---------- 卡片 ----------

class Card(tk.Frame):
    """白底 + 1px 淡蓝描边的卡片；内容放进 .body。"""

    def __init__(self, parent: tk.Misc, *, padding: int = 16, bg: str = CARD,
                 accent: str | None = None, **kw):
        super().__init__(
            parent, bg=bg, highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=BORDER, bd=0, **kw
        )
        if accent:
            # 左侧一条主色边（用来标记"这是关键卡片"）
            tk.Frame(self, bg=accent, width=3).pack(side="left", fill="y")
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(side="left", fill="both", expand=True, padx=padding, pady=padding)


class SectionTitle(tk.Frame):
    """卡片里的小标题 + 可选说明。"""

    def __init__(self, parent: tk.Misc, title: str, desc: str = "", *, bg: str = CARD,
                 wraplength: int = 0):
        super().__init__(parent, bg=bg)
        tk.Label(self, text=title, font=f(10.5, "bold"), bg=bg, fg=TEXT).pack(anchor="w")
        if desc:
            kw = {"wraplength": wraplength, "justify": "left"} if wraplength else {}
            tk.Label(self, text=desc, font=f(9), bg=bg, fg=MUTED, **kw).pack(anchor="w", pady=(3, 0))


class Badge(tk.Label):
    """小圆角标签，例如「已连接」「目标」。"""

    def __init__(self, parent: tk.Misc, text: str, *, fg: str = PRIMARY,
                 bg: str = PRIMARY_SOFT, **kw):
        super().__init__(parent, text=text, font=f(8), bg=bg, fg=fg, padx=7, pady=1, bd=0, **kw)


class StatTile(tk.Frame):
    """指标块：上面一行小标签，下面一行数值/状态。三块横向排开就是一条状态带。"""

    def __init__(self, parent: tk.Misc, label: str, value: str = "-", *,
                 bg: str = CARD, value_color: str = TEXT):
        super().__init__(parent, bg=bg)
        tk.Label(self, text=label, font=f(8.5), bg=bg, fg=FAINT).pack(anchor="w")
        self.value = tk.Label(self, text=value, font=f(11, "bold"), bg=bg, fg=value_color)
        self.value.pack(anchor="w", pady=(2, 0))

    def set(self, value: str, color: str = TEXT) -> None:
        try:
            self.value.configure(text=value, fg=color)
        except Exception:  # noqa: BLE001
            pass


# ---------- 按钮 ----------

class FlatButton(tk.Label):
    """扁平按钮。kind: primary / ghost / soft / link / danger。"""

    _KINDS = {
        "primary": {"bg": PRIMARY, "fg": "#ffffff", "hover": PRIMARY_DARK, "border": ""},
        "ghost": {"bg": CARD, "fg": TEXT, "hover": PRIMARY_SOFT, "border": BORDER_STRONG},
        "soft": {"bg": PRIMARY_SOFT, "fg": PRIMARY, "hover": "#d8e7ff", "border": ""},
        "link": {"bg": "", "fg": PRIMARY, "hover": PRIMARY_SOFT, "border": ""},
        "danger": {"bg": CARD, "fg": BAD, "hover": "#fdeceb", "border": BORDER_STRONG},
    }

    def __init__(self, parent: tk.Misc, text: str, command=None, *, kind: str = "primary",
                 font: tuple | None = None, padx: int = 14, pady: int = 7,
                 parent_bg: str | None = None, width: int | None = None):
        spec = self._KINDS.get(kind, self._KINDS["primary"])
        base_bg = spec["bg"] or parent_bg or _bg_of(parent)
        super().__init__(
            parent, text=text, font=font or f(9.5), bg=base_bg, fg=spec["fg"],
            padx=padx, pady=pady, bd=0, cursor="hand2", takefocus=0,
        )
        if width:
            self.configure(width=width)
        if spec["border"]:
            self.configure(highlightthickness=1, highlightbackground=spec["border"],
                           highlightcolor=spec["border"])
        self._spec = spec
        self._base_bg = base_bg
        self._command = command
        self._enabled = True
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        try:
            if self._enabled:
                self.configure(fg=self._spec["fg"], bg=self._base_bg, cursor="hand2")
            else:
                self.configure(fg=FAINT, bg=self._base_bg, cursor="arrow")
        except Exception:  # noqa: BLE001
            pass

    def set_text(self, text: str) -> None:
        try:
            self.configure(text=text)
        except Exception:  # noqa: BLE001
            pass

    def _on_click(self, _event=None):
        if not self._enabled or self._command is None:
            return
        try:
            self._command()
        except Exception:  # noqa: BLE001  不让异常冒到 Tk 的后台错误处理器
            pass

    def _on_enter(self, _event=None):
        if self._enabled and self._spec["hover"]:
            try:
                self.configure(bg=self._spec["hover"])
            except Exception:  # noqa: BLE001
                pass

    def _on_leave(self, _event=None):
        try:
            self.configure(bg=self._base_bg)
        except Exception:  # noqa: BLE001
            pass


# ---------- 开关 ----------

class Toggle(tk.Canvas):
    W, H = 44, 25

    def __init__(self, parent: tk.Misc, variable: tk.BooleanVar, command=None, *,
                 bg: str | None = None, on_color: str = PRIMARY, off_color: str = "#c6d2e2"):
        super().__init__(parent, width=self.W, height=self.H, bg=bg or _bg_of(parent),
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=0)
        self.variable = variable
        self.command = command
        self.on_color = on_color
        self.off_color = off_color
        self._enabled = True
        self.bind("<Button-1>", self._click)
        self.refresh()

    def set_enabled(self, enabled: bool) -> None:
        """操作进行中临时禁用（避免用户连点导致重复提交）。"""
        self._enabled = bool(enabled)
        try:
            self.configure(cursor="hand2" if enabled else "")
            self.refresh()
        except Exception:  # noqa: BLE001
            pass

    def refresh(self) -> None:
        try:
            self.delete("all")
            on = bool(self.variable.get())
            fill = self.on_color if on else self.off_color
            if not getattr(self, "_enabled", True):
                fill = "#d8dee8"   # 禁用时用灰色，和 FlatButton 的禁用观感一致
            r = self.H / 2
            self.create_oval(0, 0, self.H, self.H, fill=fill, outline=fill)
            self.create_oval(self.W - self.H, 0, self.W, self.H, fill=fill, outline=fill)
            self.create_rectangle(r, 0, self.W - r, self.H, fill=fill, outline=fill)
            pad = 3
            d = self.H - pad * 2
            x = (self.W - pad - d) if on else pad
            self.create_oval(x, pad, x + d, pad + d, fill="#ffffff", outline="#ffffff")
        except Exception:  # noqa: BLE001
            pass

    def _click(self, _event=None):
        if not getattr(self, "_enabled", True):
            return
        try:
            self.variable.set(not bool(self.variable.get()))
            self.refresh()
        except Exception:  # noqa: BLE001
            return
        if self.command is not None:
            try:
                self.command()
            except Exception:  # noqa: BLE001
                pass


# ---------- 分段选择 ----------

class Segmented(tk.Frame):
    """两段式选择器（开放网络 / 已保存配置）。"""

    def __init__(self, parent: tk.Misc, options: list[tuple[str, str]], variable: tk.StringVar,
                 command=None, *, bg: str = CARD):
        super().__init__(parent, bg=bg)
        self.variable = variable
        self.command = command
        self._items: dict[str, tk.Label] = {}
        outer = tk.Frame(self, bg=bg, highlightthickness=1, highlightbackground=BORDER, bd=0)
        outer.pack(fill="x")
        for index, (label, value) in enumerate(options):
            cell = tk.Label(outer, text=label, font=f(9.5), bg=bg, fg=MUTED,
                            padx=12, pady=6, cursor="hand2", bd=0)
            cell.pack(side="left", padx=(1 if index == 0 else 0, 1), pady=1)
            cell.bind("<Button-1>", lambda _e, v=value: self._pick(v))
            self._items[value] = cell
        self.refresh()

    def _pick(self, value: str) -> None:
        try:
            self.variable.set(value)
            self.refresh()
        except Exception:  # noqa: BLE001
            return
        if self.command is not None:
            try:
                self.command()
            except Exception:  # noqa: BLE001
                pass

    def refresh(self) -> None:
        current = self.variable.get()
        for value, cell in self._items.items():
            try:
                if value == current:
                    cell.configure(bg=PRIMARY_SOFT, fg=PRIMARY, font=f(9.5, "bold"))
                else:
                    cell.configure(bg=CARD, fg=MUTED, font=f(9.5))
            except Exception:  # noqa: BLE001
                pass


# ---------- 滚动区域 ----------

class ScrollArea(tk.Frame):
    def __init__(self, parent: tk.Misc, *, bg: str = CARD, height: int = 200):
        super().__init__(parent, bg=bg)
        self._bg = bg
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=height)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        bind_mousewheel(self, self.canvas)

    def _on_inner(self, _event=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:  # noqa: BLE001
            pass

    def _on_canvas(self, event):
        try:
            self.canvas.itemconfigure(self._window, width=event.width)
        except Exception:  # noqa: BLE001
            pass

    def _on_scroll(self, first: str, last: str):
        try:
            self.scrollbar.set(first, last)
            needed = not (float(first) <= 0.0 and float(last) >= 1.0)
            if needed and not self.scrollbar.winfo_ismapped():
                self.scrollbar.pack(side="right", fill="y")
            elif not needed and self.scrollbar.winfo_ismapped():
                self.scrollbar.pack_forget()
        except Exception:  # noqa: BLE001
            pass

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass

    def scroll_top(self) -> None:
        try:
            self.canvas.yview_moveto(0)
        except Exception:  # noqa: BLE001
            pass

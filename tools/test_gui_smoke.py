"""界面冒烟自测：不点按钮，只保证"能建起来、能切页、不掉异常"。

做法：真的创建一个 Tk 根窗口，把三个页面都建出来、依次切换、跑 2 秒事件循环，
然后检查 Tk 有没有往后台错误里塞东西（有的话写进了 logs/gui_main_error.log）。

用法：
    python tools\\test_gui_smoke.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
for path in (str(ROOT), str(TOOLS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import tkinter as tk  # noqa: E402

FAILED: list[str] = []


def _iter_labels(widget):
    """递归找出所有 Label（用来检查文字有没有被容器挤到看不见）。"""
    for child in widget.winfo_children():
        if isinstance(child, tk.Label) and not isinstance(child, tk.Entry):
            yield child
        yield from _iter_labels(child)


def client_xy(widget, root):
    """算控件相对**窗口客户区**的坐标。

    为什么不用 winfo_rootx/rooty：窗口被外部程序挪动过时，Tk 缓存的屏幕坐标会过期，
    看起来"在窗口内"其实已经跑到窗口外面了（踩过这个坑）。这里只靠父子偏移累加，
    和屏幕位置无关，才是可靠的判据。
    """
    x = y = 0
    node = widget
    while node is not None and node is not root:
        try:
            x += node.winfo_x()
            y += node.winfo_y()
        except Exception:  # noqa: BLE001
            break
        node = node.master
    return x, y


def inside_window(widget, root) -> tuple[bool, str]:
    x, y = client_xy(widget, root)
    w, h = widget.winfo_width(), widget.winfo_height()
    win_w, win_h = root.winfo_width(), root.winfo_height()
    ok = (
        widget.winfo_ismapped() == 1
        and w > 1 and h > 1
        and x >= 0 and y >= 0
        and x + w <= win_w + 1
        and y + h <= win_h + 1
    )
    return ok, f"client=({x},{y} {w}x{h}) window={win_w}x{win_h} mapped={widget.winfo_ismapped()}"


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    print("1) 配置字段完整性")
    import os

    # 用隔离的数据目录，避免测试动到用户真实配置
    os.environ.setdefault(
        "CAMPUSNET_DATA_DIR", str(Path(ROOT) / "build" / "smoke-data-gui")
    )
    import gui_service as svc
    from campusnet import paths

    cfg = svc.load_config()
    for field in ("wifi_ssid", "wifi_connect_mode", "auto_enabled",
                  "portal_auth_enabled", "autofill_click", "trusted_auth_hosts",
                  "tls_insecure_hosts", "portal_dump_enabled", "browser_fallback"):
        check(f"Config 有 {field}", hasattr(cfg, field))
    dumped = cfg.to_json()
    check("新字段会被写进 JSON", '"wifi_ssid"' in dumped and '"auto_enabled"' in dumped)
    check("V3 配置带版本号", '"config_version"' in dumped)
    check("旧配置能正常加载（不因缺字段报错）", cfg.username != "" or cfg.username == "")

    print("2) 建窗口 / 切页面")
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 当前环境没有图形界面：{exc}")
        return 0

    error_log = paths.log_dir() / "gui_error.log"
    before = error_log.read_text(encoding="utf-8") if error_log.exists() else ""

    app = None
    try:
        import gui_main

        app = gui_main.App(root)
        check("三个页面都建好了", set(app.pages) == {"overview", "settings", "auth"})
        check("三个页签都在", set(app.tabs) == {"overview", "settings", "auth"})
        check("窗口标题正确", "控制台" in root.title(), root.title())

        for _ in range(20):
            root.update()
        # 首次启动（还没配置账号）会自动落到「设置」页，这是 V3.0 的引导行为；
        # 已配置则停在概览页。两种都算正确。
        if svc.load_config().is_configured():
            check("已配置时默认停在网络概览", app.current == "overview", app.current)
        else:
            check("首次启动自动进入设置页", app.current == "settings", app.current)

        for key in ("settings", "auth", "overview"):
            app.navigate(key)
            for _ in range(20):
                root.update()
            check(f"切到 {key} 不抛异常", app.current == key)
        check("切换后没有 Tk 后台错误", app._tk_error == "", app._tk_error[:400])

        # 让后台线程（netsh 扫描 / 联网探测）跑一会儿，再把结果回灌进界面
        import time

        deadline = time.time() + 3.0
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)
        check("后台线程回灌结果没有抛异常", app._tk_error == "", app._tk_error[:400])
        check("状态栏有文字", bool(app.var_status.get().strip()))

        print("3) 布局越界 / 文字截断检查")
        root.update_idletasks()
        overview_page = app.pages["overview"]
        key_widgets = {
            "「重新扫描」按钮": overview_page.btn_refresh,
            "「立即连接」按钮": overview_page.btn_connect,
            "自动连接开关": overview_page.toggle_auto,
            "「更新于」文字": overview_page.lbl_updated,
            "指标块": overview_page.tile_net,
            "页签「认证」": app.tabs["auth"],
            "状态栏文字": app.lbl_status,
        }
        for name, widget in key_widgets.items():
            ok, detail = inside_window(widget, root)
            check(f"{name} 显示在窗口内", ok, detail)

        squeezed: list[str] = []
        for key, page in app.pages.items():
            for label in _iter_labels(page):
                try:
                    if label.winfo_ismapped() and label.winfo_width() + 2 < label.winfo_reqwidth():
                        squeezed.append(
                            f"{key}:{label.cget('text')[:24]!r} "
                            f"{label.winfo_width()}<{label.winfo_reqwidth()}"
                        )
                except Exception:  # noqa: BLE001
                    continue
        check("没有文字被挤到截断", not squeezed, "; ".join(squeezed[:5]))

        # 每页的"底部关键控件"都要真的显示出来 —— 曾经被 expand 的区块把 footer 挤没
        page_widgets = {
            "overview": [("「立即连接」", lambda p: p.btn_connect), ("网络列表", lambda p: p.list)],
            "settings": [("「保存并开启」按钮", lambda p: p.btn_save_enable),
                         ("自动填写勾选", lambda p: p.chk_autofill), ("运营商输入框", lambda p: p.entry_service)],
            "auth": [("「打开页面并填写」按钮", lambda p: p.btn_browser_login), ("「直接提交」按钮", lambda p: p.btn_login), ("认证控制台", lambda p: p.console)],
        }
        for page_key, entries in page_widgets.items():
            app.navigate(page_key)
            for _ in range(15):
                root.update()
            for name, getter in entries:
                widget = getter(app.pages[page_key])
                ok, detail = inside_window(widget, root)
                check(f"{page_key} 页 {name} 确实显示出来", ok, detail)
        app.navigate("overview")
        for _ in range(15):
            root.update()

        # 页面内部的数据渲染（列表行、控制台）
        check("附近网络渲染出内容", len(overview_page.list.inner.winfo_children()) > 0)
        auth_page = app.pages["auth"]
        check("认证页控制台有默认文字", "准备就绪" in auth_page.console.get("1.0", "end"))
        settings_page = app.pages["settings"]
        check("设置页 SSID 输入框存在", settings_page.entry_ssid.winfo_exists() == 1)

        print("4) V3.0 新增的「启动与安全」区块")
        app.navigate("settings")
        for _ in range(20):
            root.update()

        def in_scroll_area(widget) -> tuple[bool, str]:
            """新加的这一块在可滚动区域里，可能不在首屏可见范围内。

            判据改成：控件真实存在、已布局出尺寸、且挂在 ScrollArea 的 canvas 下
            （即用户可以滚动到），而不是"必须出现在首屏窗口坐标内"。
            """
            try:
                mapped = widget.winfo_ismapped() == 1
                width, height = widget.winfo_width(), widget.winfo_height()
                node, under_canvas, depth = widget, False, 0
                while node is not None and depth < 12:
                    if isinstance(node, tk.Canvas):
                        under_canvas = True
                        break
                    node = getattr(node, "master", None)
                    depth += 1
                ok = mapped and width > 1 and height > 1 and under_canvas
                return ok, f"mapped={mapped} size={width}x{height} in_canvas={under_canvas}"
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)

        for name, getter in {
            "开机自启动开关": lambda p: p.toggle_autostart,
            "自启动状态文字": lambda p: p.lbl_autostart,
            "数据目录说明": lambda p: p.lbl_paths,
            "「打开数据目录」按钮": lambda p: p.btn_data_dir,
            "「安全体检」按钮": lambda p: p.btn_security,
            "「导出诊断包」按钮": lambda p: p.btn_diag,
        }.items():
            ok, detail = in_scroll_area(getter(settings_page))
            check(f"{name} 存在于设置页", ok, detail)
        check("自启动状态文字已填充",
              bool(settings_page.lbl_autostart.cget("text").strip()))
        check("数据目录说明已填充",
              "配置、日志与备份都保存在" in settings_page.lbl_paths.cget("text"))
        check("设置页没有「加入白名单」入口",
              not any("白名单" in str(w.cget("text")) for w in _iter_labels(settings_page)
                      if hasattr(w, "cget") and "text" in w.keys()))
    except Exception:  # noqa: BLE001
        FAILED.append("建窗口")
        print("  [FAIL] 建窗口时抛异常：\n" + traceback.format_exc())
    finally:
        try:
            if app is not None:
                app.on_close()
            else:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass

    after = error_log.read_text(encoding="utf-8") if error_log.exists() else ""
    check("没有新增 GUI 错误日志记录", after == before,
          (after[len(before):][:600] if after != before else ""))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

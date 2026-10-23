"""界面响应性测试：测量"点按钮时主线程被阻塞多久"。

为什么要专门测这个：用户反馈"填完信息点『保存并开启自动连接』后程序变成未响应"。
tkinter 只在主线程处理事件，任何同步的网络/子进程调用都会让窗口变"未响应"。
肉眼很难判断是哪一步，所以这里用**心跳法**测量：

* 在主线程上每 20ms 打一次点（``after(20)``）；
* 在测试里调用某个操作（保存、切页、刷新）；
* 用 ``root.update()`` 泵事件循环；
* 记录两次心跳之间的**最大间隔** —— 那就是用户感受到的卡顿。

判定阈值：
* < 300ms        —— 感觉不到（正常）
* 300 ~ 1500ms   —— 明显迟滞（需要修）
* > 1500ms       —— 会被 Windows 标记为"未响应"
"""

from __future__ import annotations

import os
import time
import unittest

from tests.support import data_dir as isolated_data_dir

try:  # 无图形环境时整类跳过
    import tkinter as tk

    _TK_OK = True
except Exception:  # noqa: BLE001
    _TK_OK = False

BLOCK_LIMIT_MS = 300.0


class _Heartbeat:
    """在主线程上周期性打点，记录最大间隔。"""

    def __init__(self, root, interval_ms: int = 20):
        self.root = root
        self.interval = interval_ms
        self.last = time.perf_counter()
        self.max_gap = 0.0
        self.ticks = 0
        self._id = None

    def start(self) -> None:
        self.last = time.perf_counter()
        self._tick()

    def _tick(self) -> None:
        now = time.perf_counter()
        gap = (now - self.last) * 1000.0
        if self.ticks:  # 第一次是启动本身，不算
            self.max_gap = max(self.max_gap, gap)
        self.last = now
        self.ticks += 1
        try:
            self._id = self.root.after(self.interval, self._tick)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._id is not None:
            try:
                self.root.after_cancel(self._id)
            except Exception:  # noqa: BLE001
                pass
            self._id = None

    def reset(self) -> None:
        self.max_gap = 0.0
        self.ticks = 0
        self.last = time.perf_counter()


def pump(root, seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        root.update()
        time.sleep(0.005)


@unittest.skipUnless(_TK_OK, "当前环境没有 tkinter")
class UiResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._ctx = isolated_data_dir()
        cls.data_dir = cls._ctx.__enter__()
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()          # 不弹真窗口，但事件循环照常
        except Exception as exc:  # noqa: BLE001
            cls._ctx.__exit__(None, None, None)
            raise unittest.SkipTest(f"无法创建 Tk 窗口：{exc}")
        from campusnet.ui import gui_service as svc
        from campusnet.ui import main_window

        # 「保存并开启自动连接」现在会真的拉起后台守护。单元测试里不能真起进程：
        # 一是会留下脱离父进程的 pythonw 守护（实测会占着运行锁，害得后续
        # EXE 冒烟/系统集成套件接连失败），二是拉起真实守护本就不是本用例要测的东西。
        # 这里替换成桩函数，把"守护启动"行为与 UI 响应性解耦。
        cls._real_start_daemon = svc.start_daemon
        svc.start_daemon = lambda: (True, "（测试桩）后台守护已启动")  # type: ignore[assignment]

        cls.gui = main_window
        cls.app = main_window.App(cls.root)
        pump(cls.root, 0.6)

    @classmethod
    def tearDownClass(cls):
        from campusnet.ui import gui_service as svc

        try:
            svc.start_daemon = cls._real_start_daemon  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass
        try:
            cls.app.on_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        cls._ctx.__exit__(None, None, None)

    # ---------------- 基本工具 ----------------

    def _fill_settings(self) -> None:
        page = self.app.pages["settings"]
        page.var_ssid.set("Campus-Test")
        page.var_account.set("20230001")
        page.var_password.set("p@ss word")
        page.var_portal.set("http://192.0.2.10/portal")
        page.var_service.set("")
        page.var_mode.set("saved")

    def _measure(self, action, *, seconds: float = 1.5) -> tuple[float, float]:
        """执行 action，返回 (action 自身耗时秒, 主线程最大卡顿毫秒)。"""
        hb = _Heartbeat(self.root)
        hb.start()
        pump(self.root, 0.1)
        hb.reset()
        started = time.perf_counter()
        action()
        elapsed = time.perf_counter() - started
        pump(self.root, seconds)
        hb.stop()
        return elapsed, hb.max_gap

    # ---------------- 被测场景 ----------------

    def test_switch_pages_does_not_block(self):
        for key in ("settings", "auth", "overview"):
            elapsed, gap = self._measure(lambda k=key: self.app.navigate(k), seconds=0.8)
            self.assertLess(
                gap, BLOCK_LIMIT_MS * 4,
                f"切到 {key} 页时主线程卡顿 {gap:.0f}ms（{elapsed:.2f}s）",
            )

    def test_save_and_enable_does_not_block_ui(self):
        """用户报的那个操作：填完信息 → 点「保存并开启自动连接」。"""
        self.app.navigate("settings")
        pump(self.root, 0.3)
        self._fill_settings()
        page = self.app.pages["settings"]

        elapsed, gap = self._measure(
            lambda: page.on_save(enable=True), seconds=3.0
        )
        # 保存本身必须是"点完立刻返回"的操作
        self.assertLess(
            elapsed, 0.2,
            f"on_save 同步耗时 {elapsed * 1000:.0f}ms（应该只读控件 + 起后台线程）",
        )
        self.assertLess(
            gap, BLOCK_LIMIT_MS,
            f"点保存后主线程卡顿 {gap:.0f}ms，用户会看到『未响应』",
        )

        # 配置真的写进去了
        from campusnet import paths
        from campusnet.config import Config

        cfg = Config.load(paths.config_path())
        self.assertEqual(cfg.wifi_ssid, "Campus-Test")
        self.assertEqual(cfg.username, "20230001")
        self.assertTrue(cfg.auto_enabled)
        self.assertTrue(cfg.password_enc, "密码应该已加密保存")

    def test_slow_save_keeps_ui_responsive(self):
        """回归测试：即使加密/写盘慢到 3 秒，界面也不能卡成"未响应"。

        真实原因：保存要做 DPAPI 加密 + 写 %LOCALAPPDATA% + 回读校验，
        在装了实时防护、或域账户漫游主密钥的机器上这几步可能要好几秒。
        第一版把它们全放在主线程，用户点完保存就看到"未响应"。
        """
        from campusnet.ui import gui_service as svc

        original = svc.set_password

        def slow_set_password(cfg, password):
            time.sleep(3.0)          # 模拟 DPAPI 被安全软件拖慢
            return original(cfg, password)

        svc.set_password = slow_set_password  # type: ignore[assignment]
        try:
            self.app.navigate("settings")
            pump(self.root, 0.3)
            self._fill_settings()
            page = self.app.pages["settings"]

            hb = _Heartbeat(self.root)
            hb.start()
            pump(self.root, 0.05)
            hb.reset()

            started = time.perf_counter()
            page.on_save(enable=True)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.2,
                            f"点击后同步处理了 {elapsed * 1000:.0f}ms，必须立刻返回")

            # 状态栏文字是通过队列投递的，先泵一下事件循环再看
            pump(self.root, 0.15)

            # 保存进行中：按钮应该置灰，状态栏提示"正在保存"
            self.assertTrue(page._saving, "保存期间应该处于 saving 状态（按钮置灰）")
            self.assertIn("正在保存", self.app.var_status.get())

            # 一直泵事件循环直到保存完成；全过程主线程不能被卡住
            deadline = time.perf_counter() + 12
            while time.perf_counter() < deadline and page._saving:
                self.root.update()
                time.sleep(0.005)
            hb.stop()

            self.assertFalse(page._saving, "保存应该在合理时间内完成")
            self.assertLess(
                hb.max_gap, BLOCK_LIMIT_MS,
                f"慢保存期间主线程卡顿 {hb.max_gap:.0f}ms —— 这正是『未响应』的原因",
            )
            # 文案会经历"配置已保存 → 正在启动后台守护 →（成功/失败）"几个阶段，
            # 本用例只关心"界面没被卡住"，因此接受任一阶段的真实文案。
            text = self.app.var_status.get()
            self.assertTrue(
                ("配置已保存" in text) or ("配置保存成功" in text),
                f"保存后没有出现保存结果文案：{text!r}",
            )
        finally:
            svc.set_password = original  # type: ignore[assignment]

    def test_autostart_toggle_does_not_block_ui(self):
        """开机自启动开关：打开时要写注册表并拉起守护（可能几秒），绝不能卡 UI。"""
        from campusnet.ui import gui_service as svc

        original_set = svc.set_autostart
        original_start = svc.start_daemon

        def slow_set(enabled: bool):
            time.sleep(1.5)
            return True, "（测试）已设置"

        def slow_start():
            time.sleep(1.5)
            return True, "（测试）已启动"

        svc.set_autostart = slow_set  # type: ignore[assignment]
        svc.start_daemon = slow_start  # type: ignore[assignment]
        try:
            self.app.navigate("settings")
            pump(self.root, 0.3)
            page = self.app.pages["settings"]
            page.var_autostart.set(True)

            hb = _Heartbeat(self.root)
            hb.start()
            pump(self.root, 0.05)
            hb.reset()

            started = time.perf_counter()
            page.on_toggle_autostart()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.2,
                            f"点开关后同步处理了 {elapsed * 1000:.0f}ms，必须立刻返回")

            deadline = time.perf_counter() + 10
            while time.perf_counter() < deadline and page._autostart_busy:
                self.root.update()
                time.sleep(0.005)
            hb.stop()

            self.assertFalse(page._autostart_busy, "开关操作应该在合理时间内结束")
            self.assertLess(
                hb.max_gap, BLOCK_LIMIT_MS,
                f"开关自启动期间主线程卡顿 {hb.max_gap:.0f}ms（以前会长达 6 秒）",
            )
        finally:
            svc.set_autostart = original_set  # type: ignore[assignment]
            svc.start_daemon = original_start  # type: ignore[assignment]

    def test_save_failure_is_reported_without_freezing(self):
        """保存失败（例如加密失败 / 磁盘不可写）也要给出可读提示，且不卡界面。"""
        from campusnet.ui import gui_service as svc

        original = svc.set_password

        def broken_set_password(cfg, password):
            return False, "加密失败：磁盘空间不足"

        svc.set_password = broken_set_password  # type: ignore[assignment]
        try:
            self.app.navigate("settings")
            pump(self.root, 0.3)
            self._fill_settings()
            page = self.app.pages["settings"]
            elapsed, gap = self._measure(lambda: page.on_save(enable=False), seconds=2.0)
            self.assertLess(elapsed, 0.2)
            self.assertLess(gap, BLOCK_LIMIT_MS)
            self.assertFalse(page._saving, "失败后按钮必须恢复可点")
            self.assertIn("失败", self.app.var_status.get())
        finally:
            svc.set_password = original  # type: ignore[assignment]

    def test_overview_refresh_does_not_block_ui(self):
        elapsed, gap = self._measure(
            lambda: self.app.pages["overview"].refresh_all(), seconds=1.5
        )
        self.assertLess(elapsed, 0.5, f"refresh_all 同步耗时 {elapsed * 1000:.0f}ms")
        self.assertLess(gap, BLOCK_LIMIT_MS, f"刷新概览时主线程卡顿 {gap:.0f}ms")

    def test_autostart_status_read_is_fast(self):
        """保存流程里会读一次自启动状态，不能拖慢主线程。"""
        elapsed, gap = self._measure(
            lambda: self.app.pages["settings"].refresh_system_state(), seconds=0.6
        )
        self.assertLess(elapsed, 0.5, f"读自启动状态耗时 {elapsed * 1000:.0f}ms")
        self.assertLess(gap, BLOCK_LIMIT_MS, f"读自启动状态时卡顿 {gap:.0f}ms")

    def test_saving_twice_in_a_row_is_fine(self):
        page = self.app.pages["settings"]
        self.app.navigate("settings")
        pump(self.root, 0.2)
        self._fill_settings()
        self._measure(lambda: page.on_save(enable=True), seconds=1.0)
        self._fill_settings()
        elapsed, gap = self._measure(lambda: page.on_save(enable=True), seconds=1.5)
        self.assertLess(elapsed, 0.8)
        self.assertLess(gap, BLOCK_LIMIT_MS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

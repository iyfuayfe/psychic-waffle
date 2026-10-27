"""``campusnet.ui.gui_service.get_logger()`` 的失败路径回归测试。

背景（3.1.1 的真实缺陷）：初始化失败时 ``except`` 分支里写的是
``_logger = get_logger()``，而 ``_logger_lock`` 是不可重入的 ``threading.Lock``
——同一线程持锁再次抢锁会**永久死锁**。界面里十余处调用它，命中后整个界面假死，
且因为日志没建起来，用户拿不到任何线索。

这个测试用"独立线程 + 超时"来跑，避免测试进程自己被死锁挂住（否则 CI 只会卡住
而没有失败信息）。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from campusnet.ui import gui_service


class GuiLoggerFallbackTests(unittest.TestCase):
    def setUp(self) -> None:  # noqa: D102
        # 关键：每个用例换一把新锁。若被测实现真的死锁，卡住的线程会永久持有
        # **旧**锁；不换锁的话，后面用例（以及测试进程退出）都会一起挂住，
        # 表现为"跑 900 秒才超时"而不是"快速失败"。
        self._original_lock = gui_service._logger_lock
        gui_service._logger = None
        gui_service._logger_lock = threading.Lock()

    def tearDown(self) -> None:  # noqa: D102
        gui_service._logger = None
        gui_service._logger_lock = self._original_lock

    def test_setup_failure_falls_back_without_deadlock(self) -> None:
        done = threading.Event()
        box: dict = {}

        def worker() -> None:
            with mock.patch.object(
                gui_service, "setup_logging", side_effect=OSError("no log dir")
            ):
                box["log"] = gui_service.get_logger()
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(done.wait(2.0), "get_logger() 在初始化失败时死锁了")
        self.assertIsNotNone(box["log"], "初始化失败也必须返回一个可用 logger")

    def test_config_failure_falls_back_too(self) -> None:
        """连配置都读不出来时也不能死锁（load_config 自身抛异常的情况）。"""
        done = threading.Event()
        box: dict = {}

        def worker() -> None:
            with mock.patch.object(
                gui_service, "load_config", side_effect=RuntimeError("bad config")
            ):
                box["log"] = gui_service.get_logger()
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(done.wait(2.0), "配置读取失败时 get_logger() 死锁了")
        self.assertIsNotNone(box["log"])

    def test_logger_is_cached_and_reused(self) -> None:
        """同一个 GUI 会话里只初始化一次日志。

        注意：这里**也在工作线程里跑**并带超时——如果被测实现真的死锁，
        主线程一旦直接调用 ``get_logger()`` 就会永久阻塞，整个测试进程会挂住
        （CI 只会卡到超时，而不是给出失败信息）。所有调用都放进 daemon 线程，
        进程就一定能退出。
        """
        sentinel = object()
        done = threading.Event()
        box: dict = {}

        def worker() -> None:
            with mock.patch.object(gui_service, "setup_logging",
                                   return_value=sentinel) as setup:
                box["first"] = gui_service.get_logger()
                box["second"] = gui_service.get_logger()
                box["calls"] = setup.call_count
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(done.wait(3.0), "get_logger() 卡住了（没有返回）")
        self.assertIs(box["first"], sentinel)
        self.assertIs(box["second"], sentinel)
        self.assertEqual(box["calls"], 1, "同一个 GUI 会话里只应初始化一次日志")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
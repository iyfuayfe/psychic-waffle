"""第三方注入冲突检测测试（真实事故的回归防线）。

背景：某台机器上双击 ``CampusNet.exe``，窗口 3 秒后整个进程消失，
Windows 事件日志显示崩溃模块是游戏加加注入的 ``GPP64.dll``（0xc0000005）。
用最小 tkinter 程序复现同样的崩溃 → 与本项目代码无关。

这里验证"程序能不能把这件事自己说清楚"：

* 能在**当前进程**里枚举到已知注入模块（本机确实被注入了 GPP64.dll，可用于正反验证）；
* 即使没有任何注入模块，函数也不能抛异常；
* 会话标记能区分"正常退出"和"异常退出"。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from tests.support import data_dir as isolated_data_dir

from campusnet import conflicts, paths, session


class ModuleEnumerationTests(unittest.TestCase):
    def test_loaded_modules_returns_list_of_pairs(self):
        modules = conflicts.loaded_modules()
        self.assertIsInstance(modules, list)
        for name, path in modules:
            self.assertIsInstance(name, str)
            self.assertIsInstance(path, str)

    def test_current_process_has_at_least_one_module(self):
        """至少要能列出本进程的模块（否则检测能力等于没有）。"""
        modules = conflicts.loaded_modules()
        self.assertTrue(modules, "枚举不到任何模块，说明 Toolhelp32 调用失败")
        names = {name.lower() for name, _p in modules}
        # Python 进程必然加载了 python 相关的核心 DLL
        self.assertTrue(
            any("python" in n or n.endswith(".dll") for n in names),
            f"模块列表看起来不对：{sorted(names)[:10]}",
        )

    def test_detect_does_not_raise(self):
        hits = conflicts.detect()
        self.assertIsInstance(hits, list)
        for hit in hits:
            self.assertTrue(hit.module)
            self.assertTrue(hit.vendor, "命中项必须有可读的来源说明")
            self.assertTrue(hit.advice, "命中项必须给出可操作的解决建议")


class DescribeTests(unittest.TestCase):
    def test_no_hits_gives_empty_string(self):
        self.assertEqual(conflicts.describe([]), "")

    def test_verified_hit_mentions_crash_and_advice(self):
        hit = conflicts.Conflict(
            module="gpp64.dll",
            path=r"C:\Users\x\AppData\Local\GamePPSDK\GPP64.dll",
            vendor="游戏加加（GamePP）",
            advice="退出游戏加加后重试。",
            verified=True,
        )
        text = conflicts.describe([hit])
        self.assertIn("GPP64.dll".lower(), text.lower())
        self.assertIn("游戏加加", text)
        self.assertIn("闪退", text)
        self.assertIn("退出游戏加加", text)
        # 必须说明后台不受影响，避免用户以为整个程序没用
        self.assertIn("后台", text)

    def test_known_table_entries_are_well_formed(self):
        for key, value in conflicts.KNOWN_OVERLAYS.items():
            vendor, advice, verified = value
            self.assertTrue(key.islower(), key)
            self.assertTrue(key.endswith(".dll"), key)
            self.assertTrue(vendor, key)
            self.assertTrue(advice, key)
            self.assertIsInstance(verified, bool)

    def test_gpp_is_marked_as_verified(self):
        """GPP64.dll 是真实崩溃过的，必须标记为已验证，这样提示才会更强硬。"""
        vendor, advice, verified = conflicts.KNOWN_OVERLAYS["gpp64.dll"]
        self.assertTrue(verified)
        self.assertIn("游戏加加", vendor)


class SessionMarkerTests(unittest.TestCase):
    def test_begin_and_end_roundtrip(self):
        with isolated_data_dir():
            session.end()
            self.assertIsNone(session.previous())
            session.begin("gui")
            self.assertTrue(session.session_path().exists())
            session.end()
            self.assertIsNone(session.previous())

    def test_stale_marker_is_reported_as_abnormal_exit(self):
        with isolated_data_dir():
            session.end()
            # 造一个"pid 已经不存在"的标记，模拟上个进程崩溃
            marker = {
                "kind": "gui",
                "pid": 999999,
                "started_at": 1,
                "started_text": "2026-10-19 17:34:12",
                "version": "3.0.0",
                "conflicts": ["gpp64.dll"],
                "conflict_vendor": ["游戏加加（GamePP）"],
            }
            import json

            path = session.session_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")

            previous = session.previous()
            self.assertIsNotNone(previous, "崩溃留下的标记应该被识别出来")
            text = session.describe_previous(previous)
            self.assertIn("2026-10-19 17:34:12", text)
            self.assertIn("gpp64.dll", text.lower())
            self.assertIn("游戏加加", text)
            session.end()

    def test_live_marker_is_not_reported(self):
        """当前进程还活着时不能误报"上次异常退出"。"""
        with isolated_data_dir():
            session.begin("gui")
            self.assertIsNone(session.previous())
            session.end()

    def test_describe_previous_handles_empty(self):
        self.assertEqual(session.describe_previous(None), "")
        self.assertEqual(session.describe_previous({}), "")

    def test_runtime_dir_is_inside_data_dir(self):
        with isolated_data_dir() as data:
            self.assertEqual(paths.runtime_dir().parent, data)


class RegressionContextTests(unittest.TestCase):
    """把"这是环境问题而不是本程序 bug"的证据固化下来，方便以后复查。"""

    def test_known_overlay_detection_is_read_only(self):
        """检测函数不得尝试卸载/屏蔽第三方模块（只枚举）。"""
        import inspect

        source = inspect.getsource(conflicts)
        for forbidden in ("FreeLibrary", "UnmapViewOfFile", "TerminateProcess", "DeleteFile"):
            self.assertNotIn(forbidden, source, f"检测模块不应该调用 {forbidden}")

    def test_python_process_is_also_injected(self):
        """旁证：如果本机确实装了游戏加加，连 CPython 进程里也能看到它。

        这条测试不断言"必须有"（别人的机器上可能没装），只用于在本机
        复现时打印事实，帮助判断"是不是环境问题"。
        """
        modules = {name.lower() for name, _p in conflicts.loaded_modules()}
        overlays = sorted(m for m in modules if m in conflicts.KNOWN_OVERLAYS)
        if overlays:
            print(f"    [info] 当前进程已被这些叠加模块注入：{overlays}")
        self.assertIsInstance(overlays, list)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

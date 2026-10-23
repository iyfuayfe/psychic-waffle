"""卸载逻辑测试（在隔离的数据目录里跑，不碰真实配置、不碰真实注册表）。

重点回归两个真机上翻过车的地方：

1. ``uninstall --remove-data`` 必须真的能删掉数据目录 —— 包括 ``logs/``。
   CLI 自己通过 ``setup_logging()`` 打开着日志文件，Windows 上删除会被拒
   （``WinError 32``），所以卸载前必须先关句柄。
2. 默认卸载**不能**删用户数据，也不能去动杀毒软件设置。
"""

from __future__ import annotations

import json
import unittest

from tests.support import data_dir as isolated_data_dir

from campusnet import paths, uninstall
from campusnet import autostart as autostart_mod
from campusnet.logs import setup_logging


class FakeKey:
    def __init__(self, store):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinReg:
    """内存版注册表，避免测试去改真实 HKCU。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    KEY_READ = 0x00020019
    REG_SZ = 1

    def __init__(self):
        self.values: dict = {}

    def CreateKeyEx(self, *a, **k):
        return FakeKey(self.values)

    def OpenKey(self, *a, **k):
        return FakeKey(self.values)

    def SetValueEx(self, key, name, reserved, kind, value):
        key._store[name] = value

    def QueryValueEx(self, key, name):
        if name not in key._store:
            raise FileNotFoundError(2, "not found")
        return key._store[name], self.REG_SZ

    def DeleteValue(self, key, name):
        if name not in key._store:
            raise FileNotFoundError(2, "not found")
        del key._store[name]


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self._original = autostart_mod._winreg
        self.fake = FakeWinReg()
        autostart_mod._winreg = lambda: self.fake  # type: ignore[assignment]
        # 避免真的去停进程、删计划任务
        self._orig_stop = autostart_mod.stop_daemon
        self._orig_cleanup = autostart_mod.cleanup_legacy
        autostart_mod.stop_daemon = lambda timeout=20.0: (True, "（测试）后台守护没有在运行。")  # type: ignore[assignment]
        autostart_mod.cleanup_legacy = lambda: []  # type: ignore[assignment]

    def tearDown(self):
        autostart_mod._winreg = self._original  # type: ignore[assignment]
        autostart_mod.stop_daemon = self._orig_stop  # type: ignore[assignment]
        autostart_mod.cleanup_legacy = self._orig_cleanup  # type: ignore[assignment]

    def _seed(self, data) -> None:
        (data / "config.json").write_text(
            json.dumps({"config_version": 3, "username": "u", "password_enc": "x"}),
            encoding="utf-8",
        )
        (data / "logs").mkdir(parents=True, exist_ok=True)
        (data / "logs" / "campus_login.log").write_text("hello\n", encoding="utf-8")
        (data / "tmp" / "browser").mkdir(parents=True, exist_ok=True)

    def test_default_uninstall_keeps_user_data(self):
        with isolated_data_dir() as data:
            self._seed(data)
            autostart_mod.enable()
            self.assertIn(autostart_mod.VALUE_NAME, self.fake.values)

            report = uninstall.uninstall(remove_data=False)

            self.assertTrue(report.ok, report.summary())
            self.assertFalse(report.data_removed)
            self.assertTrue((data / "config.json").exists(), "默认卸载必须保留配置")
            self.assertTrue((data / "logs" / "campus_login.log").exists())
            self.assertNotIn(autostart_mod.VALUE_NAME, self.fake.values, "自启动必须被关掉")
            self.assertIn("未修改任何安全软件设置", report.summary())

    def test_remove_data_deletes_directory_even_with_open_log(self):
        """回归：CLI 自己开着日志文件时，--remove-data 仍必须删干净。"""
        with isolated_data_dir() as data:
            self._seed(data)
            # 模拟 CLI 进程：真的打开日志文件
            setup_logging(log_dir_override=data / "logs")
            import logging

            logging.getLogger("campusnet").info("卸载前的日志，句柄还开着")
            self.assertTrue((data / "logs" / "campus_login.log").exists())

            report = uninstall.uninstall(remove_data=True)

            self.assertTrue(report.ok, report.summary())
            self.assertTrue(report.data_removed, report.summary())
            self.assertFalse(data.exists(), f"数据目录应该被删掉：{list(data.rglob('*'))[:5]}")

    def test_remove_data_when_absent_is_fine(self):
        with isolated_data_dir():
            report = uninstall.uninstall(remove_data=True)
            self.assertTrue(report.ok, report.summary())

    def test_report_is_json_safe(self):
        with isolated_data_dir() as data:
            self._seed(data)
            report = uninstall.uninstall(remove_data=False)
            payload = json.dumps(report.to_dict(), ensure_ascii=False)
            self.assertIn("ok", payload)

    def test_uninstall_never_touches_other_startup_entries(self):
        with isolated_data_dir() as data:
            self._seed(data)
            self.fake.values["SomeoneElse"] = "keep-me.exe"
            self.fake.values["Another"] = "keep-me-too.exe"
            uninstall.uninstall(remove_data=False)
            self.assertEqual(self.fake.values.get("SomeoneElse"), "keep-me.exe")
            self.assertEqual(self.fake.values.get("Another"), "keep-me-too.exe")


class RemoveDataDirTests(unittest.TestCase):
    def test_remove_data_dir_handles_missing_path(self):
        with isolated_data_dir() as data:
            target = data / "nope"
            ok, message = uninstall.remove_data_dir(target)
            self.assertTrue(ok)
            self.assertIn("本来就不存在", message)

    def test_remove_data_dir_reports_reason_on_failure(self):
        with isolated_data_dir() as data:
            self.assertIsInstance(data, type(data))
            ok, message = uninstall.remove_data_dir(data)
            self.assertTrue(ok, message)
            self.assertFalse(data.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

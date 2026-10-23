"""自启动模块测试（用假注册表，绝不碰真实 HKCU）。

为什么要用假的：``autostart`` 会写 ``HKCU\\...\\Run``，单元测试不能去改用户真实的
开机启动项。这里用一个内存字典模拟 winreg，验证：

* 登记的**命令**确实指向后台守护（打包模式 = 同目录 ``CampusNetService.exe``）；
* 重复开启 / 未开启时关闭 都不会报错；
* 程序搬过目录后 ``stale`` 能被识别（提示用户重新开启一次）；
* 写注册表失败时返回可读的错误，而不是抛异常崩掉（界面上要能显示原因）。

真实注册表的一次性验证放在 ``tools/test_system_integration.py``（需要普通用户会话）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from campusnet import autostart


class FakeKey:
    """模拟 winreg 的 PyHKEY（支持 with 语句）。"""

    def __init__(self, store: dict, access: int):
        self._store = store
        self._access = access

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class FakeWinReg:
    """内存版 winreg：只实现 autostart 用到的那几个 API。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    KEY_READ = 0x00020019
    REG_SZ = 1

    def __init__(self, fail_on_write: bool = False):
        self.values: dict = {}
        self.fail_on_write = fail_on_write
        self.write_attempts = 0

    # -- 注册表 API --
    def CreateKeyEx(self, root, path, reserved=0, access=0):
        if self.fail_on_write:
            raise PermissionError(13, "拒绝访问")
        return FakeKey(self.values, access)

    def OpenKey(self, root, path, reserved=0, access=0):
        if access & self.KEY_SET_VALUE and self.fail_on_write:
            raise PermissionError(13, "拒绝访问")
        if not self.values and access & self.KEY_SET_VALUE:
            # 键存在但值为空是正常的，这里不抛
            return FakeKey(self.values, access)
        return FakeKey(self.values, access)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.write_attempts += 1
        if self.fail_on_write:
            raise PermissionError(13, "拒绝访问")
        key._store[name] = value

    def QueryValueEx(self, key, name):
        if name not in key._store:
            raise FileNotFoundError(2, "找不到指定的注册表项")
        return key._store[name], self.REG_SZ

    def DeleteValue(self, key, name):
        if name not in key._store:
            raise FileNotFoundError(2, "找不到指定的注册表项")
        del key._store[name]

    def EnumValue(self, key, index):
        items = list(key._store.items())
        if index >= len(items):
            raise OSError(259, "没有更多数据")
        name, value = items[index]
        return name, value, self.REG_SZ


class AutostartWithFakeRegistry(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWinReg()
        self._original = autostart._winreg
        autostart._winreg = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self):
        autostart._winreg = self._original  # type: ignore[assignment]

    def test_enable_registers_service_command(self):
        ok, message = autostart.enable()
        self.assertTrue(ok, message)
        registered = self.fake.values.get(autostart.VALUE_NAME)
        self.assertTrue(registered, "注册表里应该有存的值")
        self.assertIn("campusnet", registered.lower())
        self.assertIn("--daemon", registered, "自启动必须带 --daemon，否则会开成图形界面")
        # 源码模式下应该是 pythonw -m campusnet.app.service
        self.assertTrue(
            "campusnet.app.service" in registered or "CampusNetService.exe" in registered,
            registered,
        )
        self.assertNotIn("CampusNet.exe", registered,
                         "不能把自启动指向图形界面主程序")

    def test_is_enabled_reflects_registry(self):
        self.assertFalse(autostart.is_enabled())
        autostart.enable()
        self.assertTrue(autostart.is_enabled())

    def test_disable_removes_value(self):
        autostart.enable()
        ok, _message = autostart.disable()
        self.assertTrue(ok)
        self.assertNotIn(autostart.VALUE_NAME, self.fake.values)
        self.assertFalse(autostart.is_enabled())

    def test_disable_when_absent_is_not_an_error(self):
        ok, message = autostart.disable()
        self.assertTrue(ok)
        self.assertIn("本来就是关闭", message)

    def test_enable_twice_is_idempotent(self):
        autostart.enable()
        first = self.fake.values[autostart.VALUE_NAME]
        ok, _ = autostart.enable()
        self.assertTrue(ok)
        self.assertEqual(self.fake.values[autostart.VALUE_NAME], first)
        self.assertEqual(len([k for k in self.fake.values if k == autostart.VALUE_NAME]), 1)

    def test_stale_command_detected(self):
        self.fake.values[autostart.VALUE_NAME] = r'"C:\Old\Place\CampusNetService.exe" --daemon'
        status = autostart.status()
        self.assertTrue(status.enabled)
        self.assertTrue(status.stale, "搬目录后应该被识别为 stale")

    def test_status_contains_expected_command(self):
        status = autostart.status()
        self.assertIn("campusnet", status.expected.lower())
        self.assertEqual(status.mechanism, "HKCU Run")
        data = autostart.status_dict()
        for key in ("enabled", "daemon_running", "command", "expected", "stale", "error", "mechanism"):
            self.assertIn(key, data)

    def test_status_has_no_secrets(self):
        text = str(autostart.status_dict()).lower()
        for needle in ("password", "username", "token"):
            self.assertNotIn(needle, text)


class AutostartFailureTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWinReg(fail_on_write=True)
        self._original = autostart._winreg
        autostart._winreg = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self):
        autostart._winreg = self._original  # type: ignore[assignment]

    def test_enable_reports_readable_error_instead_of_raising(self):
        ok, message = autostart.enable()
        self.assertFalse(ok)
        self.assertIn("无法写入", message)
        self.assertIn("拒绝访问", message)

    def test_status_survives_read_error(self):
        """读注册表失败时 status() 不能抛异常（界面要能显示"状态未知"）。"""
        class Broken(FakeWinReg):
            def OpenKey(self, *args, **kwargs):
                raise OSError(5, "拒绝访问")

        autostart._winreg = lambda: Broken()  # type: ignore[assignment]
        info = autostart.status()
        self.assertFalse(info.enabled)


class CommandGenerationTests(unittest.TestCase):
    def test_command_has_no_credentials(self):
        command = autostart.command_string()
        self.assertNotIn("password", command.lower())
        self.assertIn("--daemon", command)

    def test_paths_are_quoted_when_needed(self):
        import subprocess

        parts = ["C:\\Program Files\\CampusNet\\CampusNetService.exe", "--daemon"]
        text = subprocess.list2cmdline(parts)
        self.assertTrue(text.startswith('"'), text)
        self.assertIn("CampusNetService.exe", text)


class LegacyCleanupTests(unittest.TestCase):
    def test_legacy_names_are_explicit(self):
        self.assertEqual(
            set(autostart.LEGACY_TASKS),
            {"CampusNetAutoLogin", "CampusNetAutoLogin-KeepAlive"},
        )
        self.assertEqual(set(autostart.LEGACY_STARTUP_SCRIPTS), {"campus-net-autologin.vbs"})

    def test_cleanup_returns_messages_and_never_raises(self):
        messages = autostart.cleanup_legacy()
        self.assertIsInstance(messages, list)
        for line in messages:
            self.assertIsInstance(line, str)

    def test_legacy_present_shape(self):
        info = autostart.legacy_present()
        self.assertIn("tasks", info)
        self.assertIn("startup_scripts", info)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

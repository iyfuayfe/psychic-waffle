"""路径与数据迁移测试。

重点：打包后安装目录只读、用户数据落在 LOCALAPPDATA、旧配置会被**复制**
（不是移动）过来且绝不覆盖新配置。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests.support import data_dir as isolated_data_dir
from tests.support import temp_dir
from tests.support import write_json

from campusnet import paths


class PathCompositionTests(unittest.TestCase):
    def test_data_dir_is_under_localappdata(self):
        with isolated_data_dir():
            os.environ.pop("CAMPUSNET_DATA_DIR", None)
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            if not base:  # 极端环境跳过
                self.skipTest("没有 LOCALAPPDATA")
            self.assertEqual(paths.data_dir(), Path(base) / "CampusNet")

    def test_env_override_wins(self):
        with isolated_data_dir() as data:
            self.assertEqual(paths.data_dir(), data)

    def test_all_user_files_are_inside_data_dir(self):
        with isolated_data_dir() as data:
            self.assertEqual(paths.config_path().parent, data)
            self.assertEqual(paths.state_path().parent, data)
            self.assertEqual(paths.heartbeat_path().parent, data)
            self.assertEqual(paths.log_dir().parent, data)
            self.assertEqual(paths.backup_dir().parent, data)
            self.assertEqual(paths.temp_dir().parent, data)
            self.assertEqual(paths.diagnostics_dir().parent, data)

    def test_install_dir_and_data_dir_are_different_in_source_mode(self):
        with isolated_data_dir():
            self.assertNotEqual(paths.install_dir(), paths.data_dir())
            self.assertFalse(paths.is_frozen())

    def test_ensure_dirs_creates_everything(self):
        with isolated_data_dir() as data:
            paths.ensure_dirs()
            for child in ("logs", "tmp", "tmp/browser"):
                self.assertTrue((data / child).is_dir(), child)

    def test_describe_has_no_secrets(self):
        info = paths.describe()
        self.assertIn("data_dir", info)
        self.assertIn("config", info)
        self.assertNotIn("password", json.dumps(info).lower())


class DaemonCommandTests(unittest.TestCase):
    def test_source_mode_uses_module_entry(self):
        command = paths.daemon_command()
        self.assertIn("-m", command)
        self.assertIn("campusnet.app.service", command)
        self.assertIn("--daemon", command)
        self.assertNotIn(sys_executable_script(), command)

    def test_config_path_is_passed_when_given(self):
        with temp_dir() as tmp:
            target = tmp / "cfg.json"
            command = paths.daemon_command(target)
            self.assertIn("--config", command)
            self.assertIn(str(target), command)


def sys_executable_script() -> str:
    """帮助断言"命令里没有硬拼 xxx.py 脚本"。"""
    return "campus_login.py"


class MigrationTests(unittest.TestCase):
    def _make_legacy(self, root: Path) -> None:
        write_json(root / "config.json", {"username": "legacy-user", "password_enc": "abc"})
        write_json(root / "state.json", {"last_strategy": "drcom:plain-yau"})
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs" / "campus_login.log").write_text("old log\n", encoding="utf-8")

    def test_legacy_files_are_copied_once(self):
        with temp_dir(prefix="install-") as install, isolated_data_dir() as data:
            self._make_legacy(install)

            original_install_dir = paths.install_dir
            try:
                paths.install_dir = lambda: install  # type: ignore[assignment]
                report = paths.migrate_legacy_data()
            finally:
                paths.install_dir = original_install_dir  # type: ignore[assignment]

            self.assertTrue(report.ran)
            self.assertTrue(report.ok, report.errors)
            self.assertTrue((data / "config.json").exists())
            self.assertTrue((data / "state.json").exists())
            self.assertTrue((data / "logs" / "campus_login.log").exists())
            # 旧文件必须原地保留（不删除用户数据）
            self.assertTrue((install / "config.json").exists())

            # 再跑一次：目标已存在 -> 跳过，不覆盖
            (data / "config.json").write_text('{"username": "new"}', encoding="utf-8")
            try:
                paths.install_dir = lambda: install  # type: ignore[assignment]
                second = paths.migrate_legacy_data()
            finally:
                paths.install_dir = original_install_dir  # type: ignore[assignment]
            self.assertTrue(second.skipped)
            self.assertEqual(
                json.loads((data / "config.json").read_text(encoding="utf-8"))["username"], "new"
            )

    def test_nothing_to_migrate_is_not_an_error(self):
        with temp_dir(prefix="install-") as install, isolated_data_dir():
            original_install_dir = paths.install_dir
            try:
                paths.install_dir = lambda: install  # type: ignore[assignment]
                report = paths.migrate_legacy_data()
            finally:
                paths.install_dir = original_install_dir  # type: ignore[assignment]
            self.assertFalse(report.ran)
            self.assertTrue(report.ok)


class FrozenSimulationTests(unittest.TestCase):
    """模拟打包环境：sys.frozen + sys.executable 指向 EXE。"""

    def setUp(self):
        import sys

        self._sys = sys
        self._frozen = getattr(sys, "frozen", None)
        self._executable = sys.executable
        self._tmp_ctx = temp_dir(prefix="frozen-")
        self.exe_dir = self._tmp_ctx.__enter__()

    def tearDown(self):
        if self._frozen is None:
            if hasattr(self._sys, "frozen"):
                del self._sys.frozen
        else:
            self._sys.frozen = self._frozen
        self._sys.executable = self._executable
        try:
            self._tmp_ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def _fake_frozen(self, name: str = "CampusNet.exe") -> Path:
        exe = self.exe_dir / name
        exe.write_bytes(b"MZ")
        self._sys.frozen = True  # type: ignore[attr-defined]
        self._sys.executable = str(exe)
        return exe

    def test_install_dir_is_the_exe_folder(self):
        self._fake_frozen()
        self.assertTrue(paths.is_frozen())
        self.assertEqual(paths.install_dir(), self.exe_dir)

    def test_daemon_command_prefers_service_exe(self):
        self._fake_frozen("CampusNet.exe")
        (self.exe_dir / "CampusNetService.exe").write_bytes(b"MZ")
        command = paths.daemon_command()
        self.assertEqual(Path(command[0]).name, "CampusNetService.exe")

    def test_daemon_command_falls_back_to_self_with_role(self):
        """只复制了一个 EXE 时，必须显式指定角色，否则会把界面再开一次。"""
        self._fake_frozen("CampusNet.exe")
        command = paths.daemon_command()
        self.assertEqual(Path(command[0]).name, "CampusNet.exe")
        self.assertIn("--role", command)
        self.assertIn("service", command)

    def test_resource_dir_falls_back_to_install_dir(self):
        self._fake_frozen()
        self.assertEqual(paths.resource_dir(), self.exe_dir)


class FrozenExeHelperTests(unittest.TestCase):
    def test_frozen_exe_is_none_in_source_mode(self):
        self.assertIsNone(paths.frozen_exe())

    def test_service_exe_empty_in_source_mode(self):
        self.assertEqual(paths.service_exe(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

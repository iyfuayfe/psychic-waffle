"""配置系统测试：默认值、校验、迁移、原子保存、TLS 策略、脱敏快照。"""

from __future__ import annotations

import json
import unittest

from tests.support import isolated_data_dir, temp_dir, write_json

from campusnet.config import CONFIG_SCHEMA_VERSION, Config, ConfigError
from campusnet.paths import data_dir as configured_data_dir

class DefaultsTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        cfg = Config()
        # V3.0：TLS 默认校验；不再默认关闭校验
        self.assertFalse(cfg.insecure_tls)
        # 默认不写认证页转储
        self.assertFalse(cfg.portal_dump_enabled)
        # 默认没有任何凭据投递白名单（只允许认证页自身）
        self.assertEqual(cfg.trusted_auth_hosts, [])
        self.assertEqual(cfg.config_version, CONFIG_SCHEMA_VERSION)

    def test_default_targets_are_http_and_multiple(self):
        cfg = Config()
        targets = cfg.targets()
        self.assertGreaterEqual(len(targets), 2, "至少要有两个独立探测目标，避免单点误判")

    def test_missing_file_returns_defaults(self):
        with isolated_data_dir():
            cfg = Config.load()
            self.assertFalse(cfg.is_configured())


class ValidationTests(unittest.TestCase):
    def test_numbers_are_clamped_into_range(self):
        cfg = Config(interval_online=1, interval_offline=0, interval_max=99999,
                     request_timeout=0, log_max_bytes=1, log_backups=99)
        problems = cfg.validate()
        self.assertGreaterEqual(cfg.interval_online, 10)
        self.assertGreaterEqual(cfg.interval_offline, 3)
        self.assertLessEqual(cfg.interval_max, 3600)
        self.assertGreaterEqual(cfg.request_timeout, 3)
        self.assertGreaterEqual(cfg.log_max_bytes, 100_000)
        self.assertLessEqual(cfg.log_backups, 20)
        self.assertTrue(problems)

    def test_interval_max_never_below_offline(self):
        cfg = Config(interval_offline=60, interval_max=10)
        cfg.validate()
        self.assertGreaterEqual(cfg.interval_max, cfg.interval_offline)

    def test_string_bools_are_coerced(self):
        cfg = Config(auto_enabled="false", autofill_click="0", portal_auth_enabled="yes")
        problems = cfg.validate()
        self.assertFalse(cfg.auto_enabled)
        self.assertFalse(cfg.autofill_click)
        self.assertTrue(cfg.portal_auth_enabled)
        self.assertTrue(any(name == "auto_enabled" for name, _ in problems))

    def test_bad_portal_url_is_dropped_not_crashed(self):
        cfg = Config(portal_url="javascript:alert(1)")
        problems = cfg.validate()
        self.assertEqual(cfg.portal_url, "")
        self.assertTrue(any(name == "portal_url" for name, _ in problems))

    def test_unknown_wifi_mode_falls_back_to_saved(self):
        cfg = Config(wifi_connect_mode="whatever")
        problems = cfg.validate()
        self.assertEqual(cfg.wifi_connect_mode, "saved")
        self.assertTrue(problems)

    def test_custom_endpoint_disabled_when_invalid(self):
        cfg = Config()
        cfg.custom.enabled = True
        cfg.custom.url = ""
        cfg.validate()
        self.assertFalse(cfg.custom.enabled)

    def test_empty_check_urls_restore_defaults(self):
        cfg = Config(check_urls=[])
        problems = cfg.validate()
        self.assertTrue(cfg.targets())
        self.assertTrue(any(name == "check_urls" for name, _ in problems))

    def test_global_insecure_tls_is_reported(self):
        cfg = Config(insecure_tls=True)
        problems = cfg.validate()
        self.assertTrue(any(name == "insecure_tls" for name, _ in problems))


class TlsPolicyTests(unittest.TestCase):
    def test_verifies_by_default(self):
        cfg = Config()
        self.assertTrue(cfg.tls_verify_for("https://example.com/"))
        self.assertTrue(cfg.tls_verify_for("http://10.0.0.1/"))

    def test_per_host_exception(self):
        cfg = Config(tls_insecure_hosts=["192.0.2.10"])
        self.assertFalse(cfg.tls_verify_for("https://192.0.2.10/portal"))
        self.assertTrue(cfg.tls_verify_for("https://www.example.com/"))

    def test_global_switch_disables_everything(self):
        cfg = Config(insecure_tls=True)
        self.assertFalse(cfg.tls_verify_for("https://bank.example.com"))


class PersistenceTests(unittest.TestCase):
    def test_save_is_atomic_and_keeps_backup(self):
        with temp_dir() as tmp:
            path = tmp / "config.json"
            cfg = Config(username="u1")
            cfg.save(path)
            first = path.read_text(encoding="utf-8")
            cfg.username = "u2"
            cfg.save(path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["username"], "u2")
            backup = path.with_suffix(".json.bak")
            self.assertTrue(backup.exists(), "第二次保存应留下 .bak")
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["username"], "u1")
            self.assertIn('"config_version"', first)

    def test_unknown_keys_survive_roundtrip(self):
        with temp_dir() as tmp:
            path = tmp / "config.json"
            write_json(path, {"config_version": CONFIG_SCHEMA_VERSION,
                              "username": "u", "future_option": {"a": 1}})
            cfg = Config.load(path)
            self.assertEqual(cfg._extra, {"future_option": {"a": 1}})
            cfg.save(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["future_option"], {"a": 1})

    def test_higher_version_config_is_refused_not_downgraded(self):
        """更高版本写过的配置不能"半读半写"：必须明确拒绝，且不改动原文件。"""
        with temp_dir() as tmp:
            path = tmp / "config.json"
            write_json(path, {"config_version": CONFIG_SCHEMA_VERSION + 1, "username": "u"})
            before = path.read_bytes()
            with self.assertRaises(ConfigError) as ctx:
                Config.load(path)
            self.assertIn("更高版本", str(ctx.exception))
            self.assertEqual(path.read_bytes(), before, "拒绝读取时不得改动原文件")

    def test_current_version_still_loads(self):
        with temp_dir() as tmp:
            path = tmp / "config.json"
            write_json(path, {"config_version": CONFIG_SCHEMA_VERSION, "username": "u2"})
            self.assertEqual(Config.load(path).username, "u2")

    def test_corrupt_json_raises_with_helpful_message_and_keeps_file(self):
        with temp_dir() as tmp:
            path = tmp / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                Config.load(path)
            self.assertIn("JSON", str(ctx.exception))
            self.assertTrue(path.exists(), "坏配置不能被删除或覆盖")

    def test_non_object_root_rejected(self):
        with temp_dir() as tmp:
            path = tmp / "config.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(ConfigError):
                Config.load(path)

    def test_old_config_is_migrated_and_backed_up(self):
        with isolated_data_dir() as data:
            path = data / "config.json"
            write_json(path, {"username": "olduser", "portal_url": "http://10.0.0.1/",
                              "auto_enabled": "true"})
            from campusnet.config import LoadReport

            report = LoadReport()
            cfg = Config.load(path, report=report)
            self.assertTrue(report.migrated)
            self.assertEqual(report.from_version, 1)
            self.assertTrue(report.backup, "迁移前必须备份原始配置")
            self.assertTrue(cfg.auto_enabled, "字符串 \"true\" 应被规整成 True")
            self.assertEqual(cfg.config_version, CONFIG_SCHEMA_VERSION)

    def test_redacted_snapshot_hides_secrets(self):
        cfg = Config(username="13800000000", password_enc="AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA1234")
        snapshot = cfg.redacted()
        self.assertNotIn("13800000000", json.dumps(snapshot, ensure_ascii=False))
        self.assertNotIn("AQAAANCMnd8BFdERjHoAwE", snapshot["password_enc"])


class SaveGuardTests(unittest.TestCase):
    """更高版本的配置读不回来时，界面侧的保存必须**拒绝**而不是覆盖原文件。

    为什么重要：概览页「设为目标」、认证页记住门户地址等路径会把"读失败后回退
    出来的默认配置"直接写盘；没有这道守卫，用户的高版本配置会被默认值覆盖
    （账号、密码、门户地址全丢）。
    """

    def test_save_refused_when_config_unreadable(self):
        from campusnet.ui import gui_service

        with isolated_data_dir() as data:
            path = data / "config.json"
            write_json(path, {"config_version": CONFIG_SCHEMA_VERSION + 1, "username": "u"})
            before = path.read_bytes()
            ok, message = gui_service.save_config(Config(username="someone-else"))
            self.assertFalse(ok)
            self.assertIn("没有写入", message)
            self.assertEqual(path.read_bytes(), before, "拒绝写入时不得改动原文件")

    def test_first_run_save_is_allowed(self):
        from campusnet.ui import gui_service

        with isolated_data_dir() as data:
            ok, message = gui_service.save_config(Config(username="u"))
            self.assertTrue(ok, message)
            self.assertTrue((data / "config.json").exists())


class StateAndHeartbeatTests(unittest.TestCase):
    def test_state_roundtrip(self):
        from campusnet.config import load_heartbeat, load_state, save_heartbeat, save_state

        with isolated_data_dir():
            save_state({"last_strategy": "drcom:plain-yau"})
            self.assertEqual(load_state()["last_strategy"], "drcom:plain-yau")
            save_heartbeat({"state": "online", "pid": 1})
            beat = load_heartbeat()
            self.assertEqual(beat["state"], "online")
            self.assertIn("updated_at", beat)

    def test_state_lives_in_user_data_dir(self):
        with isolated_data_dir() as data:
            from campusnet.config import save_state

            save_state({"last_strategy": "x"})
            self.assertTrue((data / "state.json").exists())
            self.assertEqual(configured_data_dir(), data)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

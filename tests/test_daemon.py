"""后台守护逻辑测试（不联网、不真的常驻）。

检查的是"状态机与退避"这类最容易写错、又最容易出事故的部分：

* 失败后间隔按 1.6 倍增长并封顶；
* 账号密码错误时直接退到最大间隔（不撞锁）；
* 成功一次就回到常规巡检间隔；
* 心跳文件写的是真实状态；
* stop.flag 能让循环优雅退出；
* 休眠恢复检测（wall clock vs monotonic）。

做法：把 :mod:`campusnet.daemon` 里导入的 ``check`` 换成假的，这样
``Daemon._tick`` 可以在完全离线的环境里跑完整条判定链。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import data_dir as isolated_data_dir

from campusnet import daemon as daemon_mod
from campusnet import paths
from campusnet.adapters import (
    STATUS_BAD_CREDENTIALS,
    STATUS_CAPTCHA_REQUIRED,
    STATUS_SERVER_ERROR,
    STATUS_SUCCESS,
    AuthOutcome,
)
from campusnet.checker import STATE_DNS, STATE_ONLINE, STATE_REDIRECT, CheckResult
from campusnet.config import Config
from campusnet.daemon import (
    STATE_COOLDOWN,
    STATE_NEED_USER,
    STATE_PAUSED,
    STOP_FLAG_NAME,
    Daemon,
    LoopSnapshot,
)


class FakeAuth:
    """按预设顺序返回结果的假认证器。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def login(self, portal, username, password, verify):
        self.calls += 1
        if not self.results:
            return AuthOutcome(ok=False, status=STATUS_SERVER_ERROR, reason="exhausted")
        return self.results.pop(0)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _add(self, level, message, *args):
        try:
            self.lines.append(f"{level} {message % args if args else message}")
        except Exception:  # noqa: BLE001
            self.lines.append(f"{level} {message}")

    def debug(self, m, *a):
        self._add("DEBUG", m, *a)

    def info(self, m, *a):
        self._add("INFO", m, *a)

    def warning(self, m, *a):
        self._add("WARN", m, *a)

    def error(self, m, *a):
        self._add("ERROR", m, *a)

    def exception(self, m, *a):
        self._add("EXC", m, *a)

    def log(self, m, *a):
        self._add("LOG", m, *a)


def offline(kind=STATE_REDIRECT, portal="http://portal.test/a79.htm", detail="test") -> CheckResult:
    return CheckResult(online=False, kind=kind, portal_url=portal, detail=detail)


class PatchedCheck:
    """临时替换 daemon 模块里的 ``check``。"""

    def __init__(self, result):
        self.result = result
        self._original = None

    def __enter__(self):
        self._original = daemon_mod.check
        result = self.result

        def fake_check(client, cfg, **kwargs):
            if callable(result):
                return result()
            return result

        daemon_mod.check = fake_check  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        daemon_mod.check = self._original  # type: ignore[assignment]
        return False


def new_daemon():
    return Daemon(log=FakeLogger())


class BackoffTests(unittest.TestCase):
    def test_growth_factor_and_cap(self):
        daemon = new_daemon()
        daemon._interval = 8.0
        cfg = Config(interval_offline=8, interval_max=60)
        intervals = []
        with PatchedCheck(offline()):
            for _ in range(6):
                daemon._tick(cfg, None, FakeAuth([]), lambda: False, "pw")
                intervals.append(daemon._interval)
        for previous, current in zip(intervals, intervals[1:]):
            self.assertGreaterEqual(current, previous, "间隔不应该变短")
        self.assertGreater(intervals[-1], intervals[0], "失败后应该退避变长")
        self.assertLessEqual(daemon._interval, cfg.interval_max)

    def test_bad_credentials_jumps_to_max(self):
        daemon = new_daemon()
        cfg = Config(interval_max=120)
        outcome = AuthOutcome(ok=False, status=STATUS_BAD_CREDENTIALS,
                              reason="密码错误", bad_credentials=True)
        with PatchedCheck(offline()):
            daemon._tick(cfg, None, FakeAuth([outcome]), lambda: False, "pw")
        self.assertEqual(daemon._interval, float(cfg.interval_max))
        self.assertEqual(daemon.snapshot.state, STATE_NEED_USER)

    def test_need_user_backs_off_to_max(self):
        daemon = new_daemon()
        cfg = Config(interval_max=120)
        outcome = AuthOutcome(ok=False, status=STATUS_CAPTCHA_REQUIRED, reason="验证码")
        with PatchedCheck(offline()):
            daemon._tick(cfg, None, FakeAuth([outcome]), lambda: False, "pw")
        self.assertEqual(daemon._interval, float(cfg.interval_max))
        self.assertEqual(daemon.snapshot.state, STATE_NEED_USER)

    def test_success_resets_to_online_interval(self):
        daemon = new_daemon()
        cfg = Config(interval_online=60, interval_max=120)
        daemon._interval = 100.0
        outcome = AuthOutcome(ok=True, status=STATUS_SUCCESS, reason="ok")
        with PatchedCheck(offline()):
            daemon._tick(cfg, None, FakeAuth([outcome]), lambda: True, "pw")
        self.assertEqual(daemon._interval, float(cfg.interval_online))

    def test_interval_never_below_offline_floor(self):
        daemon = new_daemon()
        daemon._interval = 0.5
        cfg = Config(interval_offline=8, interval_max=120)
        with PatchedCheck(offline()):
            daemon._tick(cfg, None, FakeAuth([]), lambda: False, "pw")
        self.assertGreaterEqual(daemon._interval, cfg.interval_offline)


class StateClassificationTests(unittest.TestCase):
    def test_network_layer_failure_skips_auth_when_no_portal(self):
        """DNS 挂了且探测不到门户地址时，不该把账号密码往门户里灌。"""
        daemon = new_daemon()
        cfg = Config()
        auth = FakeAuth([])
        with PatchedCheck(offline(STATE_DNS, portal="")):
            daemon._tick(cfg, None, auth, lambda: False, "pw")
        self.assertEqual(auth.calls, 0)
        self.assertEqual(daemon.snapshot.kind, STATE_DNS)

    def test_portal_auth_attempted_even_when_dns_blocked_but_portal_known(self):
        """未认证时 DNS 常被拦，这时仍然应该尝试认证（有门户地址）。"""
        daemon = new_daemon()
        cfg = Config()
        cfg.portal_url = "http://portal.test/a79.htm"
        auth = FakeAuth([])
        with PatchedCheck(offline(STATE_DNS, portal="")):
            daemon._tick(cfg, None, auth, lambda: False, "pw")
        self.assertEqual(auth.calls, 1)

    def test_online_short_circuits(self):
        daemon = new_daemon()
        cfg = Config(interval_online=30)
        with PatchedCheck(CheckResult(online=True, kind=STATE_ONLINE, detail="ok")):
            daemon._tick(cfg, None, FakeAuth([]), lambda: True, "pw")
        self.assertEqual(daemon._interval, float(cfg.interval_online))

    def test_paused_when_auto_disabled(self):
        daemon = new_daemon()
        cfg = Config(auto_enabled=False, interval_online=45)
        auth = FakeAuth([])
        with PatchedCheck(offline()):
            result = daemon._tick(cfg, None, auth, lambda: False, "pw")
        self.assertEqual(result, "pw")
        self.assertEqual(daemon.snapshot.state, STATE_PAUSED)
        self.assertEqual(daemon._interval, float(cfg.interval_online))
        self.assertEqual(auth.calls, 0)

    def test_failure_logging_is_throttled(self):
        daemon = new_daemon()
        cfg = Config()
        log = FakeLogger()
        daemon.log = log
        with PatchedCheck(offline()):
            for _ in range(3):
                daemon._tick(cfg, None, FakeAuth([]), lambda: False, "pw")
        errors = [line for line in log.lines if line.startswith("ERROR")]
        self.assertLessEqual(len(errors), 2, "持续失败不应该刷屏")


class HeartbeatAndStopTests(unittest.TestCase):
    def test_heartbeat_written(self):
        with isolated_data_dir() as data:
            daemon = new_daemon()
            daemon._heartbeat(state=STATE_COOLDOWN, detail="waiting")
            beat_path = data / "heartbeat.json"
            self.assertTrue(beat_path.exists())
            payload = json.loads(beat_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], STATE_COOLDOWN)
            self.assertEqual(payload["detail"], "waiting")
            self.assertIn("next_check_at", payload)
            self.assertIn("updated_at", payload)

    def test_stop_flag_roundtrip(self):
        with isolated_data_dir():
            daemon = new_daemon()
            flag = paths.data_dir() / STOP_FLAG_NAME
            self.assertFalse(daemon._stop_requested())
            flag.write_text("1", encoding="utf-8")
            self.assertTrue(daemon._stop_requested())
            daemon._clear_stop_flag()
            self.assertFalse(daemon._stop_requested())

    def test_sleep_is_interruptible(self):
        daemon = new_daemon()
        daemon.stop_event = threading.Event()
        started = time.monotonic()
        threading.Timer(0.3, daemon.stop_event.set).start()
        daemon._sleep(10)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_sleep_notices_stop_flag_within_a_second(self):
        """回归测试：守护在睡眠中也必须响应 stop.flag。

        曾经的实现只等 stop_event，卸载写下的 stop.flag 要等到
        interval_online（默认 60s、最长 120s）睡醒才被看见 ——
        表现为"卸载要等两分钟、数据目录删不掉（文件还被占着）"。
        """
        with isolated_data_dir():
            daemon = new_daemon()
            daemon.stop_event = threading.Event()
            flag = paths.data_dir() / STOP_FLAG_NAME

            def write_flag():
                time.sleep(0.4)
                flag.write_text("1", encoding="utf-8")

            threading.Thread(target=write_flag, daemon=True).start()
            started = time.monotonic()
            daemon._sleep(30.0)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 5.0, f"stop.flag 没有及时被看到（用了 {elapsed:.1f}s）")

    def test_probe_does_not_leak_the_mutex(self):
        """反复探测"守护是否在跑"不能把互斥体一直攥在手里。"""
        from campusnet import autostart

        for _ in range(30):
            autostart.daemon_running()
        # 探测完之后，真实的守护应该还能拿到互斥体
        from campusnet import crypto
        from campusnet.version import mutex_name

        handle = crypto.acquire_single_instance(mutex_name(paths.data_dir_key()))
        try:
            self.assertIsNotNone(handle, "反复探测后互斥体仍被占用，守护将无法启动")
        finally:
            crypto.release_single_instance(handle)

    def test_resume_detection_notices_clock_jump(self):
        daemon = new_daemon()
        daemon._wall_start = time.time()
        daemon._mono_start = time.monotonic()
        daemon._interval = 8.0
        self.assertEqual(daemon._detect_resume(), 0.0)
        # 伪造"睡了 10 分钟"：wall 走了 600 秒，monotonic 几乎没动
        daemon._wall_start = time.time() - 600
        daemon._mono_start = time.monotonic()
        self.assertGreaterEqual(daemon._detect_resume(), 500)

    def test_snapshot_is_json_safe(self):
        snap = LoopSnapshot(state="online", kind=STATE_ONLINE, detail="ok")
        payload = json.dumps(snap.as_dict(), ensure_ascii=False)
        self.assertIn("online", payload)


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        from campusnet import crypto

        name = r"Local\CampusNetUnittestMutex"
        first = crypto.acquire_single_instance(name)
        if first is None:
            self.skipTest("互斥体已被占用（可能是并发测试），跳过")
        try:
            second = crypto.acquire_single_instance(name)
            self.assertIsNone(second, "第二次获取同名互斥体必须失败")
        finally:
            pass


class ReloadCredentialTests(unittest.TestCase):
    """配置热加载的凭据语义：清空密码后绝不能再沿用内存里的旧密码。"""

    def _reload_daemon(self):
        daemon = new_daemon()
        daemon.config_path = Path("unused.json")
        daemon._cfg_mtime = 1
        return daemon

    def test_cleared_password_marks_unavailable(self):
        daemon = self._reload_daemon()
        old = Config(username="u", password_enc="OLD")
        new = Config(username="u", password_enc="")
        with mock.patch("campusnet.daemon._mtime", return_value=2), \
             mock.patch.object(daemon, "_load_config", return_value=new):
            _, _, _, _, password = daemon._maybe_reload(
                old, object(), object(), object(), "old-password"
            )
        self.assertIsNone(password, "清空密码后必须返回 None，让调用方暂停认证")

    def test_broken_new_password_keeps_whole_old_config(self):
        daemon = self._reload_daemon()
        old = Config(username="u", password_enc="OLD")
        new = Config(username="u2", password_enc="BROKEN")
        with mock.patch("campusnet.daemon._mtime", return_value=2), \
             mock.patch.object(daemon, "_load_config", return_value=new), \
             mock.patch("campusnet.daemon.crypto.unprotect", side_effect=OSError("dpapi")):
            cfg, _, _, _, password = daemon._maybe_reload(
                old, object(), object(), object(), "old-password"
            )
        self.assertIs(cfg, old, "新密码解不开时必须整组回退到旧配置，避免新旧混用")
        self.assertEqual(password, "old-password")

    def test_config_change_with_same_password_keeps_credentials(self):
        daemon = self._reload_daemon()
        old = Config(username="u", password_enc="SAME")
        new = Config(username="u", password_enc="SAME", request_timeout=9)
        with mock.patch("campusnet.daemon._mtime", return_value=2), \
             mock.patch.object(daemon, "_load_config", return_value=new):
            cfg, _, _, _, password = daemon._maybe_reload(
                old, object(), object(), object(), "pw"
            )
        self.assertIs(cfg, new)
        self.assertEqual(password, "pw")


class HeartbeatToleranceTests(unittest.TestCase):
    """心跳文件被写坏时，状态查询不能抛异常（界面状态页会因此整页报错）。"""

    def test_non_numeric_pid_does_not_raise(self):
        with isolated_data_dir() as data:
            (data / "heartbeat.json").write_text(
                json.dumps({"pid": "not-a-number", "state": STATE_ONLINE,
                            "updated_at": time.time()}),
                encoding="utf-8",
            )
            health = daemon_mod.daemon_health()
            self.assertEqual(health.pid, 0)
            self.assertEqual(health.state, STATE_ONLINE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

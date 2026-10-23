"""进程加固模块测试（3.1.0 语义：默认关闭、显式可选）。

背景与决策依据见 ``campusnet/hardening.py`` 的模块说明。核心事实：

* 3.0.4/3.0.5 默认施加 ``ProcessSignaturePolicy=MicrosoftSignedOnly``；
* 实测该策略会拒绝**签名有效但非微软签名**的组件（打包产物里的
  ``python313.dll`` / ``_tkinter.pyd`` / ``_ssl.pyd`` 都是 ``CN=Python Software
  Foundation`` 的有效签名），并导致 ``_sqlite3`` / ``_overlapped`` / ``winsound``
  等未预热扩展在运行时以"无法验证数字签名"失败；
* 因此 3.1.0 起默认 ``off``，只有用户显式设置 ``CAMPUSNET_HARDEN=1`` 才施加。

这些测试守住三件事：
1. 默认路径**绝对不改变**进程安全属性（不留副作用）；
2. 显式开启时策略确实生效，且自检通过；
3. 不再用第三方进程名作为加固开关。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT

from campusnet import hardening

ROOT_LITERAL = str(PROJECT_ROOT).replace("\\", "\\\\")


def _clean_env() -> dict:
    env = dict(os.environ)
    env.pop(hardening.ENV_DISABLE, None)
    env.pop(hardening.ENV_FORCE, None)
    return env


def _run(script: str, env: dict) -> tuple[str, str]:
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          timeout=180, env=env)
    return ((proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"))


def _mode_script() -> str:
    return "\n".join([
        f"import sys; sys.path.insert(0, r'{ROOT_LITERAL}')",
        "from campusnet import hardening",
        "print(hardening.requested_mode())",
        "",
    ])


class PolicyConstantTests(unittest.TestCase):
    def test_policy_indices_match_windows_headers(self):
        # 这两个值必须和 SetProcessMitigationPolicy 的枚举一致，写错了会静默生效到别的策略
        self.assertEqual(hardening.POLICY_EXTENSION_POINT_DISABLE, 6)
        self.assertEqual(hardening.POLICY_SIGNATURE, 8)

    def test_signature_policy_constant(self):
        self.assertEqual(hardening.SIGNATURE_MICROSOFT_ONLY, 1)

    def test_error_mode_flags(self):
        self.assertEqual(hardening.SEM_FAILCRITICALERRORS, 0x0001)
        self.assertEqual(hardening.SEM_NOOPENFILEERRORBOX, 0x8000)

    def test_mode_constants(self):
        self.assertEqual(hardening.MODE_OFF, "off")
        self.assertEqual(hardening.MODE_OPT_IN, "opt-in")


class DefaultModeTests(unittest.TestCase):
    """默认路径：不做任何进程缓解，不留副作用。"""

    def test_default_mode_is_off(self):
        env = _clean_env()
        env["PYTHONIOENCODING"] = "utf-8"
        out, err = _run(_mode_script(), env)
        self.assertEqual(out.strip(), "off", f"默认模式应为 off，实际 {out!r}；stderr={err[:300]}")

    def test_early_protect_applies_no_policy(self):
        report = hardening.early_protect()
        self.assertEqual(report["mode"], "off")
        self.assertEqual(report["extension_points"], "skipped")
        self.assertEqual(report["error_mode"], "skipped")
        self.assertIn("默认不施加", report["reason"])

    def test_self_protect_applies_no_policy(self):
        report = hardening.self_protect()
        self.assertEqual(report["mode"], "off")
        self.assertEqual(report["signature"], "skipped")
        self.assertEqual(report["extension_points"], "skipped")

    def test_default_does_not_touch_error_mode(self):
        """默认路径不能改 SetErrorMode —— 那会掩盖真实的依赖缺失错误。"""
        kernel32 = ctypes.WinDLL("kernel32")
        before = kernel32.GetErrorMode()
        hardening.early_protect()
        hardening.self_protect()
        after = kernel32.GetErrorMode()
        self.assertEqual(before, after, "默认模式不应改变进程的 ErrorMode")

    def test_describe_says_not_enabled(self):
        hardening.early_protect()
        self.assertIn("未启用", hardening.describe())


class OptInEnvTests(unittest.TestCase):
    """显式开启的判定：只认显式设置，不再看第三方进程在不在。"""

    def test_opt_in_values(self):
        for value in ("1", "true", "yes", "on", "all"):
            env = _clean_env()
            env[hardening.ENV_FORCE] = value
            env["PYTHONIOENCODING"] = "utf-8"
            out, err = _run(_mode_script(), env)
            self.assertEqual(out.strip(), "opt-in", f"{value}: {out} {err[:200]}")

    def test_extension_only_value_is_opt_in(self):
        env = _clean_env()
        env[hardening.ENV_FORCE] = "extpoint"
        env["PYTHONIOENCODING"] = "utf-8"
        out, _err = _run(_mode_script(), env)
        self.assertEqual(out.strip(), "opt-in")

    def test_disabled_values(self):
        for value in ("0", "false", "no", "off", ""):
            env = _clean_env()
            env[hardening.ENV_FORCE] = value
            env["PYTHONIOENCODING"] = "utf-8"
            out, _err = _run(_mode_script(), env)
            self.assertEqual(out.strip(), "off", value)

    def test_disable_env_wins(self):
        env = _clean_env()
        env[hardening.ENV_FORCE] = "1"
        env[hardening.ENV_DISABLE] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        out, _err = _run(_mode_script(), env)
        self.assertEqual(out.strip(), "off")

    def test_no_process_name_gating_in_mode_decision(self):
        """加固开关不得依赖第三方进程名（任务书明确禁止硬编码规避）。"""
        source = (PROJECT_ROOT / "campusnet" / "hardening.py").read_text(encoding="utf-8")
        mode_func = source[source.index("def requested_mode"):source.index("def _set_policy")]
        for forbidden in ("conflicts", "gamepp", "overlay_processes_running", "processes_running"):
            self.assertNotIn(forbidden, mode_func.lower(),
                             f"requested_mode 不得依赖 {forbidden}")


class HardeningOptInSubprocessTest(unittest.TestCase):
    """真正施加策略只放在子进程里跑：策略不可撤销，不能污染测试进程。"""

    def test_opt_in_applies_policy_and_passes_self_test(self):
        env = _clean_env()
        env[hardening.ENV_FORCE] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        script = "\n".join([
            f"import sys; sys.path.insert(0, r'{ROOT_LITERAL}')",
            "from campusnet import hardening",
            "rep = hardening.self_protect()",
            "print('MODE', rep.get('mode'))",
            "print('EXT', rep.get('extension_points'))",
            "print('SIG', rep.get('signature'))",
            "checks = rep.get('self_test') or dict()",
            "print('BROKEN', [k for k, v in checks.items() if v != 'ok'])",
            "import ctypes",
            "try:",
            r"    ctypes.WinDLL(r'C:\Program Files (x86)\GamePP\ffmpeg.dll')",
            "    print('UNSIGNED', 'loaded')",
            "except Exception:",
            "    print('UNSIGNED', 'blocked')",
            "",
        ])
        out, err = _run(script, env)
        self.assertIn("MODE opt-in", out, f"{out} {err[:300]}")
        self.assertIn("EXT ok", out, f"{out} {err[:300]}")
        self.assertIn("SIG ok", out, f"{out} {err[:300]}")
        self.assertIn("BROKEN []", out, f"可选加固不应破坏自检项：{out}")
        if Path(r"C:\Program Files (x86)\GamePP\ffmpeg.dll").exists():
            self.assertIn("UNSIGNED blocked", out)

    def test_default_subprocess_keeps_extension_modules_working(self):
        """默认模式必须能用 sqlite3 / asyncio / winsound（3.0.5 会以签名失败）。"""
        env = _clean_env()
        env["PYTHONIOENCODING"] = "utf-8"
        script = "\n".join([
            f"import sys; sys.path.insert(0, r'{ROOT_LITERAL}')",
            "from campusnet import hardening",
            "hardening.early_protect()",
            "hardening.self_protect()",
            "import sqlite3, asyncio, winsound",
            "print('EXTENSIONS ok', bool(sqlite3.sqlite_version))",
            "",
        ])
        out, err = _run(script, env)
        self.assertIn("EXTENSIONS ok", out, f"默认模式不应影响扩展模块：{out} {err[:400]}")


class PreloadTests(unittest.TestCase):
    def test_preload_list_is_importable(self):
        failed = hardening._preload_own_dependencies()
        self.assertEqual(failed, [], f"这些模块导入失败：{failed}")

    def test_preload_covers_codecs_and_ssl(self):
        hardening._preload_own_dependencies()
        import importlib

        for name in ("ssl", "encodings.idna", "encodings.gbk", "unicodedata", "hashlib"):
            self.assertIsNotNone(importlib.import_module(name), name)


class SelfTestTests(unittest.TestCase):
    def test_self_test_reports_every_capability(self):
        results = hardening.self_test()
        for key in ("https", "idna", "gbk", "hashlib", "json", "zlib", "subprocess"):
            self.assertIn(key, results, f"自检缺少 {key}")
            self.assertEqual(results[key], "ok", f"{key} 自检失败：{results[key]}")

    def test_self_test_never_raises(self):
        self.assertIsInstance(hardening.self_test(), dict)


class ErrorModeTests(unittest.TestCase):
    """弹窗抑制只在可选加固路径里用；单独调用时行为必须正确。"""

    def test_suppress_loader_dialogs_sets_expected_flags(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows")
        info = hardening._suppress_loader_dialogs()
        self.assertTrue(str(info).startswith("ok"), f"设置 ErrorMode 失败：{info}")
        mode = ctypes.WinDLL("kernel32").GetErrorMode()
        self.assertTrue(mode & hardening.SEM_FAILCRITICALERRORS)
        self.assertTrue(mode & hardening.SEM_NOOPENFILEERRORBOX)


class DescriptionTests(unittest.TestCase):
    def test_describe_with_no_report(self):
        self.assertTrue(hardening.describe({}))

    def test_describe_mentions_signature_lock_when_applied(self):
        text = hardening.describe({"extension_points": "ok", "signature": "ok", "reason": ""})
        self.assertIn("微软签名", text)

    def test_describe_surfaces_reason(self):
        text = hardening.describe(
            {"extension_points": "skipped", "signature": "skipped", "reason": "未检测到叠加类软件"}
        )
        self.assertIn("未检测到叠加类软件", text)

    def test_status_shape(self):
        status = hardening.status()
        for key in ("mode", "disabled_by_env", "forced_by_env",
                    "overlay_processes", "injected_modules", "last_report"):
            self.assertIn(key, status)


class GuiEntryTests(unittest.TestCase):
    """源码层守卫：GUI 入口必须把加固放在显式开关之后。"""

    def _main_body(self) -> str:
        source = (PROJECT_ROOT / "campusnet" / "ui" / "main_window.py").read_text(encoding="utf-8")
        return source[source.index("def main(argv"):]

    def test_gui_entry_gates_hardening_behind_opt_in(self):
        main_body = self._main_body()
        self.assertIn("requested_mode()", main_body,
                      "GUI 入口必须显式判断加固模式，而不是无条件施加")
        self.assertLess(main_body.index("requested_mode()"), main_body.index("self_protect()"),
                        "必须先判断模式，再决定是否施加加固")

    def test_withdraw_only_happens_after_opt_in_check(self):
        """默认模式下不应为了加固而隐藏窗口（避免无谓闪烁）。"""
        main_body = self._main_body()
        self.assertIn("root.withdraw()", main_body)
        self.assertLess(main_body.index("requested_mode()"), main_body.index("root.withdraw()"),
                        "withdraw 必须发生在'已确认用户开启加固'之后")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

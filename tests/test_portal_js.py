"""浏览器注入脚本与"来源确认"流程测试（不需要真的开浏览器）。

浏览器模式（CDP）无法在无图形环境里跑，但脚本本身可以测：

* 探针脚本里**不含任何凭据**（这是两阶段设计的关键）；
* 填写脚本里有 origin 守卫，页面对不上就直接返回 ``wrong-origin``；
* 状态文案覆盖所有会出现的状态。
"""

from __future__ import annotations

import json
import unittest

from tests.support import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from campusnet import portal_js

SECRET_PASSWORD = "s3cr3t-Pa55!"
SECRET_USER = "20230001"


def fill_script(**overrides):
    config = {
        "origin": "http://192.0.2.10",
        "username": SECRET_USER,
        "password": SECRET_PASSWORD,
        "submit": True,
        "service": "",
        "userSelector": "",
        "passwordSelector": "",
        "submitSelector": "",
        "statusOnly": False,
    }
    config.update(overrides)
    return portal_js.build(config)


class ProbeTests(unittest.TestCase):
    def test_probe_has_no_credentials(self):
        script = portal_js.build_probe("http://192.0.2.10")
        self.assertNotIn(SECRET_PASSWORD, script)
        self.assertNotIn(SECRET_USER, script)
        # 只带 origin 这一个期望值
        self.assertIn("http://192.0.2.10", script)

    def test_probe_reports_origin_and_protocol(self):
        script = portal_js.build_probe("http://a.test")
        for key in ("href", "origin", "protocol", "ready", "state"):
            self.assertIn(key, script)

    def test_probe_detects_error_pages_and_foreign_origins(self):
        script = portal_js.build_probe("http://a.test")
        self.assertIn("page-error", script)
        self.assertIn("wrong-origin", script)
        self.assertIn("insecure-origin", script)

    def test_probe_is_an_immediately_invoked_expression(self):
        script = portal_js.build_probe("")
        self.assertTrue(script.strip().startswith("(() =>"))


class FillScriptTests(unittest.TestCase):
    def test_origin_guard_is_present(self):
        script = fill_script()
        self.assertIn("location.origin !== cfg.origin", script)
        self.assertIn("'wrong-origin'", script)

    def test_credentials_are_json_escaped(self):
        script = fill_script(username='a"b\\c', password="p'w")
        payload = script.split("const cfg = ", 1)[1].split(";", 1)[0]
        parsed = json.loads(payload)
        self.assertEqual(parsed["username"], 'a"b\\c')
        self.assertEqual(parsed["password"], "p'w")

    def test_no_captcha_bypass(self):
        """脚本遇到验证码只能是"要人工处理"，绝不能自动填写/绕过。"""
        script = fill_script()
        self.assertIn("manual-required", script)
        self.assertNotIn("bypass", script.lower())

    def test_describe_covers_every_documented_state(self):
        for state in (
            "wrong-origin", "insecure-origin", "page-error", "authenticated",
            "authentication-rejected", "waiting-ready", "missing-fields",
            "manual-required", "agreement-required", "waiting-agreement",
            "service-required", "missing-service", "filled", "waiting-submit",
            "submitted", "submission-unconfirmed", "awaiting-authentication",
            "not-submitted", "invalid-selector", "missing-submit",
        ):
            text = portal_js.describe(state)
            self.assertNotEqual(text, state, f"{state} 缺少中文说明")
            self.assertNotEqual(text, "未知状态", state)


class WebauthGuardTests(unittest.TestCase):
    """不打开浏览器，只验证"来源不合法就不进入填写阶段"的分支。"""

    def test_invalid_portal_url_returns_refused(self):
        from campusnet import webauth
        from campusnet.config import Config

        result = webauth.run(Config(), "pw", portal_url="javascript:alert(1)")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "refused-target")
        self.assertTrue(result["needs_user"])

    def test_result_is_uniform_dict(self):
        from campusnet import webauth
        from campusnet.config import Config

        result = webauth.run(Config(), "pw", portal_url="")
        for key in ("ok", "status", "reason", "needs_user", "kept_open"):
            self.assertIn(key, result)

    def test_to_outcome_maps_status(self):
        from campusnet import webauth

        outcome = webauth.to_outcome({"ok": True, "status": "success", "reason": "ok"})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.adapter, "browser")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

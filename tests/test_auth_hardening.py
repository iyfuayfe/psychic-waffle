"""Credential boundary regressions. Only fake credentials and loopback servers."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import unittest
import urllib.error
from unittest import mock

from tests.support import FakeClient, FakeResponse, Silence, data_dir, http_server
from tests.test_authenticator import FORM_PAGE, PORTAL, make_cfg
from tests.test_daemon import FakeAuth, PatchedCheck, new_daemon, offline
from campusnet.adapters import STATUS_BAD_CREDENTIALS, STATUS_REFUSED_TARGET, STATUS_SUCCESS, AuthOutcome
from campusnet.authenticator import PortalAuthenticator
from campusnet.config import Config
from campusnet.drcom import DrcomAdapter, RefusedTargetError
from campusnet.http import HttpClient
from campusnet import portal_js
from campusnet.security import UrlPolicy


class RedirectSessionTests(unittest.TestCase):
    def test_cross_origin_redirect_strips_credentials(self):
        received = []

        def target(request):
            received.append(dict(request.headers))
            return 200, {}, b"ok"

        with http_server({("GET", "/target"): target}) as (other, _):
            # Different host, not just a different port: CookieJar must reject
            # the source host's cookie even though both servers are loopback.
            other = other.replace("127.0.0.1", "localhost")
            with http_server({
                ("GET", "/seed"): lambda r: (200, {"Set-Cookie": "sid=source-secret; Path=/"}, b"ok"),
                ("GET", "/login"): lambda r: (302, {"Location": other + "/target"}, b""),
            }) as (origin, _):
                client = HttpClient(timeout=3)
                client.get(origin + "/seed")
                response = client.get(origin + "/login", headers={
                    "Authorization": "Bearer fake-secret",
                    "Proxy-Authorization": "Basic fake-proxy-secret",
                    "Referer": origin + "/login?token=fake-secret",
                    "Origin": origin,
                })
        self.assertEqual(response.status, 200)
        self.assertEqual(len(received), 1)
        headers = {k.lower(): v for k, v in received[0].items()}
        for name in ("cookie", "authorization", "proxy-authorization", "referer", "origin"):
            self.assertNotIn(name, headers, name)

    def test_same_origin_redirect_uses_updated_cookie(self):
        received = []

        def target(request):
            received.append(request.headers.get("Cookie"))
            received.append(request.headers.get("Authorization"))
            return 200, {}, b"ok"

        with http_server({
            ("GET", "/seed"): lambda r: (200, {"Set-Cookie": "sid=old; Path=/"}, b""),
            ("GET", "/login"): lambda r: (302, {"Location": "/target", "Set-Cookie": "sid=new; Path=/"}, b""),
            ("GET", "/target"): target,
        }) as (origin, _):
            client = HttpClient(timeout=3)
            client.get(origin + "/seed")
            client.get(origin + "/login", headers={"Authorization": "Bearer fake-secret"})
        self.assertEqual(received, ["sid=new", "Bearer fake-secret"])

    def test_same_origin_redirect_obeys_cookie_path(self):
        received = []

        def target(request):
            received.append(request.headers.get("Cookie"))
            return 200, {}, b"ok"

        with http_server({
            ("GET", "/seed"): lambda r: (200, {"Set-Cookie": "sid=private; Path=/private"}, b""),
            ("GET", "/private/login"): lambda r: (302, {"Location": "/public"}, b""),
            ("GET", "/public"): target,
        }) as (origin, _):
            client = HttpClient(timeout=3)
            client.get(origin + "/seed")
            client.get(origin + "/private/login")
        self.assertEqual(received, [None])

    def test_https_redirect_never_downgrades_to_http(self):
        client = HttpClient(timeout=3)
        error = urllib.error.HTTPError("https://portal.test/login", 302, "redirect", {
            "Location": "http://portal.test/login",
        }, io.BytesIO(b""))
        with mock.patch.object(client, "_opener_for") as opener:
            opener.return_value.open.side_effect = error
            response = client.get("https://portal.test/login")
        self.assertEqual(response.status, 302)
        self.assertEqual(opener.return_value.open.call_count, 1)


class DrcomBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.data_context = data_dir()
        self.data_context.__enter__()
        self.addCleanup(self.data_context.__exit__, None, None, None)

    def test_untrusted_drcom_host_refused_before_probes(self):
        cfg = Config(portal_url=PORTAL)
        cfg.drcom.host = "untrusted.test"
        client = FakeClient({PORTAL: FakeResponse(200, "<html>eportal</html>", PORTAL)})
        result = PortalAuthenticator(client, cfg, Silence()).login(PORTAL, "student", "fake-secret", lambda: False)
        self.assertEqual(result.status, STATUS_REFUSED_TARGET)
        self.assertTrue(result.needs_user)
        self.assertEqual([r["url"] for r in client.requests], [PORTAL])

    def test_same_host_other_port_works_and_requires_connectivity(self):
        cfg = Config(portal_url=PORTAL)
        client = FakeClient(default=FakeResponse(200, '{"result":1}'))
        adapter = DrcomAdapter(client, cfg, Silence())
        ctx = adapter.build_context(PORTAL)
        ctx.account = "student"
        ok, _, _, _ = adapter.attempt(ctx, "fake-secret", lambda: True)
        self.assertTrue(ok)
        self.assertTrue(any(":801/eportal/portal/login?" in r["url"] for r in client.requests))
        ok, _, _, _ = adapter.attempt(ctx, "fake-secret", lambda: False)
        self.assertFalse(ok)

    def test_explicit_trust_allows_separate_api_host(self):
        cfg = Config(portal_url=PORTAL, trusted_auth_hosts=["auth.school.test"])
        cfg.drcom.host = "auth.school.test"
        cfg.drcom.wlan_user_ip = "192.0.2.30"
        client = FakeClient(default=FakeResponse(200, '{"result":1}'))
        adapter = DrcomAdapter(client, cfg, Silence())
        ctx = adapter.build_context(PORTAL)
        ctx.account = "student"
        self.assertTrue(adapter.attempt(ctx, "fake-secret", lambda: True)[0])
        self.assertTrue(all("auth.school.test" in r["url"] for r in client.requests))

    def test_https_is_used_for_config_status_login_and_logout(self):
        portal = PORTAL.replace("http:", "https:")
        cfg = Config(portal_url=portal)
        client = FakeClient(default=FakeResponse(200, '{"result":1}'))
        adapter = DrcomAdapter(client, cfg, Silence())
        ctx = adapter.build_context(portal)
        ctx.account = "student"
        adapter.query_page_set(ctx)
        self.assertTrue(adapter.attempt(ctx, "fake-secret", lambda: True)[0])
        self.assertTrue(adapter.logout(ctx)[0])
        self.assertGreaterEqual(len(client.requests), 5)
        self.assertTrue(all(r["url"].startswith("https://") for r in client.requests))
        for request in client.requests:
            for value in (request.get("headers") or {}).values():
                self.assertNotIn("http://", value)

    def test_explicit_http_cannot_downgrade_https_portal(self):
        portal = PORTAL.replace("http:", "https:")
        cfg = Config(portal_url=portal)
        cfg.drcom.scheme = "http"
        client = FakeClient()
        with self.assertRaises(RefusedTargetError):
            DrcomAdapter(client, cfg, Silence()).build_context(portal)
        self.assertEqual(client.requests, [])

    def test_explicit_https_upgrades_legacy_http_portal(self):
        cfg = Config(portal_url=PORTAL)
        cfg.drcom.scheme = "https"
        cfg.drcom.port = 8443
        client = FakeClient()
        ctx = DrcomAdapter(client, cfg, Silence()).build_context(PORTAL)
        self.assertTrue(ctx.api.startswith("https://192.0.2.10:8443/"))
        self.assertTrue(all(r["url"].startswith("https://") for r in client.requests))

    def test_retargeted_context_refused_before_credentials(self):
        cfg = Config(portal_url=PORTAL)
        client = FakeClient()
        adapter = DrcomAdapter(client, cfg, Silence())
        ctx = adapter.build_context(PORTAL)
        client.requests.clear()
        ctx.host = "untrusted.test"
        with self.assertRaises(RefusedTargetError):
            adapter.attempt(ctx, "fake-secret", lambda: True)
        with self.assertRaises(RefusedTargetError):
            adapter.query_page_set(ctx)
        with self.assertRaises(RefusedTargetError):
            adapter.logout(ctx)
        self.assertEqual(client.requests, [])

    def test_invalid_transport_is_normalized_and_roundtrips(self):
        cfg = Config()
        cfg.drcom.scheme = "file"
        self.assertTrue(any(name == "drcom.scheme" for name, _ in cfg.validate()))
        self.assertEqual(cfg.drcom.scheme, "")
        cfg.drcom.scheme = "https"
        cfg.save()
        self.assertEqual(Config.load().drcom.scheme, "https")

    def test_legacy_generic_failure_keywords_are_removed_on_load(self):
        cfg = Config()
        cfg.bad_credential_keywords = ["认证失败", "invalid", "密码错误"]
        cfg.save()
        loaded = Config.load()
        self.assertEqual(loaded.bad_credential_keywords, ["密码错误"])

    def test_trusted_host_cannot_bypass_https_downgrade_guard(self):
        policy = UrlPolicy(["other.test"])
        self.assertFalse(policy.allow("http://other.test/login", "https://portal.test"))


class FormSubmissionTests(unittest.TestCase):
    def _run(self, page, response="failed", cfg=None):
        with data_dir(), mock.patch("campusnet.authenticator.time.sleep"):
            client = FakeClient({PORTAL: FakeResponse(200, page, PORTAL)}, default=FakeResponse(200, response))
            result = PortalAuthenticator(client, cfg or make_cfg(), Silence()).login(
                PORTAL, "student", "fake-secret", lambda: False
            )
        return result, [r for r in client.requests if r.get("data")]

    def test_identical_post_variants_are_only_sent_once(self):
        result, sent = self._run(FORM_PAGE)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result.attempts, 1)

    def test_identical_get_variants_are_only_sent_once(self):
        _, sent = self._run(FORM_PAGE.replace('method="post"', 'method="get"'))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "GET")

    def test_named_submit_button_keeps_two_distinct_variants(self):
        page = FORM_PAGE.replace('type="submit"', 'type="submit" name="submit_action"')
        _, sent = self._run(page)
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["data"], sent[1]["data"])

    def test_generic_failure_is_not_classified_as_bad_credentials(self):
        result, _ = self._run(FORM_PAGE, "认证失败：当前页面已超时")
        self.assertNotEqual(result.status, STATUS_BAD_CREDENTIALS)

    def test_explicit_password_error_stops_after_one_request(self):
        page = FORM_PAGE.replace('type="submit"', 'type="submit" name="submit_action"')
        result, sent = self._run(page, "密码错误")
        self.assertEqual(result.status, STATUS_BAD_CREDENTIALS)
        self.assertEqual(len(sent), 1)


class RejectedCredentialTests(unittest.TestCase):
    def test_rejected_credentials_stay_paused_until_credentials_change(self):
        with data_dir():
            daemon = new_daemon()
            cfg = Config(username="student", password_enc="encrypted-old")
            auth = FakeAuth([
                AuthOutcome(ok=False, status=STATUS_BAD_CREDENTIALS, reason="密码错误"),
                AuthOutcome(ok=True, status=STATUS_SUCCESS, reason="ok"),
            ])
            with PatchedCheck(offline()):
                daemon._tick(cfg, None, auth, lambda: False, "fake-old")
                for _ in range(3):
                    daemon._tick(cfg, None, auth, lambda: False, "fake-old")
                cfg.request_timeout = 15  # An unrelated setting must not unlock retries.
                daemon._tick(cfg, None, auth, lambda: False, "fake-old")
                self.assertEqual(auth.calls, 1)
                cfg.password_enc = "encrypted-new"
                daemon._tick(cfg, None, auth, lambda: True, "fake-new")
            self.assertEqual(auth.calls, 2)

    def test_changing_operator_unblocks_authentication(self):
        with data_dir():
            daemon = new_daemon()
            cfg = Config(username="student", password_enc="encrypted")
            auth = FakeAuth([AuthOutcome(ok=False, status=STATUS_BAD_CREDENTIALS, reason="密码错误")])
            with PatchedCheck(offline()):
                daemon._tick(cfg, None, auth, lambda: False, "fake-secret")
                cfg.service_option = "中国移动"
                daemon._tick(cfg, None, auth, lambda: False, "fake-secret")
            self.assertEqual(auth.calls, 2)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the browser guard")
class BrowserTransportTests(unittest.TestCase):
    def test_browser_checks_transport_before_filling_credentials(self):
        # Execute the actual injection script with a minimal DOM. An empty
        # captcha stops permitted forms before filling; blocked forms must stop
        # even earlier, with wrong-form-action.
        program = r"""
const vm = require('vm');
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Input {
  constructor() { this.value = ''; }
  getClientRects() { return [1]; }
}
const user = new Input(), pass = new Input(), captcha = new Input();
const form = {
  getAttribute: () => input.action,
  setAttribute: () => {},
  addEventListener: () => {},
};
pass.form = form;
const submit = {form, getAttribute: () => null};
const document = {
  readyState: 'complete', baseURI: input.origin + '/login',
  querySelector: selector => ({'#u': user, '#p': pass, '#s': submit}[selector] || null),
  querySelectorAll: () => [captcha],
};
const context = {document, window: {}, location: new URL(document.baseURI),
  HTMLInputElement: Input, URL, URLSearchParams};
const state = vm.runInNewContext(input.script, context);
console.log(JSON.stringify({state, user: user.value, password: pass.value}));
"""
        cases = [
            ("https://portal.test", "http://portal.test/login", "wrong-form-action"),
            ("https://portal.test", "http://trusted.test/login", "wrong-form-action"),
            ("https://portal.test", "https://portal.test/login", "manual-required"),
            ("http://portal.test", "http://portal.test/login", "manual-required"),
        ]
        for origin, action, expected in cases:
            with self.subTest(origin=origin, action=action):
                script = portal_js.build({
                    "origin": origin, "username": "fake-student", "password": "fake-secret",
                    "userSelector": "#u", "passwordSelector": "#p", "submitSelector": "#s",
                    "submitHosts": ["portal.test", "trusted.test"],
                })
                result = subprocess.run(
                    [shutil.which("node"), "-e", program],
                    input=json.dumps({"origin": origin, "action": action, "script": script}),
                    text=True, encoding="utf-8", capture_output=True, timeout=10, check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                output = json.loads(result.stdout)
                self.assertEqual(output["state"], expected)
                self.assertEqual(output["user"], "")
                self.assertEqual(output["password"], "")


if __name__ == "__main__":
    unittest.main()

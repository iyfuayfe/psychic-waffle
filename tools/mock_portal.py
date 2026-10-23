"""本地模拟校园网门户（只为端到端自测，上线不用它）。

模拟 Dr.COM / 哆点 ePortal 的行为：
  - 未认证时 /connecttest.txt 返回 302 到门户，并带上 wlanuserip/wlanacname 参数（真实门户就是这么干的）
  - 门户页是 GBK 编码、无 <form>、参数内嵌在 JS 里（和 192.0.2.10 的结构一致）
  - /eportal/portal/login 用 JSONP 信封返回，支持 user_account/user_password 与 DDDDD/upass 两套参数

用法：
    python tools/mock_portal.py --port 8899 --user testuser --suffix @cmcc --password testpass123
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "authed_ips": set(),
    "account": "testuser@cmcc",
    "password": "testpass123",
    "logins": 0,
    "fails": 0,
    # 模拟"AC 那边其实已经在线了"：登录接口回 result=0 + ret_code=2 + "已经在线"
    "already_online": False,
}

PAGE_TEMPLATE = """<!DOCTYPE html>
<html><head><meta http-equiv="Content-Type" content="text/html; charset=gb2312">
<title>{title}</title>
<!--Dr.COMWebLoginID_1.htm-->
<script type="text/javascript">
time='4';flow='0';cvid=0,pvid=0;
uid='{uid}';pwd='';v4ip='{ip}';v6ip='::';
authloginport=801;
authloginpath='/eportal/?c=ACSetting&a=Login';
authloginparam='url=drappall';
authuserfield='DDDDD';
authpassfield='upass';
charset='gb2312';
account_suffix='{suffix}';
</script></head>
<body><div id="msg">{msg}</div></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MockPortal/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def _ip(self) -> str:
        return self.client_address[0]

    def _authed(self) -> bool:
        return self._ip() in STATE["authed_ips"]

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _html(self, code: int, title: str, msg: str, uid: str = "") -> None:
        suffix = ""
        if "@" in STATE["account"]:
            suffix = "@" + STATE["account"].split("@", 1)[1]
        html = PAGE_TEMPLATE.format(title=title, msg=msg, uid=uid, ip=self._ip(), suffix=suffix)
        self._send(code, html.encode("gbk", "replace"), "text/html; charset=gb2312")

    def _jsonp(self, obj: dict, callback: str = "jsonpReturn") -> None:
        body = f"{callback}({json.dumps(obj, ensure_ascii=False)});"
        self._send(200, body.encode("utf-8"), "application/javascript; charset=utf-8")

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[mock] %s - %s\n" % (self._ip(), fmt % args))
        sys.stderr.flush()

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}

        if path.endswith("connecttest.txt") or path.endswith("generate_204"):
            if self._authed():
                if path.endswith("generate_204"):
                    self._send(204, b"", "text/plain")
                else:
                    self._send(200, b"Microsoft Connect Test", "text/plain")
                return
            # 未认证：跳转到门户，带上 AC 参数（真实 Dr.COM 就是这样）
            target = (
                f"http://{self.headers.get('Host', '127.0.0.1')}/"
                f"?wlanuserip={self._ip()}&wlanacname=MOCK-AC&wlanacip=10.0.0.1"
                f"&nasip=10.0.0.1&mac=000000000000"
            )
            self._send(302, b"", "text/html", {"Location": target})
            return

        if path.rstrip("/") == "/eportal/portal/login":
            self._login(query)
            return

        if path == "/" or path.startswith("/eportal"):
            if self._authed():
                self._html(200, "注销页", "您已在线", uid=STATE["account"])
            else:
                self._html(200, "上网登录页", "请登录")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items() if v}
        if parsed.path.rstrip("/") == "/eportal/portal/login":
            self._login(form)
            return
        self._send(404, b"not found", "text/plain")

    # ---- 登录处理 ----
    def _login(self, params: dict) -> None:
        callback = params.get("callback") or "jsonpReturn"
        account = params.get("user_account") or params.get("DDDDD") or ""
        password = params.get("user_password") or params.get("upass") or ""

        # 去掉 Dr.COM 的 ",0," 前缀
        if account.startswith(","):
            account = account.rsplit(",", 1)[-1]
        account = account.strip()
        if "@" not in account and STATE["account"].count("@") == 1:
            account = account + "@" + STATE["account"].split("@", 1)[1]

        sys.stderr.write(
            "[mock] login attempt account=%r password=%r keys=%s\n"
            % (account, "*" * len(password), sorted(params.keys()))
        )
        sys.stderr.flush()

        if not account or not password:
            self._jsonp({"result": 0, "msg": "认证出现异常！", "ret_code": "1", "data": []}, callback)
            return

        # 模拟"AC 那边其实已经在线"：真实 Dr.COM 会回 result=0 + ret_code=2 + "已经在线"，
        # 这其实是成功状态。用来回归测试程序有没有把它误判成失败。
        if STATE.get("already_online"):
            STATE["authed_ips"].add(self._ip())
            self._jsonp(
                {"result": 0, "msg": f"IP: {self._ip()} 已经在线！", "ret_code": "2", "data": []},
                callback,
            )
            return

        if account == STATE["account"] and password == STATE["password"]:
            STATE["authed_ips"].add(self._ip())
            STATE["logins"] += 1
            self._jsonp(
                {"result": 1, "msg": "Portal协议认证成功！", "ret_code": "0", "data": [],
                 "uid": account},
                callback,
            )
            return

        STATE["fails"] += 1
        self._jsonp({"result": 0, "msg": "账号或密码错误，请重新输入", "ret_code": "1", "data": []}, callback)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--user", default="testuser")
    ap.add_argument("--suffix", default="@cmcc")
    ap.add_argument("--password", default="testpass123")
    ap.add_argument(
        "--already-online",
        action="store_true",
        help='故意返回 result=0 + ret_code=2 + "已经在线！（这其实是成功状态）"',
    )
    args = ap.parse_args(argv)

    STATE["account"] = args.user + (args.suffix or "")
    STATE["password"] = args.password
    STATE["already_online"] = bool(args.already_online)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"模拟门户已启动：http://{args.host}:{args.port}/")
    print(f"  账号：{STATE['account']}   密码：{STATE['password']}")
    print(f"  未认证时 http://{args.host}:{args.port}/connecttest.txt 会 302 到门户")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""浏览器填写脚本的端到端自测（会短暂弹出一个小浏览器窗口，约 10 秒）。

它做三件事：
1. 起一个本地"模拟门户"（带运营商下拉、账号密码、协议勾选、登录按钮）；
2. 用 campusnet/browser.py 打开它，并注入 campusnet/portal_js.py 的脚本；
3. 检查：运营商选中了吗？账号密码填对了吗？点了登录之后页面回"认证成功"了吗？
   以及 origin 不匹配时会不会拒绝填写。

用法：
    python tools\\test_web_autofill.py
    python tools\\test_web_autofill.py --keep   # 失败时保留窗口，方便肉眼看
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import browser, portal_js  # noqa: E402

FAILED: list[str] = []

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>模拟门户</title>
<style>body{font-family:'Microsoft YaHei';padding:24px}
label{display:block;margin:10px 0}input,select,button{font-size:14px;padding:4px}</style>
</head><body>
<h3>校园网认证</h3>
<form id="login">
  <label>运营商
    <select name="isp">
      <option value="">请选择运营商</option>
      <option value="cmcc">中国移动</option>
      <option value="cu">中国联通</option>
    </select>
  </label>
  <label>学号 <input type="text" name="user" placeholder="请输入学号"></label>
  <label>密码 <input type="password" name="pwd"></label>
  <label><input type="checkbox" name="agree" required> 我已阅读并同意用户协议</label>
  <button type="submit">登录</button>
</form>
<div id="result"></div>
<script>
  document.getElementById('login').addEventListener('submit', function (event) {
    event.preventDefault();
    var user = document.querySelector('input[name="user"]').value;
    var pwd = document.querySelector('input[name="pwd"]').value;
    var agree = document.querySelector('input[name="agree"]').checked;
    var good = !!user && !!pwd && agree;
    window.__cnResult = good ? 'success' : 'fail';
    document.getElementById('result').textContent = good ? '认证成功' : '认证失败';
  });
</script>
</body></html>
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # 别刷屏
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: N802
        """浏览器关窗口时会强断连接，这不算错，别往 stderr 刷 traceback。"""


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}/"


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    # 隔离用户数据目录：这个脚本会走到 webauth/setup_logging，默认路径是
    # %LOCALAPPDATA%\CampusNet —— 测试不该在用户真实数据目录里留下任何东西。
    import os

    os.environ.setdefault(
        "CAMPUSNET_DATA_DIR", str(Path(__file__).resolve().parent.parent / "build" / "smoke-data-web")
    )
    print("1) 脚本本身")
    script = portal_js.build({"origin": "http://x", "username": "u", "password": "p",
                              "submit": True, "service": "", "userSelector": "",
                              "passwordSelector": "", "submitSelector": "", "statusOnly": False})
    check("生成的脚本是 IIFE", script.strip().startswith("(() =>") or script.strip().startswith("(function"))
    check("账号密码被内联进脚本", '"username": "u"' in script and '"password": "p"' in script)
    check("状态文案表覆盖 submitted", portal_js.describe("submitted") != "submitted")

    print("2) 找浏览器")
    try:
        exe = browser.find_browser()
    except browser.BrowserError as exc:
        print(f"  [SKIP] 本机没有 Edge/Chrome：{exc}")
        return 0
    print(f"  使用 {exe}")

    server, url = serve()
    origin = url.rstrip("/")
    session = None
    try:
        print("3) 打开模拟门户并填写")
        session = browser.open_portal(url)
        cfg = {"origin": origin, "username": "20230001", "password": "p@ss word",
               "submit": True, "service": "中国移动", "userSelector": "",
               "passwordSelector": "", "submitSelector": "", "statusOnly": False}
        fill_script = portal_js.build(cfg)

        states: list[str] = []
        deadline = time.time() + 25
        last = ""
        while time.time() < deadline:
            if last in ("waiting-submit",):
                # 第一轮只填不点；趁这时候检查填得对不对
                user = session.devtools.evaluate("document.querySelector('input[name=user]').value")
                pwd = session.devtools.evaluate("document.querySelector('input[name=pwd]').value")
                agree = session.devtools.evaluate("document.querySelector('input[name=agree]').checked")
                isp = session.devtools.evaluate("document.querySelector('select[name=isp]').value")
                check("账号填上了", user == "20230001", repr(user))
                check("密码填上了（含空格和 @）", pwd == "p@ss word", repr(pwd))
                check("协议勾上了", agree is True, repr(agree))
                check("运营商选上了", isp == "cmcc", repr(isp))
            state = session.devtools.evaluate(fill_script)
            state = str(state or "")
            if state != last:
                states.append(state)
                print(f"     状态：{state}（{portal_js.describe(state)}）")
                last = state
            if state in ("authenticated", "authentication-rejected"):
                break
            time.sleep(0.4)

        check("流程走到 authenticated", last == "authenticated", f"最后状态={last} 过程={states}")
        check("中途确实提交过", "submitted" in states, str(states))

        print("4) origin 不匹配要拒绝填写")
        wrong = portal_js.build(dict(cfg, origin="http://example.com"))
        session.devtools.call("Page.navigate", url=url)
        time.sleep(1.5)
        state = str(session.devtools.evaluate(wrong) or "")
        check("换域名后拒绝填写", state == "wrong-origin", state)

        print("5) webauth.run 全流程（自己开窗、填写、提交、复核联网）")
        from campusnet import config as cn_config
        from campusnet import webauth

        portal_cfg = cn_config.Config()
        portal_cfg.username = "20230001"
        portal_cfg.service_option = "中国移动"
        portal_cfg.portal_url = url
        portal_cfg.autofill_click = True
        portal_cfg.request_timeout = 5
        messages: list[str] = []
        result = webauth.run(portal_cfg, "p@ss word", progress=messages.append, timeout=60)
        check("全流程判定成功", bool(result.get("ok")), str(result))
        check("过程有中文进度", any("认证页" in m for m in messages), str(messages[:5]))
        check("结束后没留窗口", not webauth.active_open(), "仍然开着")
    finally:
        server.shutdown()
        if session is not None:
            if keep and FAILED:
                print("  （--keep：窗口留着，自己关掉即可）")
            else:
                session.close()
            if not (keep and FAILED):
                check("临时用户目录已清理", not session.profile_dir.exists(), str(session.profile_dir))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

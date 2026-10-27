"""Dr.COM ePortal 登录接口参数探测（排障用）。

用假账号轮询各种参数组合，通过服务端返回的 msg 差异判断哪套参数才是对的：
  - "认证出现异常！"        -> 参数不对/缺字段
  - "账号或密码错误" 之类   -> 参数对了，只是账号假

只发假账号，不会影响真实在线会话。

用法：
    python tools/probe_login_api.py --host 192.0.2.10 --port 801
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campusnet.http import HttpClient  # noqa: E402

JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.S)

FAKE_ACCOUNT = "00000000000@cmcc"
FAKE_PASSWORD = "zzzz1111"


def unwrap(text: str) -> dict:
    m = JSONP_RE.match(text.strip())
    body = m.group(1) if m else text.strip()
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"_raw": text.strip()[:300]}
    if isinstance(obj, dict):
        return obj
    return {"_raw": str(obj)[:300]}


def show(name: str, resp) -> None:
    data = unwrap(resp.text)
    result = data.get("result")
    msg = data.get("msg", "")
    ret = data.get("ret_code", "")
    flag = ""
    if msg and "异常" not in msg and "error" not in msg.lower():
        flag = "   <== 参数可能正确"
    print(f"  [{name}]")
    print(f"      HTTP {resp.status}  result={result}  msg={msg!r}  ret_code={ret}{flag}")
    if "_raw" in data:
        print(f"      raw={data['_raw']!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.0.2.10")
    ap.add_argument("--port", type=int, default=801)
    ap.add_argument("--account", default=FAKE_ACCOUNT)
    ap.add_argument("--password", default=FAKE_PASSWORD)
    args = ap.parse_args(argv)

    client = HttpClient(timeout=10, insecure_tls=True, use_system_proxy=False)
    base = f"http://{args.host}:{args.port}"
    portal_api = f"{base}/eportal/portal/login"
    ac_api = f"{base}/eportal/"

    acct, pwd = args.account, args.password

    print("=" * 70)
    print(f"目标：{portal_api}")
    print(f"假账号：{acct}")
    print("=" * 70)

    # A. 新版 JSON API：user_account / user_password（GET）
    show("A GET user_account", client.get_query(portal_api, {
        "callback": "dr1003", "login_method": "1",
        "user_account": f",0,{acct}", "user_password": pwd,
        "wlan_user_ip": "", "wlan_user_ipv6": "", "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "", "wlan_ac_name": "", "jsVersion": "4.X",
        "v": str(random.randint(500, 9999)),
    }, follow_redirects=False))

    # B. 本地认证：DDDDD / upass（GET）
    show("B GET DDDDD", client.get_query(portal_api, {
        "callback": "dr1003", "DDDDD": acct, "upass": pwd, "0MKKey": "123456",
        "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
        "login_t": "1", "is_page": "1", "is_page_new": str(random.randint(500, 9999)),
        "terminal_type": "1", "lang": "zh-cn", "jsVersion": "4.X",
        "v": str(random.randint(500, 9999)),
    }, follow_redirects=False))

    # C. DDDDD / upass（POST）
    show("C POST DDDDD", client.post_form(portal_api, {
        "callback": "dr1003", "DDDDD": acct, "upass": pwd, "0MKKey": "123456",
        "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
        "login_t": "1", "is_page": "1", "jsVersion": "4.X",
    }, follow_redirects=False))

    # D. user_account / user_password（POST）
    show("D POST user_account", client.post_form(portal_api, {
        "callback": "dr1003", "login_method": "1",
        "user_account": f",0,{acct}", "user_password": pwd,
        "wlan_user_mac": "000000000000", "jsVersion": "4.X",
    }, follow_redirects=False))

    # E. 经典 AC 接口 POST /eportal/?c=ACSetting&a=Login
    show("E POST ACSetting", client.post_form(ac_api + "?c=ACSetting&a=Login", {
        "DDDDD": acct, "upass": pwd, "R1": "0", "R2": "0", "R3": "0", "R6": "0",
        "para": "00", "0MKKey": "123456", "buttonClicked": "",
        "redirect_url": "", "err_flag": "", "username": "", "password": "",
        "user": "", "cmd": "", "login": "",
    }, follow_redirects=False))

    # F. 经典 AC 接口 GET
    show("F GET ACSetting", client.get_query(ac_api, {
        "c": "ACSetting", "a": "Login", "protocol": "http:", "hostname": args.host,
        "iTermType": "1", "wlanuserip": "", "wlanacip": "", "wlanacname": "",
        "mac": "000000000000", "ip": "", "enAdvert": "0", "queryACIP": "0", "loginMethod": "1",
    }, follow_redirects=False))

    # G. 加密 params 形式（明文 JSON，探测服务端是否接受该字段）
    show("G GET params(json)", client.get_query(portal_api, {
        "callback": "dr1003",
        "params": json.dumps({
            "DDDDD": acct, "upass": pwd, "0MKKey": 123456, "R1": "", "R2": "",
            "R3": "", "R6": 0, "para": "", "v6ip": "",
        }, separators=(",", ":")),
        "jsVersion": "4.X", "v": str(random.randint(500, 9999)),
    }, follow_redirects=False))

    # H. 先 GET 门户页建立会话，再调 user_account API（Dr.COM 常见要求）
    print("-" * 70)
    print("  先预热门户口令会话，再用 A 的参数重试：")
    try:
        warm = client.get(f"http://{args.host}/", follow_redirects=True)
        print(f"      预热 HTTP {warm.status} -> {warm.url}")
    except Exception as exc:  # noqa: BLE001
        print(f"      预热失败：{exc}")
    print(f"      会话 cookie: {[c.name for c in client.jar] or '(无)'}")

    api_fields = {
        "callback": "dr1003", "login_method": "1",
        "user_account": f",0,{acct}", "user_password": pwd,
        "wlan_user_ip": "", "wlan_user_ipv6": "", "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "", "wlan_ac_name": "", "jsVersion": "4.X",
        "v": str(random.randint(500, 9999)),
    }
    show("H 会话后 user_account", client.get_query(portal_api, dict(api_fields), follow_redirects=False))

    api_fields["wlan_user_ip"] = local_ip(args.host)
    show("I 会话+本机IP", client.get_query(portal_api, dict(api_fields), follow_redirects=False))

    print("=" * 70)
    print("判读方法：msg 不是「认证出现异常」的那一组，就是正确的参数集；")
    print("若变成「账号或密码错误」，说明参数与流程都已正确。")
    return 0


def local_ip(host: str) -> str:
    """本机到目标主机的出口 IP（用于 wlan_user_ip）。"""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    sys.exit(main())

"""排障用：按门户 a40.js 的真实流程调用 Dr.COM 内核登录接口。

逆向结论（来自门户自己的 JS）：
  - 登录地址 = page.path + 'login' = http://<portal_host>/drcom/login  （80 端口，不是 801）
  - 经典登录时 JS 里会强制 page.login_method = 0，所以参数是**明文**（不做 AES）
  - 数据字段：DDDDD / upass / 0MKKey=123456 / R1 / R2 / R3 / R6 / para / v6ip
              / login_t / js_status / is_page=1 / is_page_new / terminal_type / lang / rcn
  - 且必须用 GET（浏览器是用 <script> 发起 jsonp）

用法：
    python tools/probe_kernel_login.py                 # 只用假账号探测参数集
    python tools/probe_kernel_login.py --real          # 用真实账号（会真的登录）
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campusnet.config import Config  # noqa: E402
from campusnet import crypto  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402

JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.S)


def unwrap(text: str):
    text = (text or "").strip()
    m = JSONP_RE.match(text)
    body = m.group(1) if m else text
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {"_raw": text[:400]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default="http://192.0.2.10")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--account", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--real", action="store_true", help="使用 config.json 里的真实账号密码")
    args = ap.parse_args(argv)

    account = args.account
    password = args.password
    suffix = ""
    if args.real:
        cfg = Config.load(Path(args.config))
        account = cfg.username
        password = crypto.unprotect(cfg.password_enc)
        suffix = cfg.drcom.account_suffix or ""
    if not account:
        account = "00000000000"
        password = "zzzz1111"
    if suffix and "@" not in account:
        account = account + suffix

    client = HttpClient(timeout=15, insecure_tls=True, use_system_proxy=False)

    print("=" * 72)
    print(f"1) 预热门户页 {args.portal}/")
    warm = client.get(args.portal + "/", follow_redirects=True)
    print(f"   HTTP {warm.status} -> {warm.url} ({len(warm.body)}B)")
    print(f"   cookie: {[c.name for c in client.jar] or '(无)'}")

    # 从门户页脚本里取 v46ip 等（模拟不认证时的真实上下文）
    m = re.search(r"v46ip\s*=\s*'([\d.]+)'", warm.text)
    user_ip = m.group(1) if m else ""
    m = re.search(r"ss4\s*=\s*\"([0-9a-fA-F]+)\"", warm.text)
    mac_hex = m.group(1) if m else ""
    print(f"   v46ip={user_ip or '(未取到)'}  ss4(mac)={mac_hex or '(未取到)'}")

    print(f"\n2) page/loadConfig  ({args.portal}:801/eportal/portal/page/loadConfig)")
    b64 = lambda s: base64.b64encode((s or "").encode()).decode()
    lc = client.get_query(
        f"{args.portal}:801/eportal/portal/page/loadConfig",
        {
            "callback": "dr1002",
            "program_index": "",
            "wlan_vlan_id": "1",
            "wlan_user_ip": b64(user_ip),
            "wlan_user_ipv6": b64(""),
            "wlan_user_ssid": "",
            "wlan_user_areaid": "",
            "wlan_ac_ip": b64(""),
            "wlan_ap_mac": "",
            "gw_id": "",
            "jsVersion": "4.X",
            "v": str(random.randint(500, 9999)),
        },
        follow_redirects=False,
    )
    cfg_json = unwrap(lc.text)
    data = cfg_json.get("data") if isinstance(cfg_json.get("data"), dict) else {}
    interesting = {k: data.get(k) for k in (
        "login_method", "en_md5", "enable_r3", "account_suffix", "rcn", "js_status",
        "login_t", "page_index", "program_index", "page_style", "en_perceive",
    ) if k in data}
    print(f"   HTTP {lc.status} result={cfg_json.get('result')}")
    print(f"   关键字段：{json.dumps(interesting, ensure_ascii=False)}")
    if not data:
        print(f"   原始：{lc.text[:400]}")

    login_method = int(data.get("login_method") or 0)
    en_md5 = int(data.get("en_md5") or 0)
    server_suffix = data.get("account_suffix") or ""
    kernel_url = args.portal + "/drcom/login"
    api_url = f"{args.portal}:801/eportal/portal/login"

    def payload(acct: str) -> dict:
        pwd = password
        if en_md5:
            import hashlib

            pwd = hashlib.md5(("1" + pwd + "12345678").encode()).hexdigest() + "12345678" + "1"
        return {
            "callback": "dr1003",
            "DDDDD": acct,
            "upass": pwd,
            "0MKKey": "123456",
            "R1": "",
            "R2": "1" if en_md5 else "",
            "R3": "",
            "R6": "0",
            "para": "",
            "v6ip": "",
            "login_t": "0",
            "js_status": "0",
            "is_page": "1",
            "is_page_new": str(random.randint(500, 10499)),
            "terminal_type": "1",
            "lang": "zh",
            "rcn": data.get("rcn") or "",
            "jsVersion": "4.X",
            "v": str(random.randint(500, 9999)),
        }

    tests = [
        ("内核接口 /drcom/login (80端口)", kernel_url, account),
        ("内核接口 + 服务端返回的后缀", kernel_url, account.split("@")[0] + server_suffix if server_suffix else account),
    ]

    for name, url, acct in tests:
        print(f"\n3) {name}\n   GET {url}\n   账号={acct}")
        try:
            r = client.get_query(url, payload(acct), follow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            print(f"   请求异常：{exc}")
            continue
        body = r.text
        j = unwrap(body)
        print(f"   HTTP {r.status}  body[:300]={body[:300]!r}")
        if isinstance(j, dict):
            print(f"   解析：result={j.get('result')!r} msg={j.get('msg')!r} ret_code={j.get('ret_code')!r}")

    print(f"\n4) 对照：门户新版 API {api_url}")
    try:
        r = client.get_query(api_url, {
            "callback": "dr1003", "login_method": "1",
            "user_account": f",0,{account}", "user_password": password,
            "wlan_user_ip": user_ip, "wlan_user_ipv6": "", "wlan_user_mac": "000000000000",
            "wlan_ac_ip": "", "wlan_ac_name": "", "jsVersion": "4.X",
            "terminal_type": "1", "lang": "zh-cn", "v": str(random.randint(500, 9999)),
        }, follow_redirects=False)
        print(f"   HTTP {r.status} body[:300]={r.text[:300]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"   请求异常：{exc}")

    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())

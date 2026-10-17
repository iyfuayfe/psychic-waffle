"""在线状态下探测：哪个登录端点才是本学校真正在用的。

原理：本机已在线时，对**正确的**登录接口发登录请求，服务端会回「已经在线」
（ret_code=2 / result=0+已在线），而不是「当前页面已超时」。据此可以零风险地
定位正确端点 —— 等真正掉线时，这个端点就会真的完成认证。

用法：
    python tools\\probe_endpoints.py
"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import aes, crypto  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.drcom import ip_to_int, local_ip_for, parse_jsonp  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402

HOST = "111.17.200.130"
PORT = 801


def enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def main() -> int:
    cfg = Config.load()
    log = setup_logging(cfg, console=True)
    try:
        pwd = crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        print(f"密码解密失败：{exc}")
        return 2

    ip = cfg.drcom.wlan_user_ip or local_ip_for(HOST) or ""
    mac = "000000000000"
    acip = "117.132.17.39"

    accounts = [
        ("无前缀@cmcc", cfg.username + "@cmcc"),
        (",0,前缀@cmcc", ",0," + cfg.username + "@cmcc"),
    ]

    client = HttpClient(timeout=10, insecure_tls=True, use_system_proxy=False)

    print("=" * 78)
    print(f"在线探测登录端点  IP={ip}  AC={acip}  密码长度={len(pwd)}")
    print("=" * 78)

    def fire(label: str, url: str) -> None:
        try:
            r = client.get(url, follow_redirects=False,
                           headers={"Referer": f"http://{HOST}/"})
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{label}] 请求异常：{exc}")
            return
        d = parse_jsonp(r.text)
        msg = str(d.get("msg", "") or "")
        ret = d.get("ret_code")
        res = d.get("result")
        online = any(k in msg for k in ("已经在线", "已在线", "在线")) or str(ret) == "2"
        mark = "  <<<< 已经在线！此端点正确" if online else ""
        print(f"\n[{label}]")
        print(f"  HTTP {r.status}  result={res!r}  ret_code={ret!r}  msg={msg!r}{mark}")
        if "_raw" in d:
            print(f"  raw={d['_raw'][:160]!r}")

    for aname, acct in accounts:
        a = enc(acct)

        # A. /eportal/portal/login  明文（campusnet provider 风格）
        q = ("callback=dr1003&login_method=1"
             f"&user_account={a}&user_password={enc(pwd)}"
             f"&wlan_user_ip={enc(ip)}&wlan_user_ipv6="
             "&wlan_user_mac=000000000000&wlan_ac_ip=&wlan_ac_name="
             "&jsVersion=4.2.1&terminal_type=1&lang=zh-cn&v=5911")
        fire(f"A portal/login 明文 [{aname}]", f"http://{HOST}:{PORT}/eportal/portal/login?{q}")

        # B. /eportal/?c=Portal&a=login  AHU 极简（6 参数）
        q = ("callback=dr1003&login_method=1"
             f"&user_account={a}&user_password={enc(pwd)}"
             f"&wlan_user_ip={enc(ip)}")
        fire(f"B c=Portal&a=login AHU极简 [{aname}]", f"http://{HOST}:{PORT}/eportal/?{q}")

        # C. /eportal/?c=Portal&a=login  GZIST 完整（jsVersion=3.3.2 + v + mac + ac）
        q = ("callback=dr1003&login_method=1"
             f"&user_account={a}&user_password={enc(pwd)}"
             f"&wlan_user_ip={enc(ip)}&wlan_user_ipv6="
             f"&wlan_user_mac={mac}&wlan_ac_ip={acip}&wlan_ac_name="
             "&jsVersion=3.3.2&v=5911")
        fire(f"C c=Portal&a=login GZIST完整 [{aname}]", f"http://{HOST}:{PORT}/eportal/?{q}")

        # D. /eportal/?c=ACSetting&a=Login  GZIST 方案1（DDDDD/upass + 新旧参数双发）
        q = ("c=ACSetting&a=Login"
             f"&DDDDD={a}&upass={enc(pwd)}&0MKKey=123456"
             "&R1=0&R2=0&R3=0&R6=0&para=00&buttonClicked=&redirect_url=&err_flag="
             "&username=&password=&user=&cmd=Login&login="
             f"&wlanuserip={enc(ip)}&wlanusermac={mac}&wlanacip={acip}"
             f"&wlan_user_ip={enc(ip)}&wlan_user_mac={mac}&wlan_ac_ip={acip}")
        fire(f"D c=ACSetting&a=Login 双参数 [{aname}]", f"http://{HOST}:{PORT}/eportal/?{q}")

        # E. /eportal/portal/login  AES（我们当前的实现）
        data = {
            "login_method": 1, "user_account": acct, "user_password": pwd,
            "wlan_user_ip": ip, "wlan_user_ipv6": "", "wlan_user_mac": "000000000000",
            "wlan_ac_ip": "", "wlan_ac_name": "", "jsVersion": "4.2.1",
            "login_t": "0", "js_status": "0", "is_page": "1",
            "is_page_new": "5911", "terminal_type": "1", "lang": "zh",
        }
        import json
        token = aes.encrypt_b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
        q = f"callback=dr1003&params={enc(token)}&jsVersion=4.2.1&lang=zh"
        fire(f"E portal/login AES [{aname}]", f"http://{HOST}:{PORT}/eportal/portal/login?{q}")

    print("\n" + "=" * 78)
    print("判读：回「已经在线 / ret_code=2」的那条 = 本学校真正在用的登录端点。")
    print("      （在线时正确的端点都该报已在线，而不是页面超时）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

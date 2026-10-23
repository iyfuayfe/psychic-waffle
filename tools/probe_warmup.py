"""验证「当前页面已超时」是不是因为缺少门户页会话（cookie）。

假设：ePortal 要求先访问门户页拿到会话，再提交登录；否则回「当前页面已超时」。

流程：
  A. 冷启动（不访问门户页）直接提交  -> 基线
  B. 同一个 cookie 会话里，先 GET 门户页（跟随跳转）再提交
  C. 先 GET 门户页根 + loadConfig，再提交

假账号，不影响真实在线会话。用法：
    python tools\\probe_warmup.py
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import aes  # noqa: E402
from campusnet.config import Config  # noqa: E402
from campusnet.drcom import parse_jsonp  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402

FAKE = "00000000000"
PWD = "zzzz1111"
SUFFIX = "@cmcc"
JS = "4.2.1"


def qs(fields: dict) -> str:
    return "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in fields.items()
    )


def login_once(client: HttpClient, api: str, ip: str, tag: str) -> None:
    payload = {
        "login_method": 1,
        "user_account": ",0," + FAKE + SUFFIX,
        "user_password": PWD,
        "wlan_user_ip": ip,
        "wlan_user_ipv6": "",
        "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": JS,
    }
    token = aes.encrypt_b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    url = api + "login?" + qs({"callback": "dr1003", "params": token, "jsVersion": JS, "lang": "zh"})
    try:
        resp = client.get(url, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        print(f"    [{tag}] 请求异常：{exc}")
        return
    data = parse_jsonp(resp.text)
    cookies = ", ".join(f"{c.name}" for c in client.jar) or "(无)"
    print(f"    [{tag}] HTTP {resp.status} result={data.get('result')!r} "
          f"ret_code={data.get('ret_code')!r} msg={str(data.get('msg',''))!r}")
    print(f"           cookie: {cookies}")


def main() -> int:
    cfg = Config.load()
    host = cfg.drcom.host or "192.0.2.10"
    port = cfg.drcom.port or 801
    api = f"http://{host}:{port}/eportal/portal/"
    ip = cfg.drcom.wlan_user_ip or (cfg.portal_url.split("wlanuserip=")[1].split("&")[0]
                                    if "wlanuserip=" in (cfg.portal_url or "") else "")

    print("=" * 74)
    print("门户会话（warm-up）对比实验")
    print("=" * 74)
    print(f"API={api}  wlan_user_ip={ip}")

    print("\nA) 冷启动直接提交（基线，复现之前的结果）")
    c1 = HttpClient(timeout=12, insecure_tls=True, use_system_proxy=False)
    login_once(c1, api, ip, "cold")

    print("\nB) 先 GET 门户页（跟随跳转）再提交，同一 cookie 会话")
    c2 = HttpClient(timeout=12, insecure_tls=True, use_system_proxy=False)
    for candidate in (f"http://{host}/", cfg.portal_url or f"http://{host}/a79.htm"):
        if not candidate:
            continue
        try:
            r = c2.get(candidate, follow_redirects=True)
            print(f"    预热 {candidate} -> HTTP {r.status} {r.url[:90]}")
            print(f"           cookie: {', '.join(x.name for x in c2.jar) or '(无)'}")
        except Exception as exc:  # noqa: BLE001
            print(f"    预热 {candidate} 失败：{exc}")
    login_once(c2, api, ip, "warm+page")

    print("\nC) 再补一次 loadConfig 后提交")
    lc = api + "page/loadConfig?" + qs({
        "callback": "dr1003", "program_index": "", "wlan_vlan_id": "1",
        "wlan_user_ip": __import__("base64").b64encode(ip.encode()).decode(),
        "wlan_user_ipv6": "", "wlan_user_ssid": "", "wlan_user_areaid": "",
        "wlan_ac_ip": "", "wlan_ap_mac": "000000000000", "gw_id": "",
        "jsVersion": JS, "lang": "zh",
    })
    try:
        r = c2.get(lc, follow_redirects=False)
        d = parse_jsonp(r.text)
        print(f"    loadConfig -> HTTP {r.status} msg={str(d.get('msg',''))!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"    loadConfig 失败：{exc}")
    login_once(c2, api, ip, "warm+config")

    print("\n" + "=" * 74)
    print("若三次 msg 都一样，说明「当前页面已超时」不是 cookie 缺失导致，")
    print("而是服务端当前没有待认证会话（本机在线时通常如此）。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
